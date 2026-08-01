# X12 837P Parser

A Python program that reads an X12 837P claims file and translates it into a JSON file.

## Project Definition

Create a program that reads an X12 837P claims file and translates it into a JSON file.

### Reference Links

- [Advanced EDI Reading X12 Webinar 2018](https://www.youtube.com/watch?v=3jr9-j6oAvE)
- [Basics of EDI (X12) in Healthcare - Fundamentals](https://www.youtube.com/watch?v=XCPvCXNsao4)
- [pyx12](https://github.com/azoner/pyx12) — a HIPAA X12 document validator and converter. Parses an ANSI X12N data file and validates it against the Implementation Guidelines for a HIPAA transaction. By default creates a 997 response for 4010 and a 999 response for 5010. Can also produce an HTML representation of the X12 document or translate to/from XML.

### ANSI X12 Development

- The ANSI X12 5010 release was published in 2004, but the healthcare industry didn't adopt it for HIPAA-mandated transactions until January 1, 2012 (with a 90-day enforcement grace period through March 31, 2012).
- X12 published 008060 versions of all HIPAA-mandated implementation guides in September 2025, as part of a phased approach to advancing beyond version 5010.

## Usage

```bash
# Print JSON to stdout (pretty-printed by default)
python3 x12_837p_parser.py input.837

# Write to a file
python3 x12_837p_parser.py input.837 -o output.json

# Compact (non-pretty-printed) JSON
python3 x12_837p_parser.py input.837 -o output.json --compact
```

## Files in this repo

| File | Description |
|---|---|
| `x12_837p_parser.py` | The parser script |
| `X12-837.txt` | Sample 837P input file |
| `output.json` | Sample JSON output generated from `X12-837.txt` |
