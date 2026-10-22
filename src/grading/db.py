"""The grading database: one SQLite file in the data folder.

Every record about a student is keyed by tracking id. There are no name
columns anywhere: names live only in roster-map.csv, which the build and the
final report read and nothing in between does.

Ingest (this stage) writes scans, pages, crops, assignments, questions and
submissions. readings, rulings and scores are here so the schema is whole;
the read, score and review steps fill them.
"""

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per assignment seen in a scan, with the manifest it was graded against.
CREATE TABLE IF NOT EXISTS assignments (
    assignment      TEXT PRIMARY KEY,
    course          TEXT,
    pages           INTEGER NOT NULL,
    manifest_path   TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    manifest_json   TEXT NOT NULL,
    first_seen      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS questions (
    assignment  TEXT NOT NULL REFERENCES assignments(assignment),
    question    TEXT NOT NULL,
    type        TEXT NOT NULL CHECK (type IN ('choice', 'short', 'rubric')),
    points      REAL NOT NULL,
    page        INTEGER NOT NULL,
    box         TEXT NOT NULL,          -- JSON [x, y, w, h], PDF points
    answer      TEXT,
    check_kind  TEXT,                   -- expression | numeric | set | text
    tolerance   REAL,
    rubric      TEXT,                   -- rubric path, relative to the lesson directory
    PRIMARY KEY (assignment, question)
);

-- One row per scanned PDF. The PDF itself is never stored: its pages carry names.
CREATE TABLE IF NOT EXISTS scans (
    id          INTEGER PRIMARY KEY,
    sha256      TEXT NOT NULL UNIQUE,
    filename    TEXT NOT NULL,
    page_count  INTEGER NOT NULL,
    ingested_at TEXT NOT NULL,
    received_on TEXT NOT NULL           -- the date the stack came in
);

-- Every scanned page. placed: matched to a student and aligned. duplicate: a
-- second copy of a page already placed (flagged, never replaces it).
-- unplaced: QR unreadable, marks not found, unknown assignment... (reason).
CREATE TABLE IF NOT EXISTS pages (
    id              INTEGER PRIMARY KEY,
    scan_id         INTEGER NOT NULL REFERENCES scans(id),
    scan_page       INTEGER NOT NULL,   -- 1-based page in the scanned PDF
    status          TEXT NOT NULL CHECK (status IN ('placed', 'duplicate', 'unplaced')),
    reason          TEXT,
    detail          TEXT,
    tracking_id     TEXT,
    assignment      TEXT,
    page            INTEGER,            -- the assignment's own page number, from the QR code
    duplicate_of    INTEGER REFERENCES pages(id),
    received_on     TEXT NOT NULL,
    transform       TEXT,               -- JSON 2x3 affine, canonical px -> scan px
    mark_residual   REAL,
    rejected_codes  TEXT,               -- JSON: decoded codes that were not the page's QR
    image_path      TEXT NOT NULL,      -- relative to the data folder; strip blanked
    resolved_at     TEXT,               -- review clears an unplaced or duplicate page
    UNIQUE (scan_id, scan_page)
);
CREATE UNIQUE INDEX IF NOT EXISTS pages_one_placed
    ON pages (tracking_id, assignment, page) WHERE status = 'placed';
CREATE INDEX IF NOT EXISTS pages_student ON pages (tracking_id, assignment);

-- One answer-box crop per question on a placed or duplicate page.
CREATE TABLE IF NOT EXISTS crops (
    id              INTEGER PRIMARY KEY,
    page_id         INTEGER NOT NULL REFERENCES pages(id),
    tracking_id     TEXT NOT NULL,
    assignment      TEXT NOT NULL,
    question        TEXT NOT NULL,
    path            TEXT NOT NULL,      -- relative to the data folder; thresholded
    border_px       TEXT NOT NULL,      -- JSON [x0, y0, x1, y1]: the box border found on the aligned page
    ink_px          INTEGER NOT NULL,
    blank           INTEGER NOT NULL CHECK (blank IN (0, 1)),
    blank_confirmed INTEGER,            -- review: 1 really blank, 0 light pencil
    UNIQUE (page_id, question)
);

-- Date received per student and assignment: recorded, never changes a score.
CREATE TABLE IF NOT EXISTS submissions (
    tracking_id       TEXT NOT NULL,
    assignment        TEXT NOT NULL,
    first_received_on TEXT NOT NULL,
    last_received_on  TEXT NOT NULL,
    PRIMARY KEY (tracking_id, assignment)
);

-- Later steps (read, score, review).
CREATE TABLE IF NOT EXISTS readings (
    id          INTEGER PRIMARY KEY,
    crop_id     INTEGER NOT NULL REFERENCES crops(id),
    pass        TEXT NOT NULL CHECK (pass IN ('read', 'verify')),
    reading     TEXT,
    latex       TEXT,
    confidence  TEXT,
    illegible   INTEGER,
    model       TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rulings (
    assignment        TEXT NOT NULL,
    question          TEXT NOT NULL,
    normalized_answer TEXT NOT NULL,
    points            REAL NOT NULL,
    feedback          TEXT,
    decided_at        TEXT NOT NULL,
    PRIMARY KEY (assignment, question, normalized_answer)
);

CREATE TABLE IF NOT EXISTS scores (
    tracking_id TEXT NOT NULL,
    assignment  TEXT NOT NULL,
    question    TEXT NOT NULL,
    points      REAL,
    max_points  REAL NOT NULL,
    source      TEXT,                   -- code | rubric | ruling | review
    feedback    TEXT,
    scored_at   TEXT NOT NULL,
    PRIMARY KEY (tracking_id, assignment, question)
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        conn.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        conn.commit()
    elif int(row["value"]) != SCHEMA_VERSION:
        raise RuntimeError(f"{path} has schema version {row['value']}, expected {SCHEMA_VERSION}")
    return conn
