# Livestock Multi-Trip Scheduler

## Intent

Produce an actionable evacuation timetable for each available livestock vehicle, including repeated farm-to-shelter trips, while preserving the existing route safety checks. Use crop-agent recommendations to identify time-sensitive work that could fit around evacuation operations and give the operator a clear assessment of whether another trip is worthwhile.

## Current behavior

`Livestock/livestock_agent.py::compute_evacuation_optimization` estimates a shared number of trips from the average outbound route duration and the earliest fire ETA. It allocates aggregate capacity by a value-and-urgency score. `apply_transport_plan` converts that allocation into planned animal counts. It does not assign vehicles, schedule departures or returns, calculate individual trip cycles, or represent crop work. Each pen has already passed destination, route, smoke, and fire-hazard assessment before transport capacity is allocated.

## Goals

- Generate a deterministic schedule by vehicle and trip, with outbound and return legs, load/unload time, destination capacity reservation, and remaining fire-deadline margin.
- Use a deadline-aware greedy earliest-finish policy: reject unsafe or late trips first; among feasible loads, prioritize the least remaining safety slack, then assign the eligible trip with the earliest completion time to the next available vehicle.
- Split a pen into capacity-sized batches only when the source pen is marked splittable; default to unsplittable because current farm data does not describe safe subgroup handling.
- Never count an animal as moved. Schedules are recommendations and planned counts only.
- Normalize crop-agent output into field tasks with time window, labor/equipment needs, estimated work duration, and value/recovery context. Report which facts are available to assess crop-versus-transport tradeoffs.
- Provide a transparent, deterministic marginal-trip assessment using schedule time, animals protected, and crop-task opportunity. If local Ollama is configured, it may add a concise explanation; it cannot change the schedule or any safety constraint. Without Ollama, the deterministic explanation remains complete.
- Keep dependencies free/open source and add no paid service. No model or external LLM is required for scheduling.

## Non-goals

- Dispatching vehicles, confirming shelter admission, or tracking actual animal movement.
- Treating crop values and animal market prices as comparable certainty-equivalent safety measures.
- Automatically choosing to leave animals at risk because crop work appears valuable.
- Training or hosting a new model, or enabling the hosted Groq API for evacuation scheduling.

## Scheduling policy

Each candidate load is derived from a pen's route options that remain feasible under the existing smoke, fire perimeter, destination confirmation, and capacity checks. The vehicle cycle includes farm-centroid-to-pen positioning, loading, pen-to-shelter travel, unloading, and shelter-to-farm return. If farm-to-pen positioning cannot be verified, hold the load for review; if return safety cannot be verified but the outbound leg is safe, allow at most a terminal one-way trip and mark the vehicle out of service. The governing deadline is the per-pen `time_to_impact_hours` already read from that pen's affected zone, measured from the plan timestamp. If that ETA is missing, the load is review-required and excluded from scheduling. A load records pen ID, animal count, route/site, every leg duration, loading and unloading allowance, source fire-arrival deadline, and the forecast run ID. Vehicle availability begins at the plan timestamp. Vehicles use the configured capacity and count; when named vehicle data is absent, stable IDs `vehicle_1...vehicle_N` are generated.

For each scheduling iteration, calculate a feasible next trip for every vehicle/load pair. A trip is infeasible if the destination is unverified, accepted head capacity is unknown or would be exceeded, outbound assessment is unknown, or the load cannot reach safety before its source deadline multiplied by the configured fire-ETA margin. Reserve site acceptance across all scheduled batches against the confirmed accepted-head total. For each load, first select the vehicle and route option with the earliest safe completion; then select the load with the smallest remaining deadline slack, breaking ties by earliest completion, existing pen priority, and stable pen ID. Advance that vehicle's availability through unloading and return; the next trip includes a new farm-to-pen positioning leg. A terminal one-way delivery is allowed only when its outbound leg is assessed safe; mark the vehicle out of service and never schedule it again. Recompute candidates after every assignment. Never use a minimum-one-trip fallback when no trip fits the actual available time.

Return route geometry and timing must be assessed separately. An outbound route being safe does not establish that a return is safe. Repeated trips use explicit return legs and are only scheduled when those legs pass available route and fire/smoke checks. If return safety cannot be established, the scheduler may offer one terminal outbound trip for human coordination, marks that vehicle unavailable afterward, and never assumes a return or schedules that vehicle again.

