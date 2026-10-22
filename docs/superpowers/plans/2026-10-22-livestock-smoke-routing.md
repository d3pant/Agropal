# Livestock Smoke Routing Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task after user approval. Steps use checkbox syntax for tracking.

**Goal:** Assess smoke along the actual livestock transport route using coherent forecaster inputs, time-dependent public smoke forecasts, and explicit uncertainty.

**Architecture:** Publish one versioned forecaster-to-livestock snapshot for both backend and CLI execution. Keep road routing, smoke data retrieval, and exposure evaluation separate; livestock consumes their results and publishes the assessed route to downstream consumers.

**Tech stack:** Existing Python/FastAPI/httpx; BSD-licensed OSRM, ODbL OpenStreetMap data, BSD-licensed Shapely and pyproj for geometry; NOAA public smoke data. HRRR parsing dependencies must have verified free/open-source licenses before installation. No paid API, subscription, or LLM dependency for this work.

**Spec:** The design, observed evidence, and approval scope are included below. Proposed choices are recommendations, not previously approved requirements.

## Evidence inspected on 2026-10-22

These are saved snapshots, not a historical event archive or measurements of model accuracy. Do not overwrite them while planning.

- `forecaster/output/status.json` and `Livestock/status.json`: timestamp `2026-05-09T21:08:10Z`; farm `(33.1253, -116.6462)`; nearest fire `(32.51042, -116.76778)`, 69.31 km away, nominal detection, FRP 0.7 MW. Current perimeter, fire size and observed spread rate are null.
- Farm weather: wind FROM 259 degrees, 20.5 km/h. Spread-model weather: wind FROM 272 degrees, 20.5 km/h. These are different sampling locations in backend code, not necessarily contradictory measurements. Their downstream transport directions would be 79 and 92 degrees respectively.
- Status already includes GeoJSON fire-spread ellipses for 6/12/24 hours. These predict fire spread, NOT smoke extent. `time_to_farm` says `threatened=false`, `hours=null`, `hours_worst_case=179.6`. This does not mean smoke is absent.
- `Livestock/wake_up_packet.json` contains ONLY Z1/Z2 impact times 11.0/11.1 hours and smoke direction 259 degrees. No farm identifier, generation timestamp, smoke origin, concentration, plume geometry or temporal forecast.
- `Livestock/farm_profile.json` and current forecaster config instead place Misa Farma at `(33.5643, -117.6196)`, with 50 cattle and 20 sheep. Saved livestock output has one cattle pen near the old farm position. Inputs and outputs are from different farm configurations/runs.
- `Livestock/erpc_message.json` contains summary financial/evacuation fields and empty `forecasting_requests`/`crop_messages`. No smoke evidence or route geometry is communicated. There is no archived sequence of messages here.
- Backend `_run_forecaster_cycle()` writes status only. `_sync_forecaster_to_livestock()` copies any existing wake-up packet without matching its run; otherwise synthesizes fixed zone times. `(wind or 0 + 180)` fails to rotate nonzero wind bearings.
- CLI `Forecaster.write_wake_up_packet()` has a richer contract: activation time, farm ID, fire, weather, affected zones and messages. Its smoke assessment still consists of a wind direction plus a threat-derived visibility label, not measured smoke. Mock inputs include rectangular fire perimeters; configured farm zones are rectangles. Neither is the livestock smoke-route test.
- OSRM currently returns only the first route's duration/distance. Livestock uses farm-to-destination bearing within 45 degrees of smoke direction and adds 2 hours. The selected route can inherit flags/source from another candidate.
- `Livestock/visualize.py` fetches a fresh road route in the browser instead of drawing the exact route evaluated by the agent.
- CalOES evacuation-zone features are currently interpreted as destination points. A warning/normal zone is not evidence of a livestock shelter, and polygon coordinates do not have point structure.
- Backend subprocess handling can return a pre-existing livestock output on failure/timeout, further obscuring stale results.

## Design and constraints

