"""
Crop Agent — Wildfire Agricultural Advisory System

Flow:
  1. Read forecast_status.json  → gate check (exit if no threat)
  2. Read farm_fields.json       → field data
  3. Fetch live USDA prices      → Step 0
  4. Fetch soil moisture + ET    → Step 0b
  5. Call Groq LLM               → Tasks 1-4
  6. Save outputs                → crop_agent_output_<ts>.json
                                   output_to_erpc.json
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from groq import Groq

load_dotenv(Path(__file__).parent / ".env")
load_dotenv(Path(__file__).parent.parent / ".env")

from usda_prices import fetch_live_prices
from soil_water import fetch_field_water_data
from landfire import fetch_field_flammability


# ── Config ────────────────────────────────────────────────────────────────────
GROQ_MODEL             = "llama-3.3-70b-versatile"
FORECAST_STATUS_FILE   = "forecast_status.json"
FARM_FIELDS_FILE       = "farm_fields.json"
CROP_PROPERTIES_FILE   = "crop_properties.json"
FARM_RESOURCES_FILE    = "farm_resources.json"

# ── System prompt ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """\
You are an agricultural wildfire management AI agent.

You receive SEVEN inputs:
  1. WILDFIRE STATUS JSON        — current fire threat data from the forecasting agent
  2. FARM FIELDS JSON            — farm field details (location, crop, size, planting date)
  3. LIVE USDA PRICES JSON       — already-fetched crop prices (Step 0 is pre-done)
  4. CROP PROPERTIES JSON        — agronomic data per crop: root system, transplantability, equipment, survival times
  5. FARM RESOURCES JSON         — what this farm actually has: labor count, equipment, trucks
  6. FIELD WATER DATA JSON       — per-field soil moisture (NASA POWER) and ET (OpenET)
  7. FIELD FLAMMABILITY DATA JSON — per-field flammability score (1–5) from LANDFIRE FBFM40 fuel model

Use ONLY the actual values provided. Never invent values. Pre-calculate all arithmetic — never write expressions like "342400 + 4068.9".
CRITICAL: Output decisions ONLY for field_ids listed in FARM FIELDS. Do not add, invent, or assume any additional fields.

OUTPUT: Return a single valid JSON object with exactly these four keys:
  "task4", "task1", "task2", "task3"
No markdown. No explanation text. Only the JSON object.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TASK 4: HARVEST / REMOVE / ABANDON DECISION  ← RUN THIS FIRST
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Run this task FIRST. Its output determines which fields enter Task 1.

For each field:
  1. Calculate fire_arrival_hours = (distance_to_field_km / spread_rate_km_per_day) × 24
     Use each field's lat/lon vs fire location lat/lon to estimate distance_to_field_km.
  2. Calculate maturity_pct using planting_date, current timestamp, and real-world
     growth period for that crop category.
  3. Decide:

     HARVEST NOW     — maturity_pct ≥ 70 AND fire arrives after harvest is possible.
                       For ANNUAL crops (tomatoes, strawberries, wheat, etc.):
                         Harvesting removes the plants → field is cleared → enters_task1 = false.
                       For PERENNIAL crops (avocado, citrus, grapes, almonds, etc.):
                         Harvesting only removes the fruit — the tree/vine STAYS in the field.
                         Check crop_properties.transplantable_mature:
                           If true AND farm has the equipment → enters_task1 = false (transplant after harvest).
                           If false → tree stays → enters_task1 = true.

     PARTIAL HARVEST — maturity_pct 40–69. Salvage what is harvestable.
                       Remaining standing biomass → enters_task1 = true.

     TRANSPLANT      — maturity_pct < 40 AND (crop_properties.transplantable_mature = true
                       OR plant age < young_threshold_years) AND farm has required uproot_equipment.
                       Field cleared after transplant → enters_task1 = false.

     ABANDON         — crop cannot be harvested AND cannot be transplanted.
                       enters_task1 = true.

  Check farm_resources to verify equipment feasibility before assigning TRANSPLANT.

