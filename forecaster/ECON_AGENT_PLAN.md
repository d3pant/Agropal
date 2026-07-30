# Econ Agent — Component Plan

Part of the Economic Reporting & Policy Coordinator (ERPC). Activated during Stage 2
(fire threat active). Computes total financial exposure by category, then ranks all
available response actions by ROI so the farmer has a single prioritized action list.

---

## Status

- [x] Financial loss estimation (crop, livestock, opportunity cost)
- [x] ROI action ranking engine
- [x] Hardcoded cost assumptions catalog (logged in output JSON)
- [x] Output schema finalized
- [x] Live crop data from `crop_agent/crop_agent_output_*.json` (with mock fallback)
- [x] Live livestock data from `Livestock/erpc_message.json` (with hardcoded fallback)
- [ ] Integration with ERPC pipeline trigger

---

## File Location

`erpc/econ_agent.py`

Output written to: `forecaster/output/econ_report.json`

---

## Trigger

Stage 2 (gate condition met — fire threat active). Called by ERPC each monitoring
cycle with updated data from Crop and Livestock agents. Re-runs every cycle so the
action queue stays current as fire_arrival_hours tick down.

---

## Inputs

### From Crop Agent (structured JSON — schema matches example below)

```
crop_agent_output = {
  "task2": { crop_destructions[], total_estimated_loss_usd, total_confidence_adjusted_loss_usd },
  "task4": [ { field_id, crop_category, maturity_pct, fire_arrival_hours, decision, reason } ],
  "task1": [ { field_id, uprooting_strategy: { transplantable, labor_hours_needed, time_window, feasible_with_farm_resources } } ],
  "task3": [ { field_id, intensity_score, hours_to_arrival, technique, urgency } ]
}
```

Key fields used per feature:
- `task2.crop_destructions[].confidence_adjusted_loss_usd` → financial loss per field
- `task2.crop_destructions[].estimated_loss_usd` → upper bound shown alongside
- `task4[].decision` → determines which actions are available (HARVEST NOW, PARTIAL HARVEST, TRANSPLANT, ABANDON)
- `task4[].maturity_pct` → scales salvageable value for PARTIAL HARVEST
- `task4[].fire_arrival_hours` → time window for each action
- `task1[].uprooting_strategy.labor_hours_needed` → cost input for transplant ROI
- `task1[].feasible_with_farm_resources` → hard gate: infeasible actions are flagged, not ranked
- `task3[].urgency` → maps to action priority (IMMEDIATE > SCHEDULED)
- `task3[].technique` → cost lookup key (e.g., WET FIREBREAK → hardcoded $/acre)

### From Livestock Agent — `Livestock/erpc_message.json`

```json
{
  "animal_valuation_at_risk": 442000,
  "transport_costs_usd": 7455,
  "cost_optimization": {
    "total_animals_at_risk": 497,
    "animals_can_evacuate": 497,
    "value_can_save_usd": 442000,
    "potential_loss_usd": 0
  }
}
```

Mapping: `total_animals_at_risk` → `total_head`, `animal_valuation_at_risk / total_head` → `value_per_head_usd`, `transport_costs_usd` → actual EVACUATE_LIVESTOCK action cost.

Falls back to `HARDCODED_LIVESTOCK` (750 head × $1,500/head) if file not found.

### From Forecasting Agent

- `status.json` → `threat_level`, `nearest_fire.fire_arrival_hours` (zone-level)

---

## Features

### 1. Financial Loss Estimation

Computes total exposure across all categories. Displayed to farmer as a dashboard.

#### Crop Loss

Source: `task2.crop_destructions`
- Per field: `confidence_adjusted_loss_usd` is the primary figure displayed
- `estimated_loss_usd` shown as upper bound
- Grouped by `crop_category` for the category breakdown
- Fields with `decision = HARVEST NOW` or `PARTIAL HARVEST`: loss is reducible
  — shown as "at risk if no action" vs "recoverable with action"
- Fields with `decision = ABANDON`: loss is locked in, shown as confirmed loss

```
crop_loss_total = sum(confidence_adjusted_loss_usd for all fields)
crop_loss_recoverable = sum(confidence_adjusted_loss_usd
                            for fields where decision in [HARVEST NOW, PARTIAL HARVEST]
                            scaled by maturity_pct)
crop_loss_confirmed = crop_loss_total - crop_loss_recoverable
```

#### Livestock Loss

Source: hardcoded `HARDCODED_LIVESTOCK` until Livestock Agent exists.
- `at_risk_value_usd = total_head × value_per_head_usd × (1 - evacuated_pct)`
- Updates each cycle as `evacuated_pct` increases (future: from Livestock Agent)

