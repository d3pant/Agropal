"""Insurance Agent — part of the Economic Reporting & Policy Coordinator (ERPC).

Fills the official USDA CCC-576 (Notice of Loss) PDF form using pypdf.
Reads status.json, econ_report.json, and farm_config.json and pre-populates
all fields we have data for. Fields requiring farmer input are left blank
or marked with a note.

All loss data comes from the Econ Agent's report: econ embeds the raw
per-field crop_destructions and a livestock summary in econ_report.json,
so this agent never bypasses it to read upstream agent files directly.
In live mode, if the econ report is missing — or its crop data is not
live crop-agent output — the agent refuses to fill the form rather than
substituting placeholder numbers. Mock data is used only for --dry-run.

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
    ECON_REPORT, FARM_CONFIG, FILLED_PDF, FORMS_DIR, LIVESTOCK_DIR, STATUS_JSON,
    get_logger, load_json,
)

logger = get_logger("insurance_agent")

CCC_576_BLANK = FORMS_DIR / "ccc_576.pdf"


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_livestock_context(econ: dict) -> Optional[dict]:
    """Livestock context for Part F (grazing) of the form.

    Head counts come from the econ report's livestock section; species and
    grazing acres come from the livestock agent's farm profile — the same
    sources the rest of the system uses. Returns None when the farm has no
    livestock at risk, in which case Part F is left blank.
    """
    lv = econ.get("livestock") or {}
    profile = load_json(LIVESTOCK_DIR / "farm_profile.json")
    pens = profile.get("pens", [])
    species = sorted({p.get("species") for p in pens if p.get("species")})
    if not species and not lv.get("total_head"):
        return None
    return {
        "species": species,
        "grazing_acres": profile.get("total_acres"),
        "total_head": lv.get("total_head"),
        "at_risk_usd": lv.get("at_risk_usd"),
    }


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
    livestock: Optional[dict] = None,
) -> Path:
    """Render a filled CCC-576 to `output_path` and return that path."""
    loc = farm_config.get("location", {})
    if not lookup_office(loc.get("state", ""), loc.get("county", ""))[0]:
        logger.warning(
            "No FSA office/FIPS on file for %s County, %s — Items 1 and 4 left blank",
            loc.get("county") or "?", loc.get("state") or "?",
        )

    field_map = build_field_map(
        farm_config, status, econ.get("crop_destructions", []), livestock=livestock
    )

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
            mock_livestock = {"species": ["cattle"], "grazing_acres": None}
            return fill_ccc576(
                self.farm_config, status, econ, self.output_path,
                livestock=mock_livestock,
            )

        status = load_json(self.status_path)
        if not status:
            logger.warning("No status at %s — disaster dates will be left blank",
                           self.status_path.name)

        econ = load_json(self.econ_path)
        if not econ:
            raise RuntimeError(
                f"Econ report not found at {self.econ_path} — run the econ agent "
                "first. Refusing to fill a CCC-576 with placeholder data."
            )
        crop_source = (econ.get("data_sources") or {}).get("crop_agent")
        if crop_source != "live" or "crop_destructions" not in econ:
            raise RuntimeError(
                "Econ report has no live crop-agent data "
                f"(crop source: {crop_source!r}) — the crop agent has not "
                "finished yet. Refusing to fill a CCC-576 with placeholder data."
            )
        self.crop_source = "econ_report"
        logger.info("Loaded %d crop_destructions from econ report",
                    len(econ["crop_destructions"]))

        livestock = _load_livestock_context(econ)
        return fill_ccc576(
            self.farm_config, status, econ, self.output_path, livestock=livestock
        )

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
        print("    Items 38–41 — Grazing section (Part F, row 1): grazing type from herd species,")
        print("                  producer share, grazing acres, practice — when the farm has livestock")
        print("  Fields left blank (farmer completes at FSA office):")
        print("    NAP unit numbers, actual production records, variety/type,")
        print("    planting period, grazing stage/carrying capacity/period days (Items 43–45), signatures")
        print("    Items 46–48 are COC use only and always stay blank.")


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
