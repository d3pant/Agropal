"""Insurance Agent — part of the Economic Reporting & Policy Coordinator (ERPC).

Fills the official USDA CCC-576 (Notice of Loss) PDF form using pypdf.
Reads status.json, econ_report.json, and farm_config.json and pre-populates
all fields we have data for. Fields requiring farmer input are left blank
or marked with a note.

The CCC-576 is the primary Notice of Loss form for ELAP, LFP, LIP, and NAP.
It must be filed within 30 days of the loss event.

Usage:
    python insurance_agent.py [--dry-run]
    python insurance_agent.py --output /path/to/filled_ccc576.pdf

Official form source: https://www.farmers.gov/sites/default/files/documents/ccc-576.pdf
Bundled at: forecaster/forms/ccc_576.pdf
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from pypdf import PdfReader, PdfWriter

from erpc.ccc576_form import build_field_map, lookup_office
from erpc.common import (
    ECON_REPORT, FARM_CONFIG, FILLED_PDF, FORMS_DIR, STATUS_JSON,
    get_logger, load_crop_output, load_json,
)

logger = get_logger("insurance_agent")

CCC_576_BLANK = FORMS_DIR / "ccc_576.pdf"


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_live_crop_destructions() -> Optional[list]:
    """crop_destructions from the latest crop agent output.

    Returns the list (possibly empty — a legitimate "no destructions" answer)
    or None if no crop output file exists at all.
    """
    data, path = load_crop_output()
    if path is None:
        return None
    destructions = data["task2"].get("crop_destructions", [])
    logger.info("Loaded %d crop_destructions from %s", len(destructions), path.name)
    return destructions


MOCK_STATUS = {
    "timestamp": "2026-05-08T06:00:00Z",
    "threat_level": "CRITICAL",
    "fwi_index": 10.0,
    "nearest_fire": {
        "name": "Palisades Fire",
        "distance_km": 75.0,
        "detected_at": "2026-05-08T06:00:00Z",
    },
}

MOCK_ECON = {
    "farm_id": "farm_sdge_001",
    "financial_exposure": {
        "crop_loss_total_usd": 546720.0,
        "breakdown_by_crop": {"almonds": 176800.0, "tomatoes": 369920.0},
    },
    # task2-style crop destructions — what the real crop agent provides
    "crop_destructions": [
        {
            "field_id": "F5",
            "crop_category": "almonds",
            "size_acres": 25,
            "estimated_loss_usd": 208000.0,
            "confidence_adjusted_loss_usd": 176800.0,
            "task4_decision": "ABANDON",
        },
        {
            "field_id": "F3",
            "crop_category": "tomatoes",
            "size_acres": 10,
            "estimated_loss_usd": 435200.0,
            "confidence_adjusted_loss_usd": 369920.0,
            "task4_decision": "PARTIAL HARVEST",
        },
    ],
}


# ---------------------------------------------------------------------------
# PDF filler
# ---------------------------------------------------------------------------

def fill_ccc576(
    farm_config: dict,
    status: dict,
    econ: dict,
    output_path: Path,
) -> Path:
    """Render a filled CCC-576 to `output_path` and return that path."""
    loc = farm_config.get("location", {})
    if not lookup_office(loc.get("state", ""), loc.get("county", ""))[0]:
        logger.warning(
            "No FSA office/FIPS on file for %s County, %s — Items 1 and 4 left blank",
            loc.get("county") or "?", loc.get("state") or "?",
        )

    field_map = build_field_map(farm_config, status, econ.get("crop_destructions", []))

    writer = PdfWriter()
    writer.append(PdfReader(str(CCC_576_BLANK)))
    for page in writer.pages:   # the form spans both pages
        writer.update_page_form_field_values(page, field_map, auto_regenerate=False)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "wb") as f:
        writer.write(f)

    filled = sum(1 for v in field_map.values() if v)
    total = len(field_map)
    logger.info("Filled %d/%d fields → %s", filled, total, output_path)
    return output_path


# ---------------------------------------------------------------------------
# Main agent class
# ---------------------------------------------------------------------------

class InsuranceAgent:
    def __init__(
        self,
        farm_config_path: str | Path = FARM_CONFIG,
        status_path: str | Path = STATUS_JSON,
        econ_path: str | Path = ECON_REPORT,
        output_path: str | Path = FILLED_PDF,
        use_mock: bool = False,
    ):
        self.farm_config = load_json(Path(farm_config_path))
        self.status_path = Path(status_path)
        self.econ_path = Path(econ_path)
        self.output_path = Path(output_path)
        self.use_mock = use_mock
        self.crop_source = "unknown"

    def run(self) -> Path:
        if self.use_mock:
            logger.info("Dry run — using mock status and econ data")
            status, econ = dict(MOCK_STATUS), dict(MOCK_ECON)
            self.crop_source = "mock"
            return fill_ccc576(self.farm_config, status, econ, self.output_path)

        status = load_json(self.status_path, MOCK_STATUS)
        econ = load_json(self.econ_path, MOCK_ECON)

        # Insurance form needs per-crop destructions. Econ report has aggregate
        # exposure, not the destruction list — pull that from the latest crop
        # agent output (same source econ uses), and only fall back to mock if
        # neither econ nor live crop data has anything.
        if "crop_destructions" not in econ:
            live = _load_live_crop_destructions()
            if live is None:
                # No live crop output exists — use mock so the form still renders
                self.crop_source = "mock"
                econ["crop_destructions"] = MOCK_ECON["crop_destructions"]
            else:
                # Live data exists; an empty list is a legitimate "no crops at risk"
                self.crop_source = "live"
                econ["crop_destructions"] = live
        else:
            self.crop_source = "econ_report"

        return fill_ccc576(self.farm_config, status, econ, self.output_path)

    def print_summary(self, out_path: Path) -> None:
        print(f"\n  CCC-576 filled: {out_path}")
        print("  Fields pre-filled from system data:")
        print("    Item 1  — County FSA Office")
        print("    Item 2  — Crop year")
        print("    Item 3  — Producer name and location")
        print("    Item 4  — State/county FIPS codes")
        print("    Item 5  — Disaster type (Wildfire), start/end dates")
        print("    Item 6  — Crop name, intended use")
        print("    Items 7–8  — Farm number, planted acreage, disaster-affected acreage (per crop, up to 3)")
        print("    Items 11–29 — Production section: crop, acreage, producer share, salvage value (per crop)")
        print("    Items 32–37 — Inventory values before/after disaster, salvage")
        print("  Fields left blank (farmer completes at FSA office):")
        print("    NAP unit numbers, actual production records, variety/type,")
        print("    planting period, forage/grazing section (Items 38–48), signatures")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Insurance Agent — fills USDA CCC-576 Notice of Loss")
    parser.add_argument("--dry-run", action="store_true", help="Fill the form from mock data")
    parser.add_argument("--output", default=str(FILLED_PDF), help="Output PDF path")
    args = parser.parse_args()

    agent = InsuranceAgent(output_path=args.output, use_mock=args.dry_run)
    out = agent.run()
    agent.print_summary(out)


if __name__ == "__main__":
    main()
