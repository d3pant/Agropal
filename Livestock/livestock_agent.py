import asyncio
import json
import logging
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Dict, List, Any

import httpx
from dotenv import load_dotenv
try:
    from .transport_scheduler import normalize_crop_tasks, schedule_evacuation_trips
except ImportError:  # direct execution from the Livestock directory
    from transport_scheduler import normalize_crop_tasks, schedule_evacuation_trips

load_dotenv(Path(__file__).parent.parent / ".env")

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# USDA NASS QuickStats API prices (2024 average, $/head)
USDA_NASS_PRICES = {
    "cattle": 1450,      # Feeder cattle
    "horse": 3500,       # Horse
    "sheep": 350,        # Lamb/sheep
    "pig": 550,          # Hog
    "goat": 300,         # Goat
}

LIVESTOCK_DIR = Path(__file__).parent
FAIRPLEX_POMONA = {"lat": 34.0564, "lon": -117.7500, "name": "Fairplex Pomona"}
DEL_MAR_FAIRGROUNDS = {"lat": 32.9595, "lon": -117.2653, "name": "Del Mar Fairgrounds"}

FALLBACK_SITES = [FAIRPLEX_POMONA, DEL_MAR_FAIRGROUNDS]

BASE_SPECIES_SCORES = {
    "cattle": 40,
    "horse": 50,
    "sheep": 30,
    "pig": 25,
    "goat": 28,
}

AGE_MODIFIERS = {
    "juvenile": 20,
    "mixed": 10,
    "adult": 0,
}

HEALTH_MODIFIERS = {
    "sick": 20,
    "mixed": 10,
    "healthy": 0,
}


def bearing_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    dlon = math.radians(lon2 - lon1)

    y = math.sin(dlon) * math.cos(lat2_rad)
    x = math.cos(lat1_rad) * math.sin(lat2_rad) - math.sin(lat1_rad) * math.cos(lat2_rad) * math.cos(dlon)
    bearing_rad = math.atan2(y, x)
    bearing_deg = math.degrees(bearing_rad)
    return (bearing_deg + 360) % 360


def angle_diff(a: float, b: float) -> float:
    diff = (a - b + 180) % 360 - 180
    return abs(diff)


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    lat1_rad, lon1_rad = math.radians(lat1), math.radians(lon1)
    lat2_rad, lon2_rad = math.radians(lat2), math.radians(lon2)
    dlat, dlon = lat2_rad - lat1_rad, lon2_rad - lon1_rad
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def compute_priority_score(pen: Dict[str, Any], time_to_impact_hours: float | None) -> float:
    base_species = BASE_SPECIES_SCORES.get(pen["species"], 0)
    age_modifier = AGE_MODIFIERS.get(pen["age_distribution"], 0)
    health_modifier = HEALTH_MODIFIERS.get(pen["health_status"], 0)

    avg_value = pen.get("avg_market_value_usd", 0)
    value_score = min(20, (avg_value / 2000) * 20)

    zone_urgency = min(30, max(0, 100 - (time_to_impact_hours * 8))) if time_to_impact_hours is not None else 0

    priority = base_species + age_modifier + health_modifier + value_score + zone_urgency
    return priority


def compute_evacuation_optimization(pens_output: List[Dict], farm: Dict[str, Any], time_available_hours: float) -> Dict[str, Any]:
    """
    Minimize financial loss under hard truck capacity.

    Scoring per pen (fractional knapsack with urgency):
      score = 0.6 * (value_per_head / max_value_per_head)
            + 0.4 * urgency
    where urgency = 1 - clamp(route_hours / time_to_impact, 0, 1).
    Pens that fit entirely go first (sorted by score desc), then partial fill
    of the highest-value remaining pen to use every slot.
    """
    vehicle_capacity = farm.get("infrastructure", {}).get("vehicle_capacity_head", 150)
    available_trailers = farm.get("infrastructure", {}).get("available_trailers", 1)

    # Realistic trips: time_available / (2 * avg_route_time), min 1
    evacuable_pens_raw = [p for p in pens_output if p["decision"] == "evacuate"]
    avg_route = (
        sum(p["route_duration_hours"] for p in evacuable_pens_raw) / len(evacuable_pens_raw)
        if evacuable_pens_raw else 1.0
    )
    trips_per_trailer = max(1, int(time_available_hours / (2 * avg_route)))
    total_capacity = vehicle_capacity * available_trailers * trips_per_trailer

    pen_lookup = {pen["pen_id"]: pen for pen in farm["pens"]}

    candidates = []
    for p in evacuable_pens_raw:
        pen_data = pen_lookup.get(p["pen_id"])
        if not pen_data:
            continue
        count = pen_data["count"]
        price = pen_data["avg_market_value_usd"]
        total_value = count * price
        route_hrs = p["route_duration_hours"] or avg_route
        candidates.append({
            "pen_id": p["pen_id"],
            "species": p["species"],
            "count": count,
            "price_per_head": price,
            "total_value": total_value,
            "route_hours": route_hrs,
        })

    if not candidates:
        total_animals = sum(int(p.get("count", 0)) for p in farm.get("pens", []))
        total_value = sum(int(p.get("count", 0)) * float(p.get("avg_market_value_usd", 0))
                          for p in farm.get("pens", []))
        return {
            "transport_capacity": {"vehicles": available_trailers, "capacity_per_vehicle": vehicle_capacity,
                                   "total_capacity": total_capacity, "trips_per_trailer": trips_per_trailer},
            "evacuation_sequence": [],
            "summary": {"total_animals_at_risk": total_animals, "animals_can_evacuate": 0,
                        "value_can_save_usd": 0, "potential_loss_usd": total_value,
                        "loss_percentage": 100 if total_value else 0}
        }

    max_price = max(c["price_per_head"] for c in candidates)

    for c in candidates:
        urgency = 1.0 - min(c["route_hours"] / max(time_available_hours, 0.1), 1.0)
        c["score"] = 0.6 * (c["price_per_head"] / max_price) + 0.4 * urgency

    # Sort highest score first — this is the load order for the trucks
    candidates.sort(key=lambda x: x["score"], reverse=True)

    evacuation_plan = []
    remaining_capacity = total_capacity
    value_saved = 0.0

    for pen in candidates:
        if remaining_capacity <= 0:
            evacuation_plan.append({
                "sequence": len(evacuation_plan) + 1,
                "pen_id": pen["pen_id"],
                "species": pen["species"],
                "count": pen["count"],
                "value_usd": pen["total_value"],
                "animals_saved": 0,
                "value_saved_usd": 0,
                "status": "NOT_PLANNED_CAPACITY",
                "reason": f"No transport capacity remains — review loading priority (score {pen['score']:.2f})",
            })
            continue

        if pen["count"] <= remaining_capacity:
            # Full pen fits
            evacuation_plan.append({
                "sequence": len(evacuation_plan) + 1,
                "pen_id": pen["pen_id"],
                "species": pen["species"],
                "count": pen["count"],
                "value_usd": pen["total_value"],
                "animals_saved": pen["count"],
                "value_saved_usd": pen["total_value"],
                "status": "EVACUATE",
                "reason": f"score {pen['score']:.2f} | ${pen['price_per_head']:,}/head | {pen['route_hours']:.1f}h route",
            })
            remaining_capacity -= pen["count"]
            value_saved += pen["total_value"]
        else:
            # Partial — fill every remaining slot with the highest-value animals
            partial_value = round(pen["total_value"] * remaining_capacity / pen["count"], 2)
            evacuation_plan.append({
                "sequence": len(evacuation_plan) + 1,
                "pen_id": pen["pen_id"],
                "species": pen["species"],
                "count": pen["count"],
                "value_usd": pen["total_value"],
                "animals_saved": remaining_capacity,
                "value_saved_usd": partial_value,
                "status": "PARTIAL",
                "reason": f"Only {remaining_capacity} slots left of {pen['count']} — take highest-value animals first",
            })
            value_saved += partial_value
            remaining_capacity = 0

    total_value_at_risk = sum(int(p.get("count", 0)) * float(p.get("avg_market_value_usd", 0))
                              for p in farm.get("pens", []))
    potential_loss = total_value_at_risk - value_saved
    total_animals = sum(int(p.get("count", 0)) for p in farm.get("pens", []))
    animals_saved = sum(e["animals_saved"] for e in evacuation_plan)

    return {
        "transport_capacity": {
            "vehicles": available_trailers,
            "capacity_per_vehicle": vehicle_capacity,
            "trips_per_trailer": trips_per_trailer,
            "total_capacity": total_capacity,
        },
        "evacuation_sequence": evacuation_plan,
        "summary": {
            "total_animals_at_risk": total_animals,
            "animals_can_evacuate": animals_saved,
            "value_can_save_usd": round(value_saved, 2),
            "potential_loss_usd": round(potential_loss, 2),
            "loss_percentage": round(potential_loss / total_value_at_risk * 100, 1) if total_value_at_risk else 0,
        },
    }


