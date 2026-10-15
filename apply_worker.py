#!/usr/bin/env python3.13
"""
Subprocess worker: reads [{uuid, lat, lon, filename}] from stdin as JSON,
applies GPS via osascript directly (bypasses photoscript initialization issues
in subprocess context), writes per-photo results to stdout as newline-delimited JSON.
"""
import json, os, subprocess, sys, time


def _osascript(script: str) -> tuple[str, str, int]:
    """Run an AppleScript snippet via osascript. Returns (stdout, stderr, returncode)."""
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True
    )
    return result.stdout.strip(), result.stderr.strip(), result.returncode


def _activate_photos():
    _osascript('tell application "Photos" to activate')
    time.sleep(3)


def _get_location(uuid: str) -> tuple | None:
    """Returns (lat, lon) tuple, None if no location, raises on error."""
    out, err, rc = _osascript(
        f'tell application "Photos" to return location of media item id "{uuid}"'
    )
    if rc != 0:
        raise RuntimeError(err or out or f"osascript error rc={rc}")
    if not out or out == "missing value":
        return None
    # osascript returns comma-separated: "37.7749, -122.4194"
    parts = [p.strip() for p in out.split(",")]
    if len(parts) >= 2:
        try:
            return (float(parts[0]), float(parts[1]))
        except ValueError:
            return None
    return None


def _set_location(uuid: str, lat: float, lon: float) -> None:
    """Set GPS on the photo. Raises on error."""
    out, err, rc = _osascript(
        f'tell application "Photos" to set location of media item id "{uuid}" to {{{lat}, {lon}}}'
    )
    if rc != 0:
        raise RuntimeError(err or out or f"osascript error rc={rc}")


def _apply_location(item: dict) -> dict:
    uuid  = item["uuid"]
    lat   = item["lat"]
    lon   = item["lon"]
    fname = item.get("filename", uuid)

    for attempt in range(3):
        try:
            existing = _get_location(uuid)
            if existing is not None:
                return {"uuid": uuid, "filename": fname, "outcome": "skipped"}
            _set_location(uuid, lat, lon)
            return {"uuid": uuid, "filename": fname, "outcome": "applied",
                    "lat": lat, "lon": lon}
        except RuntimeError as e:
            err_str = str(e)
            is_invalid = "Invalid photo id" in err_str or "-1728" in err_str
            if is_invalid and attempt < 2:
                print(json.dumps({"log": f"  Retry {attempt+2}/3 for {fname} — re-activating Photos…"}), flush=True)
                _activate_photos()
            elif is_invalid:
                # Completely invisible to Photos.app (e.g. syndicated iMessage photos)
                return {"uuid": uuid, "filename": fname, "outcome": "not_writable",
                        "error_msg": f"Not accessible via Photos.app: {err_str}"}
            else:
                return {"uuid": uuid, "filename": fname, "outcome": "error",
                        "error_msg": err_str}

    return {"uuid": uuid, "filename": fname, "outcome": "error",
            "error_msg": "Exhausted retries"}


def main():
    items = json.load(sys.stdin)
    print(json.dumps({"log": f"apply_worker pid={os.getpid()}, items={len(items)}, exe={sys.executable}"}), flush=True)

    _activate_photos()
    print(json.dumps({"log": "Photos.app activated — starting…"}), flush=True)

    for item in items:
        result = _apply_location(item)
        print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
