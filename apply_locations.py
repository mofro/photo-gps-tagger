#!/usr/bin/env python3
"""
Apply approved GPS locations from the review tool's JSON export.

Usage:
  python3.13 apply_locations.py approved_2004-05-01_to_2004-05-31.json

Reads a JSON file produced by the review tool's Export button:
  [{"uuid": "...", "lat": ..., "lon": ..., "approved": true, ...}, ...]

Only records with "approved": true are applied. Skips photos that already have
a location or that appear in a prior applied-UUIDs log for the same date range.

Writes:
  applied_2004-05-01_to_2004-05-31.txt  — one UUID per line, for dedup on re-run
"""

import json
import sys
from datetime import datetime
from pathlib import Path

try:
    import photoscript
except ImportError:
    sys.exit("photoscript not found — run: pip3.13 install photoscript")


def load_applied_log(log_path: Path) -> set:
    if not log_path.exists():
        return set()
    uuids = set()
    for line in log_path.read_text().splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            uuids.add(stripped)
    return uuids


def derive_log_path(json_path: Path) -> Path:
    stem = json_path.stem.replace("approved_", "applied_", 1)
    return json_path.parent / f"{stem}.txt"


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    json_path = Path(sys.argv[1])
    if not json_path.exists():
        sys.exit(f"File not found: {json_path}")

    records = json.loads(json_path.read_text())
    approved = [r for r in records if r.get("approved")]

    if not approved:
        print("No approved records in export — nothing to do.")
        sys.exit(0)

    log_path    = derive_log_path(json_path)
    already     = load_applied_log(log_path)
    to_apply    = [r for r in approved if r["uuid"] not in already]

    print(f"Export:   {len(records)} total, {len(approved)} approved")
    print(f"Skip log: {log_path.name}  ({len(already)} already applied)")
    print(f"To apply: {len(to_apply)}")

    if not to_apply:
        print("All approved records already applied — nothing to do.")
        sys.exit(0)

    # Ensure Photos.app is open and settled before firing AppleScript
    print("Opening Photos.app and waiting for it to be ready...")
    lib = photoscript.PhotosLibrary()
    lib.activate()
    import time
    time.sleep(10)
    print("Ready.\n")

    applied   = []
    skipped   = []
    errors    = []

    for i, r in enumerate(to_apply, 1):
        uuid  = r["uuid"]
        lat   = r["lat"]
        lon   = r["lon"]
        fname = r.get("filename", uuid)
        print(f"  [{i:>{len(str(len(to_apply)))}}/{len(to_apply)}]  {fname}  ({lat:.5f}, {lon:.5f})", end="  ")

        try:
            photo = photoscript.Photo(uuid)
            existing = photo.location
            if existing and existing[0] is not None:
                print("skip — already has GPS")
                skipped.append(uuid)
            else:
                photo.location = (lat, lon)
                print("✓")
                applied.append(uuid)
        except Exception as e:
            print(f"ERROR: {e}")
            errors.append((uuid, str(e)))

    # Append newly applied UUIDs to log
    if applied:
        with log_path.open("a") as f:
            if not log_path.exists() or log_path.stat().st_size == 0:
                f.write(f"# Applied locations — {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
                f.write(f"# Source: {json_path.name}\n")
            for uuid in applied:
                f.write(uuid + "\n")

    print(f"\nApplied: {len(applied)}  |  Skipped (had GPS): {len(skipped)}  |  Errors: {len(errors)}")
    if applied:
        print(f"Log updated: {log_path}")
    if errors:
        print("\nErrors:")
        for uuid, msg in errors:
            print(f"  {uuid}: {msg}")


if __name__ == "__main__":
    main()
