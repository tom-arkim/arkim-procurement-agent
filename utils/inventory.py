"""
Module B — Inventory Bridge
Checks internal inventory.csv for the extracted part number.
"""

import csv
import os
from typing import Optional, Tuple
from utils.models import AssetSpecs

INVENTORY_PATH = os.path.join(os.path.dirname(__file__), "..", "inventory.csv")


def check_internal(specs: AssetSpecs) -> Tuple[bool, Optional[str], Optional[float]]:
    """
    Search inventory.csv for the given part number.

    Returns:
        (found: bool, location: str | None, unit_cost: float | None)
    """
    print(f"\n[Inventory] Checking internal stock for part: {specs.part_number}")

    try:
        with open(INVENTORY_PATH, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row["Part_Number"].strip().upper() == specs.part_number.strip().upper():
                    qty = int(row.get("Qty_On_Hand", 0))
                    location = row.get("Location", "Unknown")
                    cost = float(row.get("Unit_Cost", 0))

                    if qty > 0:
                        print(f"[Inventory] HIT -- {qty} units at {location} | Cost: ${cost:.2f}")
                        return True, location, cost
                    else:
                        print(f"[Inventory] Part found but OUT OF STOCK at {location}")
                        return False, None, None

    except FileNotFoundError:
        print(f"[Inventory] inventory.csv not found at {INVENTORY_PATH}")

    print(f"[Inventory] MISS -- Part {specs.part_number} not in internal stock. Proceeding to sourcing.")
    return False, None, None
