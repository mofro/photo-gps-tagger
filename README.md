# Photo GPS Tagger

A local web tool for bulk-tagging unlocated photos in macOS Photos.app with GPS
coordinates inferred from nearby photos taken on the same day(s).

## How it works

1. **Sync** — scans your Photos library (via `osxphotos`) and builds a local
   database of unlocated photos grouped by date range.
2. **Generate** — for a given date window, finds photos that *do* have GPS
   within ±3 days and proposes those coordinates for the unlocated ones.
3. **Review** — a browser UI shows thumbnail cards; you approve, skip, or
   override each proposed location on a map.
4. **Apply** — confirmed locations are written back to Photos.app via
   AppleScript (`osascript`), which syncs to iCloud automatically.

The database (`photo_gps.db`) is backed by DoltLite for version-controlled
GPS history.

## Requirements

- macOS with Photos.app
- Python 3.13
- Full Disk Access for the app that launches the server (for example, Terminal
   or VS Code). Enable it in **System Settings → Privacy & Security → Full Disk
   Access**, then quit and reopen that app before starting the server.
- Photos.app must have Automation permission for your terminal / the app that
  launches the server

Install dependencies:

```bash
pip3.13 install flask osxphotos Pillow photoscript doltlite --break-system-packages
```

## Running

```bash
./launch_server.sh
```

Or directly:

```bash
python3.13 server.py
```

The server starts at **http://localhost:9174** and opens in your browser.

## Workflow

1. Click **Sync** to import your Photos library into the local DB (first run
   takes a few minutes for large libraries).
2. Set a **date range** and click **Generate** to produce GPS proposals for
   that window.
3. Review the cards — each shows the photo thumbnail, proposed location on a
   map, and confidence. Check the box to approve; click the map to override.
4. Click **Apply** to write approved locations to Photos.app. Progress streams
   in real time.

## Notes on photo accessibility

- **iCloud-only photos** (not yet downloaded locally) *can* be tagged — Photos
  syncs the GPS metadata back to iCloud without needing the full image file.
- **"Shared with You" photos** (iMessage/FaceTime shares, `syndicated=True`)
  are *not* accessible via Photos.app AppleScript and are automatically
  archived with status `not_writable`. This is a Photos.app limitation.
- Roughly 1,100 photos in a typical library fall into the syndicated category
  and will never be writable by this tool.

## Files

| File | Purpose |
|------|---------|
| `server.py` | Flask web server — all routes and SSE streaming |
| `db.py` | DoltLite database layer |
| `apply_worker.py` | Subprocess worker that writes GPS via `osascript` |
| `schema.sql` | Database schema |
| `launch_server.sh` | One-liner launch script |
| `review_locations.py` | (legacy) CLI review script |
| `apply_locations.py` | (legacy) CLI apply script |
| `diag_uuid_check.py` | Diagnostic: cross-references osxphotos vs AppleScript UUIDs |