Output schema for task4 (array of objects):
  { "field_id", "crop_category", "maturity_pct", "fire_arrival_hours", "decision", "reason", "enters_task1" }

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TASK 1: FIRE REDUCTION STRATEGY  ← ONLY FOR FIELDS WHERE enters_task1 = true
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
For each such field compute a UNIQUE priority score:

  A) CROP FLAMMABILITY SCORE: use the "flammability" value from FIELD FLAMMABILITY DATA JSON
     for this field's field_id. This is a live value (1–5) queried from LANDFIRE satellite data.
     Do not use crop-type estimates — use only the provided number.

  B) FUEL LOAD = flammability_score × size_acres

  C) WIND ALIGNMENT FACTOR — bearing from fire to field vs wind_direction_degrees:
       angle difference < 30°  → 1.5
       angle difference 30–60° → 1.2
       angle difference > 60°  → 1.0

  priority_score = fuel_load × wind_alignment_factor
  Every field must have a UNIQUE rank. Break ties by fuel_load, then field size.

  action must be one of: CLEAR, FIREBREAK, MONITOR

  uprooting_strategy MUST be filled for every field — never write "Not applicable".
  These fields still have plants in the ground. Use crop_properties JSON to determine:
    - transplantable_mature / transplantable_young: can remaining plants be moved?
    - uproot_equipment: what equipment is required? Check farm_resources — state if farm has it or not.
    - labor_hours_needed = uproot_labor_hrs_per_acre × size_acres
    - method: describe the physical removal steps from crop_properties.notes
    - time_window: must complete before fire_arrival_hours (from Task 4)
  If not transplantable, describe mechanical clearing (disc plow, rototiller) or firebreak creation instead.

Output schema for task1 (array of objects):
  { "field_id", "flammability", "fuel_load", "wind_factor", "priority_score", "rank",
    "action", "uprooting_strategy", "feasible_with_farm_resources" }

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TASK 2: ECONOMIC IMPACT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Only include fields whose Task 4 decision is ABANDON or PARTIAL HARVEST.
Fields with HARVEST NOW or TRANSPLANT are saved — do not count as losses.

  estimated_loss_usd           = price_per_acre_usd × size_acres
  confidence_adjusted_loss_usd = estimated_loss_usd × threat_level_confidence
  economic_impact_score        = min(100, round(confidence_adjusted_loss_usd / 30000))

Output schema for task2 (object):
  {
    "generated_at": "<ISO timestamp>",
    "threat_level": "<from wildfire JSON>",
    "price_source": "<source from live prices>",
    "price_fetched_at": "<fetched_at from live prices>",
    "crop_destructions": [
      {
        "field_id", "crop_category", "size_acres", "price_per_acre_usd",
        "usda_report_date", "estimated_loss_usd", "confidence_adjusted_loss_usd",
        "economic_impact_score", "task4_decision", "reason"
      }
    ],
    "total_estimated_loss_usd": <sum>,
    "total_confidence_adjusted_loss_usd": <sum>
  }

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TASK 3: HYDRATION STRATEGY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Farmer-executable only — no aerial services, no government resources.

  intensity_score = (temperature_c / 10) + fwi_index + (wind_speed_kmh / 10) - (humidity_percent / 10)

  technique rules (apply in order, first match wins):
    ABANDON              — hours_to_arrival < 2 OR (intensity_score > 18 AND wind_speed_kmh > 50)
    WET FIREBREAK        — intensity_score > 14 OR hours_to_arrival <= 6
    SPRINKLER IRRIGATION — intensity_score >= 8 OR hours_to_arrival <= 12
    DRIP IRRIGATION      — intensity_score < 8 AND hours_to_arrival > 12

  urgency: IMMEDIATE (hours_to_arrival < 6), SCHEDULED (6–12), MONITOR (> 12)

  soil and water adjustments using FIELD WATER DATA JSON for this field:
    soil_wetness (0–1 from NASA POWER GWETROOT):
      > 0.6  → soil already moist — reduce irrigation duration by 30%, note "soil pre-saturated"
      0.3–0.6 → normal — no adjustment
      < 0.3  → bone dry — increase irrigation duration by 40%, prioritise pre-soak before firebreak

    et_mm_per_day (from OpenET):
      > 6    → high crop water demand — bump technique up one level if on boundary
               (DRIP → SPRINKLER, SPRINKLER → WET FIREBREAK)
      2–6    → normal demand — no adjustment
      < 2    → low demand / dormant — reduce water volume, note "low ET"

    If soil_wetness or et_mm_per_day is null (API unavailable), proceed without adjustment and note "soil/ET data unavailable".

