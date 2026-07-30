"""Field layout for the USDA CCC-576 (Notice of Loss) PDF.

Everything here describes the *form*: which AcroForm field names the blank PDF
uses, and which of our values belong in each. The agent that loads data and
writes the PDF lives in insurance_agent.py.

The field names are inconsistent in the official form itself ("item 17line1"
next to "item 17_row 3"), so they are tabulated rather than derived. Read the
tables as the form's own vocabulary, not ours.

Form structure:
  Items 1-10:  Part A header — office, producer, disaster info
  Items 11-31: Part B production/acreage (page 2, per-crop rows)
  Items 32-37: Part C inventory losses
  Items 38-48: Part D forage/grazing losses — left blank, no livestock feed yet
  Items 49-52: Part E/F certifications — FSA officer completes
  Signatures:  farmer and FSA officer sign in person
"""

from __future__ import annotations

from datetime import datetime

# FSA office address and state-county FIPS, keyed by (state, county). Any county
# not listed leaves Items 1 and 4 blank rather than asserting a wrong office —
# the farmer completes them at their local FSA office.
FSA_OFFICES: dict[tuple[str, str], tuple[str, str]] = {
    ("CA", "San Diego"): (
        "San Diego County FSA Office\n1204 Mission Road, Suite 1\nEscondido, CA 92029",
        "06-073",
    ),
}

# Up to three crop rows fit on the form; extra destructions are not reported.
MAX_ROWS = 3
ROW_LABELS = ("row 1", "row 2", "row 3")

# Defaults the farmer is expected to verify at the FSA office.
PRODUCER_SHARE = "1.000"   # 100% share
PRACTICE = "N"             # N = nonirrigated
UNIT_OF_MEASURE = "Tons"
INTENDED_USE = "Sale"
SALVAGE_FRACTION = 0.05    # rough salvage estimate, 5% of adjusted loss

# Part B production rows: our item number -> the form's field name, per row.
PROD_ROW_KEYS = (
    {"17": "item 17line1",   "19": "item19_row 1", "20": "item20_line_1",
     "21": "item21_row_1",   "22": "item 22_line_1", "24": "item 24_line_1",
     "25": "item 25_line_1", "26": "item 26_line_1", "27": "item 27_line_1",
     "28": "item 28_row 1",  "29": "item 29_row 1"},
    {"17": "item 17line2",   "19": "item19_row 2", "20": "item20_line_2",
     "21": "item21_row_2",   "22": "item 22_line_2", "24": "item 24_line_2",
     "25": "item 25_line_2", "26": "item 26_line_2", "27": "item 27_line 2",
     "28": "item 28_row 2",  "29": "item 29_row 2"},
    {"17": "item 17_row 3",  "19": "item19_row 3", "20": "item20_line_3",
     "21": "item21_row_3",   "22": "item 22_line_3", "24": "item 24_line_3",
     "25": "item 25_line_3", "26": "item 26_line_3", "27": "item 27_line 3",
     "28": "item 28_row 3",  "29": "item 29_row 3"},
)

# Part C inventory rows.
INV_ROW_KEYS = (
    {"32": "item32_row 1", "33": "item 33_row 1", "34": "item 34_row 1",
     "35": "item 35_row 1", "36": "item 36_row 1", "37": "item 37_row 1"},
    {"32": "item32_row 2", "33": "item 33_row 2", "34": "item 34_row 2",
     "35": "item 35_row 2", "36": "item 36_row 2", "37": "item 37_row 2"},
    {"32": "item32_row 3", "33": "item 33_row 3", "34": "item 34_row 3",
     "35": "item 35_row 3", "36": "item 36_row 3", "37": "item 37_row 3"},
)


# --- Value helpers ---------------------------------------------------------

def _parse(iso: str) -> datetime | None:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def fmt_date(iso: str) -> str:
    """ISO timestamp as MM-DD-YYYY, the format FSA forms expect."""
    dt = _parse(iso)
    return dt.strftime("%m-%d-%Y") if dt else ""


def crop_year(iso: str) -> str:
    dt = _parse(iso)
    return dt.strftime("%Y") if dt else ""


def _acres(d: dict | None) -> str:
    return str(d["size_acres"]) if d else ""


def _crop_name(d: dict | None) -> str:
    return d["crop_category"].title() if d else ""


def _rows(destructions: list) -> list[dict | None]:
    """Pad the destruction list out to the form's fixed row count."""
    return [destructions[i] if i < len(destructions) else None for i in range(MAX_ROWS)]


# --- Section builders ------------------------------------------------------
#
# Each returns the fields for one part of the form. Empty string means "left
# for the farmer or the FSA officer to complete".

