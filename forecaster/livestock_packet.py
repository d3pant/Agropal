"""Versioned, provenance-preserving data contract for livestock advisory runs."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
import math


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _wind(direction_from: Any) -> dict:
    valid = isinstance(direction_from, (int, float)) and math.isfinite(direction_from)
    return {
        "direction_from_degrees": direction_from if valid else None,
        "direction_to_degrees": (float(direction_from) + 180) % 360 if valid else None,
        "direction_convention": "meteorological_from",
    }


def build_livestock_packet(status: dict, farm_profile: dict, farm_config: dict, run_id: str) -> dict:
    """Create a single-run packet without inventing impact or smoke observations."""
    now = _parse_utc(status.get("timestamp")) or datetime.now(timezone.utc)
    interval = status.get("next_update_minutes") or 30
    try:
        interval = max(1, int(interval))
    except (TypeError, ValueError):
        interval = 30
    profile_location = farm_profile.get("centroid", {})
    config_location = farm_config.get("location", {})
    fire = status.get("nearest_fire") or {}
    spread = status.get("spread_prediction") or {}
    impact = spread.get("time_to_farm") or {}
    farm_wind = status.get("wind_direction_degrees")
    fire_wind = spread.get("wind_direction_deg")
    impacts = []
    if fire.get("location") and fire["location"].get("lat") is not None:
        try:
            from models.spread_model import time_to_impact
            for pen in farm_profile.get("pens", []):
                point = pen.get("centroid", {})
                if point.get("lat") is None or point.get("lon") is None:
                    continue
                result = time_to_impact(
                    fire["location"]["lat"], fire["location"]["lon"],
                    point["lat"], point["lon"], fire_wind or 0,
                    spread.get("wind_speed_kmh") or status.get("wind_speed_kmh") or 0,
                    spread.get("soil_moisture") or 0.15,
                    fire.get("frp_mw") or 0,
                ) if fire_wind is not None else {"hours": None, "threatened": None}
                impacts.append({
                    "zone_id": pen.get("zone"), "pen_id": pen.get("pen_id"),
                    "time_to_impact_hours": result.get("hours"),
                    "threatened": result.get("threatened"),
                    "model": spread.get("model"), "computed_at": spread.get("computed_at"),
                })
        except ImportError:
            impacts = []
    return {
        "schema_version": 1,
        "run_id": run_id,
        "farm_id": farm_profile.get("farm_id") or farm_config.get("farm_id"),
        "status_farm_id": status.get("farm_id"),
        "farm_location": {"lat": profile_location.get("lat"), "lon": profile_location.get("lon")},
        "config_location": {"lat": config_location.get("lat"), "lon": config_location.get("lon")},
        "status_location": {"lat": status.get("farm_lat"), "lon": status.get("farm_lon")},
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "source_status_timestamp": status.get("timestamp"),
        "valid_until": (now + timedelta(minutes=interval)).isoformat().replace("+00:00", "Z"),
        "threat_level": status.get("threat_level"),
        "input_quality": [],
        "fire_data": fire,
        "wind_at_farm": _wind(farm_wind),
        "wind_at_fire": _wind(fire_wind),
        "spread_prediction": {
            "model": spread.get("model"),
            "computed_at": spread.get("computed_at"),
            "time_to_farm_hours": impact.get("hours"),
            "impact_status": "estimated" if impact.get("hours") is not None else "unknown",
            "time_to_farm_threatened": impact.get("threatened"),
            "horizons": spread.get("horizons", {}),
        },
        "affected_zones": impacts,
        "smoke_forecast": {"status": "not_fetched", "source": None},
        # Legacy-compatible data, with no synthetic impact estimates.
        "smoke_trajectory": {
            "direction_degrees": _wind(fire_wind)["direction_to_degrees"],
            "affected_zones": [],
            "visibility_impact": None,
        },
        "weather_forecast": {},
        "fire_source_timestamp": fire.get("detected_at"),
    }


def validate_livestock_packet(packet: dict, farm_profile: dict, now_utc: datetime) -> list[dict]:
    errors = []
    if packet.get("schema_version") != 1:
        errors.append({"field": "schema_version", "reason": "unsupported or missing packet schema"})
    if not packet.get("run_id"):
        errors.append({"field": "run_id", "reason": "missing run identity"})
    expected_id = farm_profile.get("farm_id")
    if expected_id and packet.get("farm_id") != expected_id:
        errors.append({"field": "farm_id", "reason": "packet belongs to a different farm"})
    for field in ("status_farm_id",):
        actual = packet.get(field)
        if actual and expected_id and actual != expected_id:
            errors.append({"field": field, "reason": "forecaster status belongs to a different farm"})
    profile = farm_profile.get("centroid", {})
    location = packet.get("farm_location", {})
    try:
        distance_m = _distance_m(float(profile["lat"]), float(profile["lon"]),
                                 float(location["lat"]), float(location["lon"]))
        if distance_m > 100:
            errors.append({"field": "farm_location", "reason": f"packet location differs by {distance_m:.0f} m"})
    except (KeyError, TypeError, ValueError):
        errors.append({"field": "farm_location", "reason": "farm coordinates missing or invalid"})
    config_location = packet.get("config_location", {})
    try:
        config_distance_m = _distance_m(float(profile["lat"]), float(profile["lon"]),
                                        float(config_location["lat"]), float(config_location["lon"]))
        if config_distance_m > 100:
            errors.append({"field": "config_location", "reason": f"forecaster config differs from livestock farm by {config_distance_m:.0f} m"})
    except (KeyError, TypeError, ValueError):
        errors.append({"field": "config_location", "reason": "forecaster coordinates missing or invalid"})
    status_location = packet.get("status_location", {})
    if status_location.get("lat") is not None or status_location.get("lon") is not None:
        try:
            status_distance_m = _distance_m(float(profile["lat"]), float(profile["lon"]),
                                            float(status_location["lat"]), float(status_location["lon"]))
            if status_distance_m > 100:
                errors.append({"field": "status_location", "reason": f"forecaster result differs from livestock farm by {status_distance_m:.0f} m"})
        except (KeyError, TypeError, ValueError):
            errors.append({"field": "status_location", "reason": "forecaster result coordinates missing or invalid"})
    expiry = _parse_utc(packet.get("valid_until"))
    now = now_utc.astimezone(timezone.utc) if now_utc.tzinfo else now_utc.replace(tzinfo=timezone.utc)
    if expiry is None or expiry < now:
        errors.append({"field": "valid_until", "reason": "packet is expired or has invalid expiry"})
    return errors


def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
