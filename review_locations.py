#!/usr/bin/env python3
"""
Location Review Tool

Generates an interactive HTML review page for approving GPS location assignments.
For each unlocated photo whose timestamp falls within a day's GPS activity window,
shows the algorithm's best-guess source (if any) and lets you override by clicking
any GPS-tagged photo on the map.

Usage:
  python3.13 review_locations.py                          # interactive TUI
  python3.13 review_locations.py --from-date 2004-05-01 --to-date 2004-05-31
  python3.13 review_locations.py --from-date 2004-05-01 --to-date 2004-05-31 --window "15 min"

Output:
  review_YYYY-MM-DD_to_YYYY-MM-DD.html  — open in browser to approve/reject/assign
  Export button downloads approved_YYYY-MM-DD_to_YYYY-MM-DD.json

Apply approved assignments:
  python3.13 apply_locations.py approved_2004-05-01_to_2004-05-31.json
"""

import argparse
import base64
import calendar
import io
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import osxphotos
except ImportError:
    sys.exit("osxphotos not found — run: pip3.13 install osxphotos")

try:
    from PIL import Image
except ImportError:
    sys.exit("Pillow not found — run: pip3.13 install Pillow")


THUMB_MAX_PX   = 240
PAD_DAYS       = 3   # days to pad date range when fetching GPS photos


# ── Date span helpers ─────────────────────────────────────────────────────────

def compute_gps_day_spans(gps_photos):
    """
    For each calendar date, compute the earliest and latest timestamp
    among GPS-tagged photos on that date.
    Returns {date_str: (min_dt, max_dt)}.
    """
    by_day = defaultdict(list)
    for p in gps_photos:
        if p.date:
            by_day[p.date.strftime("%Y-%m-%d")].append(p.date)
    return {day: (min(dts), max(dts)) for day, dts in by_day.items()}


def photo_in_any_span(photo, day_spans):
    """Return True if photo's timestamp falls within any GPS day span."""
    if not photo.date:
        return False
    dt = photo.date
    day = dt.strftime("%Y-%m-%d")
    if day not in day_spans:
        return False
    lo, hi = day_spans[day]
    # Normalise tzinfo for comparison
    if lo.tzinfo and not dt.tzinfo:
        dt = dt.replace(tzinfo=lo.tzinfo)
    elif dt.tzinfo and not lo.tzinfo:
        lo = lo.replace(tzinfo=dt.tzinfo)
        hi = hi.replace(tzinfo=dt.tzinfo)
    return lo <= dt <= hi


# ── TUI ───────────────────────────────────────────────────────────────────────

def tui_pick_dates(db):
    print("\nScanning library for unlocated photos by month...", flush=True)
    no_loc = [p for p in db.photos(images=True)
              if (not p.location or p.location[0] is None) and p.date and not p.screenshot]
    by_month = Counter(p.date.strftime("%Y-%m") for p in no_loc)
    top = sorted(by_month.items(), key=lambda x: -x[1])[:12]

    print("\n=== osxphotos Location Review ===\n")
    print("  Top months with unlocated photos:")
    for i, (ym, count) in enumerate(top, 1):
        print(f"    [{i:>2}]  {ym}   {count:>5} unlocated")
    print(f"    [ 0]  Custom date range\n")

    while True:
        raw = input("Pick a month (1-12) or 0 for custom: ").strip()
        if raw == "0":
            from_date = input("From date (YYYY-MM-DD): ").strip()
            to_date   = input("To date   (YYYY-MM-DD): ").strip()
            break
        try:
            idx = int(raw) - 1
            ym  = top[idx][0]
            year, month = int(ym[:4]), int(ym[5:])
            last_day  = calendar.monthrange(year, month)[1]
            from_date = f"{ym}-01"
            to_date   = f"{ym}-{last_day:02d}"
            break
        except (ValueError, IndexError):
            print("  Invalid choice, try again.")

    default_window = "30 min"
    win_raw = input(f"Algorithm window (default '{default_window}'): ").strip()
    window  = win_raw if win_raw else default_window

    print(f"\nSelected: {from_date} → {to_date}  |  algorithm window ±{window}\n")
    return from_date, to_date, window


# ── Timestamp health check ────────────────────────────────────────────────────

def check_timestamp_distribution(db, from_date, to_date, warn_threshold=0.20):
    photos = [
        p for p in db.photos(images=True)
        if (not p.location or p.location[0] is None)
        and p.date and not p.screenshot
        and (not from_date or p.date.date().isoformat() >= from_date)
        and (not to_date   or p.date.date().isoformat() <= to_date)
    ]
    if not photos:
        return

    total   = len(photos)
    by_date = Counter(p.date.strftime("%Y-%m-%d") for p in photos)
    top     = sorted(by_date.items(), key=lambda x: -x[1])

    print(f"\n── Timestamp distribution ({total} unlocated photos) ──")
    for date, count in top[:10]:
        pct = count / total
        bar = "█" * int(pct * 40)
        flag = "  ⚠️  HIGH — timestamps may be fabricated" if pct >= warn_threshold else ""
        print(f"  {date}  {count:>5}  {pct:>5.1%}  {bar}{flag}")
    if len(top) > 10:
        print(f"  ... and {len(top)-10} more dates")

    dominated = [(d, n) for d, n in top if n / total >= warn_threshold]
    if dominated:
        print(f"\n  ⚠️  Warning: {len(dominated)} date(s) account for ≥{warn_threshold:.0%} of unlocated photos.")
        print( "     Photos sharing a fabricated timestamp will all match the same GPS neighbor,")
        print( "     producing geographically incorrect results. Consider running timewarp first.")
    else:
        print(f"\n  ✓  Timestamps look naturally distributed — safe to proceed.")
    print()


