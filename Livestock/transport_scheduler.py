"""Deterministic vehicle and trip scheduling for livestock evacuation plans."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _number(value: Any, default: float | None = None) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _site_key(site: dict[str, Any]) -> tuple[float, float]:
    # Names are aliases; capacity belongs to a physical destination.
    return (round(float(site["lat"]), 5), round(float(site["lon"]), 5))


def _site_capacity(site: dict[str, Any], now: datetime, max_age_hours: float) -> int | None:
    if site.get("availability_confirmed") is not True:
        return None
    try:
        accepted = int(site.get("accepted_head"))
        lat, lon = float(site["lat"]), float(site["lon"])
    except (TypeError, ValueError, KeyError):
        return None
    verified_at = _as_datetime(site.get("verified_at"))
    if accepted <= 0 or not (-90 <= lat <= 90 and -180 <= lon <= 180) or verified_at is None:
        return None
    age = (now - verified_at).total_seconds() / 3600
    if not 0 <= age <= max_age_hours:
        return None
    return accepted


def normalize_crop_tasks(
    crop_output: dict[str, Any], farm_id: str, run_id: str, valid_field_ids: set[str]
) -> list[dict[str, Any]]:
    """Normalize supported crop result keys, accepting only matching farm/run data."""
    if not isinstance(crop_output, dict):
        return []
    metadata = crop_output.get("_meta") or {}
    if metadata.get("farm_id") != farm_id or metadata.get("run_id") != run_id:
        return []

    key_aliases = (
        ("task4", "field_decisions", "field_decision"),
        ("task1", "fire_reduction"),
        ("task3", "hydration_strategy"),
    )
    economic = crop_output.get("task2") or crop_output.get("economic_impact") or {}
    economic_rows = economic.get("crop_destructions", []) if isinstance(economic, dict) else []
    crop_loss_by_field = {
        row.get("field_id"): row for row in economic_rows
        if isinstance(row, dict) and row.get("field_id") in valid_field_ids
    }
    tasks: list[dict[str, Any]] = []
    for aliases in key_aliases:
        rows: Any = []
        for key in aliases:
            if isinstance(crop_output.get(key), list):
                rows = crop_output[key]
                break
        for row in rows:
            if not isinstance(row, dict) or row.get("field_id") not in valid_field_ids:
                continue
            strategy = row.get("uprooting_strategy") if isinstance(row.get("uprooting_strategy"), dict) else {}
            crop_loss = crop_loss_by_field.get(row["field_id"], {})
            duration = _number(row.get("estimated_duration_hours", row.get("duration_hours")))
            tasks.append({
                "field_id": row["field_id"],
                "task_type": str(row.get("decision") or row.get("action") or row.get("technique") or "unspecified"),
                "time_window_hours": _number(row.get("fire_arrival_hours", row.get("hours_to_arrival",
                                                                                     strategy.get("time_window_hours")))),
                "labor_hours_needed": _number(row.get("labor_hours_needed", strategy.get("labor_hours_needed"))),
                "duration_hours": duration,
                "feasible_with_farm_resources": row.get("feasible_with_farm_resources"),
                "required_equipment": row.get("required_equipment") or row.get("equipment") or strategy.get("required_equipment"),
                "resource_status": "known" if row.get("feasible_with_farm_resources") is not None else "unknown",
                "estimated_crop_loss_usd": _number(crop_loss.get("estimated_loss_usd")),
                "crop_loss_reason": crop_loss.get("reason"),
            })
    known_task_fields = {task["field_id"] for task in tasks}
    for field_id, row in crop_loss_by_field.items():
        if field_id in known_task_fields:
            continue
        tasks.append({
            "field_id": field_id, "task_type": row.get("task4_decision", "crop loss exposure"),
            "time_window_hours": None, "labor_hours_needed": None, "duration_hours": None,
            "feasible_with_farm_resources": None, "required_equipment": None,
            "resource_status": "unknown",
            "estimated_crop_loss_usd": _number(row.get("estimated_loss_usd")),
            "crop_loss_reason": row.get("reason"),
        })
    return tasks


def schedule_evacuation_trips(
    pens: list[dict[str, Any]],
    farm: dict[str, Any],
    plan_time: datetime,
    *,
    config: dict[str, Any] | None = None,
    crop_tasks: list[dict[str, Any]] | None = None,
    route_assessor: Any = None,
) -> dict[str, Any]:
    """Greedily schedule deadline-feasible loads onto the earliest-finishing vehicles."""
    config = config or {}
    now = _as_datetime(plan_time)
    if now is None:
        raise ValueError("plan_time must be a datetime")

    infrastructure = farm.get("infrastructure", {})
    capacity = int(infrastructure.get("vehicle_capacity_head", 0) or 0)
    vehicle_count = int(infrastructure.get("available_trailers", 0) or 0)
    load_minutes = max(0.0, _number(config.get("loading_minutes"), 30.0) or 0.0)
    unload_minutes = max(0.0, _number(config.get("unloading_minutes"), 30.0) or 0.0)
    eta_margin = _number(config.get("fire_eta_margin"), 0.8) or 0.8
    if not 0 < eta_margin <= 1:
        eta_margin = 0.8
    confirmation_age = max(0.0, _number(config.get("destination_max_age_hours"), 6.0) or 0.0)

    if capacity <= 0 or vehicle_count <= 0:
        reasons = {str(p.get("pen_id")): "vehicle count or per-vehicle capacity is unavailable" for p in pens}
        return {"trips": [], "scheduled_by_pen": {}, "unscheduled_by_pen": reasons,
                "crop_tasks": list(crop_tasks or []), "summary": {"animals_scheduled": 0, "vehicle_hours": 0.0}}

    vehicles = [
        {"vehicle_id": f"vehicle_{index + 1}", "available_at": now,
         "trip_number": 0, "in_service": True}
        for index in range(vehicle_count)
    ]
    site_confirmations: dict[tuple[float, float], set[int]] = {}
    for pen in pens:
        for option in pen.get("route_options", []):
            site = option.get("site")
            if not isinstance(site, dict):
                continue
            key = _site_key(site)
            confirmed = _site_capacity(site, now, confirmation_age)
            site_confirmations.setdefault(key, set()).add(confirmed or 0)
    # Conflicting, missing, or stale reports for one physical site are
    # ambiguous; do not assume the most optimistic capacity.
    remaining_site_capacity: dict[tuple[float, float], int] = {
        key: next(iter(values)) for key, values in site_confirmations.items()
        if len(values) == 1 and 0 not in values
    }

    loads: list[dict[str, Any]] = []
    unscheduled: dict[str, str] = {}
    for pen in sorted(pens, key=lambda item: str(item.get("pen_id", ""))):
        pen_id = str(pen.get("pen_id", "unknown"))
        count = max(0, int(pen.get("count", 0) or 0))
        if count == 0:
            continue
        if pen.get("decision") not in ("evacuate", "partial_plan"):
            unscheduled[pen_id] = pen.get("decision_reason", "pen is not route-approved")
            continue
        eta = _number(pen.get("time_to_impact_hours"))
        if eta is None or eta <= 0:
            unscheduled[pen_id] = "fire-arrival time is unknown"
            continue
        safe_deadline = now + timedelta(hours=eta * eta_margin)
        if count > capacity and pen.get("splittable") is not True:
            unscheduled[pen_id] = f"pen count {count} exceeds vehicle capacity {capacity}; pen splitting is not enabled"
            continue
        batch_sizes = []
        remaining = count
        while remaining:
            batch = min(capacity, remaining)
            batch_sizes.append(batch)
            remaining -= batch
        options = [option for option in pen.get("route_options", []) if isinstance(option, dict)]
        if not options:
            unscheduled[pen_id] = "no route options are available"
            continue
        for batch_index, batch_size in enumerate(batch_sizes, start=1):
            loads.append({
                "load_id": f"{pen_id}:{batch_index}", "pen_id": pen_id,
                "batch_index": batch_index, "animal_count": batch_size,
                "safe_deadline": safe_deadline,
                "priority_score": _number(pen.get("priority_score"), 0.0) or 0.0,
                "options": options,
            })

    trips: list[dict[str, Any]] = []
    scheduled_by_pen: dict[str, int] = {}
    unscheduled_loads: set[str] = set()
    while loads:
        feasible: list[dict[str, Any]] = []
        for load in loads:
            for vehicle in vehicles:
                if not vehicle["in_service"]:
                    continue
                positioning_start = vehicle["available_at"]
                for option in load["options"]:
                    site = option.get("site") or {}
                    try:
                        key = _site_key(site)
                        site_remaining = remaining_site_capacity[key]
                    except (KeyError, TypeError, ValueError):
                        continue
                    if site_remaining < load["animal_count"]:
                        continue
                    outbound_hours = _number(option.get("duration_hours"))
                    return_hours = _number(option.get("return_duration_hours"))
                    approach_hours = _number(option.get("approach_duration_hours"))
                    if outbound_hours is None or outbound_hours <= 0 or approach_hours is None or approach_hours < 0:
                        continue
                    if option.get("source") != "osrm" or not option.get("geometry"):
                        continue
                    if option.get("approach_source") != "osrm":
                        continue
                    if approach_hours > 0 and not option.get("approach_geometry"):
                        continue
                    arrive_at_pen = positioning_start + timedelta(hours=approach_hours)
                    load_start = arrive_at_pen
                    depart = load_start + timedelta(minutes=load_minutes)
                    arrival = depart + timedelta(hours=outbound_hours)
                    if arrival > load["safe_deadline"]:
                        continue
                    unloaded = arrival + timedelta(minutes=unload_minutes)
                    return_depart = unloaded
                    if route_assessor:
                        assessed = route_assessor(option, positioning_start, depart, return_depart)
                        if not isinstance(assessed, dict):
                            continue
                        approach_fire = assessed.get("approach_fire", {"status": "clear"} if approach_hours == 0 else {})
                        approach_smoke = assessed.get("approach_smoke", {"status": "assessed"} if approach_hours == 0 else {})
                        outbound_fire = assessed.get("outbound_fire", {})
                        outbound_smoke = assessed.get("outbound_smoke", {})
                        return_fire = assessed.get("return_fire", {})
                        return_smoke = assessed.get("return_smoke", {})
                    else:
                        approach_fire = option.get("approach_fire_hazard", {"status": "clear"})
                        approach_smoke = option.get("approach_smoke_assessment", {"status": "assessed"})
                        outbound_fire = option.get("fire_hazard", {})
                        outbound_smoke = option.get("smoke_assessment", {})
                        return_fire = option.get("return_fire_hazard", {})
                        return_smoke = option.get("return_smoke_assessment", {})
                    if (approach_fire.get("status") != "clear"
                            or approach_smoke.get("status") != "assessed"
                            or outbound_fire.get("status") != "clear"
                            or outbound_smoke.get("status") != "assessed"):
                        continue
                    round_trip_possible = (
                        return_hours is not None and return_hours >= 0
                        and option.get("return_source") == "osrm"
                        and bool(option.get("return_geometry"))
                        and return_fire.get("status") == "clear"
                        and return_smoke.get("status") == "assessed"
                    )
                    returned = return_depart + timedelta(hours=return_hours) if round_trip_possible else None
                    # Repositioning and loading are part of the next candidate trip.
                    available = returned if returned else unloaded
                    feasible.append({
                        "load": load, "vehicle": vehicle, "option": option,
                        "positioning_start": positioning_start, "arrive_at_pen": arrive_at_pen,
                        "loading_start": load_start, "depart": depart, "arrival": arrival,
                        "unloaded": unloaded, "returned": returned, "available": available,
                        "round_trip_possible": round_trip_possible,
                        "slack_seconds": (load["safe_deadline"] - arrival).total_seconds(),
                        "completion": available,
                    })
        if not feasible:
            break

        earliest_for_load: dict[str, dict[str, Any]] = {}
        for candidate in feasible:
            load_id = candidate["load"]["load_id"]
            current = earliest_for_load.get(load_id)
            candidate_key = (candidate["completion"], candidate["arrival"],
                             str((candidate["option"].get("site") or {}).get("name", "")),
                             candidate["vehicle"]["vehicle_id"])
            current_key = ((current["completion"], current["arrival"],
                            str((current["option"].get("site") or {}).get("name", "")),
                            current["vehicle"]["vehicle_id"]) if current else None)
            if current is None or candidate_key < current_key:
                earliest_for_load[load_id] = candidate
        chosen = min(earliest_for_load.values(), key=lambda item: (
            item["slack_seconds"], item["completion"], -item["load"]["priority_score"],
            item["load"]["pen_id"], item["load"]["batch_index"], item["vehicle"]["vehicle_id"],
        ))
        load, vehicle, option = chosen["load"], chosen["vehicle"], chosen["option"]
        key = _site_key(option["site"])
        remaining_site_capacity[key] -= load["animal_count"]
        vehicle["trip_number"] += 1
        vehicle["available_at"] = chosen["available"]
        if not chosen["round_trip_possible"]:
            vehicle["in_service"] = False
        trips.append({
            "trip_id": f"{vehicle['vehicle_id']}-trip-{vehicle['trip_number']}",
            "vehicle_id": vehicle["vehicle_id"], "trip_number": vehicle["trip_number"],
            "pen_id": load["pen_id"], "batch_index": load["batch_index"],
            "animal_count": load["animal_count"],
            "destination": {key: option["site"].get(key) for key in ("name", "lat", "lon")},
            "positioning_start_at": chosen["positioning_start"].isoformat(),
            "arrive_at_pen": chosen["arrive_at_pen"].isoformat(),
            "loading_start_at": chosen["loading_start"].isoformat(),
            "depart_at": chosen["depart"].isoformat(), "arrive_at": chosen["arrival"].isoformat(),
            "unloaded_at": chosen["unloaded"].isoformat(),
            "return_at": chosen["returned"].isoformat() if chosen["returned"] else None,
            "vehicle_available_at": chosen["returned"].isoformat() if chosen["returned"] else None,
            "round_trip_possible": chosen["round_trip_possible"],
            "round_trip_reason": None if chosen["round_trip_possible"] else
            "safe return route and forecast assessment were unavailable; vehicle cannot be scheduled again",
            "deadline_at": load["safe_deadline"].isoformat(),
            "deadline_margin_minutes": round(chosen["slack_seconds"] / 60, 1),
            "outbound_duration_hours": round((chosen["arrival"] - chosen["depart"]).total_seconds() / 3600, 3),
            "return_duration_hours": round((chosen["returned"] - chosen["unloaded"]).total_seconds() / 3600, 3)
            if chosen["returned"] else None,
            "schedule_status": "planned_advisory",
        })
        scheduled_by_pen[load["pen_id"]] = scheduled_by_pen.get(load["pen_id"], 0) + load["animal_count"]
        loads.remove(load)

    for load in loads:
        unscheduled_loads.add(load["pen_id"])
    for pen_id in sorted(unscheduled_loads):
        unscheduled.setdefault(pen_id, "no verified vehicle, route, destination capacity, or deadline-feasible trip remains")

    trips.sort(key=lambda trip: (trip["depart_at"], trip["vehicle_id"], trip["trip_number"]))
    animals_scheduled = sum(scheduled_by_pen.values())
    vehicle_hours = sum(
        ((datetime.fromisoformat(trip["return_at"]) if trip["return_at"] else datetime.fromisoformat(trip["unloaded_at"]))
         - datetime.fromisoformat(trip["loading_start_at"])).total_seconds()
        / 3600 for trip in trips
    )
    return {
        "trips": trips,
        "scheduled_by_pen": scheduled_by_pen,
        "unscheduled_by_pen": unscheduled,
        "crop_tasks": list(crop_tasks or []),
        "summary": {"animals_scheduled": animals_scheduled, "vehicle_hours": round(vehicle_hours, 2)},
    }
