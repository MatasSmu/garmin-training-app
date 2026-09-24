#!/usr/bin/env python3
"""Run the analysis engine on every file in samples/ (or the given paths),
print a per-file summary and sanity-check the results.

    ./venv/bin/python scripts/analyze_samples.py [files...]

Exit status is non-zero if any file fails to analyse or fails a check.
"""

from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.analysis.loader import load_file  # noqa: E402
from app.analysis.pipeline import analyze  # noqa: E402


def find_nans(obj, path="") -> list[str]:
    bad = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            bad += find_nans(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:50000]):
            bad += find_nans(v, f"{path}[{i}]")
            if len(bad) > 5:
                break
    elif isinstance(obj, float) and not math.isfinite(obj):
        bad.append(path)
    return bad


def checks(r: dict) -> list[str]:
    s = r["summary"]
    problems = []
    nan_paths = find_nans({k: v for k, v in r.items() if k != "series"})
    if nan_paths:
        problems.append(f"non-finite values at {nan_paths[:3]}")
    try:
        json.dumps(r, allow_nan=False)
    except ValueError as e:
        problems.append(f"not JSON-safe: {e}")
    if not (0 <= s["wind_deg"] < 360):
        problems.append(f"wind {s['wind_deg']} out of range")
    if s["max_kn"] is None or not (3 < s["max_kn"] < 50):
        problems.append(f"implausible max speed {s['max_kn']}")
    if s["upwind_twa"] is not None and not (25 <= s["upwind_twa"] <= 75):
        problems.append(f"implausible upwind TWA {s['upwind_twa']} (wind likely wrong)")
    if s["downwind_twa"] is not None and not (105 <= s["downwind_twa"] <= 175):
        problems.append(f"implausible downwind TWA {s['downwind_twa']}")
    mans_per_h = (s["tacks"] + s["gybes"]) / max(s["moving_s"] / 3600, 0.1)
    if mans_per_h > 120:
        problems.append(f"{mans_per_h:.0f} maneuvers/hour looks like noise")
    if s["moving_s"] > 1200 and s["tacks"] + s["gybes"] == 0:
        problems.append("no maneuvers detected in a long session")
    n = len(r["series"]["t"])
    for k, v in r["series"].items():
        if len(v) != n:
            problems.append(f"series {k} length {len(v)} != {n}")
    return problems


def main(argv: list[str]) -> int:
    files = [Path(a) for a in argv] or sorted((ROOT / "samples").glob("*.[fg][ip][tx]"))
    failed = 0
    hdr = (f"{'file':38} {'sport':9} {'dur':>6} {'km':>6} {'wind':>5} {'conf':6} {'upTWA':>5} {'dnTWA':>5} "
           f"{'T':>3} {'G':>3} {'T ok%':>5} {'G ok%':>5} {'on%':>5} {'max':>5} {'500m':>5} {'upVMG':>5} {'legs':>4} {'s':>4}")
    print(hdr)
    print("-" * len(hdr))
    for f in files:
        t0 = time.time()
        try:
            r = analyze(load_file(f))
        except Exception as e:  # noqa: BLE001
            print(f"{f.name:38} ERROR {type(e).__name__}: {e}")
            failed += 1
            continue
        s = r["summary"]
        fmt = lambda v, nd=0: "-" if v is None else f"{v:.{nd}f}"  # noqa: E731
        print(f"{f.name[:38]:38} {s['sport']:9} {s['duration_s']/60:6.0f} {s['distance_m']/1000:6.1f} "
              f"{s['wind_deg']:5.0f} {s['wind_confidence']:6} {fmt(s['upwind_twa']):>5} {fmt(s['downwind_twa']):>5} "
              f"{s['tacks']:3d} {s['gybes']:3d} {fmt(s['tack_success_pct']):>5} {fmt(s['gybe_success_pct']):>5} "
              f"{fmt(s['state_pct']):>5} {fmt(s['max_kn'],1):>5} {fmt(s['best_500m_kn'],1):>5} "
              f"{fmt(s['upwind_vmg_kn'],1):>5} {s['legs']:4d} {time.time()-t0:4.1f}")
        probs = checks(r)
        prior = r["wind"].get("prior_deg")
        if prior is not None:
            print(f"{'':38}   device wind hint {prior:.0f}, auto {r['wind']['auto_deg']:.0f}, "
                  f"modes {r['wind']['modes']}")
        for p in probs:
            print(f"{'':38}   CHECK FAILED: {p}")
        failed += bool(probs)
    print(f"\n{len(files) - failed}/{len(files)} files OK")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
