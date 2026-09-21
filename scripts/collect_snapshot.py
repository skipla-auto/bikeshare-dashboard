#!/usr/bin/env python3
"""Fetch current GBFS status for the tracked stations, append one NDJSON
snapshot line per station to data/history.ndjson, and push the result.

Meant to be invoked hourly by a scheduled agent. Standard library only
(no pip install) so it runs unmodified in any cloud sandbox.
"""
import json
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TRACKED_STATIONS_PATH = REPO_ROOT / "data" / "tracked-stations.json"
HISTORY_PATH = REPO_ROOT / "data" / "history.ndjson"

GBFS_STATUS_URL = "https://api-public.odpt.org/api/v4/gbfs/docomo-cycle-tokyo/station_status.json"
JST = timezone(timedelta(hours=9))


def weather_url_for(stations):
    lats = ",".join(str(s["lat"]) for s in stations)
    lons = ",".join(str(s["lon"]) for s in stations)
    return (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lats}&longitude={lons}"
        "&current=temperature_2m,precipitation,weather_code"
        "&timezone=Asia%2FTokyo"
    )

# 2026 (令和8年) national holidays, per the National Astronomical
# Observatory of Japan's official calendar. Extend this set each
# December once next year's dates are published.
JP_HOLIDAYS_2026 = {
    "2026-01-01", "2026-01-12", "2026-02-11", "2026-02-23",
    "2026-03-20", "2026-04-29", "2026-05-03", "2026-05-04",
    "2026-05-05", "2026-05-06", "2026-07-20", "2026-08-11",
    "2026-09-21", "2026-09-22", "2026-09-23", "2026-10-12",
    "2026-11-03", "2026-11-23",
}


def fetch_json(url):
    with urllib.request.urlopen(url, timeout=20) as res:
        return json.load(res)


def run_git(*args):
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, check=True,
        capture_output=True, text=True,
    )


def ensure_git_identity():
    result = subprocess.run(
        ["git", "config", "user.email"], cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if not result.stdout.strip():
        run_git("config", "user.email", "bikeshare-collector@users.noreply.github.com")
        run_git("config", "user.name", "bikeshare-collector")


def main():
    tracked = json.loads(TRACKED_STATIONS_PATH.read_text())
    tracked_by_id = {s["station_id"]: s for s in tracked}

    status = fetch_json(GBFS_STATUS_URL)
    status_by_id = {s["station_id"]: s for s in status["data"]["stations"]}

    try:
        weather_results = fetch_json(weather_url_for(tracked))
        # Open-Meteo returns a plain object (not a list) when given a single
        # coordinate pair; normalize so indexing by position always works.
        if isinstance(weather_results, dict):
            weather_results = [weather_results]
        weather_by_id = {
            s["station_id"]: weather_results[i]["current"]
            for i, s in enumerate(tracked)
        }
    except Exception as exc:  # weather is a nice-to-have, never block the snapshot
        print(f"weather fetch failed, continuing without it: {exc}", file=sys.stderr)
        weather_by_id = {}

    now_utc = datetime.now(timezone.utc)
    now_jst = now_utc.astimezone(JST)
    date_jst = now_jst.strftime("%Y-%m-%d")

    lines = []
    for station_id, info in tracked_by_id.items():
        name = info["name"]
        s = status_by_id.get(station_id)
        if s is None:
            print(f"station {station_id} ({name}) missing from GBFS response, skipping", file=sys.stderr)
            continue
        weather = weather_by_id.get(station_id, {})
        lines.append(json.dumps({
            "ts": now_utc.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "date_jst": date_jst,
            "hour_jst": now_jst.hour,
            "weekday_jst": now_jst.weekday(),  # 0=Mon .. 6=Sun
            "is_weekend": now_jst.weekday() >= 5,
            "is_holiday": date_jst in JP_HOLIDAYS_2026,
            "station_id": station_id,
            "name": name,
            "bikes_available": s["num_bikes_available"],
            "docks_available": s["num_docks_available"],
            "is_renting": s["is_renting"],
            "weather_code": weather.get("weather_code"),
            "precip_mm": weather.get("precipitation"),
            "temp_c": weather.get("temperature_2m"),
        }, ensure_ascii=False))

    if not lines:
        print("no tracked stations found in GBFS response; nothing written", file=sys.stderr)
        return 1

    with HISTORY_PATH.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    ensure_git_identity()
    run_git("add", "data/history.ndjson")
    diff = subprocess.run(
        ["git", "diff", "--cached", "--quiet"], cwd=REPO_ROOT,
    )
    if diff.returncode == 0:
        print("no changes to commit")
        return 0

    run_git("commit", "-m", f"snapshot {now_utc.strftime('%Y-%m-%dT%H:%MZ')}")
    run_git("push")
    print(f"committed {len(lines)} station snapshots for {now_utc.isoformat(timespec='seconds')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