1. Existing JSON fields remain; additive schema changes only. New readers accept legacy packets but mark unverified fields as such. Existing financial keys remain compatible.
2. A new normalized packet has `schema_version`, `run_id`, `farm_id`, `farm_location`, `generated_at`, `source_status_timestamp`, `valid_until`, `input_quality`, `fire_data`, `wind_at_farm`, `wind_at_fire`, `spread_prediction`, `affected_zones`, and `smoke_forecast` metadata. Preserve current packet fields for legacy consumers.
3. Derive expiry from generation time plus `next_update_minutes`; retain source observation/forecast timestamps separately. A recent fetch does not make an old observation current. Validate farm ID and coordinates against the current profile; use a proposed configurable 100 m location tolerance to accommodate rounding, not different farms.
4. Use fire-site wind for a fire-origin fallback plume; preserve farm wind separately. Meteorological FROM bearings convert to transport TO with `(bearing + 180) % 360`; null is unknown, zero is a valid bearing.
5. Remove synthetic 11-hour impact times. Nullable per-zone/per-pen impact times come from existing spread-model calculations at the relevant locations, labeled modeled. A farm-level ETA cannot silently become every pen's ETA. Unknown time is not zero or infinity; a non-intersection within modeled horizons is not an all-clear.
6. HRRR near-surface smoke is the primary proposed exposure source, across the entire route corridor, not just the nearest fire. No inference of concentration from FRP or fire ellipses. NOAA HMS polygons may later add observed plume context; they cannot alone establish ground-level exposure.
7. First implement route geometry and honest unknowns; no invented fallback plume dimensions. The optional wind-plume fallback stays out of scope until its assumptions can be calibrated. Thus the first release remains useful with unavailable HRRR but does not claim smoke clearance.
8. Request full GeoJSON road geometry, segment durations/distances, and alternatives from OSRM. Keep alternatives when returned; do not promise the routing engine can find a smoke-avoiding detour. Configure an OSRM base URL for local hosting; free demo hosting is not unlimited infrastructure.
9. Assess each route segment at departure plus cumulative travel time. Explicit loading delay defaults to zero only as a documented assumption. Forecast queries outside coverage or valid time are unknown, never extrapolated silently. Account for partial grid coverage in exposure totals.
10. Emit peak modeled smoke concentration, concentration-time integral in microgram-hours/m3, time/distance with forecast coverage, source resolution, model cycle, valid time, and per-segment evidence. These are comparative exposure indicators, not dose, visibility, or veterinary safety thresholds.
11. Remove the 2-hour smoke penalty. Rank routes within comparable forecast coverage by integrated exposure, then actual duration; keep missing-coverage candidates separate for manual review rather than awarding zero exposure. Report tradeoffs and candidate routes. Do not impose an invented concentration cutoff.
12. Known fire-spread intersections are a distinct modeled hazard. Keep smoke, fire threat and verified closures separate. Do not claim a road is open merely because OSRM returns it. Unknown destination acceptance is visible; do not treat evacuation warning zones as shelters.
    Receiving destinations can be configured in `farm_profile.json` with coordinates, `availability_confirmed`, `accepted_head`, and `verified_at`; require a confirmation at most six hours old and capacity at least the pen count. Existing hardcoded fallback points stay candidates only.
13. Retain legacy decision keys, but add `assessment_status` (`assessed`, `partial`, `unknown`, `invalid`) and reasons. A route search failure or unknown fire ETA must not produce an unsupported 'safe evacuation' or 'safe shelter' statement. Unknown cases require review; existing downstream financial logic must exclude them from confirmed feasible/saved totals.
14. No automatic movement or messages. Output is an advisory. Keep original input snapshots unchanged; new runtime artifacts carry run IDs. No cloud hosting purchase or country-scale routing dataset download.

## Decisions resolved during plan review

- Execution order is packet normalization → road candidate discovery → combined route corridor and arrival-window calculation → one shared smoke fetch → exposure assessment → feasibility filtering → ranking → output publication. The livestock run orchestrator owns the smoke fetch through `forecaster/data_sources/smoke.py`; the initial forecaster packet marks smoke as not yet fetched. Final output records the source packet run ID and forecast metadata without mutating the immutable input packet.
- Filter known modeled fire intersections, invalid roads, insufficient deadlines and unverified destination acceptance before ranking confirmed feasible routes. Preserve rejected candidates and reasons. Retain the existing 0.8 × modeled impact ETA as a configurable conservative planning margin, explicitly a legacy heuristic rather than validated fire safety. Test a low-exposure route missing its deadline while another route meets it.
- Only candidates with 100% spatial and temporal forecast coverage enter the exposure ranking. Partial/unknown candidates remain a separate review list; their observed-subset integrals include coverage metadata and never compare as full-trip exposure. Add a test with differently located missing intervals across alternatives.
- For invalid/missing evidence, use `decision="review_required"`, `movement_status="pending_review"`, and `assessment_status="unknown"` or `"invalid"`; partial evidence uses `assessment_status="partial"` with the same decision/status. These extend existing value sets while retaining every existing key. They contribute zero to confirmed-feasible, saved-value, planned-to-move and actual-moved totals; head count/value remain in at-risk/remaining totals. Add explicit planned counts and retain legacy moved counts as zero absent actual movement confirmation. Update report/econ/dashboard consumers wherever these fields are read. No automatic shelter recommendation follows from unknowns. Compatibility tests must cover every consumer of these new values.

## Review focus

