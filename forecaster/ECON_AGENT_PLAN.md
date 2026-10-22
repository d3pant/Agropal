# Econ Agent — Component Plan

Part of the Economic Reporting & Policy Coordinator (ERPC). Activated during Stage 2
(fire threat active). Computes total financial exposure by category, then ranks all
available response actions by ROI so the farmer has a single prioritized action list.

---

## Status

- [x] Financial loss estimation (crop, livestock, opportunity cost)
- [x] ROI action ranking engine
- [x] Farmer-provided current cost inputs (logged in output JSON)
- [x] Output schema finalized
- [x] Live crop data from USDA NASS and current crop-agent output (no fallback)
- [x] Live livestock data from current farmer inventory and `Livestock/erpc_message.json` (no fallback)
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
- `task3[].technique` → cost lookup key (e.g., WET FIREBREAK → farmer-entered $/acre)

### From Livestock Agent — `Livestock/erpc_message.json`

```json
{
  "animal_valuation_at_risk": 442000,
  "cost_optimization": {
    "total_animals_at_risk": 497,
    "animals_can_evacuate": 497,
    "value_can_save_usd": 442000,
    "potential_loss_usd": 0
  }
}
```

Mapping: `total_animals_at_risk` → `total_head`, `animal_valuation_at_risk / total_head` → `value_per_head_usd`. Transport cost comes from the farmer's current setup quote.

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

Source: current Livestock Agent output, valued using USDA NASS data.
- `at_risk_value_usd = total_head × value_per_head_usd`

#### Opportunity Cost

What we can compute from current data:
- **Lost harvest season value**: for ABANDON fields, one full growing season of that
  crop's `price_per_acre_usd × size_acres` is added as opportunity cost (replanting
  delay assumed = 1 season). Source: `task2.price_per_acre_usd`.
- **Partial season loss for PARTIAL HARVEST**: `(1 - maturity_pct) × price_per_acre_usd × size_acres`
  — the portion of the season's value that cannot be recovered even with partial harvest.

What the agent reports as unavailable until a live source is added:
- Soil rehabilitation cost (no soil damage data from any agent)
- Market timing losses (no commodity price forecasting)
- Labor disruption costs (no labor model)

```
opportunity_cost = sum(price_per_acre_usd × size_acres × farmer_season_horizon
                        for ABANDON fields)
                 + sum((1 - maturity_pct) × price_per_acre_usd × size_acres
                       × farmer_season_horizon for PARTIAL HARVEST fields)
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

All operational cost inputs are entered by the farmer during setup and recorded in
`farmer_cost_inputs_used` in the econ report.

| Action | Derived From | Loss Avoided | Cost Formula | Time Required |
|--------|-------------|-------------|-------------|---------------|
| HARVEST NOW | `task4.decision = HARVEST NOW` | `confidence_adjusted_loss_usd` (full field) | `HARVEST_LABOR_RATE_USD_PER_HOUR × harvest_hours_estimate` | Must complete before `fire_arrival_hours` |
| PARTIAL HARVEST | `task4.decision = PARTIAL HARVEST` | `confidence_adjusted_loss_usd × maturity_pct` | `HARVEST_LABOR_RATE_USD_PER_HOUR × harvest_hours_estimate × maturity_pct` | Must complete before `fire_arrival_hours` |
| TRANSPLANT | `task4.decision = TRANSPLANT` and `task1.feasible_with_farm_resources = True` | Farmer-entered seedling value | Farmer-entered labor rate × `labor_hours_needed` | Must complete before `time_window` |
| WET FIREBREAK | `task3.technique = WET FIREBREAK` | `confidence_adjusted_loss_usd` of protected field | Farmer-entered firebreak cost × field acres | Urgency: IMMEDIATE = now, SCHEDULED = within 2h |
| EVACUATE LIVESTOCK | Current livestock-agent valuation | `at_risk_value_usd` | Farmer-entered transport quote × head count | Based on zone time_to_impact |

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

## Data and Cost Inputs

The agent requires a completed farmer setup, fresh crop and livestock agent outputs, USDA NASS yield and price records, and farmer-entered current operating cost inputs. It stops with an error when any required live data is missing; it does not synthesize crop reports or livestock counts, use bundled demo inventories, or substitute historical hardcoded prices. The setup form collects labor, harvest time, firebreak, livestock transport, transplant, and lost-season inputs, which are recorded in `farmer_cost_inputs_used` in the econ report.

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
- **Input from Livestock Agent:** `Livestock/erpc_message.json` — `total_animals_at_risk`, `animal_valuation_at_risk`
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
    "farmer_cost_inputs_used": { ... },                  # entered during farm setup
    "action_queue": [ EconAction, ... ],                # sorted by priority
    "infeasible_actions": [ EconAction, ... ],          # feasible=False, shown separately
    "data_sources": {
        "crop_agent": "live" | "unavailable",
        "livestock_agent": "live" | "unavailable",
    }
}
```