The scheduler's output includes trips, vehicle timelines, trip-level status/reasons, pen-level planned counts, unscheduled animal counts, crop-task context, and totals. Unknown time-to-impact or incomplete safety evidence is not treated as infinite time; the candidate requires review and is excluded from automatic scheduling.

## Crop coordination and trip value

Add farm and forecast run metadata to newly saved crop outputs, then read the latest output only when those identifiers match the current farm and forecast run. Normalize known output shapes (`task4` / `field_decisions`, `task1` / `fire_reduction`, and `task3` / `hydration_strategy`). Ignore hallucinated or unknown field IDs. A crop task is schedulable only when required fields and resources are present and feasible. If work durations are absent, report the task as unquantified rather than inventing duration. The first version reports crop tasks and resource feasibility alongside the vehicle schedule; it does not commandeer livestock drivers or trailers for crop transport or claim a task fits a free time window.

Compute deterministic marginal benefit for each additional safe trip as the additional animals protected and vehicle-hours consumed. Include estimated animal value only when the source data supplies it, and do not present it as a measured loss avoided. Crop tasks are reported alongside the schedule with their source time window, labor/equipment feasibility, and any explicit labor duration. Current data does not define shared crew rosters or task durations consistently, so the first version must not claim a crop task fits in a gap or subtract crop work from livestock trip availability. An optional local Ollama explanation receives only the computed schedule and normalized crop facts; output is display-only and validated/escaped. It cannot add trips, alter allocations, or override safety. No Ollama configuration means no model call and no loss of scheduling functionality.

## Configuration and limitations

Use existing vehicle count/capacity fields. Add validated configuration for loading/unloading minutes and pen split permission. Use the existing 0.8 fire-arrival margin unless explicitly configured; use 30 minutes each for loading and unloading as labeled planning assumptions, configurable through environment variables and surfaced in output. Default pen splitting off; an over-capacity pen remains unscheduled unless an operator enables splitting in its farm-profile data. If a site has no return route, report the lack of round-trip feasibility. Public routing and forecast services can be unavailable or rate-limited; incomplete data produces review-required output.

## Scope

- `Livestock/livestock_agent.py`: route cycle timing, scheduling algorithm, crop output normalization and schedule integration.
- `backend/main.py`: run crop before livestock in the end-to-end pipeline so crop output can be matched to the same forecast run; accept explicit pen-split permission in setup input.
- `crop_agent/crop_agent.py`: stamp each saved full crop result with source farm and forecast run metadata.
- `tests/test_livestock_routing.py` and focused crop/scheduler tests: deterministic unit coverage with synthetic routes and crop outputs.
- `Livestock/README.md`: explain trip schedule, assumptions, crop-task treatment, optional local Ollama rationale, and advisory-only status.
- Existing JSON snapshots are runtime data and must remain untouched.

## Acceptance criteria

1. Multiple vehicles receive independent timelines, and later trips start only after that vehicle completes its preceding cycle.
2. Schedule order respects least deadline slack before earliest-finish tie-breaking and remains deterministic for ties.
3. A trip is never scheduled when its outbound arrival misses the deadline buffer, route safety is unknown, destination acceptance is unverified, or destination capacity is exceeded.
4. Positioning, return, and reload timing are included in vehicle availability; missing/unsafe return data never creates a repeat trip, and a terminal one-way delivery is marked explicitly.
5. Pen counts are not over-allocated; splitting is off by default and when enabled batches respect vehicle capacity.
6. No trip is fabricated when available time is zero or less than a feasible trip cycle.
7. Crop outputs are matched to the farm and current forecast timestamp/run when those identifiers exist, normalized from supported schemas, filtered to known fields, and missing durations/resources are surfaced as unknown.
8. Crop opportunity and optional local-model explanation cannot alter the deterministic schedule or safety result.
9. Existing route/smoke tests and the full repository test suite pass without paid credentials or network access.

## Pipeline dependency

The end-to-end backend pipeline must run the crop agent to completion before the livestock agent starts, because livestock accepts crop context only when its `_meta.run_id` matches the forecaster run. If crop execution fails or its output is stale, livestock planning proceeds without crop context and records none; it does not wait for or invoke a paid LLM service. Standalone livestock runs likewise omit crop context when no matching crop result exists.
