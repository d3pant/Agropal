"""Simplified Rothermel fire spread model with Anderson ellipse.

References:
  Rothermel, R.C. (1972). A mathematical model for predicting fire spread
    in wildland fuels. USDA Forest Service Research Paper INT-115.
  Anderson, H.E. (1983). Predicting wind-driven wild land fire size and shape.
    USDA Forest Service Research Paper INT-305.
  Alexander, M.E. (1985). Estimating the length-to-breadth ratio of elliptical
    forest fire patterns. Proc. 8th Conf. Fire and Forest Meteorology, 287–304.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Rate of Spread
# ---------------------------------------------------------------------------

def _fuel_moisture_factor(soil_moisture: float) -> float:
    """Convert volumetric soil moisture (m³/m³) to a fuel moisture multiplier.

    Soil moisture ranges roughly 0.02 (extreme drought) to 0.40 (saturated).
    Returns 1.0 for bone-dry conditions, approaching 0.05 for saturated soil.
    """
    # Normalise to 0-1 over the 0.02–0.40 range
    normalised = max(0.0, min(1.0, (soil_moisture - 0.02) / 0.38))
    return max(0.05, 1.0 - 0.95 * normalised)


def _wind_factor(wind_kmh: float) -> float:
    """Wind multiplier on head-fire rate of spread (Rothermel simplified).

    Uses a power-law relationship calibrated for SoCal chaparral.
    """
    return 1.0 + 0.075 * (wind_kmh ** 1.2)


def head_rate_of_spread(wind_kmh: float, soil_moisture: float, frp_mw: float = 0.0) -> float:
    """Compute head-fire rate of spread in km/h.

    Args:
        wind_kmh:      Wind speed at 10 m height (km/h)
        soil_moisture: Volumetric soil moisture (m³/m³)
        frp_mw:        Fire Radiative Power from FIRMS (MW) — boosts spread for intense fires

    Returns km/h.
    """
    base_ros = 0.12  # km/h, SoCal chaparral at rest (no wind, average moisture)
    ros = base_ros * _wind_factor(wind_kmh) * _fuel_moisture_factor(soil_moisture)

    # FRP intensity bonus: very large fires create their own convection column
    if frp_mw > 0:
        frp_factor = 1.0 + 0.002 * min(frp_mw, 500)
        ros *= frp_factor

    return round(ros, 4)


def back_rate_of_spread(head_ros: float, wind_kmh: float) -> float:
    """Back-fire (upwind) rate of spread — much slower than head fire.

    Uses Alexander (1985) head-to-back ratio for chaparral.
    """
    lb_ratio = 1.0 + 0.0012 * (wind_kmh ** 1.7)
    hb_ratio = (lb_ratio + math.sqrt(lb_ratio ** 2 - 1)) / 1.0  # simplified
    hb_ratio = max(1.5, hb_ratio)
    return round(head_ros / hb_ratio, 4)


# ---------------------------------------------------------------------------
# Ellipse geometry
# ---------------------------------------------------------------------------

@dataclass
class SpreadEllipse:
    center_lat: float
    center_lon: float
    semi_major_km: float   # along wind direction (head + back combined)
    semi_minor_km: float   # perpendicular to wind
    wind_direction_deg: float  # meteorological convention
    head_km: float         # distance from ignition to head perimeter
    back_km: float         # distance from ignition to back perimeter
    hours: float


def compute_ellipse(
    fire_lat: float,
    fire_lon: float,
    wind_direction_deg: float,
    wind_kmh: float,
    soil_moisture: float,
    frp_mw: float,
    hours: float,
) -> SpreadEllipse:
    """Compute the predicted fire perimeter ellipse after `hours` hours.

    The ellipse is elongated in the downwind direction. The fire origin sits
    at the upwind focus, not the ellipse centre.
    """
    head_ros = head_rate_of_spread(wind_kmh, soil_moisture, frp_mw)
    back_ros = back_rate_of_spread(head_ros, wind_kmh)

    head_km = head_ros * hours       # distance fire travels downwind
    back_km = back_ros * hours       # distance fire travels upwind
    flank_km = math.sqrt(head_km * back_km)  # geometric mean — flank spread

    semi_major = (head_km + back_km) / 2
    semi_minor = flank_km

    # Ellipse centre is offset from the fire origin toward the head
    offset_km = (head_km - back_km) / 2

    # Fire spreads downwind — opposite of meteorological wind origin direction
    spread_bearing_deg = (wind_direction_deg + 180) % 360
    bearing_rad = math.radians(spread_bearing_deg)
    dlat = (offset_km / 111.0) * math.cos(bearing_rad)
    dlon = (offset_km / (111.0 * math.cos(math.radians(fire_lat)))) * math.sin(bearing_rad)

    return SpreadEllipse(
        center_lat=fire_lat + dlat,
        center_lon=fire_lon + dlon,
        semi_major_km=semi_major,
        semi_minor_km=semi_minor,
        wind_direction_deg=wind_direction_deg,
        head_km=head_km,
        back_km=back_km,
        hours=hours,
    )


def ellipse_to_geojson_polygon(e: SpreadEllipse, n_points: int = 64) -> dict:
    """Convert a SpreadEllipse to a GeoJSON Polygon.

    The ellipse is rotated so its major axis aligns with the wind direction.
    """
    coords = []
    # Fire spreads downwind (away from wind source). Meteorological wind dir is
    # where wind comes FROM, so spread direction = wind_direction + 180.
    spread_deg = (e.wind_direction_deg + 180) % 360
    math_angle_rad = math.radians(90 - spread_deg)

    cos_a = math.cos(math_angle_rad)
    sin_a = math.sin(math_angle_rad)

    for i in range(n_points + 1):
        theta = 2 * math.pi * i / n_points
        # Point on axis-aligned ellipse
        x_km = e.semi_major_km * math.cos(theta)
        y_km = e.semi_minor_km * math.sin(theta)
        # Rotate by wind direction
        rx_km = x_km * cos_a - y_km * sin_a
        ry_km = x_km * sin_a + y_km * cos_a
        # Convert km offset to lat/lon
        lat = e.center_lat + ry_km / 111.0
        lon = e.center_lon + rx_km / (111.0 * math.cos(math.radians(e.center_lat)))
        coords.append([round(lon, 6), round(lat, 6)])

    return {"type": "Polygon", "coordinates": [coords]}


# ---------------------------------------------------------------------------
# Time-to-impact
# ---------------------------------------------------------------------------

def time_to_impact(
    fire_lat: float, fire_lon: float,
    target_lat: float, target_lon: float,
    wind_direction_deg: float,
    wind_kmh: float,
    soil_moisture: float,
    frp_mw: float,
) -> dict:
    """Estimate when (if) a fire will reach a target location.

    Returns dict with:
        hours           : estimated hours to impact (None if not threatened)
        bearing_to_target: bearing from fire to target (degrees)
        angular_diff    : degrees between wind direction and bearing to target
        threatened      : bool — is the target in the fire's path?
    """
    # Bearing from fire to target
    dlat = math.radians(target_lat - fire_lat)
    dlon = math.radians(target_lon - fire_lon)
    lat1 = math.radians(fire_lat)
    lat2 = math.radians(target_lat)
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    bearing = (math.degrees(math.atan2(x, y)) + 360) % 360

    # Wind direction is meteorological (FROM); the fire advances downwind.
    downwind_bearing = (wind_direction_deg + 180) % 360
    angular_diff = abs((downwind_bearing - bearing + 180) % 360 - 180)

    # Distance from fire to target
    R = 6371.0
    phi1, phi2 = math.radians(fire_lat), math.radians(target_lat)
    dphi = phi2 - phi1
    dlambda = math.radians(target_lon - fire_lon)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    distance_km = R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

    # Effective ROS in direction of target (cosine projection from head ROS)
    head_ros = head_rate_of_spread(wind_kmh, soil_moisture, frp_mw)
    back_ros  = back_rate_of_spread(head_ros, wind_kmh)

    angle_rad = math.radians(angular_diff)
    if angular_diff <= 90:
        # In the head-fire half — interpolate between head and flank
        flank_ros = math.sqrt(head_ros * back_ros)
        effective_ros = flank_ros + (head_ros - flank_ros) * math.cos(angle_rad)
    else:
        # In the back-fire half
        flank_ros = math.sqrt(head_ros * back_ros)
        effective_ros = back_ros + (flank_ros - back_ros) * math.cos(math.pi - angle_rad)

    effective_ros = max(effective_ros, 0.001)
    hours = distance_km / effective_ros

    # Target is "threatened" if fire is moving broadly toward it (within 90°)
    threatened = angular_diff <= 90

    return {
        "distance_km": round(distance_km, 2),
        "bearing_to_target_deg": round(bearing, 1),
        "wind_direction_deg": round(wind_direction_deg, 1),
        "angular_diff_deg": round(angular_diff, 1),
        "threatened": threatened,
        "head_ros_kmh": head_ros,
        "effective_ros_kmh": round(effective_ros, 4),
        "hours": round(hours, 1) if threatened else None,
        "hours_worst_case": round(distance_km / max(head_ros, 0.001), 1),
    }
