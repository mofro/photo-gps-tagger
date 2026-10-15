"""
DoltLite database layer for Photo GPS Tagger.

import doltlite MUST come before any sqlite3 import — it patches
libsqlite3 at the dylib level so the standard sqlite3 module talks
to DoltLite instead.
"""
import doltlite          # noqa: F401  — must be first import
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent / "photo_gps.db"
SCHEMA  = Path(__file__).parent / "schema.sql"

# ─────────────────────────────────────────────────────────────────────────────
# Connection
# ─────────────────────────────────────────────────────────────────────────────

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ─────────────────────────────────────────────────────────────────────────────
# Initialisation
# ─────────────────────────────────────────────────────────────────────────────

def init_db() -> None:
    """Create tables (if needed) and make an initial Dolt commit."""
    conn = _connect()
    conn.executescript(SCHEMA.read_text())
    conn.commit()

    # Migrate: add is_syndicated column if it doesn't exist yet
    try:
        conn.execute("ALTER TABLE photos ADD COLUMN is_syndicated INTEGER DEFAULT 0")
        conn.commit()
    except Exception:
        pass  # column already exists

    # Configure Dolt identity (idempotent)
    conn.execute("SELECT dolt_config('user.name',  'photo-gps-tagger')")
    conn.execute("SELECT dolt_config('user.email', 'photo-gps@local')")

    # Initial commit only if there are no commits yet
    try:
        conn.execute("SELECT dolt_commit('--allow-empty', '-m', 'Initialize photo GPS database')")
        conn.commit()
    except Exception:
        pass  # already initialised

    conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Sync helpers
# ─────────────────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _photo_row(p) -> dict:
    """Convert an osxphotos PhotoInfo object to a DB row dict."""
    loc = p.location or (None, None)

    exif       = p.exif_info if hasattr(p, "exif_info") else None
    exif_date  = None
    exif_lat   = None
    exif_lon   = None
    cam_make   = None
    cam_model  = None
    width      = None
    height     = None

    if exif:
        try: exif_lat, exif_lon = exif.latitude, exif.longitude
        except Exception: pass
        try: exif_date = str(exif.date_time_original) if exif.date_time_original else None
        except Exception: pass
        try: cam_make  = exif.lens_make or exif.make
        except Exception: pass
        try: cam_model = exif.lens_model or exif.model
        except Exception: pass

    if hasattr(p, "width"):  width  = p.width
    if hasattr(p, "height"): height = p.height

    return {
        "uuid":              p.uuid,
        "original_filename": p.original_filename or p.filename or p.uuid,
        "date_taken":        p.date.isoformat()          if p.date          else None,
        "date_added":        p.date_added.isoformat()    if hasattr(p, "date_added")    and p.date_added    else None,
        "date_modified":     p.date_modified.isoformat() if hasattr(p, "date_modified") and p.date_modified else None,
        "exif_date":         exif_date,
        "camera_make":       cam_make,
        "camera_model":      cam_model,
        "width":             width,
        "height":            height,
        "current_lat":       loc[0],
        "current_lon":       loc[1],
        "exif_lat":          exif_lat,
        "exif_lon":          exif_lon,
        "is_screenshot":     1 if p.screenshot else 0,
        "is_missing":        1 if p.ismissing  else 0,
        "is_in_trash":       1 if p.intrash    else 0,
        "is_shared":         1 if getattr(p, "shared", False) else 0,
        "is_syndicated":     1 if getattr(p, "syndicated", False) else 0,
        "last_synced_at":    _now(),
    }