def apply_transport_plan(pens_output: List[Dict], farm: Dict[str, Any],
                         time_available_hours: float) -> Dict[str, Any]:
    """Constrain planned animal counts to available vehicle and trip capacity."""
    optimization = compute_evacuation_optimization(pens_output, farm, time_available_hours)
    allocations = {item["pen_id"]: int(item.get("animals_saved", 0))
                   for item in optimization.get("evacuation_sequence", [])}
    pen_counts = {item["pen_id"]: int(item.get("count", 0)) for item in farm.get("pens", [])}
    for pen in pens_output:
        if pen.get("decision") != "evacuate":
            continue
        count = pen_counts.get(pen["pen_id"], 0)
        allocated = min(count, allocations.get(pen["pen_id"], 0))
        pen["animals_planned_to_move"] = allocated
        if allocated == 0:
            pen["decision"] = "review_required"
            pen["assessment_status"] = "partial"
            pen["decision_reason"] = "Route is feasible, but no transport capacity was allocated"
            pen["movement_status"] = "pending_review"
            pen["assigned_evac_site"] = None
        elif allocated < count:
            pen["decision"] = "partial_plan"
            pen["assessment_status"] = "partial"
            pen["decision_reason"] = (pen.get("decision_reason", "Route is feasible")
                                       + f"; transport capacity covers {allocated} of {count} animals")
            pen["movement_status"] = "planned_partial"
        else:
            pen["animals_planned_to_move"] = allocated
    return optimization


async def fetch_usda_prices(client: httpx.AsyncClient) -> Dict[str, float]:
    """Fetch live USDA NASS prices if API key is set, otherwise use 2024 hardcoded values."""
    api_key = os.getenv("USDA_NASS_API_KEY", "")
    if not api_key:
        logger.info("USDA_NASS_API_KEY not set — using hardcoded 2024 prices")
        return USDA_NASS_PRICES

    commodities = {
        "cattle": "CATTLE, FEEDER",
        "horse": "HORSES",
        "sheep": "SHEEP & LAMBS",
        "pig": "HOGS",
        "goat": "GOATS"
    }
    prices = {}
    for species, commodity in commodities.items():
        try:
            url = "https://quickstats.nass.usda.gov/api/api_GET/"
            params = {
                "key": api_key,
                "commodity_desc": commodity,
                "statisticcat_desc": "PRICE RECEIVED",
                "year": str(datetime.now().year - 1),
                "format": "JSON"
            }
            response = await client.get(url, params=params, timeout=5.0)
            if response.status_code == 200:
                data = response.json()
                if data.get("data"):
                    prices[species] = float(data["data"][0].get("Value", USDA_NASS_PRICES[species]))
                    continue
        except Exception as e:
            logger.warning(f"USDA NASS {species}: {e}")
        prices[species] = USDA_NASS_PRICES[species]
    return prices


async def query_calaes(client: httpx.AsyncClient, lat: float, lon: float) -> List[Dict[str, Any]]:
    url = "https://services.arcgis.com/BLN4oKB0N1YSgvY8/arcgis/rest/services/CA_EVACUATIONS_CalOESHosted_view/FeatureServer/0/query"
    params = {
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": 4326,
        "outSR": 4326,
        "distance": 50000,
        "units": "esriSRUnit_Meter",
        "outFields": "*",
        "f": "geojson",
        "returnGeometry": "true"
    }

    try:
        logger.info(f"Querying CalOES API for farm centroid ({lat}, {lon})")
        response = await client.get(url, params=params, timeout=5.0)
        response.raise_for_status()
        data = response.json()
        logger.debug(f"CalOES API raw response: {json.dumps(data)}")

        features = data.get("features", [])
        filtered = [
            f for f in features
            if f.get("properties", {}).get("Status") in ["EVACUATION WARNING", "NORMAL"]
        ]

        sites = []
        for feature in filtered:
            props = feature.get("properties", {})
            geom = feature.get("geometry", {})
            coords = geom.get("coordinates", [0, 0])

            site = {
                "name": props.get("ZoneName") or props.get("ExternalIdentifier", "Unknown"),
                "lat": coords[1],
                "lon": coords[0],
            }
            sites.append(site)

        return sites if sites else FALLBACK_SITES
    except (httpx.TimeoutException, httpx.HTTPError, Exception) as e:
        logger.warning(f"CalOES API error: {e}. Using fallback sites.")
        return FALLBACK_SITES


