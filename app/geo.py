"""Geometry / anti-cheat helpers — ported from the old RunRealm backend.

Pure functions, no I/O. The polygon itself is built in PostGIS (see the
sessions router); this module only does the point-list math needed to decide
whether a route is a valid, honestly-run closed loop.

Speed tiers (km/h), from geo_utils.py:
    CLEAN      <= 20      running / casual cycling
    SUSPICIOUS  20 - 35   fast bike / e-scooter — flagged, not rejected alone
    VEHICLE    > 35       clearly motorised — penalised heavily
    TELEPORT   > 200 m jump in < 3 s — GPS spoof / device glitch
"""
from __future__ import annotations

import math
from datetime import datetime

from app.config import settings

SPEED_CLEAN_MAX = 20.0
SPEED_SUSPICIOUS_MAX = 35.0
TELEPORT_DIST_M = 200.0
TELEPORT_TIME_S = 3.0

LOOP_GAP_RATIO_MAX = 0.15
VEHICLE_RATIO_REJECT = 0.30
SCORE_MIN = 0.40

_EARTH_RADIUS_M = 6_371_000.0


# ── basic geo math ───────────────────────────────────────────
def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance between two WGS-84 points, in metres."""
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlng / 2) ** 2
    )
    return _EARTH_RADIUS_M * 2.0 * math.asin(math.sqrt(a))


def _coerce_dt(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def route_total_distance_m(points: list[dict]) -> float:
    """Sum of consecutive haversine hops. `points` items carry `lat`,`lng`."""
    total = 0.0
    for i in range(len(points) - 1):
        total += haversine_m(
            points[i]["lat"], points[i]["lng"],
            points[i + 1]["lat"], points[i + 1]["lng"],
        )
    return total


# ── loop closure ─────────────────────────────────────────────
def validate_loop(points: list[dict]) -> tuple[bool, str]:
    """Route must return to (near) its start.

    Fails if the start/end gap exceeds ``settings.territory_loop_close_m`` or
    if that gap is more than 15 % of the total route length.
    """
    if len(points) < 2:
        return False, "Route has too few points to form a loop"

    first, last = points[0], points[-1]
    gap_m = haversine_m(first["lat"], first["lng"], last["lat"], last["lng"])
    total_m = route_total_distance_m(points)

    if gap_m > settings.territory_loop_close_m:
        return False, (
            f"Loop not closed — endpoint is {gap_m:.0f} m from start "
            f"(must be within {settings.territory_loop_close_m:.0f} m)"
        )
    if total_m > 0 and (gap_m / total_m) > LOOP_GAP_RATIO_MAX:
        return False, (
            f"Gap too large relative to route: {gap_m / total_m:.0%} "
            f"(max {LOOP_GAP_RATIO_MAX:.0%})"
        )
    return True, "ok"


# ── speed classification ─────────────────────────────────────
def classify_speeds(points: list[dict]) -> dict:
    """Bucket every consecutive segment into a speed tier.

    Uses ``recorded_at`` deltas; segments shorter than 1 s are skipped, big
    jumps in < 3 s are teleports. Returns per-tier counts plus the ratios and
    average / max speed used by :func:`anticheat`.
    """
    counts = {"clean": 0, "suspicious": 0, "vehicle": 0, "teleport": 0, "skip": 0}
    speeds: list[float] = []

    for i in range(1, len(points)):
        p0, p1 = points[i - 1], points[i]
        dist_m = haversine_m(p0["lat"], p0["lng"], p1["lat"], p1["lng"])

        t0 = _coerce_dt(p0.get("recorded_at"))
        t1 = _coerce_dt(p1.get("recorded_at"))
        dt_s = (t1 - t0).total_seconds() if (t0 and t1) else 2.0

        if dist_m > TELEPORT_DIST_M and 0 < dt_s < TELEPORT_TIME_S:
            counts["teleport"] += 1
            continue
        if dt_s < 1.0:
            counts["skip"] += 1
            continue

        kmh = (dist_m / dt_s) * 3.6
        speeds.append(kmh)
        if kmh <= SPEED_CLEAN_MAX:
            counts["clean"] += 1
        elif kmh <= SPEED_SUSPICIOUS_MAX:
            counts["suspicious"] += 1
        else:
            counts["vehicle"] += 1

    measurable = counts["clean"] + counts["suspicious"] + counts["vehicle"]
    vehicle_ratio = counts["vehicle"] / measurable if measurable else 0.0
    suspicious_ratio = counts["suspicious"] / measurable if measurable else 0.0

    return {
        "counts": counts,
        "measurableSegments": measurable,
        "vehicleRatio": round(vehicle_ratio, 4),
        "suspiciousRatio": round(suspicious_ratio, 4),
        "teleports": counts["teleport"],
        "avgKmh": round(sum(speeds) / len(speeds), 2) if speeds else 0.0,
        "maxKmh": round(max(speeds), 2) if speeds else 0.0,
    }


# ── anti-cheat ───────────────────────────────────────────────
def anticheat(points: list[dict]) -> tuple[bool, str, float]:
    """Return ``(ok, reason, score)`` where score is 0-1.

    Reject when: fewer than ``territory_min_points`` points, shorter than
    ``territory_min_distance_m``, vehicle ratio > 0.30, or score < 0.40.
        score = 1 - 0.8*vehicle_ratio - 0.2*suspicious_ratio - 0.1*teleports
    """
    n = len(points)
    if n < settings.territory_min_points:
        return False, (
            f"Too few GPS points: {n} (minimum {settings.territory_min_points})"
        ), 0.0

    total_m = route_total_distance_m(points)
    if total_m < settings.territory_min_distance_m:
        return False, (
            f"Run too short: {total_m:.0f} m "
            f"(minimum {settings.territory_min_distance_m:.0f} m)"
        ), 0.0

    stats = classify_speeds(points)
    vehicle_ratio = stats["vehicleRatio"]
    suspicious_ratio = stats["suspiciousRatio"]
    teleports = stats["teleports"]

    if vehicle_ratio > VEHICLE_RATIO_REJECT:
        return False, (
            f"Vehicle movement detected: {vehicle_ratio:.0%} of GPS segments "
            f"exceed {SPEED_SUSPICIOUS_MAX:.0f} km/h — capture requires running "
            f"or cycling only."
        ), 0.0

    score = 1.0 - 0.8 * vehicle_ratio - 0.2 * suspicious_ratio - 0.1 * teleports
    score = max(0.0, round(score, 3))

    if score < SCORE_MIN:
        return False, (
            f"Too many speed anomalies — validation score {score:.2f} is below "
            f"the minimum {SCORE_MIN:.2f}. Check your GPS signal and try again."
        ), score

    return True, "ok", score