# ── Core helpers ──────────────────────────────────────────────────────────────

def run_dry_run(from_date, to_date, window):
    cmd = ["/opt/homebrew/bin/osxphotos", "add-locations", "--dry-run", "--verbose",
           "--not-screenshot", "--window", window]
    if from_date: cmd += ["--from-date", from_date]
    if to_date:   cmd += ["--to-date",   to_date]
    print(f"Running: {' '.join(cmd)}", flush=True)
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout + result.stderr


def parse_dry_run(output):
    matches = {}  # target_uuid → {lat, lon, source_uuid, source_name}
    pattern = re.compile(
        r"Adding location \(([^,]+),\s*([^)]+)\) to (.+?) \(([A-F0-9-]{36})\) from (.+?) \(([A-F0-9-]{36})\)"
    )
    for line in output.splitlines():
        m = pattern.search(line)
        if m:
            lat, lon, target_name, target_uuid, source_name, source_uuid = m.groups()
            matches[target_uuid] = {
                "lat":         float(lat.strip()),
                "lon":         float(lon.strip()),
                "source_uuid": source_uuid,
                "source_name": source_name.strip(),
            }
    return matches


def thumb_b64(path_derivatives, max_px=THUMB_MAX_PX):
    for path in path_derivatives:
        p = Path(path)
        if p.exists():
            try:
                img = Image.open(p)
                img.thumbnail((max_px, max_px), Image.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=75)
                return base64.b64encode(buf.getvalue()).decode()
            except Exception:
                continue
    return None


def photo_meta(p):
    """Extract display metadata from a PhotoInfo object."""
    date_str = p.date.strftime("%Y-%m-%d %H:%M:%S %Z") if p.date else "unknown date"
    loc      = p.location
    has_gps  = loc and loc[0] is not None
    return {
        "uuid":      p.uuid,
        "filename":  p.original_filename or p.filename or p.uuid,
        "date":      date_str,
        "has_gps":   has_gps,
        "location":  list(loc) if has_gps else None,
    }


def format_timedelta(d1, d2):
    try:
        delta = abs((d1 - d2).total_seconds())
        if delta < 60:     return f"{int(delta)}s apart"
        elif delta < 3600: return f"{int(delta/60)}m apart"
        else:              return f"{delta/3600:.1f}h apart"
    except Exception:
        return ""


def default_output(from_date, to_date):
    if from_date and to_date:
        return f"review_{from_date}_to_{to_date}.html"
    return "review_locations.html"


def default_export_name(from_date, to_date):
    if from_date and to_date:
        return f"approved_{from_date}_to_{to_date}.json"
    return "approved_locations.json"


# ── HTML builder ──────────────────────────────────────────────────────────────

