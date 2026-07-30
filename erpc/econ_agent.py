"""Econ Agent — part of the Economic Reporting & Policy Coordinator (ERPC).

Runs during Stage 2 (fire threat active). Each monitoring cycle:
  1. Computes total financial exposure: crop loss, livestock at risk, opportunity cost.
  2. Ranks all available response actions by ROI.
  3. Writes output/econ_report.json for the farmer dashboard.

Usage:
    python econ_agent.py [--dry-run]

Live data sources (fall back to mock/hardcoded if unavailable):
  - Crop data: crop_agent/crop_agent_output_*.json (latest file)
  - Livestock data: Livestock/erpc_message.json

All cost constants are in COST_ASSUMPTIONS. See ECON_AGENT_PLAN.md.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from erpc.common import (
    ECON_REPORT, FARM_CONFIG, FARM_FIELDS_JSON, LIVESTOCK_ERPC_MSG, STATUS_JSON,
    get_logger, load_crop_output, load_json, write_report,
)

logger = get_logger("econ_agent")


def _valid_field_ids() -> set[str]:
    """Return the set of field_ids actually entered by the farmer."""
    return {f["field_id"] for f in load_json(FARM_FIELDS_JSON).get("fields", [])}


def _filter_to_valid_fields(data: dict, valid_ids: set[str]) -> dict:
    """Strip any field_id not in valid_ids from all crop task lists."""
    if not valid_ids:
        return data
    for key in ("task4", "task1", "task3"):
        if isinstance(data.get(key), list):
            data[key] = [r for r in data[key] if r.get("field_id") in valid_ids]
    t2 = data.get("task2")
    if isinstance(t2, dict) and "crop_destructions" in t2:
        t2["crop_destructions"] = [
            r for r in t2["crop_destructions"] if r.get("field_id") in valid_ids
        ]
    return data

# ---------------------------------------------------------------------------
# Hardcoded cost assumptions
# All values here are placeholders. See ECON_AGENT_PLAN.md — "Hardcoded Values"
# table for what each should be replaced with and which source provides it.
# ---------------------------------------------------------------------------

COST_ASSUMPTIONS = {
    "harvest_labor_rate_usd_per_hour": 25.0,
    "harvest_hours_per_acre": 4.0,
    "firebreak_cost_usd_per_acre": 150.0,
    "livestock_transport_cost_usd_per_head": 35.0,
    "livestock_value_per_head_usd": 1500.0,
    "transplant_seedling_value_usd_per_acre": 800.0,
    "opportunity_cost_seasons": 1,
}

# Hardcoded livestock stub — replace with Livestock Agent output.
# total_head matches farm_config.json zones (250 + 500).
HARDCODED_LIVESTOCK = {
    "total_head": 750,
    "value_per_head_usd": COST_ASSUMPTIONS["livestock_value_per_head_usd"],
    "evacuated_pct": 0.0,
}

def _build_fallback_crop_data() -> dict:
    """Build minimal crop data from farm_fields.json when crop agent hasn't run yet.
    Uses only what the farmer actually entered — no hardcoded field IDs or crop types."""
    fields = load_json(FARM_FIELDS_JSON).get("fields", [])

    task4, task1, task3, crop_destructions = [], [], [], []
    for i, fld in enumerate(fields):
        fid = fld["field_id"]
        crop = fld.get("crop_category") or fld.get("crop", "unknown")
        acres = float(fld.get("size_acres") or fld.get("acres") or 0)
        hours = 24.0 + i * 2  # placeholder spread time, evenly spaced
        task4.append({
            "field_id": fid, "crop_category": crop,
            "maturity_pct": 80, "fire_arrival_hours": hours,
            "decision": "HARVEST NOW",
            "reason": "Crop agent not yet run — defaulting to harvest recommendation",
            "enters_task1": True,
        })
        task1.append({
            "field_id": fid, "flammability": 2, "fuel_load": 30,
            "wind_factor": 1.0, "priority_score": 30, "rank": i + 1,
            "action": "MONITOR",
            "uprooting_strategy": {"transplantable": False, "labor_hours_needed": 0,
                                   "method": "Awaiting crop agent analysis.", "time_window": hours},
            "feasible_with_farm_resources": False,
        })
        task3.append({
            "field_id": fid, "intensity_score": 10.0,
            "hours_to_arrival": hours, "technique": "DRIP IRRIGATION",
            "urgency": "MONITOR", "reason": "Default — run full pipeline for live analysis",
        })
        crop_destructions.append({
            "field_id": fid, "crop_category": crop, "size_acres": acres,
            "price_per_acre_usd": 0.0, "usda_report_date": "pending",
            "estimated_loss_usd": 0.0, "confidence_adjusted_loss_usd": 0.0,
            "economic_impact_score": 0, "task4_decision": "HARVEST NOW",
            "reason": "Price lookup pending — run full pipeline",
        })

    return {
        "task4": task4,
        "task1": task1,
        "task2": {
            "generated_at": "pending",
            "threat_level": "UNKNOWN",
            "price_source": "pending",
            "crop_destructions": crop_destructions,
            "total_estimated_loss_usd": 0.0,
            "total_confidence_adjusted_loss_usd": 0.0,
        },
        "task3": task3,
    }

# ---------------------------------------------------------------------------
# Live data loaders
# ---------------------------------------------------------------------------

def _load_crop_data() -> tuple[dict, str]:
    """Load crop agent output. Returns (data, 'live'|'fallback')."""
    data, path = load_crop_output()
    # An empty crop_destructions list is a valid "no crops at risk" answer, so
    # the presence of the key — not its contents — is what makes this live.
    if path and "crop_destructions" in data["task2"]:
        logger.info("Loaded crop data from %s", path.name)
        return _filter_to_valid_fields(data, _valid_field_ids()), "live"
    logger.warning("No crop agent output found — building fallback from farm_fields.json")
    return _build_fallback_crop_data(), "fallback"


def _load_livestock_data() -> tuple[dict, str]:
    """Load livestock agent output. Returns (data, source)."""
    msg = load_json(LIVESTOCK_ERPC_MSG)
    total = msg.get("cost_optimization", {}).get("total_animals_at_risk", 0)
    if total > 0:
        return {
            "total_head": total,
            "value_per_head_usd": round(msg.get("animal_valuation_at_risk", 0) / total, 2),
            "evacuated_pct": 0.0,
            "transport_cost_usd": msg.get("transport_costs_usd", 0),
        }, "live"
    logger.warning("No livestock data at %s — using HARDCODED_LIVESTOCK", LIVESTOCK_ERPC_MSG.name)
    return {**HARDCODED_LIVESTOCK, "transport_cost_usd": None}, "hardcoded_stub"


# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------

@dataclass
class EconAction:
    action_id: str
    action_type: str          # HARVEST_NOW | PARTIAL_HARVEST | TRANSPLANT | FIREBREAK | EVACUATE_LIVESTOCK
    field_id: Optional[str]
    crop_category: Optional[str]
    priority: int             # 1 = highest
    roi: float
    confidence_adjusted_loss_avoided_usd: float
    estimated_action_cost_usd: float
    time_window_hours: Optional[float]
    urgency: str              # IMMEDIATE | HIGH | SCHEDULED
    feasible: bool = True
    infeasibility_reason: Optional[str] = None
    action_description: str = ""
    required_resources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Financial exposure computation
# ---------------------------------------------------------------------------

def _livestock_at_risk(livestock: dict) -> float:
    """Value of livestock still on the farm, net of whatever has been evacuated."""
    return (
        livestock["total_head"]
        * livestock["value_per_head_usd"]
        * (1.0 - livestock["evacuated_pct"])
    )


def _compute_financial_exposure(crop_data: dict, livestock: dict) -> dict:
    task2 = crop_data["task2"]
    task4 = crop_data["task4"]

    # Index task4 by field_id for quick lookup
    t4 = {f["field_id"]: f for f in task4}

    crop_loss_confirmed = 0.0
    crop_loss_recoverable = 0.0
    opportunity_cost = 0.0
    breakdown_by_crop: dict[str, float] = {}

    for destruction in task2["crop_destructions"]:
        fid = destruction["field_id"]
        adj_loss = destruction["confidence_adjusted_loss_usd"]
        decision = destruction.get("task4_decision") or t4.get(fid, {}).get("decision", "ABANDON")
        maturity = t4.get(fid, {}).get("maturity_pct", 100) / 100.0
        size_acres = destruction["size_acres"]
        price_per_acre = destruction["price_per_acre_usd"]
        crop = destruction["crop_category"]

        breakdown_by_crop[crop] = breakdown_by_crop.get(crop, 0.0) + adj_loss

        if decision == "ABANDON":
            crop_loss_confirmed += adj_loss
            # Opportunity cost: 1 full lost season
            opportunity_cost += price_per_acre * size_acres * COST_ASSUMPTIONS["opportunity_cost_seasons"]
        elif decision == "PARTIAL HARVEST":
            # Recoverable portion: what can be salvaged at current maturity
            recoverable = adj_loss * maturity
            confirmed = adj_loss * (1.0 - maturity)
            crop_loss_recoverable += recoverable
            crop_loss_confirmed += confirmed
            # Partial season loss — the unharvested fraction
            opportunity_cost += price_per_acre * size_acres * (1.0 - maturity) * COST_ASSUMPTIONS["opportunity_cost_seasons"]
        else:
            # HARVEST NOW, TRANSPLANT, or unknown — treat as recoverable
            crop_loss_recoverable += adj_loss

    crop_loss_total = crop_loss_confirmed + crop_loss_recoverable

    livestock_at_risk = _livestock_at_risk(livestock)
    total = crop_loss_total + livestock_at_risk + opportunity_cost

    return {
        "crop_loss_confirmed_usd": round(crop_loss_confirmed, 2),
        "crop_loss_recoverable_usd": round(crop_loss_recoverable, 2),
        "crop_loss_total_usd": round(crop_loss_total, 2),
        "livestock_at_risk_usd": round(livestock_at_risk, 2),
        "opportunity_cost_usd": round(opportunity_cost, 2),
        "total_exposure_usd": round(total, 2),
        "breakdown_by_crop": {k: round(v, 2) for k, v in breakdown_by_crop.items()},
    }


# ---------------------------------------------------------------------------
# ROI action builders
#
# One function per action type. Each takes the crop agent records for a single
# field and returns the action, or None when there is nothing to propose.
# `_build_actions` stitches them together and assigns priority.
# ---------------------------------------------------------------------------

HARVEST_RESOURCES = ["harvest crew", "transport truck"]
FIREBREAK_RESOURCES = ["water tanker", "irrigation equipment"]
EVACUATION_RESOURCES = ["livestock trailers", "transport crew", "receiving site"]


def _harvest_action(field_rec: dict, destruction: Optional[dict],
                    hydration: Optional[dict]) -> EconAction:
    """HARVEST NOW / PARTIAL HARVEST — salvage what is mature before fire arrives."""
    c = COST_ASSUMPTIONS
    fid = field_rec["field_id"]
    crop = field_rec["crop_category"]
    maturity_pct = field_rec["maturity_pct"]
    arrival_hours = field_rec["fire_arrival_hours"]
    partial = field_rec["decision"] == "PARTIAL HARVEST"

    adj_loss = destruction["confidence_adjusted_loss_usd"] if destruction else 0.0
    size_acres = destruction["size_acres"] if destruction else 0.0
    loss_avoided = adj_loss * (maturity_pct / 100.0) if partial else adj_loss

    # size_acres is 0 when the field is absent from task2 — HARVEST NOW fields
    # are not listed as crop destructions, so we cannot cost their labour.
    harvest_hours = c["harvest_hours_per_acre"] * size_acres if size_acres > 0 else None
    action_cost = c["harvest_labor_rate_usd_per_hour"] * harvest_hours if harvest_hours else 0.0
    roi = round(loss_avoided / action_cost, 1) if (action_cost > 0 and loss_avoided > 0) else 0.0
    time_ok = harvest_hours is None or arrival_hours >= harvest_hours

    action_type = "PARTIAL_HARVEST" if partial else "HARVEST_NOW"
    value = (f"saves ${loss_avoided:,.0f}" if loss_avoided > 0
             else "value unknown (not in crop destructions)")

    return EconAction(
        action_id=f"{action_type}_{fid}",
        action_type=action_type,
        field_id=fid,
        crop_category=crop,
        priority=0,
        roi=roi,
        confidence_adjusted_loss_avoided_usd=round(loss_avoided, 2),
        estimated_action_cost_usd=round(action_cost, 2),
        time_window_hours=arrival_hours,
        urgency=hydration["urgency"] if hydration else "SCHEDULED",
        feasible=time_ok,
        infeasibility_reason=None if time_ok else (
            f"Harvest requires ~{harvest_hours:.1f}h but only "
            f"{arrival_hours:.1f}h until fire arrival"
        ),
        action_description=(
            f"Harvest {fid} {crop} ({maturity_pct}% mature) — "
            f"{value}, {arrival_hours:.1f}h window"
        ),
        required_resources=HARVEST_RESOURCES,
    )


def _transplant_action(field_rec: dict, destruction: Optional[dict],
                       reduction: Optional[dict]) -> EconAction:
    """TRANSPLANT — lift seedlings out of the fire path if the farm can do it."""
    c = COST_ASSUMPTIONS
    fid = field_rec["field_id"]
    crop = field_rec["crop_category"]
    size_acres = destruction["size_acres"] if destruction else 0.0

    strategy = reduction["uprooting_strategy"] if reduction else {}
    farm_feasible = reduction["feasible_with_farm_resources"] if reduction else False
    labor_hours = strategy.get("labor_hours_needed", 0) if reduction else 0
    time_window = strategy.get("time_window") if reduction else None
    if time_window is None:
        time_window = field_rec["fire_arrival_hours"]
    equipment = strategy.get("uproot_equipment", []) if reduction else []

    seedling_value = c["transplant_seedling_value_usd_per_acre"] * size_acres
    action_cost = c["harvest_labor_rate_usd_per_hour"] * labor_hours
    roi = round(seedling_value / action_cost, 1) if action_cost > 0 else 0.0
    time_ok = time_window >= labor_hours

    if not farm_feasible:
        reason = ("Equipment not available on farm: " + ", ".join(equipment)) if equipment \
            else "Farm resources insufficient for transplant"
    elif not time_ok:
        reason = f"Transplant requires {labor_hours}h but only {time_window:.1f}h window available"
    else:
        reason = None

    return EconAction(
        action_id=f"TRANSPLANT_{fid}",
        action_type="TRANSPLANT",
        field_id=fid,
        crop_category=crop,
        priority=0,
        roi=roi,
        confidence_adjusted_loss_avoided_usd=round(seedling_value, 2),
        estimated_action_cost_usd=round(action_cost, 2),
        time_window_hours=time_window,
        urgency="SCHEDULED",
        feasible=farm_feasible and time_ok,
        infeasibility_reason=reason,
        action_description=(
            f"Transplant {fid} {crop} ({field_rec['maturity_pct']}% mature) — "
            f"saves ${seedling_value:,.0f} in seedling value"
        ),
        required_resources=equipment,
    )


def _firebreak_action(hydration: dict, destruction: dict) -> EconAction:
    """Wet firebreak / irrigation on a field with a known loss value."""
    fid = hydration["field_id"]
    crop = destruction["crop_category"]
    adj_loss = destruction["confidence_adjusted_loss_usd"]
    action_cost = COST_ASSUMPTIONS["firebreak_cost_usd_per_acre"] * destruction["size_acres"]
    urgency = hydration["urgency"]

    return EconAction(
        action_id=f"FIREBREAK_{fid}",
        action_type="FIREBREAK",
        field_id=fid,
        crop_category=crop,
        priority=0,
        roi=round(adj_loss / action_cost, 1) if action_cost > 0 else 0.0,
        confidence_adjusted_loss_avoided_usd=round(adj_loss, 2),
        estimated_action_cost_usd=round(action_cost, 2),
        time_window_hours=hydration["hours_to_arrival"],
        urgency=urgency,
        action_description=(
            f"{hydration['technique']} on {fid} {crop} — "
            f"protects ${adj_loss:,.0f} ({urgency})"
        ),
        required_resources=FIREBREAK_RESOURCES,
    )


def _livestock_action(livestock: dict) -> Optional[EconAction]:
    """Evacuate remaining head, costed from the Livestock Agent where possible."""
    at_risk = _livestock_at_risk(livestock)
    if at_risk <= 0:
        return None

    transport_cost = livestock.get("transport_cost_usd") or (
        COST_ASSUMPTIONS["livestock_transport_cost_usd_per_head"] * livestock["total_head"]
    )
    return EconAction(
        action_id="EVACUATE_LIVESTOCK",
        action_type="EVACUATE_LIVESTOCK",
        field_id=None,
        crop_category=None,
        priority=0,
        roi=round(at_risk / transport_cost, 1) if transport_cost > 0 else 0.0,
        confidence_adjusted_loss_avoided_usd=round(at_risk, 2),
        estimated_action_cost_usd=round(transport_cost, 2),
        time_window_hours=None,
        urgency="HIGH",
        action_description=(
            f"Evacuate {livestock['total_head']} head "
            f"({int(livestock['evacuated_pct'] * 100)}% already moved) — "
            f"protects ${at_risk:,.0f}"
        ),
        required_resources=EVACUATION_RESOURCES,
    )


def _prioritize(actions: list[EconAction]) -> list[EconAction]:
    """Rank by ROI, except IMMEDIATE work which jumps the queue regardless."""
    immediate = [a for a in actions if a.urgency == "IMMEDIATE"]
    rest = sorted((a for a in actions if a.urgency != "IMMEDIATE"),
                  key=lambda a: a.roi, reverse=True)
    ordered = immediate + rest
    for i, action in enumerate(ordered, start=1):
        action.priority = i
    return ordered


def _build_actions(crop_data: dict, livestock: dict) -> tuple[list[EconAction], list[EconAction]]:
    """Return (ranked feasible actions, blocked actions)."""
    by_field = {
        "destruction": {d["field_id"]: d for d in crop_data["task2"]["crop_destructions"]},
        "hydration": {f["field_id"]: f for f in crop_data["task3"]},
        "reduction": {f["field_id"]: f for f in crop_data["task1"]},
    }

    actions: list[EconAction] = []
    for field_rec in crop_data["task4"]:
        fid = field_rec["field_id"]
        destruction = by_field["destruction"].get(fid)
        decision = field_rec["decision"]

        if decision in ("HARVEST NOW", "PARTIAL HARVEST"):
            actions.append(_harvest_action(field_rec, destruction, by_field["hydration"].get(fid)))
        elif decision == "TRANSPLANT":
            actions.append(_transplant_action(field_rec, destruction, by_field["reduction"].get(fid)))
        # ABANDON and anything unrecognised: nothing worth spending money on

    for hydration in crop_data["task3"]:
        destruction = by_field["destruction"].get(hydration["field_id"])
        if destruction:   # no loss value on file means no ROI to compute
            actions.append(_firebreak_action(hydration, destruction))

    if evacuation := _livestock_action(livestock):
        actions.append(evacuation)

    feasible = [a for a in actions if a.feasible]
    infeasible = [a for a in actions if not a.feasible]
    return _prioritize(feasible), infeasible


# ---------------------------------------------------------------------------
# Main agent class
# ---------------------------------------------------------------------------

class EconAgent:
    def __init__(self, farm_config_path: str | Path = FARM_CONFIG,
                 status_path: str | Path = STATUS_JSON):
        self.farm_config = load_json(Path(farm_config_path))
        self.status_path = Path(status_path)
        self.report: dict = {}

    def _load_threat_level(self) -> str:
        return load_json(self.status_path).get("threat_level", "UNKNOWN")

    def run(self, crop_data: Optional[dict] = None, livestock: Optional[dict] = None) -> dict:
        """Run the full econ pipeline. Loads live data unless overridden."""
        crop_source = "override"
        livestock_source = "override"

        if crop_data is None:
            crop_data, crop_source = _load_crop_data()
        if livestock is None:
            livestock, livestock_source = _load_livestock_data()

        threat_level = self._load_threat_level()
        exposure = _compute_financial_exposure(crop_data, livestock)
        action_queue, infeasible_actions = _build_actions(crop_data, livestock)

        self.report = {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "farm_id": self.farm_config["farm_id"],
            "threat_level": threat_level,
            "financial_exposure": exposure,
            "cost_assumptions_used": COST_ASSUMPTIONS,
            "action_queue": [a.to_dict() for a in action_queue],
            "infeasible_actions": [a.to_dict() for a in infeasible_actions],
            "data_sources": {
                "crop_agent": crop_source,
                "livestock_agent": livestock_source,
            },
        }

        write_report(ECON_REPORT, self.report)
        return self.report

    def print_summary(self) -> None:
        e = self.report.get("financial_exposure", {})
        print("\n--- ECON REPORT SUMMARY ---")
        print(f"  Threat level          : {self.report.get('threat_level')}")
        print(f"  Total exposure        : ${e.get('total_exposure_usd', 0):>12,.2f}")
        print(f"    Crop (confirmed)    : ${e.get('crop_loss_confirmed_usd', 0):>12,.2f}")
        print(f"    Crop (recoverable)  : ${e.get('crop_loss_recoverable_usd', 0):>12,.2f}")
        print(f"    Livestock at risk   : ${e.get('livestock_at_risk_usd', 0):>12,.2f}")
        print(f"    Opportunity cost    : ${e.get('opportunity_cost_usd', 0):>12,.2f}")
        if e.get("breakdown_by_crop"):
            print("\n  Crop breakdown:")
            for crop, val in e["breakdown_by_crop"].items():
                print(f"    {crop:<20} ${val:>12,.2f}")
        print(f"\n  Action queue ({len(self.report.get('action_queue', []))} actions):")
        for a in self.report.get("action_queue", []):
            roi_str = f"{a['roi']:5.1f}x" if a["confidence_adjusted_loss_avoided_usd"] > 0 else "  N/Ax"
            print(
                f"  [{a['priority']:2}] ROI {roi_str}  [{a['urgency']:<10}]  "
                f"{a['action_description']}"
            )
        if self.report.get("infeasible_actions"):
            print(f"\n  Infeasible ({len(self.report['infeasible_actions'])} actions):")
            for a in self.report["infeasible_actions"]:
                print(f"       BLOCKED  {a['action_id']}: {a['infeasibility_reason']}")
        print("\n  output/econ_report.json written.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Econ Agent — financial exposure and ROI action ranking")
    parser.add_argument("--dry-run", action="store_true",
                        help="Skip live crop output; build fallback data from farm_fields.json")
    parser.add_argument("--status", default=str(STATUS_JSON), help="Path to forecaster status.json")
    args = parser.parse_args()

    agent = EconAgent(status_path=args.status)
    agent.run(crop_data=_build_fallback_crop_data() if args.dry_run else None)
    agent.print_summary()


if __name__ == "__main__":
    main()
