-- photo_gps.db  —  DoltLite schema for Photo GPS Tagger
-- ─────────────────────────────────────────────────────────────────────────────

-- ── Core photo record ────────────────────────────────────────────────────────
-- One row per UUID. Synced from the Photos library; app columns are
-- managed exclusively by this tool.

CREATE TABLE IF NOT EXISTS photos (
    uuid                TEXT    PRIMARY KEY,
    original_filename   TEXT,

    -- Dates Photos knows about
    date_taken          TEXT,           -- ISO8601; what Photos shows
    date_added          TEXT,           -- when the photo was imported
    date_modified       TEXT,           -- last change in Photos

    -- Raw EXIF (may differ from date_taken when Photos corrected it)
    exif_date           TEXT,

    -- Camera
    camera_make         TEXT,
    camera_model        TEXT,
    width               INTEGER,
    height              INTEGER,

    -- Current GPS in Photos (what the library currently holds)
    current_lat         REAL,
    current_lon         REAL,

    -- Original EXIF GPS, if the photo arrived with one (snapshot at first sync)
    exif_lat            REAL,
    exif_lon            REAL,

    -- Library state flags (refreshed on every sync)
    is_screenshot       INTEGER DEFAULT 0,
    is_missing          INTEGER DEFAULT 0,  -- iCloud-only, not on local disk
    is_in_trash         INTEGER DEFAULT 0,
    is_shared           INTEGER DEFAULT 0,  -- iCloud Shared Library photo
    is_syndicated       INTEGER DEFAULT 0,  -- "Shared with You" iMessage photo; not writable via AppleScript

    -- ── App-managed workflow state ───────────────────────────────────────────
    -- Status lifecycle:
    --   unchecked       → never appeared in the review UI
    --   reviewed        → seen in UI, no decision recorded
    --   approved        → user confirmed a GPS assignment (coords below)
    --   rejected        → user said "no GPS for this photo"
    --   applied         → GPS successfully written to Photos.app
    --   archived        → suppressed; will never appear again
    --   pending_download→ is_missing=1; shown in a separate "needs download" list
    app_status          TEXT    DEFAULT 'unchecked',

    -- GPS this tool assigned (may differ from current_lat/lon if Photos synced
    -- something else in the meantime — detected by the Sync Diff button)
    assigned_lat        REAL,
    assigned_lon        REAL,
    assignment_source   TEXT,   -- algorithm | manual_pin | manual_entry
    source_uuid         TEXT,   -- UUID of the GPS-donor photo (algorithm/pin)

    -- Outcome
    applied_at          TEXT,   -- ISO8601 timestamp of successful write
    archived_at         TEXT,   -- ISO8601 timestamp of archive action

    notes               TEXT,

    -- Sync bookkeeping
    last_synced_at      TEXT    -- ISO8601 timestamp of most recent sync read
);

-- ── Sync run log ─────────────────────────────────────────────────────────────
-- One row per sync operation (full or incremental).

CREATE TABLE IF NOT EXISTS sync_runs (
    id              INTEGER PRIMARY KEY,
    started_at      TEXT    NOT NULL,
    completed_at    TEXT,
    sync_type       TEXT,           -- full | incremental
    photos_added    INTEGER DEFAULT 0,
    photos_updated  INTEGER DEFAULT 0,
    photos_removed  INTEGER DEFAULT 0,  -- UUIDs that disappeared from library
    library_path    TEXT,
    notes           TEXT
);

-- ── Apply run log ────────────────────────────────────────────────────────────
-- One row per "Apply Approved" session.

CREATE TABLE IF NOT EXISTS apply_runs (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT    NOT NULL,
    completed_at TEXT,
    from_date   TEXT,
    to_date     TEXT,
    submitted   INTEGER DEFAULT 0,  -- photos sent to photoscript
    applied     INTEGER DEFAULT 0,  -- successfully written
    skipped     INTEGER DEFAULT 0,  -- already had GPS
    errors      INTEGER DEFAULT 0
);

-- ── Per-photo apply results ───────────────────────────────────────────────────
-- One row per photo per apply run.

CREATE TABLE IF NOT EXISTS apply_results (
    id          INTEGER PRIMARY KEY,
    run_id      INTEGER REFERENCES apply_runs(id),
    uuid        TEXT    NOT NULL,
    filename    TEXT,
    outcome     TEXT,           -- applied | skipped | error
    error_msg   TEXT,
    lat         REAL,
    lon         REAL,
    ts          TEXT            -- ISO8601 timestamp of this result
);

-- ── Settings / bookmarks ─────────────────────────────────────────────────────
-- Key-value store for persistent app configuration.
-- Known keys:
--   last_full_sync_at        ISO8601 of most recent full sync
--   last_incremental_sync_at ISO8601 of most recent incremental sync
--   library_path             Photos library path used at last sync

CREATE TABLE IF NOT EXISTS settings (
    key     TEXT PRIMARY KEY,
    value   TEXT
);

-- ── Useful views ─────────────────────────────────────────────────────────────

CREATE VIEW IF NOT EXISTS v_needs_location AS
    SELECT * FROM photos
    WHERE current_lat IS NULL
      AND is_in_trash  = 0
      AND app_status NOT IN ('rejected', 'applied', 'archived');

CREATE VIEW IF NOT EXISTS v_needs_download AS
    SELECT * FROM photos
    WHERE current_lat IS NULL
      AND is_missing   = 1
      AND is_in_trash  = 0
      AND app_status NOT IN ('rejected', 'applied', 'archived');

CREATE VIEW IF NOT EXISTS v_applied AS
    SELECT * FROM photos
    WHERE app_status = 'applied'
    ORDER BY applied_at DESC;

CREATE VIEW IF NOT EXISTS v_sync_drift AS
    -- Photos where we applied a location but current_lat no longer matches
    -- (iCloud synced something different, or Photos reverted the write)
    SELECT uuid, original_filename, date_taken,
           assigned_lat, assigned_lon,
           current_lat,  current_lon,
           applied_at
    FROM   photos
    WHERE  app_status = 'applied'
      AND  (current_lat IS NULL
            OR ABS(current_lat - assigned_lat) > 0.0001
            OR ABS(current_lon - assigned_lon) > 0.0001);