- Mixed farm/run inputs: reject inconsistent snapshots before recommending routes.
- Zero/null wind, differing farm/fire winds: retain provenance and correct direction.
- Partial/stale forecast data: distinguish unknown segments from clear segments.
- OSRM failure, missing alternatives or annotations: preserve useful partial output without fabricating verified roads/timing.
- Unknown site acceptance or failed subprocess: never return old success or assert confirmed feasibility.

## Task 1: Normalize forecaster communication and provenance

**Files:** create `forecaster/livestock_packet.py`; modify `backend/main.py`, `forecaster/forecaster.py`; create `tests/test_livestock_packet.py` and saved fixtures under `tests/fixtures/livestock/`.

**Interfaces:** `build_livestock_packet(status: dict, farm_profile: dict, farm_config: dict, run_id: str) -> dict`; `validate_livestock_packet(packet: dict, farm_profile: dict, now_utc: datetime) -> list[dict]`.

- [ ] Write failing unittest cases for the inspected mismatch, copied old packet, valid wind 0, null wind, 259-to-79 conversion, distinct 272-degree fire wind, and missing ETA. Preserve coordinate-heavy fixture data only where required.
- [ ] Run `python3 -m unittest discover -s tests -p 'test_livestock_packet.py' -v`; confirm intended failures.
- [ ] Build the shared packet from the current run in both execution paths; pass the same captured farm profile/config through the run. Add consistent run/farm identity to status; stop reusing unmatched wake-up files. Compute model impact at pen/zone coordinates using existing spread functions with explicit assumptions, never fixed 11-hour values.
- [ ] Write snapshot files via temporary files and replacement; validate both files share run ID when reading. Reject mixed snapshots rather than relying on filesystem timing. Serialize overlapping pipeline runs or use per-run working directories.
- [ ] Re-run tests; add no-fire, no-spread and expired-input cases. Verify existing status fields are retained.

## Task 2: Retain and assess actual road candidates

**Files:** create `Livestock/routing.py`; modify `Livestock/livestock_agent.py`; create `tests/test_livestock_routing.py`.

**Interfaces:** `fetch_routes(client, origin: dict, destination: dict, base_url: str) -> list[dict]`; candidates contain `route_id`, site identity, GeoJSON geometry, duration hours, distance km, segment durations/distances, source and quality warnings.

- [ ] Add mocked OSRM fixtures covering two alternatives, curved road crossing a polygon, malformed geometry, mismatched annotation lengths, timeout and zero alternatives.
- [ ] Run the routing unittest file and confirm intended failures.
- [ ] Request `overview=full`, `geometries=geojson`, duration/distance annotations and alternatives; preserve each candidate and selected metadata together. Use HTTPS for remote demo access and configurable local OSRM URL.
- [ ] Move straight-line fallback into an explicitly unverified estimate; it cannot establish a traversable road. Missing annotations yield approximate timing with recorded assumptions or unknown timing, never silently exact timing.
- [ ] Re-run tests; verify geometry/annotation correspondence against OSRM's documented contract.

## Task 3: Add the free smoke forecast provider

**Files:** create `forecaster/data_sources/smoke.py`, `tests/test_smoke_provider.py`; modify `requirements.txt` only after license/source verification.

**Interface:** `fetch_smoke_forecast(bounds: dict, start_utc: datetime, end_utc: datetime) -> dict`, returning provider/model cycle, validity, grid coordinates/projection, resolution, units, concentration arrays or local cache references, provenance and warnings. Packet contains metadata/reference, not huge embedded grids. References must be local within the run workspace.

- [ ] Verify an official NOAA public HRRR near-surface smoke distribution, exact variable/units, spatial projection, access terms and an available sample. Record URL/variable in adapter docs. Check licenses of parser and transitive native libraries. Do this before committing to parser dependencies; no paid gateway.
- [ ] Capture a small real-format fixture plus metadata; use synthetic grid fixtures for deterministic edge cases, explicitly labeled synthetic.
- [ ] Add failing tests for unit normalization, missing variable, negative/fill values, grid orientation, expired model cycle, coverage outside requested corridor, and source outage.
- [ ] Implement bounded downloads, timeout, cached regional data and model-cycle invalidation. Reuse one fetch per run across pens/routes. Download only required records/region where source format permits; set a configurable byte cap, default 100 MB per run, and return unavailable if exceeded.
- [ ] Run provider tests offline. Perform one bounded live read as integration verification; document the source/model resolution and report actual access or deployment limitations. No real-time accuracy claim from successful ingestion.

## Task 4: Compute exposure and integrate route decisions

**Files:** create `Livestock/smoke_exposure.py`; modify `Livestock/livestock_agent.py`, `forecaster/agents/econ_agent.py` where feasibility is consumed; create `tests/test_smoke_exposure.py`, `tests/test_livestock_decisions.py`.