def osrm_route_url(lat1: float, lon1: float, lat2: float, lon2: float,
                   base_url: str | None = None) -> str:
    base_url = (base_url or os.getenv("OSRM_BASE_URL") or "https://router.project-osrm.org").rstrip("/")
    return f"{base_url}/route/v1/driving/{lon1},{lat1};{lon2},{lat2}"


async def query_osrm(
    client: httpx.AsyncClient,
    lat1: float, lon1: float,
    lat2: float, lon2: float
) -> Optional[Dict[str, float]]:
    url = osrm_route_url(lat1, lon1, lat2, lon2)
    params = {
        "overview": "full", "geometries": "geojson", "steps": "true",
        "annotations": "duration,distance", "alternatives": "true",
    }

    try:
        logger.info(f"OSRM route query: ({lat1}, {lon1}) -> ({lat2}, {lon2})")
        response = await client.get(url, params=params, timeout=5.0)
        response.raise_for_status()
        data = response.json()

        routes = parse_osrm_routes(data)
        if not routes:
            raise ValueError(f"OSRM error: {data.get('code')}")
        return {**routes[0], "alternatives": routes[1:], "source": "osrm"}
    except Exception as e:
        logger.warning(f"OSRM error: {e}. Using fallback calculation.")
        distance_km = haversine(lat1, lon1, lat2, lon2)
        return {
            "duration_hours": (distance_km * 1.35) / 60.0,
            "distance_km": distance_km,
            "source": "fallback", "geometry": None,
            "segment_durations_seconds": None, "segment_distances_meters": None,
            "quality": "unverified_estimate", "alternatives": [],
        }