def sync_full(db: "osxphotos.PhotosDB", emit=print) -> dict:
    """
    Full sync — read every photo from the Photos library into the DB.
    Preserves existing app_status / assigned coords / applied_at etc.
    Returns counts: added, updated, removed.
    """
    conn   = _connect()
    cursor = conn.cursor()
    started = _now()
    emit("Full sync: reading all photos from library…")

    all_photos = db.photos(images=True)
    lib_uuids  = set()
    added = updated = 0

    for i, p in enumerate(all_photos):
        if i % 500 == 0 and i:
            emit(f"  …{i} photos processed")
        row       = _photo_row(p)
        lib_uuids.add(p.uuid)

        existing = cursor.execute(
            "SELECT uuid FROM photos WHERE uuid = ?", (p.uuid,)
        ).fetchone()

        if existing is None:
            # INSERT — preserve defaults for all app_ columns
            cols = ", ".join(row.keys())
            plch = ", ".join("?" * len(row))
            cursor.execute(f"INSERT INTO photos ({cols}) VALUES ({plch})", list(row.values()))
            added += 1
        else:
            # UPDATE only the library-sourced columns, leave app_ columns alone
            set_clause = ", ".join(f"{k} = ?" for k in row if not k.startswith("app_")
                                   and k not in ("assigned_lat","assigned_lon",
                                                 "assignment_source","source_uuid",
                                                 "applied_at","archived_at","notes"))
            vals = [row[k] for k in row if not k.startswith("app_")
                    and k not in ("assigned_lat","assigned_lon",
                                  "assignment_source","source_uuid",
                                  "applied_at","archived_at","notes")]
            cursor.execute(f"UPDATE photos SET {set_clause} WHERE uuid = ?",
                           vals + [p.uuid])
            updated += 1

    # Mark photos no longer in the library (deleted / removed)
    db_uuids = {r[0] for r in cursor.execute("SELECT uuid FROM photos").fetchall()}
    removed_uuids = db_uuids - lib_uuids
    removed = len(removed_uuids)
    for uuid in removed_uuids:
        cursor.execute(
            "UPDATE photos SET app_status = 'archived', archived_at = ? WHERE uuid = ?",
            (_now(), uuid)
        )

    # Record sync run
    cursor.execute(
        "INSERT INTO sync_runs (started_at, completed_at, sync_type, "
        "photos_added, photos_updated, photos_removed) VALUES (?,?,?,?,?,?)",
        (started, _now(), "full", added, updated, removed)
    )
    _set(cursor, "last_full_sync_at", _now())

    conn.commit()
    _dolt_commit(conn, f"Full sync: +{added} ~{updated} -{removed} photos")
    conn.close()

    emit(f"Full sync complete: {added} added, {updated} updated, {removed} removed.")
    return {"added": added, "updated": updated, "removed": removed}


def sync_incremental(db: "osxphotos.PhotosDB", emit=print) -> dict:
    """
    Incremental sync — only re-read photos modified since last sync.
    Also checks for newly disappeared UUIDs.
    """
    conn   = _connect()
    cursor = conn.cursor()
    started = _now()

    last = _get(cursor, "last_incremental_sync_at") or _get(cursor, "last_full_sync_at")
    emit(f"Incremental sync since {last or 'beginning'}…")

    from datetime import datetime as dt
    cutoff = dt.fromisoformat(last) if last else None

    candidates = [
        p for p in db.photos(images=True)
        if cutoff is None
        or (p.date_modified and p.date_modified.replace(tzinfo=None) >= cutoff.replace(tzinfo=None))
    ]
    emit(f"  {len(candidates)} photos to re-examine.")

    added = updated = 0
    for p in candidates:
        row      = _photo_row(p)
        existing = cursor.execute(
            "SELECT uuid FROM photos WHERE uuid = ?", (p.uuid,)
        ).fetchone()

        if existing is None:
            cols = ", ".join(row.keys())
            plch = ", ".join("?" * len(row))
            cursor.execute(f"INSERT INTO photos ({cols}) VALUES ({plch})", list(row.values()))
            added += 1
        else:
            set_clause = ", ".join(f"{k} = ?" for k in row
                                   if not k.startswith("app_")
                                   and k not in ("assigned_lat","assigned_lon",
                                                 "assignment_source","source_uuid",
                                                 "applied_at","archived_at","notes"))
            vals = [row[k] for k in row
                    if not k.startswith("app_")
                    and k not in ("assigned_lat","assigned_lon",
                                  "assignment_source","source_uuid",
                                  "applied_at","archived_at","notes")]
            cursor.execute(f"UPDATE photos SET {set_clause} WHERE uuid = ?",
                           vals + [p.uuid])
            updated += 1

    cursor.execute(
        "INSERT INTO sync_runs (started_at, completed_at, sync_type, "
        "photos_added, photos_updated) VALUES (?,?,?,?,?)",
        (started, _now(), "incremental", added, updated)
    )
    _set(cursor, "last_incremental_sync_at", _now())

    conn.commit()
    if added or updated:
        _dolt_commit(conn, f"Incremental sync: +{added} ~{updated} photos")
    conn.close()

    emit(f"Incremental sync: {added} added, {updated} updated.")
    return {"added": added, "updated": updated, "removed": 0}