**Interfaces:** `assess_route(route: dict, forecast: dict, departure_utc: datetime, loading_minutes: float = 0) -> dict`; `rank_routes(candidates: list[dict]) -> dict`. Assessment adds metrics, per-segment evidence, forecast coverage, status and warnings.

- [ ] Write failing tests: curved route intersects smoke, same endpoints/different paths, plume arrives after departure, departure delay changes exposure, constant concentration gives analytically correct integral, partial coverage does not count as zero, temporal bounds, coordinate order and projected geometry units.
- [ ] Sample road paths at no more than 1 km spacing and at forecast-cell/time boundaries; use segment-time weighting. Interpolate only within available valid times/cells; label interpolation and spatial resolution. Test that densification converges on the synthetic grid without implying sub-grid accuracy.
- [ ] Keep fire-spread intersection output separate from smoke exposure. Handle 6/12/24-hour fire horizons as snapshots with temporal uncertainty, not precise continuous fire-arrival ground truth.
- [ ] Remove bearing penalty, preserve selected route provenance, compare only candidates with comparable coverage, and retain alternative/tradeoff outputs. Unknown data blocks a claim of confirmed feasible evacuation; it does not automatically recommend sheltering.
- [ ] Preserve raw route duration in `route_duration_hours`; add smoke metrics under `smoke_assessment`. Keep legacy `smoke_penalty_applied=false` with an explicit deprecation note. Guard optimizer/econ against nullable impact times and unconfirmed feasibility.
- [ ] Run exposure/decision/econ compatibility tests; confirm output counts represent plans, not observed animals already moved.

## Task 5: Publish the evaluated route and reliable downstream messages

**Files:** modify `backend/main.py`, `Livestock/visualize.py`, `Livestock/livestock_agent.py`, livestock rendering in `backend/static/dashboard.html`, and affected consumers in `forecaster/agents/report_agent.py` and `backend/main.py`; create `tests/test_livestock_pipeline.py`.

- [ ] Add fixtures where a subprocess fails with old output present, packet generation changes mid-read, and the best route is not the first returned route.
- [ ] Require successful subprocess completion and matching output run/farm identity before returning new success; explicitly report stale prior output only as historical data. Add `run_id`, `assessment_status`, source age, selected geometry and smoke summary to livestock outputs and ERPC; populate `forecasting_requests` for absent/expired input evidence. This is a request field, not an automatic new agent conversation loop.
- [ ] Render the selected stored geometry and segment evidence; remove independent browser rerouting. Display unknown coverage, forecast age, unverified destinations and travel/exposure separately. Do not label concentration as visibility.
- [ ] Replace automatic use of evacuation-zone geometries as shelter points with explicit candidate destinations. Retain existing fallback sites only as unverified candidates, requiring recorded availability/acceptance before confirmed feasibility. Validate geometry types.
- [ ] Run integration tests with network mocks; manually inspect assessed, partial and unknown map states.

## Task 6: Validate improvement and document limits

**Files:** add `tests/fixtures/livestock/README.md`; update `Livestock/README.md` and root README.

- [ ] Run `python3 -m unittest discover -s tests -v`, existing `forecaster/tests/test_gate_logic.py`, `test_no_fire.py`, and `test_fire_threat.py` from their documented working directory in an isolated fixture/output workspace so saved snapshots are not overwritten.
- [ ] Demonstrate the old heuristic misses a curved smoke crossing while new geometry evaluation detects it; demonstrate a destination downwind whose road avoids the modeled plume.
- [ ] Document software/data licenses, source URLs, attribution, OSRM hosting limits, forecast age/resolution, sample assumptions and unknown-data behavior.
- [ ] Separate correctness from forecast accuracy. The existing JSONs cannot quantify detection accuracy. A later historical evaluation needs matched route/time smoke forecasts and independent surface observations; report false negatives/positives and route-ranking stability only when such labeled evidence exists.

## Approval scope and exclusions

Approval authorizes Tasks 1–6 with the stated free/open-source stack, additive schemas and bounded data access. No product changes have been made for this plan. Before marking implementation complete, report tests, real source access verification and any unresolved data limitations.

Excluded: custom atmospheric dispersion modeling, paid routing/air-quality services, veterinary smoke limits, nationwide live road-closure integration, full multi-trip vehicle scheduling, automatic evacuation actions, and uncalibrated plume-width formulas. These can be separately planned after the data contract and route assessment are trustworthy.

Reference documentation verified earlier in this chat: https://project-osrm.org/docs/v26.6.1/http ; https://www.weather.gov/sto/ExperimentalSmokeForecast ; https://www.weather.gov/mfr/hrrr_smoke_tutorial ; https://ospo.noaa.gov/products/land/hms.html ; https://project-osrm.org/ ; https://www.openstreetmap.org/copyright ; https://shapely.readthedocs.io/en/stable/index.html .