def _header(producer_address: str, office: str, fips: str,
            event_iso: str, destructions: list) -> dict[str, str]:
    return {
        "Item 1_ County FSA Office": office,
        "item_2": crop_year(event_iso),
        "item_3": producer_address,
        "4": fips,
        "item_5a": "Wildfire",
        "item 56b": fmt_date(event_iso),   # disaster start date
        "item 5c": fmt_date(event_iso),    # disaster end date — same, farmer updates
        "item6A": _crop_name(destructions[0]) if destructions else "",
        "item6B": "",                      # crop type/variety
        "item6c": INTENDED_USE,
        "item7d": PRACTICE,
        "item6e": "",                      # planting period
        "6F": fmt_date(event_iso),         # date loss first apparent
    }


def _acreage(farm_id: str, rows: list[dict | None], destructions: list) -> dict[str, str]:
    """Items 7A-7E and 8A-8D — one block per crop row."""
    fields: dict[str, str] = {}
    for i, (label, d) in enumerate(zip(ROW_LABELS, rows)):
        fields[f"item 7A_{label}"] = farm_id
        fields[f"item 7B_{label}"] = ""            # NAP unit number
        fields[f"item 7C_{label}"] = _acres(d)     # intended acres
        fields[f"Item 7D_{label}"] = _acres(d)     # planted acres
        fields[f"7E_Row {i + 1}"] = ""             # prevented planted
        fields[f"item 8C_{label}"] = _acres(d)     # total planted acreage
        fields[f"item 8D_{label}"] = _acres(d)     # disaster affected acreage

    # Item 8A/8B names row 1 as "item8a1" but row 2 as "item8a_Row 2" — the form
    # is inconsistent here, so these four keys are spelled out rather than looped.
    fields["item8a1"] = farm_id
    fields["item8b1"] = ""
    fields["item8a_Row 2"] = farm_id if len(destructions) > 1 else ""
    fields["item8b1_row 2"] = ""
    return fields


def _production(farm_name: str, event_iso: str, rows: list[dict | None]) -> dict[str, str]:
    """Part B, items 11-29."""
    fields = {
        "11": farm_name,
        "12": crop_year(event_iso),
        "13": "",   # unit number — farmer fills
        "14": "",   # pay crop code — FSA fills
        "15": "",   # pay type code — FSA fills
        "16": "",   # planting period — farmer fills
    }
    for keys, d in zip(PROD_ROW_KEYS, rows):
        fields[keys["17"]] = _crop_name(d)
        fields[keys["19"]] = PRODUCER_SHARE
        fields[keys["20"]] = _acres(d)
        fields[keys["21"]] = PRACTICE
        fields[keys["22"]] = ""              # stage — farmer fills
        fields[keys["24"]] = ""              # actual production — farmer fills
        fields[keys["25"]] = UNIT_OF_MEASURE
        fields[keys["26"]] = INTENDED_USE
        fields[keys["27"]] = "Wildfire Loss" if d else ""
        fields[keys["28"]] = str(round(d["confidence_adjusted_loss_usd"], 2)) if d else ""
        fields[keys["29"]] = ""              # production not to count — FSA fills
    return fields


def _inventory(rows: list[dict | None]) -> dict[str, str]:
    """Part C, items 32-37 — inventory value before and after the disaster."""
    fields: dict[str, str] = {}
    for keys, d in zip(INV_ROW_KEYS, rows):
        abandoned = bool(d) and d.get("task4_decision") == "ABANDON"
        fields[keys["32"]] = _crop_name(d)
        fields[keys["33"]] = PRODUCER_SHARE if d else ""
        fields[keys["34"]] = str(round(d["estimated_loss_usd"], 2)) if d else ""
        fields[keys["35"]] = "0.00" if abandoned else ""   # value after disaster
        fields[keys["36"]] = ""                            # ineligible — FSA fills
        fields[keys["37"]] = (
            str(round(d["confidence_adjusted_loss_usd"] * SALVAGE_FRACTION, 2)) if d else ""
        )
    return fields


def build_field_map(farm_config: dict, status: dict, destructions: list) -> dict[str, str]:
    """Map every CCC-576 field we can fill to a value from farm and loss data.

    Unfillable fields are set to empty string so the PDF renders them blank.
    Callers that need to warn about an unknown county should call
    `lookup_office` themselves — this function fills blanks silently.
    """
    loc = farm_config.get("location", {})
    state, county = loc.get("state", ""), loc.get("county", "")
    farm_name = farm_config.get("farm_name", "")
    farm_id = farm_config.get("farm_id", "")

    fire = status.get("nearest_fire") or {}
    event_iso = fire.get("detected_at") or status.get("timestamp", "")

    producer_address = (
        f"{farm_name}\nLat: {loc.get('lat', '')}, Lon: {loc.get('lon', '')}\n{county}, {state}"
    )
    office, fips = lookup_office(state, county)
    rows = _rows(destructions)

    return {
        **_header(producer_address, office, fips, event_iso, destructions),
        **_acreage(farm_id, rows, destructions),
        **_production(farm_name, event_iso, rows),
        **_inventory(rows),
    }


def lookup_office(state: str, county: str) -> tuple[str, str]:
    """FSA office address and FIPS for a county, or ("", "") if not on file."""
    return FSA_OFFICES.get((state, county), ("", ""))