# ─────────────────────────────────────────────────────────────────────────────
# Generate — query unlocated photos for the review UI
# ─────────────────────────────────────────────────────────────────────────────

def get_excluded_uuids() -> set:
    """UUIDs the DB considers done or unwritable — applied, archived, rejected, or pending_download."""
    conn  = _connect()
    rows  = conn.execute(
        "SELECT uuid FROM photos WHERE app_status IN "
        "('applied','archived','rejected','pending_download')"
    ).fetchall()
    conn.close()
    return {r[0] for r in rows}


def get_unlocated(from_date: Optional[str], to_date: Optional[str],
                  include_missing: bool = False) -> list[dict]:
    """
    Return photos that need a GPS location for the given date range.
    Excludes: already-located, trashed, applied, archived, rejected.
    Optionally includes iCloud-only (missing) photos.
    """
    conn   = _connect()
    cursor = conn.cursor()

    clauses = [
        "current_lat IS NULL",
        "is_in_trash = 0",
        "is_syndicated = 0",
        "app_status NOT IN ('applied', 'archived', 'rejected')",
    ]
    if not include_missing:
        clauses.append("is_missing = 0")
    if from_date:
        clauses.append(f"date_taken >= '{from_date}'")
    if to_date:
        clauses.append(f"date_taken <= '{to_date}T23:59:59'")

    where = " AND ".join(clauses)
    rows  = cursor.execute(f"SELECT * FROM photos WHERE {where} ORDER BY date_taken").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_missing_count(from_date: Optional[str], to_date: Optional[str]) -> int:
    """Count iCloud-only unlocated photos excluded from the current range."""
    conn   = _connect()
    cursor = conn.cursor()
    clauses = [
        "current_lat IS NULL",
        "is_missing = 1",
        "is_in_trash = 0",
        "app_status NOT IN ('applied', 'archived', 'rejected')",
    ]
    if from_date: clauses.append(f"date_taken >= '{from_date}'")
    if to_date:   clauses.append(f"date_taken <= '{to_date}T23:59:59'")
    n = cursor.execute(
        f"SELECT COUNT(*) FROM photos WHERE {' AND '.join(clauses)}"
    ).fetchone()[0]
    conn.close()
    return n


# ─────────────────────────────────────────────────────────────────────────────
# Apply — record results
# ─────────────────────────────────────────────────────────────────────────────

