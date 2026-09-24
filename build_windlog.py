#!/usr/bin/env python3
"""Build windlog.html from a Garmin activities.json export."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

STRIP_RE = re.compile(
    r"\b(windsurfing|windfoiling|windsurf|banglenčių sportas|banglenciu sportas)\b",
    re.IGNORECASE,
)
RACE_RE = re.compile(r"^(mindauginių maratonas)\b", re.IGNORECASE)
NORMALIZE = {
    "rumsiskes": "Rumšiškės",
    "pastrevys": "Pastrėvys",
    "seimenos seniunija": "Seimena",
    "arco (garda)": "Arco",
    "elektrenai iqfoil, varžybos": "Elektrenai",
    "elektrenai wing, varžybos": "Elektrenai",
}


def is_wind(activity: dict) -> bool:
    type_key = activity.get("activityType", {}).get("typeKey")
    name = activity.get("activityName") or ""
    return type_key == "windsurfing_v2" or bool(re.search(r"wind", name, re.IGNORECASE))


def location_of(name: str) -> str:
    cleaned = STRIP_RE.sub("", name or "").strip()
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -")
    cleaned = re.sub(r"\(.*?\)", "", cleaned).strip()
    if not cleaned:
        return "Unspecified"
    race_match = RACE_RE.match(cleaned)
    if race_match:
        return race_match.group(1).title()
    return NORMALIZE.get(cleaned.lower(), cleaned)


def extract_sessions(activities: list[dict]) -> list[dict]:
    sessions = []
    for activity in activities:
        if not is_wind(activity):
            continue
        duration = activity.get("duration") or 0
        if duration < 60:
            continue
        start = activity.get("startTimeLocal") or ""
        sessions.append(
            {
                "id": activity["activityId"],
                "date": start[:10],
                "time": start[11:16],
                "loc": location_of(activity.get("activityName") or ""),
                "dist": round((activity.get("distance") or 0) / 1000, 2),
                "dur": round((activity.get("duration") or 0) / 60, 1),
                "avg": round((activity.get("averageSpeed") or 0) * 3.6, 1),
                "max": round((activity.get("maxSpeed") or 0) * 3.6, 1),
                "cal": round(activity.get("calories") or 0),
            }
        )
    sessions.sort(key=lambda s: (s["date"], s["time"]))
    return sessions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--activities", type=Path, default=Path("activities.json"))
    parser.add_argument("--template", type=Path, default=Path("windlog_template.html"))
    parser.add_argument("--output", type=Path, default=Path("windlog.html"))
    args = parser.parse_args()

    activities = json.loads(args.activities.read_text(encoding="utf-8"))
    sessions = extract_sessions(activities)

    template = args.template.read_text(encoding="utf-8")
    sessions_json = json.dumps(sessions, ensure_ascii=False, separators=(",", ":"))
    html = template.replace("__SESSIONS_JSON__", sessions_json)
    args.output.write_text(html, encoding="utf-8")

    print(f"{len(sessions)} sessions -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
