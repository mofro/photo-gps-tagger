#!/usr/bin/env python3.13
"""
osxphotos Location Review — local web server
  python3.13 server.py
  Opens http://localhost:9174 automatically.
"""

import doltlite  # noqa: F401 — must precede all sqlite3 imports

import base64, io, json, os, queue, re, signal, subprocess, sys, threading
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

try:
    from flask import Flask, Response, jsonify, request, stream_with_context
except ImportError:
    sys.exit("flask not found — run: pip3.13 install flask --break-system-packages")

try:
    import osxphotos
except ImportError:
    sys.exit("osxphotos not found — run: pip3.13 install osxphotos")

try:
    from PIL import Image
except ImportError:
    sys.exit("Pillow not found — run: pip3.13 install Pillow")

try:
    import photoscript
except ImportError:
    sys.exit("photoscript not found — run: pip3.13 install photoscript")

import db as photo_db

PORT         = 9174
THUMB_MAX_PX = 240
PAD_DAYS     = 3

app = Flask(__name__)

# ── Global state ──────────────────────────────────────────────────────────────

_db       = None
_db_ready = threading.Event()
_db_error = None

_state = {
    "status":    "idle",
    "cards":     [],
    "gps_photos":[],
    "from_date": None,
    "to_date":   None,
    "window":    None,
}


def _is_permission_error(exc):
    while exc:
        if isinstance(exc, PermissionError) or getattr(exc, "errno", None) == 1:
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def _load_db():
    global _db, _db_error
    print("Loading Photos library…", flush=True)
    try:
        _db = osxphotos.PhotosDB()
        print("Library ready.", flush=True)
    except Exception as exc:
        _db_error = exc
        print(f"Could not load Photos library: {exc}", flush=True)
        if _is_permission_error(exc):
            print("Grant Full Disk Access to the app launching this server in "
                  "System Settings > Privacy & Security > Full Disk Access, "
                  "then quit and reopen that app.", flush=True)
    finally:
        _db_ready.set()


# ── Shared helpers ────────────────────────────────────────────────────────────

def compute_gps_day_spans(gps_photos):
    by_day = defaultdict(list)
    for p in gps_photos:
        if p.date:
            by_day[p.date.strftime("%Y-%m-%d")].append(p.date)
    return {day: (min(dts), max(dts)) for day, dts in by_day.items()}


def photo_in_any_span(photo, day_spans):
    if not photo.date:
        return False
    dt  = photo.date
    day = dt.strftime("%Y-%m-%d")
    if day not in day_spans:
        return False
    lo, hi = day_spans[day]
    if lo.tzinfo and not dt.tzinfo:
        dt = dt.replace(tzinfo=lo.tzinfo)
    elif dt.tzinfo and not lo.tzinfo:
        lo = lo.replace(tzinfo=dt.tzinfo)
        hi = hi.replace(tzinfo=dt.tzinfo)
    return lo <= dt <= hi


def run_dry_run(from_date, to_date, window):
    cmd = ["/opt/homebrew/bin/osxphotos", "add-locations", "--dry-run", "--verbose",
           "--not-screenshot", "--window", window]
    if from_date: cmd += ["--from-date", from_date]
    if to_date:   cmd += ["--to-date",   to_date]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout + result.stderr


def parse_dry_run(output):
    matches = {}
    pattern = re.compile(
        r"Adding location \(([^,]+),\s*([^)]+)\) to (.+?) \(([A-F0-9-]{36})\) from (.+?) \(([A-F0-9-]{36})\)"
    )
    for line in output.splitlines():
        m = pattern.search(line)
        if m:
            lat, lon, _, target_uuid, source_name, source_uuid = m.groups()
            matches[target_uuid] = {
                "lat":         float(lat.strip()),
                "lon":         float(lon.strip()),
                "source_uuid": source_uuid,
                "source_name": source_name.strip(),
            }
    return matches


def thumb_b64(path_derivatives):
    for path in path_derivatives:
        p = Path(path)
        if p.exists():
            try:
                img = Image.open(p)
                img.thumbnail((THUMB_MAX_PX, THUMB_MAX_PX), Image.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=75)
                return base64.b64encode(buf.getvalue()).decode()
            except Exception:
                continue
    return None


def fmt_delta(d1, d2):
    try:
        s = abs((d1 - d2).total_seconds())
        if s < 60:   return f"{int(s)}s apart"
        if s < 3600: return f"{int(s/60)}m apart"
        return f"{s/3600:.1f}h apart"
    except Exception:
        return ""


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return PAGE_HTML


@app.route("/api/db-stats")
def api_db_stats():
    return jsonify(photo_db.get_db_stats())


@app.route("/api/months")
def api_months():
    _db_ready.wait()
    if _db_error:
        message = str(_db_error)
        if _is_permission_error(_db_error):
            message += (" Grant Full Disk Access to the app launching this server in "
                        "System Settings > Privacy & Security > Full Disk Access, "
                        "then quit and reopen that app.")
        return jsonify({"error": message}), 503
    excluded = photo_db.get_excluded_uuids()
    no_loc = [
        p for p in _db.photos(images=True)
        if (not p.location or p.location[0] is None) and p.date and not p.screenshot
        and p.uuid not in excluded
    ]
    by_month = Counter(p.date.strftime("%Y-%m") for p in no_loc)
    top = sorted(by_month.items(), key=lambda x: -x[1])[:12]
    return jsonify({
        "total":  len(no_loc),
        "months": [{"ym": ym, "count": n} for ym, n in top],
    })