def record_apply_run(from_date: str, to_date: str,
                     results: list[dict]) -> int:
    """
    Persist an apply run and its per-photo results.
    Each result dict: {uuid, filename, outcome, lat, lon, error_msg}
    Returns the run_id.
    """
    conn   = _connect()
    cursor = conn.cursor()
    now    = _now()

    applied  = sum(1 for r in results if r["outcome"] == "applied")
    skipped  = sum(1 for r in results if r["outcome"] == "skipped")
    errors   = sum(1 for r in results if r["outcome"] == "error")

    cursor.execute(
        "INSERT INTO apply_runs (started_at, completed_at, from_date, to_date, "
        "submitted, applied, skipped, errors) VALUES (?,?,?,?,?,?,?,?)",
        (now, now, from_date, to_date, len(results), applied, skipped, errors)
    )
    run_id = cursor.lastrowid

    for r in results:
        cursor.execute(
            "INSERT INTO apply_results (run_id, uuid, filename, outcome, "
            "error_msg, lat, lon, ts) VALUES (?,?,?,?,?,?,?,?)",
            (run_id, r["uuid"], r.get("filename"), r["outcome"],
             r.get("error_msg"), r.get("lat"), r.get("lon"), now)
        )
        if r["outcome"] == "applied":
            cursor.execute(
                "UPDATE photos SET app_status='applied', applied_at=?, "
                "assigned_lat=?, assigned_lon=? WHERE uuid=?",
                (now, r.get("lat"), r.get("lon"), r["uuid"])
            )
        elif r["outcome"] == "not_writable":
            # Not accessible via Photos.app AppleScript (e.g. "Shared with You" iMessage photos)
            cursor.execute(
                "UPDATE photos SET app_status='archived', archived_at=?, "
                "notes=? WHERE uuid=? AND app_status NOT IN ('applied','archived')",
                (now, "not_writable: not accessible via Photos.app AppleScript", r["uuid"])
            )
        elif r["outcome"] == "error" and "Invalid photo id" in (r.get("error_msg") or ""):
            cursor.execute(
                "UPDATE photos SET app_status='pending_download' WHERE uuid=? "
                "AND app_status NOT IN ('applied','archived')",
                (r["uuid"],)
            )

    conn.commit()
    _dolt_commit(conn,
        f"Apply run {run_id}: {applied} applied, {skipped} skipped, {errors} errors "
        f"({from_date} → {to_date})")
    conn.close()
    return run_id


def mark_archived(uuids: list[str]) -> None:
    """Suppress a list of photos from future review runs."""
    if not uuids:
        return
    conn   = _connect()
    now    = _now()
    conn.executemany(
        "UPDATE photos SET app_status='archived', archived_at=? WHERE uuid=?",
        [(now, u) for u in uuids]
    )
    conn.commit()
    _dolt_commit(conn, f"Archived {len(uuids)} photo(s)")
    conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Sync diff — detect drift between DB and current Photos state
# ─────────────────────────────────────────────────────────────────────────────

def get_sync_drift() -> list[dict]:
    """
    Photos we marked 'applied' but whose current GPS no longer matches
    what we wrote — iCloud may have synced something different.
    """
    conn  = _connect()
    rows  = conn.execute("SELECT * FROM v_sync_drift").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_db_stats() -> dict:
    conn   = _connect()
    cursor = conn.cursor()
    total     = cursor.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
    located   = cursor.execute("SELECT COUNT(*) FROM photos WHERE current_lat IS NOT NULL").fetchone()[0]
    applied   = cursor.execute("SELECT COUNT(*) FROM photos WHERE app_status='applied'").fetchone()[0]
    archived  = cursor.execute("SELECT COUNT(*) FROM photos WHERE app_status='archived'").fetchone()[0]
    missing   = cursor.execute("SELECT COUNT(*) FROM photos WHERE is_missing=1").fetchone()[0]
    pending   = cursor.execute("SELECT COUNT(*) FROM photos WHERE app_status='pending_download'").fetchone()[0]
    last_sync = _get(cursor, "last_full_sync_at") or _get(cursor, "last_incremental_sync_at")
    conn.close()
    return {
        "total": total, "located": located, "unlocated": total - located,
        "applied": applied, "archived": archived,
        "missing": missing, "pending_download": pending,
        "last_sync": last_sync,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get(cursor, key: str) -> Optional[str]:
    row = cursor.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _set(cursor, key: str, value: str) -> None:
    cursor.execute(
        "INSERT INTO settings (key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value)
    )


def _dolt_commit(conn: sqlite3.Connection, message: str) -> None:
    try:
        conn.execute("SELECT dolt_add('-A')")
        conn.execute("SELECT dolt_commit('--allow-empty', '-m', ?)", (message,))
        conn.commit()
    except Exception as e:
        print(f"[dolt commit warning] {e}")
