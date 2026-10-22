# Livestock Multi-Trip Scheduler Implementation Plan

> **Implementation status:** Complete. All checks below ran successfully; see `.superpowers/sdd/2026-10-22-livestock-multi-trip-scheduler/progress.md` for the execution record.

**Goal:** Replace aggregate transport capacity estimates with a deadline-aware per-vehicle multi-trip schedule that includes return legs and crop-task context.

**Architecture:** Add a pure deterministic scheduler module, then enrich OSRM route options with return legs and assess scheduled trips using existing smoke/fire checks. Stamp crop outputs with farm/run metadata, normalize crop tasks for context, and optionally request display-only rationale from a locally configured Ollama endpoint. Integrate the schedule into existing livestock status and ERPC outputs.

**Tech Stack:** Python 3, stdlib datetime/math, existing `httpx`, unittest.

**Spec:** `docs/superpowers/specs/2026-10-22-livestock-multi-trip-scheduler-design.md`

## Global Constraints

- No paid service or new runtime dependency; Ollama is optional and local only.
- Safety, destination acceptance, time-to-impact, and route assessment are deterministic constraints; LLM output cannot alter decisions.
- Missing safety or crop task data stays unknown and is surfaced for review.
- All plan output remains advisory; `animals_moved_count` stays zero until independently confirmed.
- Do not modify runtime JSON snapshots.

## Review Focus

- Wrong-trip ordering from deadline slack, earliest finish, or nondeterministic ties.
- Repeated trips scheduled despite missing/unsafe return routes or destination capacity.
- Smoke/fire assessments evaluated against the wrong departure time.
- Crop output mixed across farms or forecast runs, or LLM output affecting schedule decisions.
- Existing route safety and packet validation regressions.

## File Map

- `Livestock/transport_scheduler.py` — pure load normalization and vehicle timeline scheduler.
- `Livestock/livestock_agent.py` — query return routes, assess per-trip route timing, integrate transport/crop plan and optional local rationale.
- `backend/main.py` — serialize crop then livestock execution for one forecast run; accept per-pen split permission from setup.
- `crop_agent/crop_agent.py` — save `_meta` with source `farm_id` and `run_id`.
- `tests/test_transport_scheduler.py` — scheduler ordering, capacity, deadlines, return-cycle and crop normalization tests.
- `tests/test_crop_metadata.py` — crop output source metadata tests.
- `tests/test_livestock_routing.py` — return-route parsing/assessment integration regressions.
- `tests/test_setup_pen_options.py` — pen split permission defaults and explicit opt-in.
- `tests/test_backend_pipeline_order.py` — crop completion precedes livestock run consumption.
- `tests/test_local_schedule_rationale.py` — optional local-model disabled, success, and failure behavior.
- `Livestock/README.md` — explain schedule output, assumptions, crop-task treatment, and optional local model.

## Tasks

### Task 1: Define deterministic scheduler behavior with failing tests

- [x] Add scheduler tests for fixed-time synthetic pens, vehicles, crop identity and allocation.
- [x] Verify independent vehicle cycles, deadline slack ordering, ties, capacities, opt-in splitting, unknown evidence, and terminal one-way trips.
- [x] Run scheduler tests; all pass.

### Task 2: Implement pure scheduling and crop normalization

- [x] Implement deterministic scheduling, explicit per-vehicle timelines, accepted-capacity reservation, and same-run crop normalization.
- [x] Schedule repeated trips only when route, smoke/fire, deadline and return checks pass; terminal one-way trips retire that vehicle.
- [x] Return per-pen allocations, unscheduled reasons, trip records and summary metrics.

### Task 3: Add route cycle data and departure-time assessments

- [x] Query and retain farm-to-pen, outbound, and return route alternatives with geometry and timing.
- [x] Include all route legs in smoke bounds and extend coverage for conservative multi-trip horizons.
- [x] Reassess each candidate leg at its scheduled time; return timing begins after unloading.
- [x] Require verified destination acceptance and consistent capacity confirmations.
- [x] Add route/smoke tests and run them successfully.

### Task 4: Stamp crop outputs and integrate optional local rationale

- [x] Persist crop source farm/run metadata without changing crop task keys.
- [x] Match crop context to the active farm/run and preserve unknown durations/resources.
- [x] Add optional local Ollama explanation with timeout and no ability to alter schedule decisions.
- [x] Test metadata and optional local rationale without network access.

### Task 5: Integrate output, document, and verify

- [x] Integrate per-vehicle scheduling into livestock status and ERPC output.
- [x] Add opt-in pen splitting, disabled by default, and run crop processing before livestock in the shared pipeline.
- [x] Keep moved counts at zero; planned counts derive from scheduled trips only.
- [x] Document scheduler assumptions and add dashboard views for trips and crop context.
- [x] Run all 47 tests, compileall, and `git diff --check` successfully.
- [x] Confirm the four pre-existing untracked runtime JSON snapshots were not modified.