@app.route("/generate", methods=["POST"])
def generate():
    data        = request.json
    from_date   = data.get("from_date")
    to_date     = data.get("to_date")
    window      = data.get("window", "30 min")
    ignore_span = data.get("ignore_span", False)

    _state.update(status="generating", from_date=from_date, to_date=to_date,
                  window=window, cards=[], gps_photos=[])

    q = queue.Queue()

    def run():
        def emit(msg, **kw):
            q.put({"message": msg, **kw})

        try:
            _db_ready.wait()

            # UUIDs the DB says are already applied/archived/rejected
            excluded = photo_db.get_excluded_uuids()

            # Unlocated photos in range
            all_candidates = [
                p for p in _db.photos(images=True)
                if (not p.location or p.location[0] is None)
                and p.date and not p.screenshot
                and p.uuid not in excluded
                and (not from_date or p.date.strftime("%Y-%m-%d") >= from_date)
                and (not to_date   or p.date.strftime("%Y-%m-%d") <= to_date)
            ]
            all_unlocated = all_candidates
            total = len(all_unlocated)
            icloud_count = sum(1 for p in all_unlocated if p.ismissing)
            emit(f"Found {total} unlocated photos in range ({icloud_count} iCloud-only).")

            # Timestamp distribution warning
            if total:
                by_date = Counter(p.date.strftime("%Y-%m-%d") for p in all_unlocated)
                dominated = [(d, n) for d, n in by_date.items() if n / total >= 0.20]
                if dominated:
                    for d, n in dominated:
                        emit(f"⚠️  {d}: {n} photos ({n/total:.0%}) — timestamps may be fabricated", warn=True)
                else:
                    emit("✓ Timestamps look naturally distributed.")

            # GPS photos (padded range)
            pad     = timedelta(days=PAD_DAYS)
            from_dt = datetime.strptime(from_date, "%Y-%m-%d") - pad if from_date else None
            to_dt   = datetime.strptime(to_date,   "%Y-%m-%d") + pad if to_date   else None

            emit("Fetching GPS-tagged photos…")
            gps_photos = [
                p for p in _db.photos(images=True)
                if p.location and p.location[0] is not None and not p.screenshot and p.date
                and (not from_dt or p.date.replace(tzinfo=None) >= from_dt)
                and (not to_dt   or p.date.replace(tzinfo=None) <= to_dt)
            ]
            emit(f"Found {len(gps_photos)} GPS-tagged photos.")

            day_spans = compute_gps_day_spans(gps_photos)
            emit(f"GPS activity spans {len(day_spans)} calendar day(s).")

            _state["gps_photos"] = [
                {
                    "uuid":     p.uuid,
                    "lat":      p.location[0],
                    "lon":      p.location[1],
                    "date":     p.date.strftime("%Y-%m-%d %H:%M:%S") if p.date else "",
                    "filename": p.original_filename or p.filename or p.uuid,
                }
                for p in gps_photos
            ]

            # Algorithm dry-run
            emit(f"Running osxphotos dry-run (window ±{window})…")
            raw          = run_dry_run(from_date, to_date, window)
            algo_matches = parse_dry_run(raw)
            emit(f"Algorithm matched {len(algo_matches)} photos.")

            gps_by_uuid = {p.uuid: p for p in gps_photos}
            if ignore_span:
                in_span = all_unlocated
                emit(f"Day span filter bypassed — showing all {len(in_span)} unlocated photos.")
            else:
                in_span = [p for p in all_unlocated if photo_in_any_span(p, day_spans)]
                emit(f"{len(in_span)} unlocated photos within GPS day spans → building cards…")

            matched, unmatched = [], []

            for idx, p in enumerate(in_span):
                if idx % 20 == 0:
                    emit(f"Building cards… {idx+1}/{len(in_span)}", progress=True)

                tthumb = thumb_b64(p.path_derivatives) if p.path_derivatives else None
                tdate  = p.date.strftime("%Y-%m-%d %H:%M:%S %Z") if p.date else ""
                match  = algo_matches.get(p.uuid)

                if match:
                    src    = gps_by_uuid.get(match["source_uuid"])
                    sthumb = thumb_b64(src.path_derivatives) if src and src.path_derivatives else None
                    sdate  = src.date.strftime("%Y-%m-%d %H:%M:%S %Z") if src and src.date else ""
                    matched.append({
                        "target_uuid":              p.uuid,
                        "target_filename":          p.original_filename or p.filename or p.uuid,
                        "target_date":              tdate,
                        "target_thumb":             tthumb,
                        "has_match":                True,
                        "proposed_lat":             match["lat"],
                        "proposed_lon":             match["lon"],
                        "proposed_source_uuid":     match["source_uuid"],
                        "proposed_source_filename": src.original_filename if src else match["source_name"],
                        "proposed_source_date":     sdate,
                        "proposed_source_thumb":    sthumb,
                        "delta":                    fmt_delta(p.date, src.date) if src and src.date else "",
                    })
                else:
                    unmatched.append({
                        "target_uuid":              p.uuid,
                        "target_filename":          p.original_filename or p.filename or p.uuid,
                        "target_date":              tdate,
                        "target_thumb":             tthumb,
                        "has_match":                False,
                        "proposed_lat":             None,
                        "proposed_lon":             None,
                        "proposed_source_uuid":     None,
                        "proposed_source_filename": None,
                        "proposed_source_date":     None,
                        "proposed_source_thumb":    None,
                        "delta":                    "",
                    })

            _state["cards"]  = matched + unmatched
            _state["status"] = "ready"
            emit(
                f"Ready — {len(matched)} auto-matched + {len(unmatched)} manual-assign cards.", done=True,
            )

        except Exception as e:
            import traceback
            emit(f"Error: {e}\n{traceback.format_exc()}", error=True, done=True)
            _state["status"] = "idle"
        finally:
            q.put(None)

    threading.Thread(target=run, daemon=True).start()

    def stream():
        ok = True
        while True:
            msg = q.get()
            if msg is None:
                break
            if msg.get("error"):
                ok = False
            yield f"data: {json.dumps(msg)}\n\n"
        cards_ready = len(_state.get("cards", [])) > 0
        if ok and not cards_ready:
            yield f'data: {json.dumps({"message": "No photos fell within any GPS day span for this range.", "warn": True})}\n\n'
        yield f'data: {json.dumps({"final": True, "success": ok and cards_ready})}\n\n'

    return Response(stream_with_context(stream()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/cards")
def api_cards():
    return jsonify({
        "cards":       _state["cards"],
        "gps_photos":  _state["gps_photos"],
        "from_date":   _state["from_date"],
        "to_date":     _state["to_date"],
        "window":      _state["window"],
        "n_matched":   sum(1 for c in _state["cards"] if c["has_match"]),
        "n_unmatched": sum(1 for c in _state["cards"] if not c["has_match"]),
    })


@app.route("/apply", methods=["POST"])
def apply_route():
    approved  = request.json   # [{uuid, filename, lat, lon}, …]
    from_date = _state["from_date"] or "unknown"
    to_date   = _state["to_date"]   or "unknown"
    log_path  = Path.home() / "Downloads" / f"applied_{from_date}_to_{to_date}.txt"

    already = set()
    if log_path.exists():
        for line in log_path.read_text().splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                already.add(s)

    to_apply = [r for r in approved if r["uuid"] not in already]
    _state["status"] = "applying"

    q = queue.Queue()

    def run():
        try:
            # photoscript uses NSAppleScript (PyObjC) which requires a runloop.
            # Flask background threads don't have one, so we run the apply step
            # in a subprocess that gets its own main thread + runloop.
            # Use a minimal env — the server's os.environ has been mutated by
            # doltlite (DYLD_INSERT_LIBRARIES and other vars that corrupt
            # Foundation/NSAppleScript in the child). Terminal invocations of
            # apply_worker.py work fine; this env matches that clean context.
            worker_env = {
                "HOME":    os.environ.get("HOME", ""),
                "USER":    os.environ.get("USER", ""),
                "LOGNAME": os.environ.get("LOGNAME", ""),
                "PATH":    os.environ.get("PATH", "/opt/homebrew/bin:/usr/bin:/bin"),
                "TMPDIR":  os.environ.get("TMPDIR", "/tmp"),
                "LANG":    os.environ.get("LANG", "en_US.UTF-8"),
                "SHELL":   os.environ.get("SHELL", "/bin/zsh"),
            }
            worker = Path(__file__).parent / "apply_worker.py"
            proc = subprocess.Popen(
                [sys.executable, str(worker)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=worker_env,
            )
            payload = json.dumps(to_apply)
            proc.stdin.write(payload)
            proc.stdin.close()

            db_results = []
            idx = 0

            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    q.put({"message": line})
                    continue

                if "log" in ev:
                    q.put({"message": ev["log"]})
                    continue

                if "fatal" in ev:
                    q.put({"message": f"Fatal: {ev['fatal']}", "error": True})
                    continue

                idx += 1
                uuid  = ev.get("uuid", "")
                fname = ev.get("filename", uuid)
                lat   = ev.get("lat")
                lon   = ev.get("lon")
                outcome = ev.get("outcome", "error")
                errmsg  = ev.get("error_msg", "")
                n       = len(to_apply)

                if outcome == "applied":
                    q.put({"message": f"[{idx}/{n}] {fname} ✓", "ok": True})
                elif outcome == "skipped":
                    q.put({"message": f"[{idx}/{n}] {fname} — skipped (already has GPS)", "skip": True})
                elif outcome == "not_writable":
                    q.put({"message": f"[{idx}/{n}] {fname} — not writable (archived)", "skip": True})
                else:
                    q.put({"message": f"[{idx}/{n}] {fname} — ERROR: {errmsg}", "error": True})

                db_results.append({"uuid": uuid, "filename": fname, "outcome": outcome,
                                   "lat": lat, "lon": lon, "error_msg": errmsg or None})

            proc.wait()
            stderr_out = proc.stderr.read().strip()
            if stderr_out:
                q.put({"message": f"[worker stderr] {stderr_out}"})

            n_applied      = sum(1 for r in db_results if r["outcome"] == "applied")
            n_errors       = sum(1 for r in db_results if r["outcome"] == "error")
            n_not_writable = sum(1 for r in db_results if r["outcome"] == "not_writable")
            first_err      = next((r["error_msg"] for r in db_results if r["outcome"] == "error"), None)

            # Persist to DoltLite DB (updates app_status → 'applied' for successes)
            photo_db.record_apply_run(from_date, to_date, db_results)

            # Keep human-readable per-batch log in Downloads
            applied_uuids = [r["uuid"] for r in db_results if r["outcome"] == "applied"]
            if applied_uuids:
                with log_path.open("a") as f:
                    if log_path.stat().st_size == 0:
                        f.write(f"# Applied {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
                    for uuid in applied_uuids:
                        f.write(uuid + "\n")

            _state["status"] = "done"
            done_msg = f"Done — {n_applied} applied, {n_errors} errors"
            if n_not_writable:
                done_msg += f", {n_not_writable} archived (not writable via Photos.app)"
            done_msg += "."
            q.put({
                "message": done_msg,
                "summary": {"applied": n_applied, "errors": n_errors,
                            "not_writable": n_not_writable,
                            "log": str(log_path), "first_error": first_err},
                "done":    True,
            })

        except Exception as e:
            import traceback
            q.put({"message": f"Fatal: {e}\n{traceback.format_exc()}", "error": True, "done": True})
            _state["status"] = "idle"
        finally:
            q.put(None)

    threading.Thread(target=run, daemon=True).start()

    def stream():
        while True:
            msg = q.get()
            if msg is None:
                break
            yield f"data: {json.dumps(msg)}\n\n"
        yield 'data: {"final":true}\n\n'

    return Response(stream_with_context(stream()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/sync", methods=["POST"])
def sync():
    """Stream sync progress as SSE. Runs full sync if never synced, else incremental."""
    _db_ready.wait()

    def stream():
        q = queue.Queue()

        def emit(msg):
            q.put({"type": "log", "msg": str(msg)})

        def run():
            try:
                stats = photo_db.get_db_stats()
                if stats.get("last_sync"):
                    result = photo_db.sync_incremental(_db, emit)
                else:
                    result = photo_db.sync_full(_db, emit)
                q.put({"type": "done", "result": result,
                       "stats": photo_db.get_db_stats()})
            except Exception as exc:
                q.put({"type": "error", "msg": str(exc)})

        threading.Thread(target=run, daemon=True).start()

        while True:
            item = q.get()
            yield f"data: {json.dumps(item)}\n\n"
            if item["type"] in ("done", "error"):
                break

    return Response(stream_with_context(stream()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/shutdown", methods=["POST"])
def shutdown():
    def _stop():
        import time; time.sleep(0.4)
        os.kill(os.getpid(), signal.SIGTERM)
    threading.Thread(target=_stop, daemon=True).start()
    return jsonify({"ok": True})


# ── Page HTML ─────────────────────────────────────────────────────────────────

PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Location Review</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<link rel="stylesheet" href="https://unpkg.com/leaflet-control-geocoder@2.4.0/dist/Control.Geocoder.css"/>
<script src="https://unpkg.com/leaflet-control-geocoder@2.4.0/dist/Control.Geocoder.js"></script>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, sans-serif; background: #1a1a1a; color: #e0e0e0; }

/* ── Header ── */
header { background: #111; padding: 10px 20px; border-bottom: 1px solid #333;
         display: flex; align-items: center; gap: 14px; position: sticky; top: 0; z-index: 1000; }
header h1 { font-size: 14px; font-weight: 600; color: #fff; }
.hstatus { font-size: 11px; color: #555; }

/* ── Panels ── */
.panel { display: none; }
.panel.active { display: block; }

/* ── Setup panel ── */
.setup-wrap { max-width: 700px; margin: 60px auto; padding: 0 20px; }
.setup-wrap h2 { font-size: 18px; color: #fff; margin-bottom: 6px; }
.setup-wrap p  { font-size: 12px; color: #666; margin-bottom: 28px; }
.month-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-bottom: 28px; }
.month-btn { background: #242424; border: 1px solid #333; border-radius: 7px;
             padding: 10px 14px; cursor: pointer; text-align: left; color: #ccc; transition: border-color .1s; }
.month-btn:hover { border-color: #555; }
.month-btn .ym  { font-size: 13px; font-weight: 600; color: #fff; }
.month-btn .cnt { font-size: 11px; color: #666; margin-top: 2px; }
.month-btn.selected { border-color: #2563eb; background: #1a2a4a; }
.divider { border: none; border-top: 1px solid #2a2a2a; margin: 20px 0; }
.form-row { display: flex; gap: 10px; align-items: flex-end; margin-bottom: 14px; flex-wrap: wrap; }
.form-group { display: flex; flex-direction: column; gap: 4px; }
label { font-size: 11px; color: #888; }
input[type=text], input[type=date] {
  background: #222; color: #ddd; border: 1px solid #444; border-radius: 5px;
  padding: 7px 10px; font-size: 12px; }
input[type=text]:focus, input[type=date]:focus { outline: none; border-color: #2563eb; }
.total-note { font-size: 11px; color: #555; margin-bottom: 20px; }
.loading-dots::after { content: '…'; animation: dots 1.2s infinite; }
@keyframes dots { 0%,100%{content:'…'} 33%{content:'.'} 66%{content:'..'} }

/* ── Progress panel ── */
.progress-wrap { max-width: 700px; margin: 40px auto; padding: 0 20px; }
.progress-wrap h2 { font-size: 15px; color: #fff; margin-bottom: 16px; }
.log { background: #111; border: 1px solid #2a2a2a; border-radius: 7px;
       padding: 14px 16px; font-family: monospace; font-size: 12px; color: #aaa;
       max-height: 60vh; overflow-y: auto; }
.log-line         { margin-bottom: 4px; }
.log-line.warn    { color: #f59e0b; }
.log-line.error   { color: #ef5350; }
.log-line.ok      { color: #4caf50; }
.log-line.done    { color: #fff; font-weight: 600; margin-top: 8px; }

/* ── Review panel ── */
#map { height: 280px; border-bottom: 1px solid #333; }
.map-wrap { position: relative; }
.map-legend { position: absolute; bottom: 10px; right: 10px; z-index: 500;
              background: #111c; border: 1px solid #444; border-radius: 6px;
              padding: 6px 10px; font-size: 11px; color: #ccc; pointer-events: none; }
.map-legend span { display: inline-block; width: 10px; height: 10px;
                   border-radius: 50%; margin-right: 5px; vertical-align: middle; }
.dot-orange { background: #f59e0b; }
.dot-blue   { background: #38bdf8; }
.dot-manual { background: #a78bfa; }

#assign-banner { display: none; background: #1e3a5f; border-bottom: 1px solid #2563eb;
                 padding: 8px 20px; font-size: 12px; color: #93c5fd;
                 align-items: center; gap: 12px; }
#assign-banner.visible { display: flex; }
#assign-banner b { color: #fff; }
#btn-assign { background: #2563eb; color: #fff; border: none; border-radius: 5px;
              padding: 5px 14px; cursor: pointer; font-size: 12px; }
#btn-assign:hover { background: #1d4ed8; }
#btn-cancel-assign { background: none; border: none; color: #93c5fd; cursor: pointer;
                     font-size: 12px; text-decoration: underline; }

.toolbar { padding: 8px 20px; background: #111; border-bottom: 1px solid #333;
           display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
button { padding: 5px 12px; border-radius: 5px; border: none; cursor: pointer;
         font-size: 12px; font-weight: 500; }
#btn-approve-all  { background: #1a4a2a; color: #4caf50; border: 1px solid #2a6a3a; }
#btn-approve-sel  { background: #1a4a2a; color: #4caf50; border: 1px solid #2a6a3a; opacity: .5; }
#btn-approve-sel.has-sel { opacity: 1; }
#btn-reject-all   { background: #3a1a1a; color: #ef5350; border: 1px solid #5a2a2a; }
#btn-select-page  { background: #2a2a2a; color: #ccc; border: 1px solid #444; }
#btn-deselect     { background: #2a2a2a; color: #ccc; border: 1px solid #444; }
#btn-apply-server { background: #7c3aed; color: #fff; margin-left: auto; }
#btn-apply-server:hover { background: #6d28d9; }
#btn-apply-server:disabled { opacity: .4; cursor: default; }
.toolbar-right { display: flex; align-items: center; gap: 10px; }
.sz-btn { padding: 2px 7px; font-size: 11px; background: #2a2a2a; color: #777;
           border: 1px solid #444; border-radius: 4px; cursor: pointer; }
.sz-btn.active { background: #1a3a5a; color: #60a0d0; border-color: #2563eb; }
#count-label { font-size: 12px; color: #888; }
select { background: #222; color: #ccc; border: 1px solid #444; border-radius: 5px;
         padding: 4px 8px; font-size: 12px; cursor: pointer; }

.filter-bar { padding: 6px 20px; background: #0f0f0f; border-bottom: 1px solid #2a2a2a;
              display: flex; gap: 4px; }
.ftab { background: none; border: 1px solid #333; color: #777; border-radius: 4px;
        padding: 4px 12px; font-size: 11px; }
.ftab.active { background: #333; color: #fff; border-color: #555; }

.pager { display: flex; align-items: center; gap: 8px; padding: 8px 20px;
         background: #161616; border-bottom: 1px solid #2a2a2a; font-size: 12px; color: #888; }
.pager button { background: #222; color: #ccc; border: 1px solid #3a3a3a; padding: 3px 10px; }
.pager button:disabled { opacity: .35; cursor: default; }
.pager button:not(:disabled):hover { background: #333; }
#page-info { min-width: 140px; text-align: center; color: #aaa; font-size: 11px; }
.pager-right { margin-left: auto; display: flex; align-items: center; gap: 6px; font-size: 11px; }
.pager-right input { width: 46px; background: #222; color: #ccc; border: 1px solid #444;
                     border-radius: 4px; padding: 2px 6px; font-size: 11px; }

:root { --thumb-size: 80px; }
.cards { padding: 12px 20px; display: grid;
         grid-template-columns: repeat(2, 1fr); gap: 12px; }
@media (max-width: 860px) { .cards { grid-template-columns: 1fr; } }

.card { background: #242424; border: 1px solid #333; border-radius: 8px;
        overflow: hidden; transition: border-color .12s; cursor: pointer; }
.card.approved   { border-color: #1a7a3a; }
.card.rejected   { border-color: #7a1a1a; opacity: .5; }
.card.overridden { border-color: #7c3aed; }
.card.selected   { outline: 2px solid #38bdf8; outline-offset: 1px; }
.card.highlighted { box-shadow: 0 0 0 2px #f59e0b; }

.card-header { padding: 7px 12px; background: #1e1e1e; display: flex; align-items: center;
               gap: 8px; font-size: 11px; color: #888; border-bottom: 1px solid #2a2a2a;
               user-select: none; }
.card-index  { font-weight: 700; color: #aaa; min-width: 30px; }
.card-coords { font-family: monospace; font-size: 10px; color: #60a0d0; }
.badge { padding: 1px 7px; border-radius: 10px; font-size: 10px; font-weight: 600; }
.badge-auto   { background: #1a3a1a; color: #4caf50; }
.badge-manual { background: #2d1a5f; color: #a78bfa; }
.badge-none   { background: #2a2a2a; color: #666; }
.card-delta { margin-left: auto; font-size: 11px; color: #666; }
.sel-indicator { margin-left: 4px; color: #38bdf8; font-size: 13px; }

.card-body { display: flex; align-items: flex-start; gap: 10px; padding: 8px 12px; }
.photo-side { display: flex; flex-direction: row; align-items: center; gap: 8px; flex: 1; }
.photo-side + .photo-side { border-left: 1px solid #2a2a2a; padding-left: 10px; }
.photo-side img { width: var(--thumb-size); height: var(--thumb-size); object-fit: cover; border-radius: 4px;
                  background: #111; flex-shrink: 0; }
.photo-placeholder { width: var(--thumb-size); height: var(--thumb-size); background: #1a1a1a; border-radius: 4px; flex-shrink: 0;
                     border: 1px dashed #333; display: flex; align-items: center;
                     justify-content: center; color: #555; font-size: 10px; text-align: center;
                     padding: 6px; }
.photo-meta { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.photo-label { font-size: 9px; font-weight: 600; text-transform: uppercase; letter-spacing: .04em; }
.photo-label.target   { color: #f59e0b; }
.photo-label.source   { color: #60c060; }
.photo-label.override { color: #a78bfa; }
.photo-name { font-size: 11px; color: #bbb; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.photo-date { font-size: 10px; color: #666; }

.card-actions { padding: 7px 12px; border-top: 1px solid #2a2a2a; display: flex; gap: 6px; align-items: center; }
.btn-approve { background: #1a4a2a; color: #4caf50; border: 1px solid #2a6a3a; }
.btn-reject  { background: #3a1a1a; color: #ef5350; border: 1px solid #5a2a2a; }
.btn-approve.active { background: #1a7a3a; color: #fff; border-color: #1a7a3a; }
.btn-reject.active  { background: #7a1a1a; color: #fff; border-color: #7a1a1a; }
.btn-clear-override { background: none; border: none; color: #7c3aed; font-size: 11px;
                      cursor: pointer; text-decoration: underline; padding: 0; margin-left: auto; }

/* ── Applying panel ── */
.applying-wrap { max-width: 700px; margin: 40px auto; padding: 0 20px; }
.applying-wrap h2 { font-size: 15px; color: #fff; margin-bottom: 16px; }

/* ── Done panel ── */
.done-wrap { max-width: 700px; margin: 80px auto; padding: 0 20px; text-align: center; }
.done-wrap h2 { font-size: 24px; color: #4caf50; margin-bottom: 10px; }
.done-wrap p  { font-size: 13px; color: #888; margin-bottom: 6px; }
.done-wrap .log-path { font-family: monospace; font-size: 11px; color: #555; margin-bottom: 30px; }
.done-actions { display: flex; gap: 12px; justify-content: center; }
#btn-start-over { background: #2563eb; color: #fff; padding: 8px 20px; }
#btn-shutdown   { background: #2a2a2a; color: #999; border: 1px solid #444; padding: 8px 20px; }

/* ── Sync bar ── */
.sync-bar { display: flex; align-items: center; justify-content: space-between;
            background: #1e1e1e; border: 1px solid #2a2a2a; border-radius: 8px;
            padding: 10px 14px; margin-bottom: 14px; gap: 12px; flex-wrap: wrap; }
.sync-stats { display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
              font-size: 12px; color: #999; }
.sync-sep { color: #444; }
.sync-btn { background: #2a2a2a; color: #ccc; border: 1px solid #3a3a3a;
            padding: 5px 14px; border-radius: 6px; font-size: 12px;
            cursor: pointer; white-space: nowrap; flex-shrink: 0; }
.sync-btn:hover:not(:disabled) { background: #333; color: #fff; }
.sync-btn:disabled { opacity: 0.5; cursor: not-allowed; }
.sync-log-wrap { padding: 0 2px; }

/* ── Misc ── */
.big-btn { padding: 10px 28px; font-size: 14px; font-weight: 600; }
.big-btn.primary { background: #2563eb; color: #fff; }
.big-btn.primary:hover { background: #1d4ed8; }
footer { padding: 16px; text-align: center; font-size: 11px; color: #333; }
</style>
</head>
<body>

<header>
  <h1>osxphotos · Location Review</h1>
  <span class="hstatus" id="hstatus">Loading library…</span>
</header>

<!-- ── Panel: Setup ── -->
<div class="panel active" id="panel-setup">
  <div class="setup-wrap">

    <!-- DB sync bar -->
    <div class="sync-bar" id="sync-bar">
      <div class="sync-stats" id="sync-stats">
        <span id="stat-total">— photos</span>
        <span class="sync-sep">·</span>
        <span id="stat-unlocated">— unlocated</span>
        <span class="sync-sep">·</span>
        <span id="stat-applied">— applied</span>
        <span class="sync-sep">·</span>
        <span id="stat-last-sync" style="color:#666">Never synced</span>
      </div>
      <button class="sync-btn" id="btn-sync">↻ Sync Library</button>
    </div>
    <div class="sync-log-wrap" id="sync-log-wrap" style="display:none">
      <div class="log" id="sync-log" style="max-height:120px;margin-bottom:10px"></div>
    </div>
    <hr class="divider" style="margin-top:0">

    <h2>Select a date range</h2>
    <p>Top months with unlocated photos — click to pre-fill, or enter a custom range below.</p>
    <div class="month-grid" id="month-grid">
      <div style="color:#555;font-size:12px" class="loading-dots">Loading</div>
    </div>
    <hr class="divider">
    <div class="form-row">
      <div class="form-group">
        <label>From date</label>
        <input type="date" id="from-date">
      </div>
      <div class="form-group">
        <label>To date</label>
        <input type="date" id="to-date">
      </div>
      <div class="form-group">
        <label>Algorithm window</label>
        <input type="text" id="window" value="30 min" style="width:100px">
      </div>
    </div>
    <label style="display:flex;align-items:center;gap:8px;font-size:12px;color:#aaa;margin-bottom:14px;cursor:pointer">
      <input type="checkbox" id="ignore-span">
      Show all unlocated photos (ignore GPS day span — use when timestamps are unreliable)
    </label>
    <p class="total-note" id="total-note"></p>
    <button class="big-btn primary" id="btn-generate">Generate Review →</button>
  </div>
</div>

<!-- ── Panel: Generating ── -->
<div class="panel" id="panel-generating">
  <div class="progress-wrap">
    <h2>Building review…</h2>
    <div class="log" id="gen-log"></div>
    <div id="gen-back" style="display:none;margin-top:16px">
      <button class="big-btn" style="background:#2a2a2a;color:#ccc;border:1px solid #444"
        onclick="goHome()">← Back to date picker</button>
    </div>
  </div>
</div>

<!-- ── Panel: Review ── -->
<div class="panel" id="panel-review">
  <div class="map-wrap">
    <div id="map"></div>
    <div class="map-legend">
      <span class="dot-orange"></span> Auto-match &nbsp;
      <span class="dot-blue"></span> GPS source &nbsp;
      <span class="dot-manual"></span> Manual location
    </div>
  </div>

  <div id="assign-banner">
    <span>Active source: <b id="active-pin-label">—</b></span>
    <button id="btn-assign">Assign to <span id="assign-count">0</span> selected card(s)</button>
    <button id="btn-cancel-assign">cancel</button>
  </div>

  <div class="toolbar">
    <button id="btn-approve-all">Approve all visible</button>
    <button id="btn-approve-sel">Approve selected (<span id="sel-count">0</span>)</button>
    <button id="btn-reject-all">Reject all visible</button>
    <button id="btn-select-page">Select page</button>
    <button id="btn-deselect">Deselect all</button>
    <div class="toolbar-right">
      <label style="font-size:11px;color:#777">Per page
        <select id="page-size-sel">
          <option value="20" selected>20</option>
          <option value="50">50</option>
          <option value="100">100</option>
          <option value="0">All</option>
        </select>
      </label>
      <span id="count-label">—</span>
      <span style="font-size:11px;color:#777;display:flex;gap:3px;align-items:center">
        <span style="color:#555">Size</span>
        <button class="sz-btn active" data-sz="60">S</button>
        <button class="sz-btn" data-sz="80">M</button>
        <button class="sz-btn" data-sz="120">L</button>
      </span>
      <button id="btn-back-home" style="background:#2a2a2a;color:#888;border:1px solid #444">← New batch</button>
    <button id="btn-apply-server" disabled>Apply Approved (0)</button>
    </div>
  </div>

  <div class="filter-bar">
    <button class="ftab active" data-filter="all">All</button>
    <button class="ftab" data-filter="matched">Auto-matched</button>
    <button class="ftab" data-filter="unmatched">Manual</button>
    <button class="ftab" data-filter="approved">Approved</button>
    <button class="ftab" data-filter="pending">Pending</button>
  </div>

  <div class="pager" id="pager-top">
    <button id="btn-prev-top">← Prev</button>
    <span id="page-info">—</span>
    <button id="btn-next-top">Next →</button>
    <div class="pager-right">
      Jump: <input id="jump-input" type="number" min="1">
      <button id="btn-jump">Go</button>
    </div>
  </div>

  <div class="cards" id="cards"></div>

  <div class="pager">
    <button id="btn-prev-bot">← Prev</button>
    <span style="font-size:11px;color:#666" id="page-info-bot">—</span>
    <button id="btn-next-bot">Next →</button>
  </div>
  <footer>osxphotos location review · <span id="footer-range"></span></footer>
</div>

<!-- ── Panel: Applying ── -->
<div class="panel" id="panel-applying">
  <div class="applying-wrap">
    <h2>Applying locations to Photos…</h2>
    <div class="log" id="apply-log"></div>
  </div>
</div>

<!-- ── Panel: Done ── -->
<div class="panel" id="panel-done">
  <div class="done-wrap">
    <h2>Done ✓</h2>
    <p id="done-summary"></p>
    <p id="done-first-error" style="color:#ef5350;font-size:12px;font-family:monospace;display:none"></p>
    <details id="done-log-details" style="display:none;margin-bottom:12px">
      <summary style="font-size:12px;color:#888;cursor:pointer">Show apply log</summary>
      <div id="done-log-copy" class="log" style="max-height:240px;overflow-y:auto;margin-top:8px"></div>
    </details>
    <p class="log-path" id="done-log-path"></p>
    <div class="done-actions">
      <button id="btn-start-over" class="big-btn primary">Review another batch</button>
      <button id="btn-shutdown">Shut down server</button>
    </div>
  </div>
</div>

<script>
// ═══════════════════════════════════════════════════════════════════════════
// State
// ═══════════════════════════════════════════════════════════════════════════
let CARDS       = [];
let GPS_SRCS    = [];
let ST          = [];   // per-card runtime state
let activePin   = null;
let curPage     = 0;
let curFilter   = 'all';
let mapInstance = null;
let blueMarkers = {};
let manualMarker = null;

// ═══════════════════════════════════════════════════════════════════════════
// Panel navigation
// ═══════════════════════════════════════════════════════════════════════════
function showPanel(id) {
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.getElementById(id).classList.add('active');
}

// ═══════════════════════════════════════════════════════════════════════════
// DB stats bar
// ═══════════════════════════════════════════════════════════════════════════
async function loadDbStats() {
  try {
    const res  = await fetch('/api/db-stats');
    const s    = await res.json();
    document.getElementById('stat-total').textContent =
      s.total ? `${s.total.toLocaleString()} photos` : '— photos';
    document.getElementById('stat-unlocated').textContent =
      s.unlocated != null ? `${s.unlocated.toLocaleString()} unlocated` : '— unlocated';
    document.getElementById('stat-applied').textContent =
      s.applied != null ? `${s.applied.toLocaleString()} applied` : '— applied';
    if (s.last_sync) {
      const d = new Date(s.last_sync);
      document.getElementById('stat-last-sync').textContent =
        'Synced ' + d.toLocaleString(undefined, {month:'short',day:'numeric',
          hour:'2-digit',minute:'2-digit'});
      document.getElementById('stat-last-sync').style.color = '#777';
    } else {
      document.getElementById('stat-last-sync').textContent = 'Never synced';
      document.getElementById('stat-last-sync').style.color = '#c0392b';
    }
  } catch(e) { /* ignore — DB not ready yet */ }
}

loadDbStats();

document.getElementById('btn-sync').onclick = async () => {
  const btn     = document.getElementById('btn-sync');
  const logWrap = document.getElementById('sync-log-wrap');
  const log     = document.getElementById('sync-log');
  btn.disabled  = true;
  btn.textContent = '↻ Syncing…';
  logWrap.style.display = 'block';
  log.innerHTML = '';

  const addLine = msg => {
    const div = document.createElement('div');
    div.textContent = msg;
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
  };

  try {
    const resp = await fetch('/sync', {method:'POST'});
    const reader = resp.body.getReader();
    const dec    = new TextDecoder();
    let buf = '';
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      buf += dec.decode(value, {stream: true});
      const lines = buf.split('\\n');
      buf = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data:')) continue;
        try {
          const ev = JSON.parse(line.slice(5).trim());
          if (ev.type === 'log')  { addLine(ev.msg); }
          if (ev.type === 'done') {
            const r = ev.result || {};
            addLine(`✓ Done: +${r.added||0} added, ~${r.updated||0} updated, -${r.removed||0} removed`);
            await loadDbStats();
            monthsInit();
          }
          if (ev.type === 'error') { addLine('✗ ' + ev.msg); }
        } catch(_) {}
      }
    }
  } catch(e) {
    addLine('Error: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = '↻ Sync Library';
  }
};

// ═══════════════════════════════════════════════════════════════════════════
// Setup panel — load months
// ═══════════════════════════════════════════════════════════════════════════
async function monthsInit() {
  const res  = await fetch('/api/months');
  const data = await res.json();
  if (!res.ok) {
    document.getElementById('hstatus').textContent = 'Photos library unavailable';
    document.getElementById('total-note').textContent = data.error || 'Could not load Photos library.';
    return;
  }
  document.getElementById('hstatus').textContent =
    `${data.total.toLocaleString()} unlocated photos total`;
  document.getElementById('total-note').textContent =
    `Library contains ${data.total.toLocaleString()} photos without GPS.`;

  const grid = document.getElementById('month-grid');
  grid.innerHTML = '';
  data.months.forEach(m => {
    const [y, mo] = m.ym.split('-');
    const lastDay = new Date(+y, +mo, 0).getDate();
    const btn = document.createElement('button');
    btn.className = 'month-btn';
    btn.innerHTML = `<div class="ym">${m.ym}</div><div class="cnt">${m.count.toLocaleString()} unlocated</div>`;
    btn.onclick = () => {
      document.querySelectorAll('.month-btn').forEach(b => b.classList.remove('selected'));
      btn.classList.add('selected');
      document.getElementById('from-date').value = `${y}-${mo}-01`;
      document.getElementById('to-date').value   = `${y}-${mo}-${String(lastDay).padStart(2,'0')}`;
    };
    grid.appendChild(btn);
  });
}

monthsInit();

document.getElementById('btn-generate').onclick = async () => {
  const from       = document.getElementById('from-date').value;
  const to         = document.getElementById('to-date').value;
  const win        = document.getElementById('window').value || '30 min';
  const ignoreSpan = document.getElementById('ignore-span').checked;
  if (!from || !to) { alert('Please select or enter a date range.'); return; }

  showPanel('panel-generating');
  const log = document.getElementById('gen-log');
  log.innerHTML = '';

  const es = new EventSource('/generate?' + new URLSearchParams({from_date:from, to_date:to, window:win}));
  // Actually use fetch+SSE via POST
  es.close();

  const resp = await fetch('/generate', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({from_date:from, to_date:to, window:win, ignore_span:ignoreSpan}),
  });

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';

  while (true) {
    const {value, done} = await reader.read();
    if (done) break;
    buf += decoder.decode(value, {stream:true});
    const lines = buf.split('\\n');
    buf = lines.pop();
    for (const line of lines) {
      if (!line.startsWith('data:')) continue;
      const msg = JSON.parse(line.slice(5).trim());
      if (msg.final) {
        document.getElementById('gen-back').style.display = 'block';
        if (msg.success) await loadReview();
        return;
      }
      const div = document.createElement('div');
      div.className = 'log-line' +
        (msg.warn  ? ' warn'  : '') +
        (msg.error ? ' error' : '') +
        (msg.done  ? ' done'  : '');
      div.textContent = msg.message;
      log.appendChild(div);
      log.scrollTop = log.scrollHeight;
    }
  }
};

// ═══════════════════════════════════════════════════════════════════════════
// Review panel
// ═══════════════════════════════════════════════════════════════════════════
async function loadReview() {
  const res  = await fetch('/api/cards');
  const data = await res.json();

  CARDS    = data.cards;
  GPS_SRCS = data.gps_photos;
  ST       = CARDS.map(() => ({status: null, override: null, selected: false}));
  curPage  = 0;
  curFilter = 'all';
  document.querySelectorAll('.ftab').forEach(b => b.classList.toggle('active', b.dataset.filter==='all'));

  document.getElementById('footer-range').textContent =
    `${data.from_date} → ${data.to_date} · window ±${data.window} · ${data.n_matched} auto + ${data.n_unmatched} manual`;

  showPanel('panel-review');
  initMap();
  renderCards();
}

function initMap() {
  const el = document.getElementById('map');
  if (mapInstance) { mapInstance.remove(); mapInstance = null; }
  mapInstance = L.map(el);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '© OpenStreetMap contributors', maxZoom: 18
  }).addTo(mapInstance);

  blueMarkers = {};
  manualMarker = null;
  activePin = null;

  // Orange pins — algorithm proposed
  CARDS.forEach((c, i) => {
    if (!c.proposed_lat) return;
    const m = L.circleMarker([c.proposed_lat, c.proposed_lon], {
      radius: 6, color: '#f59e0b', fillColor: '#f59e0b', fillOpacity: .7,
      weight: 2, bubblingMouseEvents: false
    }).addTo(mapInstance);
    m.bindPopup(`<b>#${i+1}</b> ${c.target_filename}<br>${c.proposed_lat.toFixed(5)}, ${c.proposed_lon.toFixed(5)}`);
    m.on('click', () => { jumpToCard(i); highlightCard(i); });
  });

  // Blue pins — all GPS sources
  GPS_SRCS.forEach(g => {
    const m = L.circleMarker([g.lat, g.lon], {
      radius: 7, color: '#38bdf8', fillColor: '#38bdf8', fillOpacity: .6,
      weight: 2, bubblingMouseEvents: false
    }).addTo(mapInstance);
    m.bindPopup(`<b>${g.filename}</b><br>${g.date}<br>${g.lat.toFixed(5)}, ${g.lon.toFixed(5)}`);
    m.on('click', () => activatePin(g, m));
    blueMarkers[g.uuid] = m;
  });

  mapInstance.on('click', e => activateManualPin(e.latlng));

  L.Control.geocoder({
    defaultMarkGeocode: false,
    placeholder: 'Search address…',
    errorMessage: 'No results.',
    geocoder: L.Control.Geocoder.nominatim({ geocodingQueryParams: { limit: 5 } }),
  }).on('markgeocode', e => {
    activateManualPin(e.geocode.center);
    mapInstance.fitBounds(e.geocode.bbox);
  }).addTo(mapInstance);

  const all = [
    ...CARDS.filter(c => c.proposed_lat).map(c => [c.proposed_lat, c.proposed_lon]),
    ...GPS_SRCS.map(g => [g.lat, g.lon]),
  ];
  if (all.length) mapInstance.fitBounds(all, {padding:[30,30]});
  else mapInstance.setView([40,-75], 5);
}

// ── Pin activation ────────────────────────────────────────────────────────
function activatePin(g, marker) {
  resetActivePin();
  activePin = g;
  marker.setStyle({color:'#fff', fillColor:'#38bdf8', weight:3});
  updateAssignBanner();
}
function activateManualPin(latlng) {
  resetActivePin();
  const lat = latlng.lat;
  const lon = latlng.lng;
  activePin = {lat, lon, uuid:null, filename:'Map location', date:'Manual pin'};
  manualMarker = L.circleMarker(latlng, {
    radius: 8, color: '#fff', fillColor: '#a78bfa', fillOpacity: .95,
    weight: 3, bubblingMouseEvents: false
  }).addTo(mapInstance);
  manualMarker.bindPopup(`Manual location<br>${lat.toFixed(5)}, ${lon.toFixed(5)}`).openPopup();
  manualMarker.on('click', clearActivePin);
  updateAssignBanner();
}
function resetActivePin() {
  if (activePin && blueMarkers[activePin.uuid])
    blueMarkers[activePin.uuid].setStyle({color:'#38bdf8', fillColor:'#38bdf8', weight:2});
  if (manualMarker && mapInstance) mapInstance.removeLayer(manualMarker);
  manualMarker = null;
}
function clearActivePin() {
  resetActivePin();
  activePin = null;
  updateAssignBanner();
}
function updateAssignBanner() {
  const banner   = document.getElementById('assign-banner');
  const selected = ST.filter(s => s.selected).length;
  if (activePin && selected > 0) {
    banner.classList.add('visible');
    document.getElementById('active-pin-label').textContent =
      `${activePin.filename} · ${activePin.lat.toFixed(5)}, ${activePin.lon.toFixed(5)}`;
    document.getElementById('assign-count').textContent = selected;
  } else {
    banner.classList.remove('visible');
  }
}
document.getElementById('btn-assign').onclick = () => {
  if (!activePin) return;
  ST.forEach((s, i) => {
    if (!s.selected) return;
    s.override = {lat:activePin.lat, lon:activePin.lon, uuid:activePin.uuid,
                  filename:activePin.filename, date:activePin.date};
    s.status   = 'approved';
    s.selected = false;
  });
  clearActivePin();
  renderCards();
};
document.getElementById('btn-cancel-assign').onclick = clearActivePin;

// ── Filtering ─────────────────────────────────────────────────────────────
function filteredIndices() {
  return CARDS.map((_,i)=>i).filter(i => {
    const s = ST[i];
    switch (curFilter) {
      case 'matched':   return CARDS[i].has_match;
      case 'unmatched': return !CARDS[i].has_match;
      case 'approved':  return s.status === 'approved';
      case 'pending':   return s.status === null;
      default:          return true;
    }
  });
}
document.querySelectorAll('.ftab').forEach(btn => {
  btn.onclick = () => {
    curFilter = btn.dataset.filter; curPage = 0;
    document.querySelectorAll('.ftab').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    renderCards();
  };
});

// ── Pagination ─────────────────────────────────────────────────────────────
function pageSize() { const v=parseInt(document.getElementById('page-size-sel').value); return v===0?CARDS.length:v; }
function pageSlice() { const fi=filteredIndices(),ps=pageSize(); return fi.slice(curPage*ps,(curPage+1)*ps); }
function totalPages() { return Math.max(1,Math.ceil(filteredIndices().length/pageSize())); }
function prevPage() { if(curPage>0){curPage--;renderCards();} }
function nextPage() { if(curPage<totalPages()-1){curPage++;renderCards();} }

// ── Render ─────────────────────────────────────────────────────────────────
function effectiveCoords(i) {
  const s=ST[i],c=CARDS[i];
  if(s.override)     return {lat:s.override.lat, lon:s.override.lon};
  if(c.proposed_lat) return {lat:c.proposed_lat, lon:c.proposed_lon};
  return null;
}
function sourceSide(i) {
  const s=ST[i],c=CARDS[i];
  if(s.override) return `
    <div class="photo-side">
      <div class="photo-placeholder" style="font-size:9px">${s.override.lat.toFixed(4)},<br>${s.override.lon.toFixed(4)}</div>
      <div class="photo-meta">
        <span class="photo-label override">Manual 📍</span>
        <span class="photo-name">${s.override.filename}</span>
        <span class="photo-date">${s.override.date}</span>
      </div>
    </div>`;
  if(c.proposed_source_thumb) return `
    <div class="photo-side">
      <img src="data:image/jpeg;base64,${c.proposed_source_thumb}" alt="${c.proposed_source_filename}">
      <div class="photo-meta">
        <span class="photo-label source">Source ✓</span>
        <span class="photo-name">${c.proposed_source_filename}</span>
        <span class="photo-date">${c.proposed_source_date||''}</span>
      </div>
    </div>`;
  return `
    <div class="photo-side">
      <div class="photo-placeholder">Click<br>blue pin</div>
      <div class="photo-meta">
        <span class="photo-label source" style="color:#555">No match</span>
        <span class="photo-name" style="color:#555">—</span>
      </div>
    </div>`;
}
function badgeHTML(i) {
  const s=ST[i],c=CARDS[i];
  if(s.override)   return `<span class="badge badge-manual">manual</span>`;
  if(c.has_match)  return `<span class="badge badge-auto">auto</span>`;
  return `<span class="badge badge-none">unmatched</span>`;
}

function renderCards() {
  const fi=filteredIndices(), tp=totalPages(), ps=pageSize();
  const start=curPage*ps+1, end=Math.min((curPage+1)*ps,fi.length);
  const info=`Page ${curPage+1} of ${tp}  (${start}–${end} of ${fi.length} filtered)`;
  document.getElementById('page-info').textContent     = info;
  document.getElementById('page-info-bot').textContent = info;
  ['top','bot'].forEach(s=>{
    document.getElementById(`btn-prev-${s}`).disabled = curPage===0;
    document.getElementById(`btn-next-${s}`).disabled = curPage>=tp-1;
  });

  const nApproved  = ST.filter(s=>s.status==='approved').length;
  const nPending   = ST.filter(s=>s.status===null).length;
  const nSel       = ST.filter(s=>s.selected).length;
  const nApplyable = CARDS.filter((_,i)=>ST[i].status==='approved'&&effectiveCoords(i)!==null).length;
  const nNoCoords  = nApproved - nApplyable;
  document.getElementById('count-label').textContent =
    `${nApproved} approved · ${nPending} pending`
    + (nNoCoords>0 ? ` · ${nNoCoords} approved but no location` : '');

  const applyBtn = document.getElementById('btn-apply-server');
  applyBtn.textContent = `Apply Approved (${nApplyable})`;
  applyBtn.disabled = nApplyable === 0;

  const appSelBtn = document.getElementById('btn-approve-sel');
  document.getElementById('sel-count').textContent = nSel;
  appSelBtn.classList.toggle('has-sel', nSel > 0);
  appSelBtn.disabled = nSel === 0;

  const indices = pageSlice();
  const container = document.getElementById('cards');
  container.innerHTML = '';

  indices.forEach(i => {
    const c=CARDS[i], s=ST[i], eff=effectiveCoords(i);
    const coordStr = eff ? `${eff.lat.toFixed(5)}, ${eff.lon.toFixed(5)}` : '— no source —';
    const selIcon  = s.selected ? '<span class="sel-indicator">◉</span>' : '';
    const cls = ['card',
      s.override?'overridden':s.status==='approved'?'approved':s.status==='rejected'?'rejected':'',
      s.selected?'selected':'',
    ].filter(Boolean).join(' ');

    const card = document.createElement('div');
    card.className = cls;
    card.id = `card-${i}`;
    card.innerHTML = `
      <div class="card-header">
        <span class="card-index">#${i+1}</span>
        ${badgeHTML(i)}
        <span class="card-coords">${coordStr}</span>
        <span class="card-delta">${c.delta||''}</span>
        ${selIcon}
      </div>
      <div class="card-body">
        <div class="photo-side">
          ${c.target_thumb
            ? `<img src="data:image/jpeg;base64,${c.target_thumb}" alt="${c.target_filename}">`
            : `<div class="photo-placeholder">no thumb</div>`}
          <div class="photo-meta">
            <span class="photo-label target">Target</span>
            <span class="photo-name">${c.target_filename}</span>
            <span class="photo-date">${c.target_date}</span>
          </div>
        </div>
        ${sourceSide(i)}
      </div>
      <div class="card-actions">
        <button class="btn-approve${s.status==='approved'?' active':''}"
          onclick="event.stopPropagation();setState(${i},'approved')">✓ Approve</button>
        <button class="btn-reject${s.status==='rejected'?' active':''}"
          onclick="event.stopPropagation();setState(${i},'rejected')">✗ Reject</button>
        ${s.override?`<button class="btn-clear-override"
          onclick="event.stopPropagation();clearOverride(${i})">clear override</button>`:''}
      </div>`;
    card.addEventListener('click', e => { if(e.target.tagName==='BUTTON') return; toggleSelect(i); });
    container.appendChild(card);
  });

  updateAssignBanner();
}

function setState(i,val) { ST[i].status = ST[i].status===val?null:val; renderCards(); }
function clearOverride(i) { ST[i].override=null; renderCards(); }
function toggleSelect(i)  { ST[i].selected=!ST[i].selected; renderCards(); }
function highlightCard(i) {
  document.querySelectorAll('.card').forEach(c=>c.classList.remove('highlighted'));
  const card=document.getElementById(`card-${i}`);
  if(card) card.classList.add('highlighted');
}
function jumpToCard(i) {
  const fi=filteredIndices(), pos=fi.indexOf(i);
  if(pos<0) return;
  const tp=Math.floor(pos/pageSize());
  if(tp!==curPage){curPage=tp;renderCards();}
  setTimeout(()=>{
    const card=document.getElementById(`card-${i}`);
    if(card) card.scrollIntoView({behavior:'smooth',block:'center'});
  },50);
}

// ── Toolbar ───────────────────────────────────────────────────────────────
document.getElementById('btn-approve-all').onclick = () => {
  filteredIndices().forEach(i=>{if(ST[i].status===null)ST[i].status='approved';}); renderCards(); };
document.getElementById('btn-approve-sel').onclick  = () => {
  ST.forEach((s,i)=>{if(s.selected){s.status='approved';s.selected=false;}}); renderCards(); };
document.getElementById('btn-reject-all').onclick  = () => {
  filteredIndices().forEach(i=>{if(ST[i].status===null)ST[i].status='rejected';}); renderCards(); };
document.getElementById('btn-select-page').onclick = () => { pageSlice().forEach(i=>ST[i].selected=true); renderCards(); };
document.getElementById('btn-deselect').onclick    = () => { ST.forEach(s=>s.selected=false); renderCards(); };
document.getElementById('page-size-sel').onchange  = () => { curPage=0; renderCards(); };
document.getElementById('btn-prev-top').onclick = prevPage;
document.getElementById('btn-next-top').onclick = nextPage;
document.getElementById('btn-prev-bot').onclick = prevPage;
document.getElementById('btn-next-bot').onclick = nextPage;
document.getElementById('btn-jump').onclick = () => {
  const v=parseInt(document.getElementById('jump-input').value)-1;
  if(v>=0&&v<totalPages()){curPage=v;renderCards();}
};
document.getElementById('jump-input').onkeydown = e => { if(e.key==='Enter') document.getElementById('btn-jump').click(); };

// ── Apply ─────────────────────────────────────────────────────────────────
document.getElementById('btn-apply-server').onclick = async () => {
  const approved = CARDS
    .map((c,i) => {
      if (ST[i].status !== 'approved') return null;
      const eff = effectiveCoords(i);
      if (!eff) return null;
      return {uuid:c.target_uuid, filename:c.target_filename, lat:eff.lat, lon:eff.lon,
              source: ST[i].override?'manual':'algorithm'};
    })
    .filter(Boolean);

  if (!approved.length) return;
  if (!confirm(`Apply ${approved.length} approved location(s) to Photos.app?`)) return;

  showPanel('panel-applying');
  const log = document.getElementById('apply-log');
  log.innerHTML = '';

  const resp   = await fetch('/apply', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify(approved),
  });
  const reader  = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = '', summary = null;

  while (true) {
    const {value, done} = await reader.read();
    if (done) break;
    buf += decoder.decode(value, {stream:true});
    const lines = buf.split('\\n');
    buf = lines.pop();
    for (const line of lines) {
      if (!line.startsWith('data:')) continue;
      const msg = JSON.parse(line.slice(5).trim());
      if (msg.final) {
        showDone(summary);
        return;
      }
      if (msg.summary) summary = msg.summary;
      const div = document.createElement('div');
      div.className = 'log-line' +
        (msg.ok    ? ' ok'    : '') +
        (msg.error ? ' error' : '') +
        (msg.done  ? ' done'  : '');
      div.textContent = msg.message;
      log.appendChild(div);
      log.scrollTop = log.scrollHeight;
    }
  }
};

function showDone(s) {
  showPanel('panel-done');
  let summary = s ? `${s.applied} location(s) applied, ${s.errors} error(s)` : 'Complete.';
  if (s && s.not_writable) summary += `, ${s.not_writable} archived (not writable)`;
  if (s) summary += '.';
  document.getElementById('done-summary').textContent = summary;
  const errEl = document.getElementById('done-first-error');
  if (s && s.errors > 0 && s.first_error) {
    errEl.textContent = `First error: ${s.first_error}`;
    errEl.style.display = '';
  } else { errEl.style.display = 'none'; }
  const details = document.getElementById('done-log-details');
  if (s && s.errors > 0) {
    details.style.display = '';
    details.open = true;
    const copy = document.getElementById('done-log-copy');
    copy.innerHTML = document.getElementById('apply-log').innerHTML;
  } else { details.style.display = 'none'; details.open = false; }
  document.getElementById('done-log-path').textContent =
    s ? `Log: ${s.log}` : '';
}

function goHome() {
  CARDS=[]; GPS_SRCS=[]; ST=[]; activePin=null; curPage=0; curFilter='all';
  if(mapInstance){mapInstance.remove();mapInstance=null;}
  showPanel('panel-setup');
  loadDbStats();
  monthsInit();
}
document.getElementById('btn-start-over').onclick = goHome;
document.querySelectorAll('.sz-btn').forEach(btn => {
  btn.onclick = () => {
    document.querySelectorAll('.sz-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    document.documentElement.style.setProperty('--thumb-size', btn.dataset.sz + 'px');
  };
});

document.getElementById('btn-back-home').onclick  = () => {
  if (ST.some(s => s.status === 'approved') &&
      !confirm('Go back to home? Your current approvals will be lost.')) return;
  goHome();
};
document.getElementById('btn-shutdown').onclick = async () => {
  if(!confirm('Shut down the review server?')) return;
  await fetch('/shutdown', {method:'POST'});
  document.body.innerHTML = '<div style="text-align:center;padding:80px;color:#555">Server stopped. You may close this tab.</div>';
};

// Keyboard
document.addEventListener('keydown', e => {
  if(e.target.tagName==='INPUT') return;
  if(e.key==='ArrowLeft')  prevPage();
  if(e.key==='ArrowRight') nextPage();
  if(e.key==='Escape')     clearActivePin();
});
</script>
</body>
</html>"""


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    photo_db.init_db()
    threading.Thread(target=_load_db, daemon=True).start()

    def _open_browser():
        import time
        time.sleep(1.5)
        subprocess.run(["open", f"http://localhost:{PORT}"])

    threading.Thread(target=_open_browser, daemon=True).start()

    print(f"osxphotos Location Review → http://localhost:{PORT}", flush=True)
    app.run(host="127.0.0.1", port=PORT, threaded=True, debug=False, use_reloader=False)