#### Opportunity Cost

What we can compute from current data:
- **Lost harvest season value**: for ABANDON fields, one full growing season of that
  crop's `price_per_acre_usd × size_acres` is added as opportunity cost (replanting
  delay assumed = 1 season). Source: `task2.price_per_acre_usd`.
- **Partial season loss for PARTIAL HARVEST**: `(1 - maturity_pct) × price_per_acre_usd × size_acres`
  — the portion of the season's value that cannot be recovered even with partial harvest.

What we cannot compute yet (hardcoded as None):
- Soil rehabilitation cost (no soil damage data from any agent)
- Market timing losses (no commodity price forecasting)
- Labor disruption costs (no labor model)

```
opportunity_cost = sum(price_per_acre_usd × size_acres for ABANDON fields)   # 1 lost season
                 + sum((1 - maturity_pct) × price_per_acre_usd × size_acres
                       for PARTIAL HARVEST fields)
```

#### Total Exposure Summary

```
total_financial_exposure = crop_loss_total + at_risk_livestock_value + opportunity_cost

breakdown = {
    "crop_loss_confirmed":    <locked-in losses from ABANDON fields>,
    "crop_loss_recoverable":  <losses preventable with action>,
    "livestock_at_risk":      <value of unvacuated animals>,
    "opportunity_cost":       <lost future seasons>,
    "total":                  <sum of all>,
}
```

---

### 2. ROI Action Ranking

Every available action is evaluated as:

```
ROI = confidence_adjusted_loss_avoided / estimated_action_cost
```

`confidence_adjusted_loss_usd` is always used as the loss_avoided numerator — never
the raw `estimated_loss_usd`. This gives a conservative, realistic floor for the
farmer rather than a best-case projection.

#### Action Types and Cost Inputs

All cost inputs below are hardcoded. See "Hardcoded Values" table for the specific
numbers and which source should replace them.

| Action | Derived From | Loss Avoided | Cost Formula | Time Required |
|--------|-------------|-------------|-------------|---------------|
| HARVEST NOW | `task4.decision = HARVEST NOW` | `confidence_adjusted_loss_usd` (full field) | `HARVEST_LABOR_RATE_USD_PER_HOUR × harvest_hours_estimate` | Must complete before `fire_arrival_hours` |
| PARTIAL HARVEST | `task4.decision = PARTIAL HARVEST` | `confidence_adjusted_loss_usd × maturity_pct` | `HARVEST_LABOR_RATE_USD_PER_HOUR × harvest_hours_estimate × maturity_pct` | Must complete before `fire_arrival_hours` |
| TRANSPLANT | `task4.decision = TRANSPLANT` and `task1.feasible_with_farm_resources = True` | Seedling/plant replacement value (hardcoded per crop) | `LABOR_RATE_USD_PER_HOUR × labor_hours_needed` | Must complete before `time_window` |
| WET FIREBREAK | `task3.technique = WET FIREBREAK` | `confidence_adjusted_loss_usd` of protected field | `FIREBREAK_COST_USD_PER_ACRE × field_size_acres` | Urgency: IMMEDIATE = now, SCHEDULED = within 2h |
| EVACUATE LIVESTOCK | Hardcoded livestock stub | `at_risk_value_usd` (partial, per pen) | `LIVESTOCK_TRANSPORT_COST_USD_PER_HEAD × head_count` | Based on zone time_to_impact |

#### Feasibility Gates (hard — action removed from queue if failed)

- `task1.feasible_with_farm_resources = False` → TRANSPLANT action removed, flagged as "infeasible — equipment unavailable"
- `fire_arrival_hours < estimated_action_hours` → action removed, flagged as "insufficient time window"
- `task4.decision = ABANDON` → no harvest/transplant action generated for that field

#### Output Per Action

```python
{
    "action_id": str,             # e.g. "HARVEST_F1"
    "action_type": str,           # HARVEST_NOW | PARTIAL_HARVEST | TRANSPLANT | FIREBREAK | EVACUATE_LIVESTOCK
    "field_id": str | None,
    "crop_category": str | None,
    "priority": int,              # 1 = highest — computed from ROI rank + urgency override
    "roi": float,                 # loss_avoided / action_cost — rounded to 1 decimal
    "confidence_adjusted_loss_avoided_usd": float,
    "estimated_action_cost_usd": float,
    "time_window_hours": float,   # hours until action is no longer viable
    "urgency": str,               # IMMEDIATE | HIGH | SCHEDULED
    "feasible": bool,
    "infeasibility_reason": str | None,
    "action_description": str,    # human-readable: "Harvest F1 wheat (100% mature) before 12h window closes"
    "required_resources": list[str],  # e.g. ["harvest crew", "truck"]
}
```

