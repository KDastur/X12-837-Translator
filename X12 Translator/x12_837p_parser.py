#!/usr/bin/env python3
"""
x12_837p_parser.py

Reads an ANSI X12N 837 Health Care Claim: Professional (837P) file
and translates it into structured JSON.

Supports 5010 (004010X098A1 / 005010X222A1) style 837P files with the
standard ISA/GS/ST envelope and HL-based provider/subscriber/patient
hierarchy.

Usage:
    python3 x12_837p_parser.py input.837 -o output.json
    python3 x12_837p_parser.py input.837            # prints JSON to stdout
    python3 x12_837p_parser.py input.837 --pretty    # pretty-print

This is a pragmatic, dependency-free parser. It does not perform full
HIPAA Implementation Guide validation (that is the job of tools like
pyx12); instead it focuses on reliably extracting the claim-relevant
data into a clean JSON structure: submitter/receiver, billing provider,
subscriber, patient, payer, claims, diagnoses, and service lines.
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Segment tokenizer
# ---------------------------------------------------------------------------

@dataclass
class Delimiters:
    segment_terminator: str = "~"
    element_separator: str = "*"
    subelement_separator: str = ":"
    repetition_separator: str = "^"


def detect_delimiters(raw: str) -> Delimiters:
    """
    The ISA segment is fixed-width (106 characters including the segment
    terminator) and self-describes the delimiters used throughout the file:
      - element separator: the character immediately after 'ISA'
      - subelement separator: character at fixed position 104 (0-indexed)
      - segment terminator: the character immediately following ISA16 (pos 105)
      - repetition separator (5010 only): character at position 82
    """
    if not raw.startswith("ISA"):
        # Not a standard envelope; fall back to common defaults.
        return Delimiters()

    element_sep = raw[3]
    # ISA is composed of 16 elements of fixed lengths ending right before
    # the segment terminator. Element separator lets us split precisely.
    isa_end = raw.find(raw[3], 105)  # heuristic fallback, refined below
    # More robust: split on element_sep up to the 17th token, the 17th
    # character after that boundary is the segment terminator.
    # Standard ISA is exactly 106 bytes: "ISA" + 16 elements + terminator,
    # each element separated by element_sep, with fixed lengths.
    subelement_sep = raw[104] if len(raw) > 104 else ":"
    segment_terminator = raw[105] if len(raw) > 105 else "~"
    repetition_sep = raw[82] if len(raw) > 82 else "^"

    return Delimiters(
        segment_terminator=segment_terminator,
        element_separator=element_sep,
        subelement_separator=subelement_sep,
        repetition_separator=repetition_sep,
    )


def tokenize(raw: str, delims: Delimiters) -> List[List[str]]:
    """
    Split the raw X12 text into a list of segments, each a list of
    elements (composite elements are further split on demand via
    split_composite()).
    """
    # Normalize line endings that sometimes get inserted between segments
    # for human readability; they are not meaningful X12 syntax.
    cleaned = raw.replace("\r\n", "").replace("\n", "")

    raw_segments = [s for s in cleaned.split(delims.segment_terminator) if s.strip() != ""]
    segments = []
    for seg in raw_segments:
        elements = seg.split(delims.element_separator)
        segments.append(elements)
    return segments


def split_composite(element: str, delims: Delimiters) -> List[str]:
    if element is None:
        return []
    return element.split(delims.subelement_separator)


def el(segment: List[str], index: int, default: str = "") -> str:
    """Safely fetch element `index` (1-based, matching X12 convention where
    element 0 is the segment ID) from a segment."""
    if index < len(segment):
        val = segment[index]
        return val if val is not None else default
    return default


# ---------------------------------------------------------------------------
# Code lookups (small, commonly-referenced subsets)
# ---------------------------------------------------------------------------

ENTITY_TYPE_QUALIFIER = {"1": "Person", "2": "Non-Person Entity"}

CLAIM_FILING_INDICATOR = {
    "11": "Other Non-Federal Programs", "12": "Preferred Provider Organization (PPO)",
    "13": "Point of Service (POS)", "14": "Exclusive Provider Organization (EPO)",
    "15": "Indemnity Insurance", "16": "Health Maintenance Organization (HMO) Medicare Risk",
    "17": "Dental Maintenance Organization", "AM": "Automobile Medical",
    "BL": "Blue Cross/Blue Shield", "CH": "Champus", "CI": "Commercial Insurance Co.",
    "DS": "Disability", "FI": "Federal Employees Program", "HM": "Health Maintenance Organization",
    "LM": "Liability Medical", "MA": "Medicare Part A", "MB": "Medicare Part B",
    "MC": "Medicaid", "OF": "Other Federal Program", "TV": "Title V",
    "VA": "Veterans Affairs Plan", "WC": "Workers' Compensation Health Claim",
    "ZZ": "Mutually Defined",
}

INDIVIDUAL_RELATIONSHIP_CODE = {
    "01": "Spouse", "18": "Self", "19": "Child", "20": "Employee",
    "21": "Unknown", "39": "Organ Donor", "40": "Cadaver Donor",
    "53": "Life Partner", "G8": "Other Relationship",
}

DIAGNOSIS_QUALIFIER = {
    "ABK": "Principal Diagnosis (ICD-10)", "ABF": "Diagnosis (ICD-10)",
    "BK": "Principal Diagnosis (ICD-9)", "BF": "Diagnosis (ICD-9)",
}

CLAIM_FREQUENCY_CODE = {
    "1": "Original claim", "7": "Replacement of prior claim", "8": "Void/cancel of prior claim",
}

FACILITY_CODE_PLACE_OF_SERVICE = {
    "11": "Office", "12": "Home", "21": "Inpatient Hospital", "22": "Outpatient Hospital",
    "23": "Emergency Room - Hospital", "24": "Ambulatory Surgical Center", "31": "Skilled Nursing Facility",
    "81": "Independent Laboratory", "02": "Telehealth",
}

DTP_QUALIFIER = {
    "096": "Discharge", "431": "Onset of Current Illness/Symptom", "435": "Admission",
    "439": "Accident", "444": "First Visit/Consultation", "454": "Initial Treatment",
    "471": "Prescription", "472": "Service Date", "484": "Last Menstrual Period",
    "090": "Assumed and Relinquished Care Date",
}

REF_QUALIFIER = {
    "EI": "Employer's Identification Number", "SY": "Social Security Number",
    "6R": "Provider Control Number", "F8": "Original Reference Number",
    "9F": "Referral Number", "G1": "Prior Authorization Number", "EA": "Medical Record Number",
    "IG": "Insurance Policy Number", "1L": "Group or Policy Number", "D9": "Claim Number",
    "LU": "Location Number", "0B": "State License Number", "1G": "Provider UPIN Number",
    "N5": "Provider Plan Network ID Number",
}

SBR_PAYER_RESPONSIBILITY = {
    "P": "Primary", "S": "Secondary", "T": "Tertiary", "A": "Payer Responsibility Four",
    "B": "Payer Responsibility Five", "C": "Payer Responsibility Six",
    "D": "Payer Responsibility Seven", "E": "Payer Responsibility Eight",
    "F": "Payer Responsibility Nine", "G": "Payer Responsibility Ten", "U": "Unknown",
}


def date_from_d8(value: str) -> Optional[str]:
    """Convert a D8 (CCYYMMDD) date into ISO 8601 (YYYY-MM-DD)."""
    if not value or len(value) != 8 or not value.isdigit():
        return value or None
    return f"{value[0:4]}-{value[4:6]}-{value[6:8]}"


def date_range_from_rd8(value: str) -> Optional[Dict[str, str]]:
    """Convert an RD8 (CCYYMMDD-CCYYMMDD) date range into ISO start/end."""
    if not value or "-" not in value:
        return None
    start, _, end = value.partition("-")
    return {"start": date_from_d8(start), "end": date_from_d8(end)}


# ---------------------------------------------------------------------------
# Name / address / contact helpers (NM1, N3, N4, PER)
# ---------------------------------------------------------------------------

def parse_nm1(seg: List[str]) -> Dict[str, Any]:
    entity_type = el(seg, 2)
    is_person = entity_type == "1"
    name: Dict[str, Any] = {
        "entity_identifier_code": el(seg, 1),
        "entity_type": ENTITY_TYPE_QUALIFIER.get(entity_type, entity_type),
    }
    if is_person:
        name["last_name"] = el(seg, 3)
        name["first_name"] = el(seg, 4)
        name["middle_name"] = el(seg, 5) or None
        name["suffix"] = el(seg, 7) or None
    else:
        name["organization_name"] = el(seg, 3)

    id_qualifier = el(seg, 8)
    id_value = el(seg, 9)
    if id_value:
        name["identification"] = {"qualifier": id_qualifier, "value": id_value}
    return {k: v for k, v in name.items() if v not in (None, "")}


def parse_n3(seg: List[str]) -> Dict[str, Any]:
    return {k: v for k, v in {
        "address_line_1": el(seg, 1),
        "address_line_2": el(seg, 2) or None,
    }.items() if v}


def parse_n4(seg: List[str]) -> Dict[str, Any]:
    return {k: v for k, v in {
        "city": el(seg, 1),
        "state": el(seg, 2),
        "zip": el(seg, 3),
        "country": el(seg, 4) or None,
    }.items() if v}


def parse_per(seg: List[str]) -> Dict[str, Any]:
    contact = {"contact_function": el(seg, 1), "name": el(seg, 2) or None}
    # PER alternates comm qualifier / number starting at element 3
    i = 3
    while i + 1 < len(seg):
        qual, num = el(seg, i), el(seg, i + 1)
        if qual and num:
            label = {"TE": "phone", "EM": "email", "FX": "fax", "UR": "url"}.get(qual, qual)
            contact[label] = num
        i += 2
    return {k: v for k, v in contact.items() if v}


def parse_ref(seg: List[str]) -> Dict[str, Any]:
    qual = el(seg, 1)
    return {
        "qualifier": qual,
        "qualifier_description": REF_QUALIFIER.get(qual, qual),
        "value": el(seg, 2),
    }


def parse_dtp(seg: List[str]) -> Dict[str, Any]:
    qual = el(seg, 1)
    fmt = el(seg, 2)
    raw_val = el(seg, 3)
    entry: Dict[str, Any] = {
        "qualifier": qual,
        "qualifier_description": DTP_QUALIFIER.get(qual, qual),
    }
    if fmt == "D8":
        entry["date"] = date_from_d8(raw_val)
    elif fmt == "RD8":
        entry["date_range"] = date_range_from_rd8(raw_val)
    else:
        entry["value"] = raw_val
    return entry


# ---------------------------------------------------------------------------
# 837P transaction-set parser
# ---------------------------------------------------------------------------

class Segment:
    __slots__ = ("id", "elements")

    def __init__(self, elements: List[str]):
        self.id = elements[0]
        self.elements = elements

    def __repr__(self):
        return f"<{self.id} {self.elements[1:]}>"


class SegmentCursor:
    """A simple forward-only cursor over a list of segments."""

    def __init__(self, segments: List[List[str]]):
        self.segments = [Segment(s) for s in segments]
        self.i = 0

    def peek(self) -> Optional[Segment]:
        if self.i < len(self.segments):
            return self.segments[self.i]
        return None

    def next(self) -> Optional[Segment]:
        seg = self.peek()
        if seg is not None:
            self.i += 1
        return seg

    def at_end(self) -> bool:
        return self.i >= len(self.segments)


def parse_837p_transaction(cursor: SegmentCursor, delims: Delimiters) -> Dict[str, Any]:
    """
    Parse a single ST...SE 837P transaction set into a structured dict.
    Assumes cursor.peek() is currently positioned at the ST segment.
    """
    tx: Dict[str, Any] = {
        "header": {},
        "submitter": None,
        "receiver": None,
        "billing_provider": None,
        "subscribers": [],
    }

    current_billing_provider = None
    current_subscriber = None
    current_patient = None
    current_claim = None
    current_service_line = None
    # Tracks the dict that the most recent NM1 populated, so that any
    # following N3/N4/PER segments (which have no entity identifier of
    # their own) attach to the correct party rather than being guessed
    # from the surrounding hierarchy.
    last_nm1_target: Optional[Dict[str, Any]] = None

    # Track HL hierarchy: hl_id -> node reference + level type, so children
    # can be attached under the correct parent regardless of order.
    hl_nodes: Dict[str, Dict[str, Any]] = {}

    while not cursor.at_end():
        seg = cursor.next()

        if seg.id == "ST":
            tx["header"]["transaction_set_id"] = el(seg.elements, 1)
            tx["header"]["control_number"] = el(seg.elements, 2)
            tx["header"]["implementation_convention_reference"] = el(seg.elements, 3) or None

        elif seg.id == "BHT":
            tx["header"]["hierarchical_structure_code"] = el(seg.elements, 1)
            tx["header"]["transaction_set_purpose_code"] = el(seg.elements, 2)
            tx["header"]["reference_identification"] = el(seg.elements, 3)
            tx["header"]["date"] = date_from_d8(el(seg.elements, 4))
            tx["header"]["time"] = el(seg.elements, 5) or None
            tx["header"]["transaction_type_code"] = el(seg.elements, 6) or None

        elif seg.id == "NM1":
            entity_code = el(seg.elements, 1)
            parsed = parse_nm1(seg.elements)

            last_nm1_target = parsed

            if entity_code == "41":  # Submitter
                tx["submitter"] = parsed
                _attach_trailing_contact(cursor, tx["submitter"])
            elif entity_code == "40":  # Receiver
                tx["receiver"] = parsed
            elif entity_code == "85":  # Billing provider
                current_billing_provider = parsed
                tx["billing_provider"] = current_billing_provider
            elif entity_code == "87":  # Pay-to provider
                if current_billing_provider is not None:
                    current_billing_provider["pay_to_provider"] = parsed
            elif entity_code == "IL":  # Subscriber
                if current_subscriber is not None:
                    current_subscriber["insured"] = parsed
                    last_nm1_target = current_subscriber["insured"]
            elif entity_code == "PR":  # Payer
                if current_subscriber is not None:
                    current_subscriber["payer"] = parsed
            elif entity_code == "QC":  # Patient
                if current_patient is not None:
                    current_patient["patient"] = parsed
                    last_nm1_target = current_patient["patient"]
            elif entity_code in ("DN", "P3"):  # Referring provider
                if current_claim is not None:
                    current_claim.setdefault("referring_providers", []).append(
                        {**parsed, "role": "referring" if entity_code == "DN" else "primary_care"}
                    )
                    last_nm1_target = current_claim["referring_providers"][-1]
            elif entity_code == "82":  # Rendering provider
                if current_claim is not None:
                    current_claim["rendering_provider"] = parsed
                    last_nm1_target = current_claim["rendering_provider"]
            elif entity_code == "77":  # Service facility location
                if current_claim is not None:
                    current_claim["service_facility_location"] = parsed
                    last_nm1_target = current_claim["service_facility_location"]
            elif entity_code == "DQ":  # Supervising provider
                if current_claim is not None:
                    current_claim["supervising_provider"] = parsed
                    last_nm1_target = current_claim["supervising_provider"]

        elif seg.id == "N3":
            if last_nm1_target is not None:
                last_nm1_target["address"] = parse_n3(seg.elements)

        elif seg.id == "N4":
            if last_nm1_target is not None:
                last_nm1_target.setdefault("address", {}).update(parse_n4(seg.elements))

        elif seg.id == "PER":
            if last_nm1_target is not None and last_nm1_target is not tx.get("submitter"):
                last_nm1_target["contact"] = parse_per(seg.elements)
            elif tx["submitter"] is not None and current_billing_provider is None:
                tx["submitter"]["contact"] = parse_per(seg.elements)

        elif seg.id == "PRV":
            target = last_nm1_target if last_nm1_target is not None else current_billing_provider
            if target is not None:
                target["provider_taxonomy"] = {
                    "provider_code": el(seg.elements, 1),
                    "qualifier": el(seg.elements, 2),
                    "taxonomy_code": el(seg.elements, 3),
                }

        elif seg.id == "HL":
            hl_id = el(seg.elements, 1)
            parent_id = el(seg.elements, 2) or None
            level_code = el(seg.elements, 3)  # 20=Billing Provider, 22=Subscriber, 23=Patient
            has_child = el(seg.elements, 4) == "1"

            if level_code == "20":
                current_billing_provider = current_billing_provider or {}
                if tx["billing_provider"] is None:
                    tx["billing_provider"] = current_billing_provider
                node = {"type": "billing_provider", "ref": current_billing_provider}
            elif level_code == "22":
                current_subscriber = {
                    "hl_id": hl_id, "insured": {}, "claims": [],
                }
                tx["subscribers"].append(current_subscriber)
                current_patient = None
                node = {"type": "subscriber", "ref": current_subscriber}
            elif level_code == "23":
                current_patient = {"hl_id": hl_id, "patient": {}, "claims": []}
                if current_subscriber is not None:
                    current_subscriber.setdefault("dependents", []).append(current_patient)
                node = {"type": "patient", "ref": current_patient}
            else:
                node = {"type": level_code, "ref": None}

            hl_nodes[hl_id] = node

        elif seg.id == "SBR":
            if current_subscriber is not None:
                responsibility = el(seg.elements, 1)
                current_subscriber["subscriber_info"] = {
                    "payer_responsibility_sequence": responsibility,
                    "payer_responsibility_description": SBR_PAYER_RESPONSIBILITY.get(
                        responsibility, responsibility
                    ),
                    "individual_relationship_code": el(seg.elements, 2) or None,
                    "individual_relationship_description": INDIVIDUAL_RELATIONSHIP_CODE.get(
                        el(seg.elements, 2), None
                    ),
                    "group_number": el(seg.elements, 3) or None,
                    "insurance_type_code": el(seg.elements, 5) or None,
                    "claim_filing_indicator_code": el(seg.elements, 9) or None,
                    "claim_filing_indicator_description": CLAIM_FILING_INDICATOR.get(
                        el(seg.elements, 9), el(seg.elements, 9)
                    ),
                }

        elif seg.id == "PAT":
            if current_patient is not None:
                current_patient["relationship_to_subscriber"] = INDIVIDUAL_RELATIONSHIP_CODE.get(
                    el(seg.elements, 1), el(seg.elements, 1)
                )

        elif seg.id == "DMG":
            if last_nm1_target is not None:
                last_nm1_target["date_of_birth"] = date_from_d8(el(seg.elements, 2))
                last_nm1_target["gender"] = {"M": "Male", "F": "Female", "U": "Unknown"}.get(
                    el(seg.elements, 3), el(seg.elements, 3)
                )

        elif seg.id == "CLM":
            current_claim = {
                "claim_id": el(seg.elements, 1),
                "total_charge_amount": _to_number(el(seg.elements, 2)),
                "place_of_service_code": None,
                "provider_signature_indicator": el(seg.elements, 6) or None,
                "assignment_of_benefits_indicator": el(seg.elements, 7) or None,
                "benefits_assignment_certification_indicator": el(seg.elements, 8) or None,
                "release_of_information_code": el(seg.elements, 9) or None,
                "diagnoses": [],
                "service_lines": [],
                "referring_providers": [],
            }
            facility_composite = split_composite(el(seg.elements, 5), delims)
            if facility_composite and facility_composite[0]:
                pos_code = facility_composite[0]
                current_claim["place_of_service_code"] = pos_code
                current_claim["place_of_service_description"] = FACILITY_CODE_PLACE_OF_SERVICE.get(
                    pos_code, pos_code
                )
                if len(facility_composite) > 1:
                    current_claim["claim_frequency_code"] = facility_composite[-1]
                    current_claim["claim_frequency_description"] = CLAIM_FREQUENCY_CODE.get(
                        facility_composite[-1], facility_composite[-1]
                    )
            current_service_line = None
            _attach_claim(current_claim, current_patient, current_subscriber)

        elif seg.id == "HI":
            if current_claim is not None:
                for raw_composite in seg.elements[1:]:
                    if not raw_composite:
                        continue
                    parts = split_composite(raw_composite, delims)
                    if not parts or not parts[0]:
                        continue
                    qual, code = parts[0], parts[1] if len(parts) > 1 else None
                    current_claim["diagnoses"].append({
                        "qualifier": qual,
                        "qualifier_description": DIAGNOSIS_QUALIFIER.get(qual, qual),
                        "code": code,
                    })

        elif seg.id == "LX":
            current_service_line = {
                "line_number": el(seg.elements, 1),
                "procedure": {},
                "diagnosis_pointers": [],
            }
            if current_claim is not None:
                current_claim["service_lines"].append(current_service_line)

        elif seg.id == "SV1":
            if current_service_line is not None:
                proc_composite = split_composite(el(seg.elements, 1), delims)
                qualifier = proc_composite[0] if proc_composite else None
                proc_code = proc_composite[1] if len(proc_composite) > 1 else None
                modifiers = [m for m in proc_composite[2:6] if m]
                current_service_line["procedure"] = {
                    "qualifier": qualifier,
                    "code": proc_code,
                    "modifiers": modifiers,
                }
                current_service_line["charge_amount"] = _to_number(el(seg.elements, 2))
                current_service_line["unit_of_measure"] = el(seg.elements, 3) or None
                current_service_line["units"] = _to_number(el(seg.elements, 4))
                pointers = el(seg.elements, 7)
                if pointers:
                    current_service_line["diagnosis_pointers"] = [p for p in pointers.split(delims.subelement_separator) if p]

        elif seg.id == "DTP":
            entry = parse_dtp(seg.elements)
            if current_service_line is not None:
                current_service_line.setdefault("dates", []).append(entry)
            elif current_claim is not None:
                current_claim.setdefault("dates", []).append(entry)
            elif current_patient is not None or current_subscriber is not None:
                pass  # patient/subscriber-level dates are rare in 837P; skip gracefully

        elif seg.id == "REF":
            entry = parse_ref(seg.elements)
            if current_service_line is not None:
                current_service_line.setdefault("references", []).append(entry)
            elif current_claim is not None:
                current_claim.setdefault("references", []).append(entry)
            elif current_billing_provider is not None and current_subscriber is None:
                current_billing_provider.setdefault("references", []).append(entry)
            elif current_subscriber is not None:
                current_subscriber.setdefault("references", []).append(entry)

        elif seg.id == "AMT":
            if current_claim is not None:
                current_claim.setdefault("amounts", []).append({
                    "qualifier": el(seg.elements, 1),
                    "amount": _to_number(el(seg.elements, 2)),
                })

        elif seg.id == "SE":
            tx["header"]["segment_count"] = el(seg.elements, 1)
            tx["header"]["trailer_control_number"] = el(seg.elements, 2)
            break  # end of this transaction set

    # Clean up helper-only fields before returning
    for sub in tx["subscribers"]:
        sub.pop("hl_id", None)
        for dep in sub.get("dependents", []):
            dep.pop("hl_id", None)

    return tx


def _attach_trailing_contact(cursor: SegmentCursor, target: Dict[str, Any]) -> None:
    """NM1 for submitter is typically followed by one or more PER segments."""
    while cursor.peek() is not None and cursor.peek().id == "PER":
        seg = cursor.next()
        contacts = target.setdefault("contacts", [])
        contacts.append(parse_per(seg.elements))


def _attach_claim(claim: Dict[str, Any], patient: Optional[Dict[str, Any]],
                   subscriber: Optional[Dict[str, Any]]) -> None:
    if patient is not None:
        patient["claims"].append(claim)
    elif subscriber is not None:
        subscriber["claims"].append(claim)


def _to_number(value: str):
    if value in (None, ""):
        return None
    try:
        if re.fullmatch(r"-?\d+", value):
            return int(value)
        return float(value)
    except ValueError:
        return value


# ---------------------------------------------------------------------------
# Top-level envelope parser (ISA/GS/ST...SE/GE/IEA)
# ---------------------------------------------------------------------------

def parse_837p_file(raw: str) -> Dict[str, Any]:
    delims = detect_delimiters(raw)
    segments = tokenize(raw, delims)
    cursor = SegmentCursor(segments)

    result: Dict[str, Any] = {
        "interchange": {},
        "functional_groups": [],
    }

    current_group = None

    while not cursor.at_end():
        seg = cursor.peek()

        if seg.id == "ISA":
            e = seg.elements
            result["interchange"] = {
                "authorization_info_qualifier": el(e, 1),
                "security_info_qualifier": el(e, 3),
                "sender_qualifier": el(e, 5),
                "sender_id": el(e, 6).strip(),
                "receiver_qualifier": el(e, 7),
                "receiver_id": el(e, 8).strip(),
                "date": date_from_d8("20" + el(e, 9)) if len(el(e, 9)) == 6 else el(e, 9),
                "time": el(e, 10),
                "control_version_number": el(e, 12),
                "control_number": el(e, 13),
                "usage_indicator": {"P": "Production", "T": "Test"}.get(el(e, 15), el(e, 15)),
            }
            cursor.next()

        elif seg.id == "GS":
            e = seg.elements
            current_group = {
                "functional_id_code": el(e, 1),
                "sender_id": el(e, 2),
                "receiver_id": el(e, 3),
                "date": date_from_d8(el(e, 4)),
                "time": el(e, 5),
                "control_number": el(e, 6),
                "version_release_industry_id": el(e, 8),
                "transactions": [],
            }
            result["functional_groups"].append(current_group)
            cursor.next()

        elif seg.id == "ST":
            tx = parse_837p_transaction(cursor, delims)
            if current_group is not None:
                current_group["transactions"].append(tx)
            else:
                # No GS wrapper found; attach to a synthetic group
                result.setdefault("transactions", []).append(tx)

        elif seg.id == "GE":
            cursor.next()

        elif seg.id == "IEA":
            cursor.next()

        else:
            # Unknown/unhandled top-level segment; skip
            cursor.next()

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Translate an X12 837P health care claim file into JSON."
    )
    parser.add_argument("input", help="Path to the X12 837P input file")
    parser.add_argument("-o", "--output", help="Path to write the JSON output "
                                                 "(default: stdout)")
    parser.add_argument("--pretty", action="store_true", default=True,
                         help="Pretty-print the JSON output (default: on)")
    parser.add_argument("--compact", action="store_true",
                         help="Emit compact JSON instead of pretty-printed")
    args = parser.parse_args(argv)

    with open(args.input, "r", encoding="utf-8-sig", errors="replace") as f:
        raw = f.read()

    doc = parse_837p_file(raw)

    indent = None if args.compact else 2
    json_text = json.dumps(doc, indent=indent, ensure_ascii=False)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(json_text)
        n_claims = sum(
            len(sub.get("claims", [])) + sum(len(d.get("claims", [])) for d in sub.get("dependents", []))
            for grp in doc.get("functional_groups", [])
            for tx in grp.get("transactions", [])
            for sub in tx.get("subscribers", [])
        )
        print(f"Wrote {args.output} ({n_claims} claim(s) parsed).", file=sys.stderr)
    else:
        print(json_text)


if __name__ == "__main__":
    main()