def build_html(matched_cards, unmatched_cards, gps_photos_data,
               from_date, to_date, window, total_processed, total_missing):

    all_cards      = matched_cards + unmatched_cards
    cards_json     = json.dumps(all_cards,      default=str)
    gps_json       = json.dumps(gps_photos_data, default=str)
    export_name    = default_export_name(from_date, to_date)
    date_range     = f"{from_date or 'start'} → {to_date or 'end'}"
    generated      = datetime.now().strftime("%Y-%m-%d %H:%M")
    n_matched      = len(matched_cards)
    n_unmatched    = len(unmatched_cards)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Location Review {from_date or ''}–{to_date or ''}</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, sans-serif; background: #1a1a1a; color: #e0e0e0; }}

  header {{ background: #111; padding: 10px 20px; border-bottom: 1px solid #333;
            position: sticky; top: 0; z-index: 1000;
            display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }}
  header h1 {{ font-size: 14px; font-weight: 600; color: #fff; white-space: nowrap; }}
  .stats {{ font-size: 11px; color: #666; }}
  .stats b {{ color: #aaa; }}

  #map {{ height: 280px; border-bottom: 1px solid #333; }}

  /* legend */
  .map-legend {{ position: absolute; bottom: 10px; right: 10px; z-index: 500;
                 background: #111c; border: 1px solid #444; border-radius: 6px;
                 padding: 6px 10px; font-size: 11px; color: #ccc; pointer-events: none; }}
  .map-legend span {{ display: inline-block; width: 10px; height: 10px;
                      border-radius: 50%; margin-right: 5px; vertical-align: middle; }}
  .dot-orange {{ background: #f59e0b; }}
  .dot-blue   {{ background: #38bdf8; }}

  /* assign banner */
  #assign-banner {{ display: none; background: #1e3a5f; border-bottom: 1px solid #2563eb;
                    padding: 8px 20px; font-size: 12px; color: #93c5fd;
                    align-items: center; gap: 12px; }}
  #assign-banner.visible {{ display: flex; }}
  #assign-banner b {{ color: #fff; }}
  #btn-assign {{ background: #2563eb; color: #fff; border: none; border-radius: 5px;
                 padding: 5px 14px; cursor: pointer; font-size: 12px; }}
  #btn-assign:hover {{ background: #1d4ed8; }}
  #btn-cancel-assign {{ background: none; border: none; color: #93c5fd; cursor: pointer;
                        font-size: 12px; text-decoration: underline; }}

  .toolbar {{ padding: 8px 20px; background: #111; border-bottom: 1px solid #333;
              display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }}
  button {{ padding: 5px 12px; border-radius: 5px; border: none; cursor: pointer;
            font-size: 12px; font-weight: 500; }}
  #btn-approve-all {{ background: #1a4a2a; color: #4caf50; border: 1px solid #2a6a3a; }}
  #btn-reject-all  {{ background: #3a1a1a; color: #ef5350; border: 1px solid #5a2a2a; }}
  #btn-export      {{ background: #2563eb; color: #fff; }}
  #btn-select-page {{ background: #2a2a2a; color: #ccc; border: 1px solid #444; }}
  #btn-deselect    {{ background: #2a2a2a; color: #ccc; border: 1px solid #444; }}
  .toolbar-right   {{ margin-left: auto; display: flex; align-items: center; gap: 10px; }}
  #count-label     {{ font-size: 12px; color: #888; }}
  select {{ background: #222; color: #ccc; border: 1px solid #444; border-radius: 5px;
            padding: 4px 8px; font-size: 12px; cursor: pointer; }}

  /* filter tabs */
  .filter-bar {{ padding: 6px 20px; background: #0f0f0f; border-bottom: 1px solid #2a2a2a;
                 display: flex; gap: 4px; }}
  .ftab {{ background: none; border: 1px solid #333; color: #777; border-radius: 4px;
           padding: 4px 12px; font-size: 11px; }}
  .ftab.active {{ background: #333; color: #fff; border-color: #555; }}

  /* pager */
  .pager {{ display: flex; align-items: center; gap: 8px; padding: 8px 20px;
            background: #161616; border-bottom: 1px solid #2a2a2a; font-size: 12px; color: #888; }}
  .pager button {{ background: #222; color: #ccc; border: 1px solid #3a3a3a; padding: 3px 10px; }}
  .pager button:disabled {{ opacity: .35; cursor: default; }}
  .pager button:not(:disabled):hover {{ background: #333; }}
  #page-info {{ min-width: 140px; text-align: center; color: #aaa; font-size: 11px; }}
  .pager-right {{ margin-left: auto; display: flex; align-items: center; gap: 6px; font-size: 11px; }}
  .pager-right input {{ width: 46px; background: #222; color: #ccc; border: 1px solid #444;
                        border-radius: 4px; padding: 2px 6px; font-size: 11px; }}

  .cards {{ padding: 12px 20px; display: flex; flex-direction: column; gap: 12px; }}

  .card {{ background: #242424; border: 1px solid #333; border-radius: 8px;
           overflow: hidden; transition: border-color .12s; cursor: pointer; }}
  .card.approved  {{ border-color: #1a7a3a; }}
  .card.rejected  {{ border-color: #7a1a1a; opacity: .5; }}
  .card.overridden {{ border-color: #7c3aed; }}
  .card.highlighted {{ box-shadow: 0 0 0 2px #f59e0b; }}
  .card.selected  {{ outline: 2px solid #38bdf8; outline-offset: 1px; }}

  .card-header {{ padding: 7px 12px; background: #1e1e1e; display: flex; align-items: center;
                  gap: 8px; font-size: 11px; color: #888; border-bottom: 1px solid #2a2a2a;
                  user-select: none; }}
  .card-index  {{ font-weight: 700; color: #aaa; min-width: 30px; }}
  .card-coords {{ font-family: monospace; font-size: 10px; color: #60a0d0; }}
  .badge {{ padding: 1px 7px; border-radius: 10px; font-size: 10px; font-weight: 600; }}
  .badge-auto {{ background: #1a3a1a; color: #4caf50; }}
  .badge-manual {{ background: #2d1a5f; color: #a78bfa; }}
  .badge-none  {{ background: #2a2a2a; color: #666; }}
  .card-delta  {{ margin-left: auto; font-size: 11px; color: #666; }}
  .sel-indicator {{ margin-left: 4px; color: #38bdf8; font-size: 13px; }}

  .card-body {{ display: grid; grid-template-columns: 1fr 1fr; }}
  .photo-side {{ padding: 10px; display: flex; flex-direction: column; gap: 5px; }}
  .photo-side + .photo-side {{ border-left: 1px solid #2a2a2a; }}
  .photo-side img {{ width: 100%; aspect-ratio: 1; object-fit: cover; border-radius: 5px; background: #111; }}
  .photo-placeholder {{ width: 100%; aspect-ratio: 1; background: #1a1a1a; border-radius: 5px;
                        border: 1px dashed #333; display: flex; align-items: center;
                        justify-content: center; color: #555; font-size: 11px; text-align: center;
                        padding: 12px; }}
  .photo-label {{ font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: .04em; }}
  .photo-label.target {{ color: #f59e0b; }}
  .photo-label.source {{ color: #60c060; }}
  .photo-label.override {{ color: #a78bfa; }}
  .photo-name {{ font-size: 11px; color: #bbb; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .photo-date {{ font-size: 10px; color: #666; }}

  .card-actions {{ padding: 7px 12px; border-top: 1px solid #2a2a2a;
                   display: flex; gap: 6px; align-items: center; }}
  .btn-approve {{ background: #1a4a2a; color: #4caf50; border: 1px solid #2a6a3a; }}
  .btn-reject  {{ background: #3a1a1a; color: #ef5350; border: 1px solid #5a2a2a; }}
  .btn-approve.active {{ background: #1a7a3a; color: #fff; border-color: #1a7a3a; }}
  .btn-reject.active  {{ background: #7a1a1a; color: #fff; border-color: #7a1a1a; }}
  .btn-clear-override {{ background: none; border: none; color: #7c3aed; font-size: 11px;
                         cursor: pointer; text-decoration: underline; padding: 0; margin-left: auto; }}

  footer {{ padding: 16px; text-align: center; font-size: 11px; color: #444; }}
</style>
</head>
<body>

<header>
  <h1>Location Review</h1>
  <div class="stats">
    <b>{date_range}</b> &nbsp;·&nbsp; window <b>±{window}</b> &nbsp;·&nbsp;
    processed <b>{total_processed}</b> &nbsp;·&nbsp;
    auto-matched <b>{n_matched}</b> &nbsp;·&nbsp;
    in-span (manual) <b>{n_unmatched}</b> &nbsp;·&nbsp;
    GPS sources <b>{len(gps_photos_data)}</b>
  </div>
</header>

<div style="position:relative">
  <div id="map"></div>
  <div class="map-legend">
    <span class="dot-orange"></span> Auto-match &nbsp;
    <span class="dot-blue"></span> GPS source
  </div>
</div>

<div id="assign-banner">
  <span>Active source: <b id="active-pin-label">—</b></span>
  <button id="btn-assign">Assign to <span id="assign-count">0</span> selected card(s)</button>
  <button id="btn-cancel-assign">cancel</button>
</div>

<div class="toolbar">
  <button id="btn-approve-all">Approve all visible</button>
  <button id="btn-reject-all">Reject all visible</button>
  <button id="btn-select-page">Select page</button>
  <button id="btn-deselect">Deselect all</button>
  <button id="btn-export">Export approved JSON</button>
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
  </div>
</div>

<div class="filter-bar">
  <button class="ftab active" data-filter="all">All</button>
  <button class="ftab" data-filter="matched">Auto-matched ({n_matched})</button>
  <button class="ftab" data-filter="unmatched">In-span / manual ({n_unmatched})</button>
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

<div class="pager" id="pager-bottom">
  <button id="btn-prev-bot">← Prev</button>
  <span style="font-size:11px;color:#666" id="page-info-bot">—</span>
  <button id="btn-next-bot">Next →</button>
</div>

<footer>Generated {generated} &nbsp;·&nbsp; osxphotos location review tool</footer>

<script>
const CARDS     = {cards_json};
const GPS_SRCS  = {gps_json};

// ── State ────────────────────────────────────────────────────────────────────
// Per-card runtime state
const ST = CARDS.map(c => ({{
  status:    null,        // null | 'approved' | 'rejected'
  override:  null,        // null | {{lat, lon, uuid, filename, date}} — manual GPS pick
  selected:  false,
}}));

let activePin  = null;   // currently highlighted blue GPS source pin
let curPage    = 0;
let curFilter  = 'all';

// ── Map ──────────────────────────────────────────────────────────────────────
const map = L.map('map');
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
  attribution: '© OpenStreetMap contributors', maxZoom: 18
}}).addTo(map);

// Orange markers — algorithm proposed locations
const orangeLayer = L.layerGroup().addTo(map);
CARDS.forEach((c, i) => {{
  if (!c.proposed_lat) return;
  const m = L.circleMarker([c.proposed_lat, c.proposed_lon], {{
    radius: 6, color: '#f59e0b', fillColor: '#f59e0b', fillOpacity: .7, weight: 2
  }}).addTo(orangeLayer);
  m.bindPopup(`<b>#${{i+1}}</b> ${{c.target_filename}}<br>${{c.proposed_lat.toFixed(5)}}, ${{c.proposed_lon.toFixed(5)}}`);
  m.on('click', () => {{ jumpToCard(i); highlightCard(i); }});
}});

// Blue markers — GPS source photos
const blueMarkers = {{}};
const blueLayer   = L.layerGroup().addTo(map);
GPS_SRCS.forEach(g => {{
  const m = L.circleMarker([g.lat, g.lon], {{
    radius: 7, color: '#38bdf8', fillColor: '#38bdf8', fillOpacity: .6, weight: 2
  }}).addTo(blueLayer);
  m.bindPopup(`<b>${{g.filename}}</b><br>${{g.date}}<br>${{g.lat.toFixed(5)}}, ${{g.lon.toFixed(5)}}`);
  m.on('click', () => activatePin(g, m));
  blueMarkers[g.uuid] = m;
}});

const allBounds = [
  ...CARDS.filter(c => c.proposed_lat).map(c => [c.proposed_lat, c.proposed_lon]),
  ...GPS_SRCS.map(g => [g.lat, g.lon])
];
if (allBounds.length) map.fitBounds(allBounds, {{ padding: [30, 30] }});
else map.setView([40, -75], 6);

// ── Pin activation ────────────────────────────────────────────────────────────
function activatePin(g, marker) {{
  // reset previous active
  if (activePin && blueMarkers[activePin.uuid]) {{
    blueMarkers[activePin.uuid].setStyle({{ color: '#38bdf8', fillColor: '#38bdf8' }});
  }}
  activePin = g;
  marker.setStyle({{ color: '#fff', fillColor: '#38bdf8', weight: 3 }});
  updateAssignBanner();
}}

function clearActivePin() {{
  if (activePin && blueMarkers[activePin.uuid]) {{
    blueMarkers[activePin.uuid].setStyle({{ color: '#38bdf8', fillColor: '#38bdf8', weight: 2 }});
  }}
  activePin = null;
  updateAssignBanner();
}}

function updateAssignBanner() {{
  const banner   = document.getElementById('assign-banner');
  const selected = ST.filter(s => s.selected).length;
  if (activePin && selected > 0) {{
    banner.classList.add('visible');
    document.getElementById('active-pin-label').textContent =
      `${{activePin.filename}} · ${{activePin.lat.toFixed(5)}}, ${{activePin.lon.toFixed(5)}}`;
    document.getElementById('assign-count').textContent = selected;
  }} else {{
    banner.classList.remove('visible');
  }}
}}

document.getElementById('btn-assign').onclick = () => {{
  if (!activePin) return;
  ST.forEach((s, i) => {{
    if (!s.selected) return;
    s.override = {{
      lat:      activePin.lat,
      lon:      activePin.lon,
      uuid:     activePin.uuid,
      filename: activePin.filename,
      date:     activePin.date,
    }};
    s.selected = false;
  }});
  clearActivePin();
  renderCards();
}};
document.getElementById('btn-cancel-assign').onclick = clearActivePin;

// ── Filtering ────────────────────────────────────────────────────────────────
function filteredIndices() {{
  return CARDS.map((c, i) => i).filter(i => {{
    const s = ST[i];
    switch (curFilter) {{
      case 'matched':   return c.has_match;
      case 'unmatched': return !c.has_match;
      case 'approved':  return s.status === 'approved';
      case 'pending':   return s.status === null;
      default:          return true;
    }}
  }});
}}

document.querySelectorAll('.ftab').forEach(btn => {{
  btn.onclick = () => {{
    curFilter = btn.dataset.filter;
    curPage   = 0;
    document.querySelectorAll('.ftab').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    renderCards();
  }};
}});

// ── Pagination ────────────────────────────────────────────────────────────────
function pageSize() {{
  const v = parseInt(document.getElementById('page-size-sel').value);
  return v === 0 ? CARDS.length : v;
}}

function pageSlice() {{
  const fi = filteredIndices();
  const ps = pageSize();
  return fi.slice(curPage * ps, (curPage + 1) * ps);
}}

function totalPages() {{
  return Math.max(1, Math.ceil(filteredIndices().length / pageSize()));
}}

function prevPage() {{ if (curPage > 0)               {{ curPage--; renderCards(); window.scrollTo(0,0); }} }}
function nextPage() {{ if (curPage < totalPages() - 1) {{ curPage++; renderCards(); window.scrollTo(0,0); }} }}

// ── Render ────────────────────────────────────────────────────────────────────
function effectiveCoords(i) {{
  const s = ST[i], c = CARDS[i];
  if (s.override)    return {{ lat: s.override.lat, lon: s.override.lon }};
  if (c.proposed_lat) return {{ lat: c.proposed_lat, lon: c.proposed_lon }};
  return null;
}}

function cardClass(i) {{
  const s = ST[i];
  const classes = ['card'];
  if (s.override)             classes.push('overridden');
  else if (s.status === 'approved') classes.push('approved');
  else if (s.status === 'rejected') classes.push('rejected');
  if (s.selected) classes.push('selected');
  return classes.join(' ');
}}

function sourceSide(i) {{
  const s = ST[i], c = CARDS[i];
  if (s.override) {{
    return `
      <div class="photo-side">
        <span class="photo-label override">Source — manual 📍</span>
        <div class="photo-placeholder">${{s.override.filename}}<br><span style="color:#7c3aed;font-size:10px">${{s.override.lat.toFixed(5)}}, ${{s.override.lon.toFixed(5)}}</span></div>
        <span class="photo-name">${{s.override.filename}}</span>
        <span class="photo-date">${{s.override.date}}</span>
      </div>`;
  }}
  if (c.proposed_source_thumb) {{
    return `
      <div class="photo-side">
        <span class="photo-label source">Source — auto ✓</span>
        <img src="data:image/jpeg;base64,${{c.proposed_source_thumb}}" alt="${{c.proposed_source_filename}}">
        <span class="photo-name">${{c.proposed_source_filename}}</span>
        <span class="photo-date">${{c.proposed_source_date || ''}}</span>
      </div>`;
  }}
  return `
    <div class="photo-side">
      <span class="photo-label source" style="color:#555">Source — none</span>
      <div class="photo-placeholder">No auto-match<br><span style="color:#3a8fd8;font-size:10px">Click a blue map pin<br>then Assign</span></div>
      <span class="photo-name" style="color:#555">—</span>
      <span class="photo-date"></span>
    </div>`;
}}

function badgeHTML(i) {{
  const s = ST[i], c = CARDS[i];
  if (s.override) return `<span class="badge badge-manual">manual</span>`;
  if (c.has_match) return `<span class="badge badge-auto">auto</span>`;
  return `<span class="badge badge-none">unmatched</span>`;
}}

function renderCards() {{
  const indices   = pageSlice();
  const fi        = filteredIndices();
  const tp        = totalPages();
  const ps        = pageSize();
  const start     = curPage * ps + 1;
  const end       = Math.min((curPage + 1) * ps, fi.length);
  const infoText  = `Page ${{curPage+1}} of ${{tp}}  (${{start}}–${{end}} of ${{fi.length}} filtered)`;

  document.getElementById('page-info').textContent    = infoText;
  document.getElementById('page-info-bot').textContent = infoText;
  ['top','bot'].forEach(s => {{
    document.getElementById(`btn-prev-${{s}}`).disabled = curPage === 0;
    document.getElementById(`btn-next-${{s}}`).disabled = curPage >= tp - 1;
  }});

  const approved = ST.filter(s => s.status === 'approved').length;
  const pending  = ST.filter(s => s.status === null).length;
  document.getElementById('count-label').textContent =
    `${{approved}} approved · ${{pending}} pending`;
  document.getElementById('total-matches') && (document.getElementById('total-matches').textContent = CARDS.length);

  const container = document.getElementById('cards');
  container.innerHTML = '';

  indices.forEach(i => {{
    const c   = CARDS[i];
    const s   = ST[i];
    const eff = effectiveCoords(i);

    const card = document.createElement('div');
    card.className = cardClass(i);
    card.id        = `card-${{i}}`;

    const coordStr = eff ? `${{eff.lat.toFixed(5)}}, ${{eff.lon.toFixed(5)}}` : '— no source —';
    const selIcon  = s.selected ? '<span class="sel-indicator">◉</span>' : '';

    card.innerHTML = `
      <div class="card-header">
        <span class="card-index">#${{i+1}}</span>
        ${{badgeHTML(i)}}
        <span class="card-coords">${{coordStr}}</span>
        <span class="card-delta">${{c.delta || ''}}</span>
        ${{selIcon}}
      </div>
      <div class="card-body">
        <div class="photo-side">
          <span class="photo-label target">Target — no GPS</span>
          ${{c.target_thumb ? `<img src="data:image/jpeg;base64,${{c.target_thumb}}" alt="${{c.target_filename}}">` : `<div class="photo-placeholder">no thumbnail</div>`}}
          <span class="photo-name">${{c.target_filename}}</span>
          <span class="photo-date">${{c.target_date}}</span>
        </div>
        ${{sourceSide(i)}}
      </div>
      <div class="card-actions">
        <button class="btn-approve${{s.status==='approved'?' active':''}}" onclick="event.stopPropagation();setState(${{i}},'approved')">✓ Approve</button>
        <button class="btn-reject${{s.status==='rejected'?' active':''}}" onclick="event.stopPropagation();setState(${{i}},'rejected')">✗ Reject</button>
        ${{s.override ? `<button class="btn-clear-override" onclick="event.stopPropagation();clearOverride(${{i}})">clear override</button>` : ''}}
      </div>`;

    // Card click = toggle selection
    card.addEventListener('click', e => {{
      if (e.target.tagName === 'BUTTON') return;
      toggleSelect(i);
    }});

    container.appendChild(card);
  }});

  updateAssignBanner();
}}

// ── Card actions ──────────────────────────────────────────────────────────────
function setState(i, val) {{
  ST[i].status = ST[i].status === val ? null : val;
  renderCards();
}}

function clearOverride(i) {{
  ST[i].override = null;
  renderCards();
}}

function toggleSelect(i) {{
  ST[i].selected = !ST[i].selected;
  renderCards();
}}

function highlightCard(i) {{
  document.querySelectorAll('.card').forEach(c => c.classList.remove('highlighted'));
  const card = document.getElementById(`card-${{i}}`);
  if (card) card.classList.add('highlighted');
}}

function jumpToCard(i) {{
  const fi = filteredIndices();
  const pos = fi.indexOf(i);
  if (pos < 0) return;
  const ps = pageSize();
  const targetPage = Math.floor(pos / ps);
  if (targetPage !== curPage) {{ curPage = targetPage; renderCards(); }}
  setTimeout(() => {{
    const card = document.getElementById(`card-${{i}}`);
    if (card) card.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
  }}, 50);
}}

// ── Toolbar ───────────────────────────────────────────────────────────────────
document.getElementById('btn-approve-all').onclick = () => {{
  filteredIndices().forEach(i => {{ if (ST[i].status === null) ST[i].status = 'approved'; }});
  renderCards();
}};
document.getElementById('btn-reject-all').onclick = () => {{
  filteredIndices().forEach(i => {{ if (ST[i].status === null) ST[i].status = 'rejected'; }});
  renderCards();
}};
document.getElementById('btn-select-page').onclick = () => {{
  pageSlice().forEach(i => ST[i].selected = true);
  renderCards();
}};
document.getElementById('btn-deselect').onclick = () => {{
  ST.forEach(s => s.selected = false);
  renderCards();
}};

document.getElementById('page-size-sel').onchange = () => {{ curPage = 0; renderCards(); }};
document.getElementById('btn-prev-top').onclick = prevPage;
document.getElementById('btn-next-top').onclick = nextPage;
document.getElementById('btn-prev-bot').onclick = prevPage;
document.getElementById('btn-next-bot').onclick = nextPage;
document.getElementById('btn-jump').onclick = () => {{
  const v = parseInt(document.getElementById('jump-input').value) - 1;
  if (v >= 0 && v < totalPages()) {{ curPage = v; renderCards(); window.scrollTo(0,0); }}
}};
document.getElementById('jump-input').onkeydown = e => {{
  if (e.key === 'Enter') document.getElementById('btn-jump').click();
}};

// ── Export ────────────────────────────────────────────────────────────────────
document.getElementById('btn-export').onclick = () => {{
  const out = CARDS
    .map((c, i) => {{
      const s   = ST[i];
      const eff = effectiveCoords(i);
      if (!eff) return null;
      return {{
        uuid:       c.target_uuid,
        filename:   c.target_filename,
        date:       c.target_date,
        lat:        eff.lat,
        lon:        eff.lon,
        approved:   s.status === 'approved',
        source:     s.override ? 'manual' : 'algorithm',
        source_uuid: s.override ? s.override.uuid : c.proposed_source_uuid,
      }};
    }})
    .filter(x => x && x.approved);

  if (!out.length) {{ alert('No approved cards to export.'); return; }}
  const blob = new Blob([JSON.stringify(out, null, 2)], {{ type: 'application/json' }});
  const a    = document.createElement('a');
  a.href     = URL.createObjectURL(blob);
  a.download = '{export_name}';
  a.click();
}};

// Keyboard shortcuts
document.addEventListener('keydown', e => {{
  if (e.target.tagName === 'INPUT') return;
  if (e.key === 'ArrowLeft')  prevPage();
  if (e.key === 'ArrowRight') nextPage();
  if (e.key === 'Escape')     clearActivePin();
}});

renderCards();
</script>
</body>
</html>"""


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Location review tool for osxphotos")
    parser.add_argument("--from-date")
    parser.add_argument("--to-date")
    parser.add_argument("--window", help="Algorithm window (default: '30 min')")
    parser.add_argument("--output")
    args = parser.parse_args()

    interactive = not args.from_date and not args.to_date

    print("Loading Photos library...", flush=True)
    db = osxphotos.PhotosDB()

    if interactive:
        from_date, to_date, window = tui_pick_dates(db)
    else:
        from_date = args.from_date
        to_date   = args.to_date
        window    = args.window or "30 min"

    output_path = args.output or default_output(from_date, to_date)

    # ── Sanity check ──────────────────────────────────────────────────────────
    check_timestamp_distribution(db, from_date, to_date)

    # ── Fetch GPS-tagged photos (with padding) ────────────────────────────────
    pad = timedelta(days=PAD_DAYS)
    from_dt = datetime.strptime(from_date, "%Y-%m-%d") - pad if from_date else None
    to_dt   = datetime.strptime(to_date,   "%Y-%m-%d") + pad if to_date   else None

    print("Fetching GPS-tagged photos in range...", flush=True)
    gps_photos = [
        p for p in db.photos(images=True)
        if p.location and p.location[0] is not None
        and not p.screenshot
        and p.date
        and (not from_dt or p.date.replace(tzinfo=None) >= from_dt)
        and (not to_dt   or p.date.replace(tzinfo=None) <= to_dt)
    ]
    print(f"  {len(gps_photos)} GPS-tagged photos found", flush=True)

    # ── Compute day spans ─────────────────────────────────────────────────────
    day_spans = compute_gps_day_spans(gps_photos)
    print(f"  GPS activity spans {len(day_spans)} calendar day(s)", flush=True)

    # GPS photos data for blue map pins (no thumbnails — loaded lazily via popup)
    gps_photos_data = [
        {
            "uuid":     p.uuid,
            "lat":      p.location[0],
            "lon":      p.location[1],
            "date":     p.date.strftime("%Y-%m-%d %H:%M:%S") if p.date else "",
            "filename": p.original_filename or p.filename or p.uuid,
        }
        for p in gps_photos
    ]

    # ── Algorithm dry-run ─────────────────────────────────────────────────────
    raw_output = run_dry_run(from_date, to_date, window)

    total_processed = total_missing = 0
    for line in raw_output.splitlines():
        m = re.search(r"Processed:\s*(\d+) photos, missing location:\s*(\d+)", line)
        if m:
            total_processed, total_missing = int(m.group(1)), int(m.group(2))

    algo_matches = parse_dry_run(raw_output)
    print(f"Algorithm matched {len(algo_matches)} photos", flush=True)

    # ── Build GPS source lookup for enrichment ────────────────────────────────
    gps_by_uuid = {p.uuid: p for p in gps_photos}

    # ── Fetch all unlocated photos in range ───────────────────────────────────
    print("Fetching unlocated photos in range...", flush=True)
    unlocated = [
        p for p in db.photos(images=True)
        if (not p.location or p.location[0] is None)
        and not p.screenshot
        and p.date
        and (not from_date or p.date.strftime("%Y-%m-%d") >= from_date)
        and (not to_date   or p.date.strftime("%Y-%m-%d") <= to_date)
    ]

    # Filter to photos within GPS day spans
    in_span = [p for p in unlocated if photo_in_any_span(p, day_spans)]
    print(f"  {len(unlocated)} unlocated in range, {len(in_span)} within GPS day spans", flush=True)

    # ── Build card data ───────────────────────────────────────────────────────
    matched_cards   = []
    unmatched_cards = []

    total_cards = len(in_span)
    for idx, p in enumerate(in_span):
        if idx % 20 == 0:
            print(f"  Building card {idx+1}/{total_cards}...", end="\r", flush=True)

        target_thumb = thumb_b64(p.path_derivatives) if p.path_derivatives else None
        target_date  = p.date.strftime("%Y-%m-%d %H:%M:%S %Z") if p.date else ""

        match = algo_matches.get(p.uuid)

        if match:
            src = gps_by_uuid.get(match["source_uuid"])
            src_thumb = thumb_b64(src.path_derivatives) if src and src.path_derivatives else None
            src_date  = src.date.strftime("%Y-%m-%d %H:%M:%S %Z") if src and src.date else ""
            delta     = format_timedelta(p.date, src.date) if src and src.date else ""

            matched_cards.append({
                "target_uuid":            p.uuid,
                "target_filename":        p.original_filename or p.filename or p.uuid,
                "target_date":            target_date,
                "target_thumb":           target_thumb,
                "has_match":              True,
                "proposed_lat":           match["lat"],
                "proposed_lon":           match["lon"],
                "proposed_source_uuid":   match["source_uuid"],
                "proposed_source_filename": src.original_filename if src else match["source_name"],
                "proposed_source_date":   src_date,
                "proposed_source_thumb":  src_thumb,
                "delta":                  delta,
            })
        else:
            unmatched_cards.append({
                "target_uuid":            p.uuid,
                "target_filename":        p.original_filename or p.filename or p.uuid,
                "target_date":            target_date,
                "target_thumb":           target_thumb,
                "has_match":              False,
                "proposed_lat":           None,
                "proposed_lon":           None,
                "proposed_source_uuid":   None,
                "proposed_source_filename": None,
                "proposed_source_date":   None,
                "proposed_source_thumb":  None,
                "delta":                  "",
            })

    print(f"\nBuilding HTML → {output_path}", flush=True)
    html = build_html(
        matched_cards, unmatched_cards, gps_photos_data,
        from_date, to_date, window, total_processed, total_missing,
    )
    Path(output_path).write_text(html, encoding="utf-8")

    print(f"\nDone → {Path(output_path).resolve()}")
    print(f"  {len(matched_cards)} auto-matched cards  +  {len(unmatched_cards)} manual-assign cards")
    print(f"  {len(gps_photos_data)} GPS source pins on map")
    print(f"\nOpen in browser, review, export approved JSON, then:")
    print(f"  python3.13 apply_locations.py {default_export_name(from_date, to_date)}")


if __name__ == "__main__":
    main()