def parse_osrm_routes(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Normalize route geometry and per-edge annotations without inventing data."""
    if data.get("code") != "Ok":
        return []
    parsed = []
    for index, route in enumerate(data.get("routes", [])):
        geometry = route.get("geometry")
        if (not isinstance(geometry, dict) or geometry.get("type") != "LineString"
                or not isinstance(geometry.get("coordinates"), list)
                or len(geometry["coordinates"]) < 2):
            continue
        legs = route.get("legs") or []
        annotation = legs[0].get("annotation", {}) if legs else {}
        durations = annotation.get("duration")
        distances = annotation.get("distance")
        if not isinstance(durations, list) or not isinstance(distances, list):
            durations = distances = None
        parsed.append({
            "route_id": f"osrm-{index}",
            "duration_hours": float(route.get("duration", 0)) / 3600.0,
            "distance_km": float(route.get("distance", 0)) / 1000.0,
            "geometry": geometry,
            "segment_durations_seconds": durations,
            "segment_distances_meters": distances,
            "quality": ("assessed" if durations and distances
                        and len(durations) == len(geometry["coordinates"]) - 1
                        and len(distances) == len(geometry["coordinates"]) - 1 else "partial"),
        })
    return parsed


def destination_is_confirmed(site: Dict[str, Any], required_head: int,
                            now: datetime, max_age_hours: float = 6) -> bool:
    """Only trust a receiving site's explicit, recent acceptance and capacity record."""
    if site.get("availability_confirmed") is not True:
        return False
    try:
        accepted = int(site["accepted_head"])
        checked_at = datetime.fromisoformat(site["verified_at"].replace("Z", "+00:00"))
        checked_at = checked_at.astimezone(timezone.utc) if checked_at.tzinfo else checked_at.replace(tzinfo=timezone.utc)
        current = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
    except (KeyError, TypeError, ValueError):
        return False
    age_hours = (current - checked_at).total_seconds() / 3600
    return accepted >= required_head and 0 <= age_hours <= max_age_hours


def fire_eta_margin() -> float:
    """Read the conservative fire-arrival margin, defaulting safely to 0.8."""
    try:
        value = float(os.getenv("LIVESTOCK_FIRE_ETA_MARGIN", "0.8"))
        return value if 0 < value <= 1 else 0.8
    except (TypeError, ValueError):
        return 0.8


def loading_delay_minutes() -> float:
    """Configured livestock loading duration, defaulting to a 30-minute estimate."""
    try:
        value = float(os.getenv("LIVESTOCK_LOADING_MINUTES", "30"))
        return value if math.isfinite(value) and value >= 0 else 30.0
    except (TypeError, ValueError):
        return 30.0


def unloading_delay_minutes() -> float:
    """Configured shelter unloading duration, defaulting to a 30-minute estimate."""
    try:
        value = float(os.getenv("LIVESTOCK_UNLOADING_MINUTES", "30"))
        return value if math.isfinite(value) and value >= 0 else 30.0
    except (TypeError, ValueError):
        return 30.0


def smoke_forecast_window_end(departure: datetime, max_route_hours: float,
                              loading_minutes: float) -> datetime:
    return departure + timedelta(hours=max_route_hours, minutes=loading_minutes)


async def explain_multitrip_value_locally(
    schedule: Dict[str, Any], crop_tasks: List[Dict[str, Any]], client: Any = None
) -> Optional[str]:
    """Optionally ask a configured local Ollama model for display-only rationale."""
    base_url = os.getenv("OLLAMA_BASE_URL", "").strip().rstrip("/")
    model = os.getenv("OLLAMA_MODEL", "").strip()
    if not base_url or not model:
        return None
    prompt_data = {
        "trip_summary": schedule.get("summary", {}),
        "trips": [{key: trip.get(key) for key in (
            "vehicle_id", "pen_id", "animal_count", "destination", "depart_at", "arrive_at",
            "round_trip_possible", "deadline_margin_minutes",
        )} for trip in schedule.get("trips", [])],
        "unscheduled_by_pen": schedule.get("unscheduled_by_pen", {}),
        "crop_tasks": crop_tasks,
    }
    prompt = (
        "Assess whether the additional scheduled trip(s) appear worthwhile in at most 120 words. "
        "Use only supplied animal counts, vehicle-hours, deadlines, and crop facts. State 'unclear' "
        "when costs, crop duration, or resource overlap are unknown. Do not infer shared staff/equipment, "
        "change trip assignments, infer route safety, or claim work or movement occurred.\n\n"
        + json.dumps(prompt_data, separators=(",", ":"))
    )
    try:
        if client is not None:
            response = await client.post(
                f"{base_url}/api/generate", json={"model": model, "prompt": prompt, "stream": False}, timeout=15.0
            )
            response.raise_for_status()
            body = response.json()
        else:
            async with httpx.AsyncClient(timeout=15.0) as local_client:
                response = await local_client.post(
                    f"{base_url}/api/generate", json={"model": model, "prompt": prompt, "stream": False}
                )
                response.raise_for_status()
                body = response.json()
        text = body.get("response") if isinstance(body, dict) else None
        return text.strip()[:1200] if isinstance(text, str) and text.strip() else None
    except Exception as exc:
        logger.info("Local schedule explanation unavailable: %s", exc)
        return None


def load_latest_crop_output(crop_dir: Path) -> Dict[str, Any]:
    """Load the newest full crop result, ignoring ERPC and raw-response artifacts."""
    outputs = [path for path in crop_dir.glob("output_*.json")
               if path.name != "output_to_erpc.json" and "raw" not in path.name]
    for path in sorted(outputs, key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read crop result %s: %s", path.name, exc)
            continue
        if isinstance(data, dict):
            return data
    return {}


def reserve_site_capacity(remaining_by_site: Dict[tuple, int], site: Dict[str, Any],
                          required_head: int) -> bool:
    key = (round(float(site["lat"]), 5), round(float(site["lon"]), 5))
    remaining = remaining_by_site.get(key, 0)
    if remaining < int(required_head):
        return False
    remaining_by_site[key] = remaining - int(required_head)
    return True


async def compute_routes_for_pen(
    client: httpx.AsyncClient,
    pen: Dict[str, Any],
    sites: List[Dict[str, Any]],
    pen_routes: Dict[str, List[Dict]],
    farm_centroid: Dict[str, float],
) -> None:
    pen_id = pen["pen_id"]
    pen_lat, pen_lon = pen["centroid"]["lat"], pen["centroid"]["lon"]
    approach_result = await query_osrm(
        client, farm_centroid["lat"], farm_centroid["lon"], pen_lat, pen_lon
    )

    results = await asyncio.gather(
        *[
            asyncio.gather(
                query_osrm(client, pen_lat, pen_lon, site["lat"], site["lon"]),
                query_osrm(client, site["lat"], site["lon"], farm_centroid["lat"], farm_centroid["lon"]),
            ) for site in sites
        ],
        return_exceptions=True
    )
    routes = []
    for site, result in zip(sites, results):
        if result and not isinstance(result, Exception):
            outbound_result, return_result = result
            outbound_candidates = [outbound_result, *(outbound_result.get("alternatives", []) if outbound_result else [])]
            approach_candidates = [approach_result, *(approach_result.get("alternatives", []) if approach_result else [])]
            return_candidates = [return_result, *(return_result.get("alternatives", []) if return_result else [])]
            for candidate in outbound_candidates:
                if not candidate:
                    continue
                for approach in approach_candidates:
                    for return_route in return_candidates:
                        approach = approach or {}
                        return_route = return_route or {}
                        routes.append({
                            "site": site,
                            "duration_hours": candidate["duration_hours"],
                            "distance_km": candidate["distance_km"],
                            "source": candidate.get("source", outbound_result.get("source") if outbound_result else None),
                            "route_id": candidate.get("route_id"),
                            "geometry": candidate.get("geometry"),
                            "segment_durations_seconds": candidate.get("segment_durations_seconds"),
                            "segment_distances_meters": candidate.get("segment_distances_meters"),
                            "quality": candidate.get("quality", "partial"),
                            "approach_duration_hours": approach.get("duration_hours"),
                            "approach_distance_km": approach.get("distance_km"),
                            "approach_source": approach.get("source", approach_result.get("source") if approach_result else None),
                            "approach_route_id": approach.get("route_id"),
                            "approach_geometry": approach.get("geometry"),
                            "approach_segment_durations_seconds": approach.get("segment_durations_seconds"),
                            "approach_segment_distances_meters": approach.get("segment_distances_meters"),
                            "return_duration_hours": return_route.get("duration_hours"),
                            "return_distance_km": return_route.get("distance_km"),
                            "return_source": return_route.get("source", return_result.get("source") if return_result else None),
                            "return_route_id": return_route.get("route_id"),
                            "return_geometry": return_route.get("geometry"),
                            "return_segment_durations_seconds": return_route.get("segment_durations_seconds"),
                            "return_segment_distances_meters": return_route.get("segment_distances_meters"),
                        })
    pen_routes[pen_id] = routes


async def compute_pool_route(
    client: httpx.AsyncClient,
    farm_lat: float, farm_lon: float,
    neighbor_lat: float, neighbor_lon: float,
    site_lat: float, site_lon: float
) -> Optional[Dict[str, float]]:
    leg1 = await query_osrm(client, farm_lat, farm_lon, neighbor_lat, neighbor_lon)
    if not leg1:
        return None

    leg2 = await query_osrm(client, neighbor_lat, neighbor_lon, site_lat, site_lon)
    if not leg2:
        return None

    return {
        "total_duration_hours": leg1["duration_hours"] + leg2["duration_hours"],
        "total_distance_km": leg1["distance_km"] + leg2["distance_km"]
    }


async def compute_return_trip(
    client: httpx.AsyncClient,
    site_lat: float, site_lon: float,
    neighbor_lat: float, neighbor_lon: float,
    neighbor_site_lat: float, neighbor_site_lon: float
) -> Optional[Dict[str, float]]:
    leg1 = await query_osrm(client, site_lat, site_lon, neighbor_lat, neighbor_lon)
    if not leg1:
        return None

    leg2 = await query_osrm(client, neighbor_lat, neighbor_lon, neighbor_site_lat, neighbor_site_lon)
    if not leg2:
        return None

    return {
        "total_duration_hours": leg1["duration_hours"] + leg2["duration_hours"],
        "total_distance_km": leg1["distance_km"] + leg2["distance_km"]
    }


async def main():
    with open(LIVESTOCK_DIR / "status.json") as f:
        status = json.load(f)

    with open(LIVESTOCK_DIR / "wake_up_packet.json") as f:
        wake_up = json.load(f)

    with open(LIVESTOCK_DIR / "farm_profile.json") as f:
        farm = json.load(f)

    # Refuse stale or cross-farm snapshots before querying routes or recommending movement.
    forecaster_dir = str(LIVESTOCK_DIR.parent / "forecaster")
    if forecaster_dir not in sys.path:
        sys.path.insert(0, forecaster_dir)
    from livestock_packet import validate_livestock_packet
    packet_errors = validate_livestock_packet(
        wake_up, farm, datetime.now(timezone.utc)
    )
    if status.get("run_id") != wake_up.get("run_id"):
        packet_errors.append({"field": "run_id", "reason": "status and wake-up packet are from different runs"})
    if packet_errors:
        total_head = sum(int(pen.get("count", 0)) for pen in farm.get("pens", []))
        total_value = sum(
            int(pen.get("count", 0)) * float(pen.get("avg_market_value_usd", USDA_NASS_PRICES.get(pen.get("species"), 0)))
            for pen in farm.get("pens", [])
        )
        blockers = [f"{e['field']}: {e['reason']}" for e in packet_errors]
        invalid_status = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": status.get("run_id"), "farm_id": farm.get("farm_id"),
            "threat_level": status.get("threat_level", "UNKNOWN"),
            "phase": "review_required", "assessment_status": "invalid",
            "pens": [{
                "pen_id": pen["pen_id"], "lat": pen["centroid"]["lat"], "lon": pen["centroid"]["lon"],
                "species": pen["species"], "decision": "review_required",
                "movement_status": "pending_review", "assessment_status": "invalid",
                "decision_reason": "; ".join(blockers), "route_duration_hours": None,
                "assigned_evac_site": None, "smoke_assessment": {"status": "unknown"},
            } for pen in farm.get("pens", [])],
            "blockers": blockers, "animals_at_risk_usd": total_value,
            "animals_moved_count": 0, "animals_remaining_count": total_head,
            "animals_planned_to_move_count": 0,
        }
        invalid_erpc = {
            "sender": "livestock_agent", "timestamp": invalid_status["timestamp"],
            "run_id": status.get("run_id"), "farm_id": farm.get("farm_id"),
            "evacuation_status": "review_required", "animal_valuation_at_risk": total_value,
            "transport_costs_usd": 0, "blockers": blockers,
            "forecasting_requests": [{"type": "valid_forecaster_snapshot", "reasons": blockers}],
            "cost_optimization": {"total_animals_at_risk": total_head,
                                  "animals_can_evacuate": 0, "value_can_save_usd": 0,
                                  "potential_loss_usd": total_value, "loss_percentage": 100.0},
        }
        for filename, value in (("livestock_status.json", invalid_status), ("erpc_message.json", invalid_erpc)):
            temp = LIVESTOCK_DIR / (filename + ".tmp")
            temp.write_text(json.dumps(value, indent=2))
            temp.replace(LIVESTOCK_DIR / filename)
        logger.error("Refusing livestock movement recommendations: %s", "; ".join(blockers))
        return

    # Fetch live USDA NASS prices
    async with httpx.AsyncClient() as price_client:
        usda_prices = await fetch_usda_prices(price_client)
        logger.info(f"Using USDA NASS prices: {usda_prices}")

    with open(LIVESTOCK_DIR / "neighboring_farms.json") as f:
        neighbors_data = json.load(f)

    async with httpx.AsyncClient() as client:
        farm_centroid = farm["centroid"]
        # CalOES evacuation-zone polygons are not verified shelter locations.
        # Use only existing fallback candidate destinations and keep acceptance unknown.
        configured_sites = farm.get("evacuation_sites")
        if isinstance(configured_sites, list) and configured_sites:
            evac_sites = [site for site in configured_sites if isinstance(site, dict)
                          and site.get("name") and isinstance(site.get("lat"), (int, float))
                          and isinstance(site.get("lon"), (int, float))
                          and -90 <= site["lat"] <= 90 and -180 <= site["lon"] <= 180]
        else:
            evac_sites = [dict(site) for site in FALLBACK_SITES]

        # Update pen values with USDA prices
        for pen in farm["pens"]:
            species = pen["species"]
            pen["avg_market_value_usd"] = usda_prices.get(species, USDA_NASS_PRICES.get(species, 1000))
            logger.info(f"Pen {pen['pen_id']} ({species}): ${pen['avg_market_value_usd']}/head")

        pen_routes: Dict[str, List[Dict]] = {}
        await asyncio.gather(
            *[compute_routes_for_pen(client, pen, evac_sites, pen_routes, farm_centroid) for pen in farm["pens"]]
        )

        from data_sources.smoke import fetch_smoke_forecast
        all_routes = [route for routes in pen_routes.values() for route in routes]
        route_geometries = [
            route.get(key) for route in all_routes
            for key in ("approach_geometry", "geometry", "return_geometry") if route.get(key)
        ]
        coordinates = [coord for geometry in route_geometries for coord in geometry.get("coordinates", [])]
        departure = datetime.now(timezone.utc)
        loading_minutes = loading_delay_minutes()
        unloading_minutes = unloading_delay_minutes()
        if coordinates:
            route_bounds = {
                "min_lon": max(-125, min(point[0] for point in coordinates) - 0.1),
                "max_lon": min(-66, max(point[0] for point in coordinates) + 0.1),
                "min_lat": max(24, min(point[1] for point in coordinates) - 0.1),
                "max_lat": min(50, max(point[1] for point in coordinates) + 0.1),
            }
            max_cycle_hours = max((
                float(route.get("approach_duration_hours") or 0)
                + float(route.get("duration_hours") or 0)
                + float(route.get("return_duration_hours") or 0)
                + (loading_minutes + unloading_minutes) / 60
                for route in all_routes
            ), default=0.0)
            # Conservatively allow every possible load to run sequentially on one
            # vehicle; this avoids leaving later trips outside fetched smoke data.
            schedule_horizon_hours = max_cycle_hours * max(1, len(farm.get("pens", [])))
            known_etas = [float(item["time_to_impact_hours"]) for item in wake_up.get("affected_zones", [])
                          if item.get("time_to_impact_hours") is not None]
            if known_etas:
                schedule_horizon_hours = min(schedule_horizon_hours, max(known_etas) * fire_eta_margin())
            smoke_forecast = await asyncio.to_thread(
                fetch_smoke_forecast, route_bounds, departure,
                smoke_forecast_window_end(departure, schedule_horizon_hours, 0),
            )
        else:
            smoke_forecast = {"status": "unavailable", "reason": "no verified route geometries", "source": "NOAA HRRR"}
        from smoke_exposure import assess_fire_hazard, assess_route, rank_routes
        spread_horizons = wake_up.get("spread_prediction", {}).get("horizons", {})
        fire_present = bool(wake_up.get("fire_data") or status.get("nearest_fire"))
        now_for_destination = datetime.now(timezone.utc)
        pen_counts = {p["pen_id"]: p.get("count", 0) for p in farm.get("pens", [])}
        for pen_id, routes in pen_routes.items():
            for route in routes:
                route["approach_smoke_assessment"] = assess_route(
                    {"duration_hours": route.get("approach_duration_hours"), "geometry": route.get("approach_geometry"),
                     "segment_durations_seconds": route.get("approach_segment_durations_seconds")},
                    smoke_forecast, departure,
                )
                route["approach_fire_hazard"] = assess_fire_hazard(
                    {"duration_hours": route.get("approach_duration_hours"), "geometry": route.get("approach_geometry"),
                     "segment_durations_seconds": route.get("approach_segment_durations_seconds")},
                    spread_horizons, fire_present,
                )
                approach_delay = float(route.get("approach_duration_hours") or 0) * 60 + loading_minutes
                route["smoke_assessment"] = assess_route(route, smoke_forecast, departure, approach_delay)
                route["fire_hazard"] = assess_fire_hazard(route, spread_horizons, fire_present, approach_delay)
                return_leg = {
                    "duration_hours": route.get("return_duration_hours"), "geometry": route.get("return_geometry"),
                    "segment_durations_seconds": route.get("return_segment_durations_seconds"),
                }
                return_delay = approach_delay + float(route.get("duration_hours") or 0) * 60 + unloading_minutes
                route["return_smoke_assessment"] = assess_route(
                    return_leg, smoke_forecast, departure, return_delay,
                )
                route["return_fire_hazard"] = assess_fire_hazard(
                    return_leg, spread_horizons, fire_present, return_delay,
                )
                route["destination_verified"] = destination_is_confirmed(
                    route["site"], 1, now_for_destination
                )

        threat_level = status["threat_level"]
        if threat_level == "WATCH":
            phase = "pre_stage"
        elif threat_level == "WARNING":
            phase = "movement_orders"
        elif threat_level == "CRITICAL":
            phase = "execute_movement"
        elif threat_level == "EMERGENCY":
            phase = "real_time_tracking"
        else:
            phase = "unknown"

        pen_impacts = {item.get("pen_id"): item.get("time_to_impact_hours")
                       for item in wake_up.get("affected_zones", []) if item.get("pen_id")}

        pens_output = []
        total_animals_at_risk = sum(
            int(pen.get("count", 0)) * float(pen.get("avg_market_value_usd", 0))
            for pen in farm.get("pens", [])
        )
        animals_moved = 0
        animals_remaining = 0

        ordered_pens = sorted(
            farm["pens"],
            key=lambda item: compute_priority_score(item, pen_impacts.get(item["pen_id"])),
            reverse=True,
        )
        for pen in ordered_pens:
            pen_id = pen["pen_id"]
            time_to_impact = pen_impacts.get(pen_id)
            priority_score = compute_priority_score(pen, time_to_impact)

            if pen_id not in pen_routes or not pen_routes[pen_id]:
                pens_output.append({
                    "pen_id": pen_id,
                    "count": int(pen.get("count", 0)),
                    "lat": pen["centroid"]["lat"],
                    "lon": pen["centroid"]["lon"],
                    "species": pen["species"],
                    "priority_score": priority_score,
                    "decision": "review_required",
                    "assessment_status": "unknown",
                    "decision_reason": "No verified road routes available",
                    "assigned_evac_site": None,
                    "route_duration_hours": None,
                    "route_source": None,
                    "smoke_penalty_applied": False,
                    "assigned_vehicle": None,
                    "movement_status": "pending_review",
                    "route_options": [],
                    "time_to_impact_hours": time_to_impact,
                })
                animals_remaining += pen["count"]
                continue

            routes = pen_routes[pen_id]
            eta_margin = fire_eta_margin()
            ranking = rank_routes(routes, impact_eta_hours=time_to_impact,
                                  loading_minutes=loading_minutes, deadline_margin=eta_margin)
            best = ranking["ranked"][0] if ranking["ranked"] else None
            preview = best or min(routes, key=lambda route: route["duration_hours"])
            best_duration = preview["duration_hours"]
            best_site = {
                "name": preview["site"]["name"], "lat": preview["site"]["lat"],
                "lon": preview["site"]["lon"], "distance_km": preview["distance_km"],
            } if best else None
            if best:
                decision = "evacuate"
                assessment_status = "assessed"
                decision_reason = "Verified route and destination; trip capacity is assigned by the vehicle schedule"
                movement_status = "planned"
            else:
                decision = "review_required"
                assessment_status = "partial" if any(r.get("smoke_assessment", {}).get("status") == "assessed" for r in routes) else "unknown"
                reasons = [r.get("rejection_reason") or r.get("review_reason") for r in ranking["rejected"] + ranking["review_required"]]
                decision_reason = "; ".join(dict.fromkeys(reason for reason in reasons if reason)) or "Route assessment incomplete"
                movement_status = "pending_review"
                animals_remaining += pen["count"]

            pens_output.append({
                "pen_id": pen_id,
                "count": int(pen.get("count", 0)),
                "lat": pen["centroid"]["lat"],
                "lon": pen["centroid"]["lon"],
                "species": pen["species"],
                "priority_score": priority_score,
                "decision": decision,
                "splittable": pen.get("splittable") is True,
                "time_to_impact_hours": time_to_impact,
                "decision_reason": decision_reason,
                "assigned_evac_site": best_site,
                "candidate_evac_site": {"name": preview["site"]["name"], "lat": preview["site"]["lat"],
                                        "lon": preview["site"]["lon"], "distance_km": preview["distance_km"]},
                "route_duration_hours": best_duration,
                "route_source": preview.get("source"),
                "route_geometry": preview.get("geometry"),
                "smoke_penalty_applied": False,
                "assessment_status": assessment_status,
                "smoke_assessment": preview.get("smoke_assessment"),
                "route_options": [{"route_id": r.get("route_id"), "site": r["site"],
                                   "duration_hours": r["duration_hours"], "distance_km": r["distance_km"],
                                   "source": r["source"], "geometry": r.get("geometry"),
                                   "approach_duration_hours": r.get("approach_duration_hours"),
                                   "approach_distance_km": r.get("approach_distance_km"),
                                   "approach_source": r.get("approach_source"),
                                   "approach_geometry": r.get("approach_geometry"),
                                   "approach_segment_durations_seconds": r.get("approach_segment_durations_seconds"),
                                   "approach_segment_distances_meters": r.get("approach_segment_distances_meters"),
                                   "approach_smoke_assessment": r.get("approach_smoke_assessment"),
                                   "approach_fire_hazard": r.get("approach_fire_hazard"),
                                   "return_duration_hours": r.get("return_duration_hours"),
                                   "return_distance_km": r.get("return_distance_km"),
                                   "return_source": r.get("return_source"),
                                   "return_geometry": r.get("return_geometry"),
                                   "return_segment_durations_seconds": r.get("return_segment_durations_seconds"),
                                   "return_segment_distances_meters": r.get("return_segment_distances_meters"),
                                   "return_smoke_assessment": r.get("return_smoke_assessment"),
                                   "return_fire_hazard": r.get("return_fire_hazard"),
                                   "smoke_assessment": r.get("smoke_assessment"),
                                   "fire_hazard": r.get("fire_hazard"),
                                   "rejection_reason": r.get("rejection_reason"),
                                   "review_reason": r.get("review_reason")}
                                  for r in ranking["ranked"] + ranking["rejected"] + ranking["review_required"]],
                "assigned_vehicle": None,
                "movement_status": movement_status
            })

        pens_output.sort(key=lambda p: p["priority_score"], reverse=True)

        # Match crop advice to this farm and forecast run before presenting it beside transport options.
        crop_dir = LIVESTOCK_DIR.parent / "crop_agent"
        crop_output = load_latest_crop_output(crop_dir)
        try:
            farm_fields = json.loads((crop_dir / "farm_fields.json").read_text())
        except (OSError, json.JSONDecodeError):
            farm_fields = {"fields": []}
        valid_field_ids = {str(field.get("field_id")) for field in farm_fields.get("fields", []) if field.get("field_id")}
        crop_tasks = normalize_crop_tasks(crop_output, farm.get("farm_id"), status.get("run_id"), valid_field_ids)

        def assess_leg_at(route: Dict[str, Any], departure_offset_minutes: float) -> tuple[Dict[str, Any], Dict[str, Any]]:
            duration = route.get("duration_hours")
            if duration == 0:
                return ({"status": "clear", "intersects": False}, {"status": "assessed", "integrated_exposure_ug_h_m3": 0.0})
            if not route.get("geometry"):
                return ({"status": "unknown", "intersects": None}, {"status": "unknown", "reason": "verified route geometry unavailable"})
            offset = max(0.0, departure_offset_minutes)
            smoke = assess_route(route, smoke_forecast, departure, offset)
            fire = assess_fire_hazard(route, spread_horizons, fire_present, offset)
            return fire, smoke

        def offset_minutes(when: datetime) -> float:
            return (when - departure).total_seconds() / 60

        def assess_scheduled_route(option: Dict[str, Any], positioning_departure: datetime,
                                   outbound_departure: datetime, return_departure: datetime) -> Dict[str, Any]:
            approach = {"duration_hours": option.get("approach_duration_hours"),
                        "geometry": option.get("approach_geometry"),
                        "segment_durations_seconds": option.get("approach_segment_durations_seconds")}
            outbound = {"duration_hours": option.get("duration_hours"), "geometry": option.get("geometry"),
                        "segment_durations_seconds": option.get("segment_durations_seconds")}
            return_leg = {"duration_hours": option.get("return_duration_hours"),
                          "geometry": option.get("return_geometry"),
                          "segment_durations_seconds": option.get("return_segment_durations_seconds")}
            approach_fire, approach_smoke = assess_leg_at(
                approach, offset_minutes(positioning_departure))
            outbound_fire, outbound_smoke = assess_leg_at(
                outbound, offset_minutes(outbound_departure))
            return_fire, return_smoke = assess_leg_at(
                return_leg, offset_minutes(return_departure))
            return {"approach_fire": approach_fire, "approach_smoke": approach_smoke,
                    "outbound_fire": outbound_fire, "outbound_smoke": outbound_smoke,
                    "return_fire": return_fire, "return_smoke": return_smoke}

        transport_schedule = schedule_evacuation_trips(
            pens_output, farm, departure,
            config={"loading_minutes": loading_minutes,
                    "unloading_minutes": unloading_minutes,
                    "fire_eta_margin": fire_eta_margin()},
            crop_tasks=crop_tasks,
            route_assessor=assess_scheduled_route,
        )
        animals_moved = 0
        for pen_result in pens_output:
            pen_id = pen_result["pen_id"]
            count = int(pen_result.get("count", 0))
            allocated = int(transport_schedule["scheduled_by_pen"].get(pen_id, 0))
            pen_result["animals_planned_to_move"] = allocated
            pen_trips = [trip for trip in transport_schedule["trips"] if trip["pen_id"] == pen_id]
            pen_result["scheduled_trip_ids"] = [trip["trip_id"] for trip in pen_trips]
            if pen_trips:
                pen_result["assigned_vehicle"] = pen_trips[0]["vehicle_id"]
                pen_result["assigned_evac_site"] = pen_trips[0]["destination"]
                animals_moved += allocated
            if allocated == 0 and pen_result["decision"] == "evacuate":
                pen_result["decision"] = "review_required"
                pen_result["assessment_status"] = "partial"
                pen_result["movement_status"] = "pending_review"
                pen_result["decision_reason"] = transport_schedule["unscheduled_by_pen"].get(
                    pen_id, "No deadline-feasible vehicle trip was scheduled")
            elif 0 < allocated < count:
                pen_result["decision"] = "partial_plan"
                pen_result["assessment_status"] = "partial"
                pen_result["movement_status"] = "planned_partial"
                pen_result["decision_reason"] = (
                    f"Scheduled {allocated} of {count} animals; "
                    + transport_schedule["unscheduled_by_pen"].get(pen_id, "remaining animals need review")
                )

        total_value_at_risk = sum(int(p.get("count", 0)) * float(p.get("avg_market_value_usd", 0))
                                  for p in farm.get("pens", []))
        value_saved = sum(int(result.get("animals_planned_to_move", 0))
                          * float(next((pen.get("avg_market_value_usd", 0) for pen in farm["pens"]
                                       if pen.get("pen_id") == result["pen_id"]), 0))
                          for result in pens_output)
        potential_loss = max(0.0, total_value_at_risk - value_saved)
        animals_at_risk = sum(int(p.get("count", 0)) for p in farm.get("pens", []))
        animals_remaining = max(0, animals_at_risk - animals_moved)
        evacuation_summary = {
            "total_animals_at_risk": animals_at_risk,
            "animals_can_evacuate": animals_moved,
            "value_can_save_usd": round(value_saved, 2),
            "potential_loss_usd": round(potential_loss, 2),
            "loss_percentage": round(potential_loss / total_value_at_risk * 100, 1) if total_value_at_risk else 0,
            "vehicle_hours": transport_schedule["summary"]["vehicle_hours"],
        }
        evac_optimization = {
            "transport_capacity": {
                "vehicles": int(farm.get("infrastructure", {}).get("available_trailers", 0)),
                "capacity_per_vehicle": int(farm.get("infrastructure", {}).get("vehicle_capacity_head", 0)),
                "trips_available": len(transport_schedule["trips"]),
                "total_capacity": animals_moved,
            },
            "trips": transport_schedule["trips"],
            "unscheduled_by_pen": transport_schedule["unscheduled_by_pen"],
            "crop_tasks": crop_tasks,
            "summary": evacuation_summary,
        }
        multi_trip_rationale = await explain_multitrip_value_locally(transport_schedule, crop_tasks)
        if multi_trip_rationale:
            evac_optimization["multi_trip_rationale"] = multi_trip_rationale
        logger.info("Transport schedule covers %s animals in %s trips", animals_moved, len(transport_schedule["trips"]))

        run_blockers = list(dict.fromkeys(
            pen["decision_reason"] for pen in pens_output
            if pen.get("decision") == "review_required" and pen.get("decision_reason")
        ))
        forecast_requests = []
        if smoke_forecast.get("status") != "available":
            forecast_requests.append({"type": "HRRR_near_surface_smoke",
                                     "reason": smoke_forecast.get("reason", "forecast coverage incomplete")})
        if any(p.get("time_to_impact_hours") is None for p in wake_up.get("affected_zones", []) if p.get("pen_id")):
            forecast_requests.append({"type": "per_pen_fire_arrival", "reason": "per-pen fire arrival estimate is unknown"})

        # Neighbor pooling — haversine only (no OSRM calls) for speed
        pool_blocks = []
        our_species = {pen["species"] for pen in farm["pens"]}
        our_best_sites = {
            p["assigned_evac_site"]["name"]: p["assigned_evac_site"]
            for p in pens_output if p["assigned_evac_site"]
        }
        for neighbor in neighbors_data.get("farms", []):
            neighbor_id = neighbor["farm_id"]
            neighbor_centroid = neighbor["centroid"]
            if not our_species & set(neighbor.get("species", [])):
                continue
            distance_km = haversine(farm_centroid["lat"], farm_centroid["lon"],
                                    neighbor_centroid["lat"], neighbor_centroid["lon"])
            if distance_km > 20:
                continue
            neighbor_best_sites = set()
            for n_pen in neighbor.get("pens", []):
                n_lat, n_lon = n_pen["centroid"]["lat"], n_pen["centroid"]["lon"]
                best = min(evac_sites, key=lambda s: haversine(n_lat, n_lon, s["lat"], s["lon"]))
                neighbor_best_sites.add(best["name"])
            for common_site in set(our_best_sites.keys()) & neighbor_best_sites:
                site_obj = our_best_sites[common_site]
                our_total = sum(p["route_duration_hours"] for p in pens_output
                                if p["assigned_evac_site"] and p["assigned_evac_site"]["name"] == common_site
                                and p["route_duration_hours"])
                detour_km = (haversine(farm_centroid["lat"], farm_centroid["lon"],
                                       neighbor_centroid["lat"], neighbor_centroid["lon"]) +
                             haversine(neighbor_centroid["lat"], neighbor_centroid["lon"],
                                       site_obj["lat"], site_obj["lon"]))
                pool_duration = (detour_km * 1.35) / 60.0
                time_saved_minutes = (our_total - pool_duration) * 60
                if time_saved_minutes < 15:
                    continue
                pool_blocks.append({
                    "pool_id": f"pool_{farm['farm_id']}_{neighbor_id}_{common_site.replace(' ', '_')}",
                    "farms_involved": [farm["farm_id"], neighbor_id],
                    "shared_route_summary": f"{farm['farm_id']} -> {neighbor_id} -> {common_site}",
                    "time_saved_minutes": round(time_saved_minutes, 1),
                    "return_trip_assist": False,
                    "estimated_cost_sharing_usd": round(time_saved_minutes * 5, 2)
                })

        livestock_status = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": status.get("run_id"), "farm_id": farm.get("farm_id"),
            "assessment_status": (
                "assessed" if pens_output and all(p.get("assessment_status") == "assessed" for p in pens_output)
                else "partial" if any(p.get("assessment_status") in {"assessed", "partial"} for p in pens_output)
                else "unknown"
            ),
            "planning_assumptions": {"fire_eta_margin": fire_eta_margin(),
                                     "loading_delay_minutes": loading_minutes,
                                     "unloading_delay_minutes": unloading_minutes,
                                     "pen_splitting_default": False,
                                     "scheduler": "deadline-slack then earliest safe vehicle finish",
                                     "movement_confirmed": False},
            "smoke_forecast": {key: value for key, value in smoke_forecast.items() if key != "layers"},
            "threat_level": threat_level,
            "phase": phase,
            "pens": pens_output,
            "trip_schedule": transport_schedule["trips"],
            "crop_tasks_considered": crop_tasks,
            "multi_trip_rationale": multi_trip_rationale,
            "transport_pool": pool_blocks,
            "evacuation_optimization": evac_optimization,
            "blockers": run_blockers,
            "animals_at_risk_usd": total_animals_at_risk,
            "animals_moved_count": 0,
            "animals_planned_to_move_count": animals_moved,
            "animals_remaining_count": max(0, animals_at_risk - animals_moved)
        }

        erpc_message = {
            "sender": "livestock_agent",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": status.get("run_id"), "farm_id": farm.get("farm_id"),
            "evacuation_status": "planned" if animals_moved > 0 else "review_required",
            "animal_valuation_at_risk": total_animals_at_risk,
            "transport_costs_usd": round(animals_moved * 15, 2),
            "blockers": run_blockers,
            "transport_pool": pool_blocks,
            "cost_optimization": evac_optimization["summary"],
            "trip_schedule_summary": {"trip_count": len(transport_schedule["trips"]),
                                      "animals_scheduled": animals_moved,
                                      "vehicle_hours": transport_schedule["summary"]["vehicle_hours"]},
            "crop_messages": crop_tasks,
            "multi_trip_rationale": multi_trip_rationale,
            "forecasting_requests": forecast_requests,
        }

        for filename, value in (("livestock_status.json", livestock_status), ("erpc_message.json", erpc_message)):
            temp = LIVESTOCK_DIR / (filename + ".tmp")
            temp.write_text(json.dumps(value, indent=2))
            temp.replace(LIVESTOCK_DIR / filename)

        logger.info(f"Cycle complete. Animals at risk: {total_animals_at_risk} USD. Planned: {animals_moved}, Remaining: {animals_remaining}")


if __name__ == "__main__":
    asyncio.run(main())
