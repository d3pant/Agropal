"""Route exposure sampling against time-valid gridded smoke forecasts."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
import math
from typing import Any


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lon1, lat1 = a
    lon2, lat2 = b
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.atan2(math.sqrt(h), math.sqrt(1 - h))


def _route_samples(route: dict, spacing_km: float = 1.0) -> tuple[list[tuple[float, float]], list[float], list[int]]:
    geometry = route.get("geometry") or {}
    if geometry.get("type") != "LineString":
        return [], [], []
    coords = geometry.get("coordinates", [])
    if len(coords) < 2:
        return [], [], []
    edge_distances = [_distance_km(tuple(a), tuple(b)) for a, b in zip(coords, coords[1:])]
    total_distance = sum(edge_distances)
    total_duration = float(route.get("duration_hours") or 0) * 3600
    if total_distance <= 0 or total_duration <= 0:
        return [], [], []
    annotations = route.get("segment_durations_seconds")
    aligned = isinstance(annotations, list) and len(annotations) == len(edge_distances) and all(
        isinstance(v, (int, float)) and v >= 0 for v in annotations
    )
    if not aligned:
        return [], [], []
    edge_times = [float(v) for v in annotations]
    points, elapsed, segment_ids = [tuple(coords[0])], [0.0], [0]
    prior_time = 0.0
    for edge_index, (a, b, edge_distance, edge_duration) in enumerate(zip(coords, coords[1:], edge_distances, edge_times)):
        if edge_index > 0:
            points.append(tuple(a))
            elapsed.append(prior_time)
            segment_ids.append(edge_index)
        steps = max(1, math.ceil(edge_distance / spacing_km))
        for step in range(1, steps + 1):
            fraction = step / steps
            points.append((a[0] + (b[0] - a[0]) * fraction, a[1] + (b[1] - a[1]) * fraction))
            elapsed.append(prior_time + edge_duration * fraction)
            segment_ids.append(edge_index)
        prior_time += edge_duration
    if elapsed[-1] > 0 and abs(elapsed[-1] - total_duration) > 1:
        scale = total_duration / elapsed[-1]
        elapsed = [t * scale for t in elapsed]
    return points, elapsed, segment_ids


def _sample_layer(layer: dict, point: tuple[float, float], max_distance_km: float) -> float | None:
    lat_rows = layer.get("latitudes", [])
    lon_rows = layer.get("longitudes", [])
    values = layer.get("concentration_ug_m3", [])
    best = None
    best_distance = float("inf")
    for y, row in enumerate(lat_rows):
        for x, lat in enumerate(row):
            try:
                value = values[y][x]
                lon = lon_rows[y][x]
                if value is None or not math.isfinite(float(value)) or float(value) < 0:
                    continue
                distance = _distance_km(point, (float(lon), float(lat)))
            except (IndexError, TypeError, ValueError):
                continue
            if distance < best_distance:
                best, best_distance = float(value), distance
    return best if best_distance <= max_distance_km else None


def _sample_at(layers: list[dict], point: tuple[float, float], when: datetime,
               max_distance_km: float) -> float | None:
    valid = sorted(((_parse_time(layer["valid_at"]), layer) for layer in layers), key=lambda item: item[0])
    before = [(t, layer) for t, layer in valid if t <= when]
    after = [(t, layer) for t, layer in valid if t >= when]
    if not before or not after:
        return None
    t0, l0 = before[-1]
    t1, l1 = after[0]
    v0 = _sample_layer(l0, point, max_distance_km)
    if v0 is None:
        return None
    if t0 == t1:
        return v0
    v1 = _sample_layer(l1, point, max_distance_km)
    if v1 is None:
        return None
    fraction = (when - t0).total_seconds() / (t1 - t0).total_seconds()
    return v0 + (v1 - v0) * fraction


def assess_route(route: dict, forecast: dict, departure_utc: datetime,
                 loading_minutes: float = 0) -> dict:
    if forecast.get("status") not in {"available", "partial"} or not forecast.get("layers"):
        return {"status": "unknown", "reason": forecast.get("reason", "smoke forecast unavailable"),
                "coverage_fraction": 0.0, "integrated_exposure_ug_h_m3": None,
                "peak_concentration_ug_m3": None, "source": forecast.get("source")}
    points, elapsed, segment_ids = _route_samples(route)
    if not points:
        return {"status": "unknown", "reason": "route geometry or OSRM segment timing unavailable",
                "coverage_fraction": 0.0, "integrated_exposure_ug_h_m3": None,
                "peak_concentration_ug_m3": None, "source": forecast.get("source")}
    departure = departure_utc.astimezone(timezone.utc) if departure_utc.tzinfo else departure_utc.replace(tzinfo=timezone.utc)
    departure += timedelta(minutes=loading_minutes)
    max_distance = max(2.5, float(forecast.get("spatial_resolution_km", 3)) * 0.85)
    concentrations = [_sample_at(forecast["layers"], point, departure + timedelta(seconds=seconds), max_distance)
                      for point, seconds in zip(points, elapsed)]
    covered_seconds = 0.0
    integrated = 0.0
    edge_count = len((route.get("geometry") or {}).get("coordinates", [])) - 1
    edge_integrals = [0.0] * edge_count
    edge_covered = [0.0] * edge_count
    total_seconds = max(elapsed[-1], 0.0)
    for index in range(len(points) - 1):
        dt = elapsed[index + 1] - elapsed[index]
        c0, c1 = concentrations[index], concentrations[index + 1]
        if dt > 0 and c0 is not None and c1 is not None:
            contribution = ((c0 + c1) / 2) * dt / 3600
            integrated += contribution
            covered_seconds += dt
            edge_index = segment_ids[index + 1]
            edge_integrals[edge_index] += contribution
            edge_covered[edge_index] += dt
    coverage = covered_seconds / total_seconds if total_seconds else 0.0
    complete = coverage >= 0.999
    segment_evidence = []
    for edge_index in range(edge_count):
        sample_indices = [i for i, segment_id in enumerate(segment_ids) if segment_id == edge_index]
        segment_start = elapsed[sample_indices[0]] if sample_indices else 0.0
        segment_end = elapsed[sample_indices[-1]] if sample_indices else segment_start
        segment_seconds = max(0.0, segment_end - segment_start)
        segment_coverage = edge_covered[edge_index] / segment_seconds if segment_seconds else 0.0
        segment_complete = segment_coverage >= 0.999
        segment_evidence.append({
            "route_segment_index": edge_index,
            "start_coordinate": (route["geometry"]["coordinates"][edge_index]),
            "end_coordinate": (route["geometry"]["coordinates"][edge_index + 1]),
            "arrival_minutes_from_departure": round(segment_start / 60, 1),
            "departure_minutes_from_departure": round(segment_end / 60, 1),
            "sample_count": len(sample_indices),
            "coverage_fraction": round(segment_coverage, 4),
            "peak_concentration_ug_m3": max(
                (concentrations[i] for i in sample_indices if concentrations[i] is not None), default=None),
            "integrated_exposure_ug_h_m3": round(edge_integrals[edge_index], 3) if segment_complete else None,
        })
    return {
        "status": "assessed" if complete else ("partial" if coverage > 0 else "unknown"),
        "reason": None if complete else "forecast does not cover the full route and travel time",
        "coverage_fraction": round(coverage, 4),
        "covered_travel_minutes": round(covered_seconds / 60, 1),
        "total_travel_minutes": round(total_seconds / 60, 1),
        "integrated_exposure_ug_h_m3": round(integrated, 3) if complete else None,
        "peak_concentration_ug_m3": max((c for c in concentrations if c is not None), default=None),
        "source": forecast.get("source"), "spatial_resolution_km": forecast.get("spatial_resolution_km"),
        "valid_from": forecast.get("valid_from"), "valid_until": forecast.get("valid_until"),
        "route_segment_evidence": segment_evidence,
        "sampling_method": "nearest HRRR grid cell; linear time interpolation; route densified to <=1 km",
        "exposure_metric_note": "Comparative modeled concentration-time indicator; not an inhaled dose or safety threshold.",
    }


def assess_fire_hazard(route: dict, horizons: dict, fire_present: bool,
                       loading_minutes: float = 0) -> dict:
    """Compare route samples with available forecast fire perimeters, separate from smoke."""
    if not fire_present:
        return {"status": "clear", "intersects": False, "source": "no active fire in forecaster snapshot"}
    parsed = []
    for key, value in (horizons or {}).items():
        try:
            hours = float(str(key).lower().removesuffix("h"))
            polygon = value.get("polygon")
            if polygon and polygon.get("type") == "Polygon":
                parsed.append((hours, polygon["coordinates"][0]))
        except (TypeError, ValueError, KeyError, IndexError):
            continue
    points, elapsed, _ = _route_samples(route)
    if not parsed or not points:
        return {"status": "unknown", "intersects": None, "reason": "fire perimeter forecast or route timing unavailable"}
    parsed.sort(key=lambda item: item[0])
    for point, seconds in zip(points, elapsed):
        travel_hour = (loading_minutes * 60 + seconds) / 3600
        candidates = [item for item in parsed if item[0] >= travel_hour]
        if not candidates:
            return {"status": "unknown", "intersects": None, "reason": "route exceeds fire-spread forecast horizon"}
        horizon_hours, ring = candidates[0]
        inside = False
        x, y = point
        for a, b in zip(ring, ring[1:]):
            x1, y1 = a
            x2, y2 = b
            if (y1 > y) != (y2 > y):
                crossing_x = (x2 - x1) * (y - y1) / (y2 - y1) + x1
                if x < crossing_x:
                    inside = not inside
        if inside:
            return {"status": "intersects", "intersects": True,
                    "forecast_horizon_hours": horizon_hours,
                    "reason": "route sample falls inside a predicted fire perimeter"}
    return {"status": "clear", "intersects": False,
            "forecast_horizon_hours": parsed[-1][0],
            "reason": "route samples fall outside available predicted fire perimeters"}


def rank_routes(candidates: list[dict], impact_eta_hours: float | None,
                loading_minutes: float = 0, deadline_margin: float = 0.8) -> dict:
    eligible, rejected, review = [], [], []
    for route in candidates:
        reason = None
        review_reason = None
        if route.get("source") != "osrm" or not route.get("geometry"):
            reason = "verified road geometry unavailable"
        elif route.get("fire_hazard", {}).get("status") == "unknown":
            review_reason = route["fire_hazard"].get("reason", "fire-spread risk is unknown")
        elif route.get("fire_hazard_intersection") is True:
            reason = "route intersects modeled fire-spread area"
        elif route.get("fire_hazard", {}).get("intersects") is True:
            reason = route["fire_hazard"].get("reason", "route intersects modeled fire-spread area")
        elif impact_eta_hours is None:
            review_reason = "fire arrival time is unknown"
        elif (float(route.get("duration_hours", 0)) + loading_minutes / 60) > impact_eta_hours * deadline_margin:
            reason = "route exceeds conservative fire-arrival planning window"
        elif route.get("destination_verified") is not True:
            review_reason = "destination availability or acceptance is unverified"
        if reason:
            rejected.append({**route, "rejection_reason": reason})
        elif review_reason:
            review.append({**route, "review_reason": review_reason})
        elif route.get("smoke_assessment", {}).get("status") != "assessed":
            review.append({**route, "review_reason": route.get("smoke_assessment", {}).get("reason", "full smoke coverage unavailable")})
        else:
            eligible.append(route)
    eligible.sort(key=lambda r: (r["smoke_assessment"]["integrated_exposure_ug_h_m3"], r["duration_hours"]))
    return {"ranked": eligible, "rejected": rejected, "review_required": review}