Output schema for task3 (array of objects):
  { "field_id", "intensity_score", "hours_to_arrival", "soil_wetness", "et_mm_per_day",
    "technique", "urgency", "reason" }
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_json(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"ERROR: {path} not found.")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"ERROR: {path} is not valid JSON — {e}")
        sys.exit(1)


def _eval_math_in_json(text: str) -> str:
    """Evaluate any arithmetic expressions the LLM left in numeric fields."""
    def replacer(m):
        try:
            return str(round(eval(m.group(0)), 2))  # noqa: S307
        except Exception:
            return m.group(0)
    return re.sub(r'[\d.]+(?:\s*[+\-]\s*[\d.]+)+', replacer, text)


def parse_llm_json(text: str) -> dict | None:
    """Extract and parse the JSON object returned by the LLM."""
    text = _eval_math_in_json(text)
    # Strip markdown code fences if the LLM added them despite instructions
    text = re.sub(r'^```(?:json)?\s*', '', text.strip(), flags=re.MULTILINE)
    text = re.sub(r'\s*```$', '', text.strip(), flags=re.MULTILINE)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find the outermost { ... } block
        match = re.search(r'(\{.*\})', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                return None
    return None


def add_source_metadata(parsed: dict, farm_fields: dict, wildfire_status: dict,
                       generated_at: str | None = None) -> dict:
    """Attach the farm and exact forecaster run without changing agent task payloads."""
    result = dict(parsed)
    result["_meta"] = {
        "farm_id": farm_fields.get("farm_id"),
        "run_id": wildfire_status.get("run_id"),
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
    }
    return result


def save_outputs(parsed: dict, raw_text: str, timestamp: str,
                 farm_fields: dict | None = None, wildfire_status: dict | None = None) -> None:
    """Save full agent JSON output and the ERPC economic slice."""
    if farm_fields is not None and wildfire_status is not None:
        parsed = add_source_metadata(parsed, farm_fields, wildfire_status)
    out_file = f"output_{timestamp}.json"
    with open(out_file, "w") as f:
        json.dump(parsed, f, indent=2)
    print(f"\nFull output saved  -> {out_file}")

    economic = parsed.get("task2")
    if economic:
        erpc_payload = {"economic_report": economic}
        with open("output_to_erpc.json", "w") as f:
            json.dump(erpc_payload, f, indent=2)
        print(f"ERPC output saved  -> output_to_erpc.json")
    else:
        print("WARNING: task2 missing from LLM output — output_to_erpc.json not updated.")
        fallback = f"output_{timestamp}_raw.json"
        with open(fallback, "w") as f:
            json.dump({"raw_llm_response": raw_text}, f, indent=2)
        print(f"Raw LLM text saved -> {fallback}")


# ── Main agent logic ──────────────────────────────────────────────────────────

def run_crop_agent(wildfire_status: dict, farm_fields: dict) -> None:

    # ── Gate check ────────────────────────────────────────────────────────────
    if not wildfire_status.get("gate_condition_met", False):
        stage        = wildfire_status.get("stage", "N/A")
        next_update  = wildfire_status.get("next_update_minutes", 60)
        threat       = wildfire_status.get("threat_level", "GREEN")
        print(f"Gate condition NOT met. Stage {stage} | Threat: {threat}")
        print(f"No wildfire within activation threshold. Agent sleeping {next_update} min.")
        return

    fire_name = wildfire_status.get("nearest_fire", {}).get("name", "Unknown Fire")
    threat    = wildfire_status.get("threat_level", "UNKNOWN")
    distance  = wildfire_status.get("nearest_fire", {}).get("distance_km", "?")
    print(f"\n{'='*60}")
    print(f"CROP AGENT ACTIVATED")
    print(f"Threat: {threat}  |  Fire: {fire_name}  |  Distance: {distance} km")
    print(f"{'='*60}")

    # ── Step 0: Fetch live USDA prices ────────────────────────────────────────
    crop_categories = list({f["crop_category"] for f in farm_fields.get("fields", [])})
    print(f"\nSTEP 0: Fetching USDA prices for: {', '.join(crop_categories)}")
    prices_data = fetch_live_prices(crop_categories)
    fetched = len(prices_data["live_prices"])
    missed  = len(crop_categories) - fetched
    print(f"Prices ready: {fetched}/{len(crop_categories)} crops", end="")
    print(f"  ({missed} missing)" if missed else "")

    # ── Step 0b: Fetch soil moisture + ET per field ───────────────────────────
    fields = farm_fields.get("fields", [])
    print(f"\nSTEP 0b: Fetching soil moisture + ET for {len(fields)} fields")
    field_water_data = fetch_field_water_data(fields)

    # ── Step 0c: Fetch LANDFIRE flammability per field ────────────────────────
    print(f"\nSTEP 0c: Fetching LANDFIRE fuel model for {len(fields)} fields")
    field_flammability = fetch_field_flammability(fields)

    # ── Load static datasets ──────────────────────────────────────────────────
    crop_properties = load_json(CROP_PROPERTIES_FILE)
    farm_resources  = load_json(FARM_RESOURCES_FILE)

    # ── Build LLM input message ───────────────────────────────────────────────
    _d = json.dumps
    user_message = (
        f"WILDFIRE STATUS:\n{_d(wildfire_status)}\n\n"
        f"FARM FIELDS:\n{_d(farm_fields)}\n\n"
        f"USDA PRICES:\n{_d(prices_data)}\n\n"
        f"CROP PROPERTIES:\n{_d(crop_properties)}\n\n"
        f"FARM RESOURCES:\n{_d(farm_resources)}\n\n"
        f"FIELD WATER:\n{_d(field_water_data)}\n\n"
        f"FIELD FLAMMABILITY:\n{_d(field_flammability)}\n\n"
        "Run Task 4 first, then Tasks 1, 2, 3 in that order."
    )

    # ── Call Groq ─────────────────────────────────────────────────────────────
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        print("ERROR: GROQ_API_KEY not set in environment.")
        sys.exit(1)

    client = Groq(api_key=api_key, timeout=60.0)
    print(f"\nSending to Groq ({GROQ_MODEL})...")

    response = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_message},
        ],
        temperature=0.1,
        max_tokens=2048,
    )

    raw = response.choices[0].message.content
    tokens = response.usage

    # ── Parse + save ──────────────────────────────────────────────────────────
    parsed = parse_llm_json(raw)

    # Strip any field_ids the LLM hallucinated that don't exist in farm_fields
    valid_ids = {f["field_id"] for f in farm_fields.get("fields", [])}
    if parsed and valid_ids:
        for key in ("field_decisions", "fire_reduction", "hydration_strategy"):
            if isinstance(parsed.get(key), list):
                parsed[key] = [r for r in parsed[key] if r.get("field_id") in valid_ids]
        econ = parsed.get("economic_impact", {})
        if isinstance(econ.get("crop_destructions"), list):
            econ["crop_destructions"] = [
                r for r in econ["crop_destructions"] if r.get("field_id") in valid_ids
            ]

    print(f"\n{'='*60}")
    print("CROP AGENT OUTPUT")
    print(f"{'='*60}\n")

    if parsed:
        print(json.dumps(parsed, indent=2))
    else:
        print("WARNING: LLM did not return valid JSON. Raw output:")
        print(raw)

    print(f"\n{'='*60}")
    print(f"Tokens — prompt: {tokens.prompt_tokens} | completion: {tokens.completion_tokens}")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if parsed:
        save_outputs(parsed, raw, ts, farm_fields, wildfire_status)
    else:
        fallback = f"output_{ts}_raw.json"
        with open(fallback, "w") as f:
            json.dump({"raw_llm_response": raw}, f, indent=2)
        print(f"Raw output saved   -> {fallback}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    # Allow overriding file paths via CLI args
    forecast_file   = sys.argv[1] if len(sys.argv) > 1 else FORECAST_STATUS_FILE
    farm_file       = sys.argv[2] if len(sys.argv) > 2 else FARM_FIELDS_FILE

    wildfire_status = load_json(forecast_file)
    farm_fields     = load_json(farm_file)
    run_crop_agent(wildfire_status, farm_fields)


if __name__ == "__main__":
    main()
