"""Pure functions mapping raw Garmin payloads to DB row dicts. No I/O here."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from build_windlog import is_wind, location_of


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _drop_none(d: dict[str, Any]) -> dict[str, Any]:
    """Only include keys with a real value, so a partial upsert never
    overwrites a column already set by a different, earlier-called source
    with NULL."""
    return {k: v for k, v in d.items() if v is not None}


def activity_to_session_row(activity: dict[str, Any]) -> dict[str, Any]:
    activity_type = activity.get("activityType", {}).get("typeKey", "unknown")
    start_local = activity.get("startTimeLocal") or ""
    return {
        "activity_id": activity["activityId"],
        "activity_type": activity_type,
        "activity_name": activity.get("activityName"),
        "start_time_local": start_local,
        "start_date": start_local[:10],
        "duration_s": activity.get("duration"),
        "distance_m": activity.get("distance"),
        "elevation_gain_m": activity.get("elevationGain"),
        "avg_speed_mps": activity.get("averageSpeed"),
        "max_speed_mps": activity.get("maxSpeed"),
        "avg_hr": activity.get("averageHR"),
        "max_hr": activity.get("maxHR"),
        "calories": activity.get("calories"),
        "location": location_of(activity.get("activityName") or "") if is_wind(activity) else None,
        "raw_json": json.dumps(activity, ensure_ascii=False),
        "updated_at": _now(),
    }


def exercise_set_rows(sets_payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for i, s in enumerate(sets_payload.get("exerciseSets") or []):
        exercises = s.get("exercises") or []
        top = exercises[0] if exercises else {}
        weight = s.get("weight")
        rows.append(
            {
                "set_index": i,
                "exercise_name": top.get("name") or top.get("category"),
                "category": top.get("category"),
                "reps": s.get("repetitionCount"),
                "weight_kg": (weight / 1000) if weight is not None else None,
                "duration_s": s.get("duration"),
                "raw_json": json.dumps(s, ensure_ascii=False),
            }
        )
    return rows


def sleep_row(entry: dict[str, Any]) -> dict[str, Any]:
    v = entry.get("values") or {}
    return _drop_none(
        {
            "sleep_duration_s": v.get("totalSleepTimeInSeconds"),
            "sleep_deep_s": v.get("deepTime"),
            "sleep_light_s": v.get("lightTime"),
            "sleep_rem_s": v.get("remTime"),
            "sleep_awake_s": v.get("awakeTime"),
            "sleep_score": v.get("sleepScore"),
            "hrv_last_night_avg": v.get("avgOvernightHrv"),
            "hrv_status": v.get("hrvStatus"),
            "rhr": v.get("restingHeartRate"),
        }
    )


def hrv_row(entry: dict[str, Any]) -> dict[str, Any]:
    return _drop_none(
        {
            "hrv_last_night_avg": entry.get("lastNightAvg"),
            "hrv_status": entry.get("status"),
        }
    )


def rhr_row(entry: dict[str, Any]) -> dict[str, Any]:
    return _drop_none({"rhr": entry.get("value")})


def stress_and_battery_row(payload: dict[str, Any]) -> dict[str, Any]:
    levels = [
        v[2]
        for v in (payload.get("bodyBatteryValuesArray") or [])
        if len(v) > 2 and v[2] is not None
    ]
    return _drop_none(
        {
            "stress_avg": payload.get("avgStressLevel"),
            "body_battery_max": max(levels) if levels else None,
            "body_battery_min": min(levels) if levels else None,
        }
    )
