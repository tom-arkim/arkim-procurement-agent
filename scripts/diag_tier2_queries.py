"""
Diagnostic: Tier 3 query construction for the five empty-result scenarios.
Prints the exact specs dict, which branch executes, and the query string.
No network calls — only imports _build_tier3_query and inspect the output.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

from utils.models import AssetSpecs
from utils.sourcing_archieved.tavily_client import _build_tier3_query

SCENARIOS = [
    # 1. Baldor 5HP motor — no model, no PN (Equipment)
    AssetSpecs(
        manufacturer="Baldor",
        model="N/A",
        part_number="UNKNOWN-PN",
        voltage="N/A",
        category="Equipment",
        hp="5HP",
        rpm="1750",
        detected_type="induction motor",
        description="5HP induction motor 1750 RPM",
    ),
    # 2. John Crane MR-1-1375 — mechanical seal (Part)
    AssetSpecs(
        manufacturer="John Crane",
        model="MR-1",
        part_number="MR-1-1375",
        voltage="N/A",
        category="Part",
        shaft_size='1-3/8"',
        detected_type="mechanical seal",
        description="Type 1 single mechanical seal, 1-3/8 inch shaft",
    ),
    # 3. PMD11-AA1V1HFVXJA (typo — PMD is a real E+H prefix, full PN is fabricated)
    AssetSpecs(
        manufacturer="Endress+Hauser",
        model="PMD11",
        part_number="PMD11-AA1V1HFVXJA",
        voltage="24VDC",
        category="Part",
        detected_type="differential pressure transmitter",
        description="Differential pressure transmitter, ceramic cell",
    ),
    # 4. Promag 10W — full electromagnetic flow meter unit (Equipment)
    #    Specs as they arrive from the maintenance handoff and intake confirm.
    AssetSpecs(
        manufacturer="Endress+Hauser",
        model="Promag 10W",
        part_number="10W40-AA2B1AA0AAAA",
        voltage="N/A",
        category="Equipment",
        detected_type="electromagnetic flow meter",
        description="Complete electromagnetic flow meter (full unit replacement), Promag 10W",
    ),
    # 5. Hyundai Crown Triton — 3-phase induction motor (Equipment)
    #    Realistic nameplate extraction: manufacturer, model, hp, voltage, frame, rpm.
    AssetSpecs(
        manufacturer="Hyundai",
        model="Crown Triton",
        part_number="UNKNOWN-PN",
        voltage="460V",
        category="Equipment",
        hp="15HP",
        frame="254T",
        phase="3-phase",
        rpm="1800",
        detected_type="induction motor",
        description="Hyundai Crown Triton 15HP 3-phase induction motor 460V",
    ),
]

LABELS = [
    "Baldor 5HP motor 1750rpm (Equipment, no PN)",
    "John Crane MR-1-1375 (Part, known PN)",
    "PMD11-AA1V1HFVXJA typo (Part, fabricated PN)",
    "Promag 10W full unit (Equipment, has PN in specs)",
    "Hyundai Crown Triton (Equipment, no PN)",
]

SEP = "-" * 80

print()
for label, specs in zip(LABELS, SCENARIOS):
    branch  = "Part (line 152)" if specs.category == "Part" else "Equipment (line 175)"
    known_pn = specs.part_number not in (None, "", "N/A", "UNKNOWN-PN", "Unknown")
    query   = _build_tier3_query(specs)

    print(SEP)
    print(f"SCENARIO: {label}")
    print(f"  manufacturer  : {specs.manufacturer}")
    print(f"  category      : {specs.category}")
    print(f"  detected_type : {specs.detected_type}")
    print(f"  model         : {specs.model!r}")
    print(f"  part_number   : {specs.part_number!r}")
    print(f"  hp            : {specs.hp!r}")
    print(f"  voltage       : {specs.voltage!r}")
    print(f"  frame         : {specs.frame!r}")
    print(f"  rpm           : {specs.rpm!r}")
    print(f"  shaft_size    : {specs.shaft_size!r}")
    print()
    print(f"  branch        : {branch}")
    print(f"  pn non-null   : {known_pn}")
    print()
    print(f"  QUERY: {query!r}")
    print()