#### Priority Assignment

1. Compute ROI for all feasible actions
2. Sort descending by ROI
3. Override: any action with `urgency = IMMEDIATE` from `task3` jumps to top regardless of ROI
4. Infeasible actions appended at bottom with `feasible = False`

---

## Hardcoded Values (Replace When Dynamic Data Available)

All cost constants are hardcoded in `econ_agent.py` as a `COST_ASSUMPTIONS` dict.

| Constant | Hardcoded Value | Unit | Make Dynamic From |
|----------|----------------|------|-------------------|
| `HARVEST_LABOR_RATE_USD_PER_HOUR` | 25 | $/hr | Farmer labor cost input or regional USDA farm wage data |
| `HARVEST_HOURS_PER_ACRE` | 4 | hrs/acre | Crop-specific — hardcoded average; make per-crop from crop agent |
| `FIREBREAK_COST_USD_PER_ACRE` | 150 | $/acre | CAL FIRE cost estimates or farmer input |
| `LIVESTOCK_TRANSPORT_COST_USD_PER_HEAD` | 35 | $/head | **Now live** from `Livestock/erpc_message.json` `transport_costs_usd` |
| `LIVESTOCK_VALUE_PER_HEAD_USD` | 1500 | $/head | **Now live** from `animal_valuation_at_risk / total_animals_at_risk` |
| `LIVESTOCK_TOTAL_HEAD` | 750 | head | **Now live** from `Livestock/erpc_message.json` `total_animals_at_risk` |
| `LIVESTOCK_EVACUATED_PCT` | 0.0 | fraction | Still hardcoded — needs real-time evacuation status from Livestock Agent |
| `TRANSPLANT_SEEDLING_VALUE_USD_PER_ACRE` | 800 | $/acre | Crop agent or nursery price index |
| `OPPORTUNITY_COST_SEASONS` | 1 | seasons | Agronomist input — assumed 1 lost season for ABANDON fields |

---

## Opportunity Cost — What's Computable Now vs Later

| Component | Computable Now | Data Needed | Source When Available |
|-----------|---------------|-------------|----------------------|
| Lost harvest season (ABANDON fields) | Yes — 1 season × price_per_acre × acres | `task2.price_per_acre_usd`, `task2.size_acres` | Already in crop agent output |
| Partial season loss (PARTIAL HARVEST) | Yes — `(1 - maturity_pct)` fraction of season value | Same as above | Already in crop agent output |
| Soil rehabilitation cost | No | Soil damage assessment | Future: NRCS soil health data or post-event survey |
| Replanting cost | No | Species-specific replanting rates | Future: crop agent extension or USDA crop budgets |
| Market timing loss | No | Commodity price forecast | Future: USDA NASS commodity price feed |
| Labor disruption | No | Labor model, crew size | Future: farm operations profile |

---

## Dependencies & Integration Points

- **Input from Crop Agent:** `crop_agent/crop_agent_output_*.json` (latest) — keys remapped: `field_decisions`→task4, `fire_reduction`→task1, `economic_impact`→task2, `hydration_strategy`→task3
- **Input from Livestock Agent:** `Livestock/erpc_message.json` — `total_animals_at_risk`, `animal_valuation_at_risk`, `transport_costs_usd`
- **Input from Forecasting Agent:** `forecaster/output/status.json` → `threat_level`
- **Output to ERPC:** `econ_report.json` — financial exposure breakdown + ranked action list
- **Output written to:** `forecaster/output/econ_report.json`

---

## Output Schema (top-level)

```python
{
    "generated_at": str,
    "farm_id": str,
    "threat_level": str,
    "financial_exposure": {
        "crop_loss_confirmed_usd": float,
        "crop_loss_recoverable_usd": float,
        "crop_loss_total_usd": float,
        "livestock_at_risk_usd": float,
        "opportunity_cost_usd": float,
        "total_exposure_usd": float,
        "breakdown_by_crop": { crop_category: confidence_adjusted_loss_usd },
    },
    "cost_assumptions_used": { ...COST_ASSUMPTIONS },   # logged for transparency
    "action_queue": [ EconAction, ... ],                # sorted by priority
    "infeasible_actions": [ EconAction, ... ],          # feasible=False, shown separately
    "data_sources": {
        "crop_agent": "live" | "unavailable",
        "livestock_agent": "hardcoded_stub",            # updated when agent exists
    }
}
```
