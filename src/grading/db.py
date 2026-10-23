"""The grading database: one SQLite file in the data folder.

Every record about a student is keyed by tracking id. There are no name
columns anywhere: names live only in roster-map.csv, which the build and the
final report read and nothing in between does.

Ingest writes scans, pages, crops, assignments, questions and submissions.
Read, score and verify (pipeline.py) write readings, judgments, scores,
api_calls, batches and the review queue. Review writes rulings, corrections
and blank confirmations, and places unplaced pages. The report writes reports
and reported (tracking ids only: the names it joins stay in its PDFs).

The schema changes only by migration: SCHEMA is version 1 as session 3 left
it, and MIGRATIONS[n] takes a database from version n - 1 to n.
"""

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 5

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

-- One row per scanned PDF. The PDF itself is never stored: its cover sheets carry names.
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


MIGRATIONS = {
    # Read, score and verify.
    2: """
-- Which call produced a reading, and the answer as grouping and rulings see it.
ALTER TABLE readings ADD COLUMN request_key TEXT;
ALTER TABLE readings ADD COLUMN normalized TEXT;
CREATE UNIQUE INDEX readings_one_per_pass ON readings (crop_id, pass);

-- A rubric score (one per group of identical answers, on the group's first
-- crop) or a verify verdict (one per crop).
CREATE TABLE judgments (
    id                INTEGER PRIMARY KEY,
    crop_id           INTEGER NOT NULL REFERENCES crops(id),
    pass              TEXT NOT NULL CHECK (pass IN ('rubric', 'verify')),
    assignment        TEXT NOT NULL,
    question          TEXT NOT NULL,
    normalized_answer TEXT,
    points            REAL,
    criteria          TEXT,             -- JSON {criterion: {points, evidence, confidence}}
    feedback          TEXT,
    agrees            INTEGER,          -- verify: 1 agrees with the first reading and score
    dispute           TEXT,
    model             TEXT,
    request_key       TEXT,
    created_at        TEXT NOT NULL,
    UNIQUE (crop_id, pass)
);

-- Every API call and its token counts (dollars are worked out from config prices).
CREATE TABLE api_calls (
    id                    INTEGER PRIMARY KEY,
    request_key           TEXT NOT NULL,
    call_type             TEXT NOT NULL CHECK (call_type IN ('read', 'rubric', 'verify')),
    crop_id               INTEGER REFERENCES crops(id),
    mode                  TEXT NOT NULL CHECK (mode IN ('standard', 'batch')),
    batch_id              TEXT,
    model                 TEXT,
    status                TEXT NOT NULL,  -- ok | refusal | truncated | no-tool-call | invalid | errored | expired | canceled
    error                 TEXT,
    input_tokens          INTEGER NOT NULL DEFAULT 0,
    output_tokens         INTEGER NOT NULL DEFAULT 0,
    cache_write_5m_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_1h_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
    created_at            TEXT NOT NULL
);
CREATE INDEX api_calls_key ON api_calls (request_key);

-- Batch API submissions. A rerun collects an in_progress batch instead of
-- submitting the same requests again.
CREATE TABLE batches (
    id           TEXT PRIMARY KEY,      -- the Batch API's id
    submitted_at TEXT NOT NULL,
    request_keys TEXT NOT NULL,         -- JSON list of custom_ids
    status       TEXT NOT NULL CHECK (status IN ('in_progress', 'collected')),
    collected_at TEXT
);

-- The proposed score and whether it is final or waits for review.
ALTER TABLE scores ADD COLUMN crop_id INTEGER REFERENCES crops(id);
ALTER TABLE scores ADD COLUMN normalized_answer TEXT;
ALTER TABLE scores ADD COLUMN status TEXT;          -- final | review
ALTER TABLE scores ADD COLUMN detail TEXT;          -- JSON: criteria, evidence, confidence

-- What review (session 4b) works through: one row per crop, grouped by
-- question and normalized answer, lowest priority number first.
CREATE TABLE review_queue (
    id                INTEGER PRIMARY KEY,
    crop_id           INTEGER NOT NULL UNIQUE REFERENCES crops(id),
    tracking_id       TEXT NOT NULL,
    assignment        TEXT NOT NULL,
    question          TEXT NOT NULL,
    type              TEXT NOT NULL,
    reasons           TEXT NOT NULL,    -- JSON list, e.g. ["verify-disagreement", "rubric"]
    priority          INTEGER NOT NULL, -- 0 disagreement, 1 no score, 2 low confidence, 3 rubric, 4 blank
    normalized_answer TEXT,
    reading           TEXT,
    verify_reading    TEXT,
    points            REAL,             -- proposed; NULL when nothing could be proposed
    verify_points     REAL,
    max_points        REAL NOT NULL,
    feedback          TEXT,
    detail            TEXT,             -- JSON
    created_at        TEXT NOT NULL,
    resolved_at       TEXT
);
CREATE VIEW review_groups AS
    SELECT assignment, question, normalized_answer, min(priority) AS priority, count(*) AS n
    FROM review_queue WHERE resolved_at IS NULL
    GROUP BY assignment, question, normalized_answer;
""",
    # Review and the reports.
    3: """
-- The reading a ruling was made on, as shown in review (rulings are keyed by
-- its normalized form).
ALTER TABLE rulings ADD COLUMN reading TEXT;

-- A reading fixed by hand in review. It replaces the read pass's reading for
-- this crop in every later run, and needs no verify call.
CREATE TABLE corrections (
    crop_id    INTEGER PRIMARY KEY REFERENCES crops(id),
    reading    TEXT NOT NULL,
    normalized TEXT NOT NULL,
    decided_at TEXT NOT NULL
);

-- How a review item was closed: settled (a ruling, a correction or a
-- confirmed blank made it final) or decided (a score for this crop alone).
ALTER TABLE review_queue ADD COLUMN resolution TEXT;

-- Every report written, and which submissions each final one covered. A
-- report covers the submissions no earlier final report has.
CREATE TABLE reports (
    id         INTEGER PRIMARY KEY,
    run_date   TEXT NOT NULL,
    folder     TEXT NOT NULL,           -- relative to the data folder
    partial    INTEGER NOT NULL CHECK (partial IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE reported (
    report_id   INTEGER NOT NULL REFERENCES reports(id),
    tracking_id TEXT NOT NULL,
    assignment  TEXT NOT NULL,
    PRIMARY KEY (report_id, tracking_id, assignment)
);
""",
    # grading batch cancel. SQLite can't change a CHECK, so batches is rebuilt.
    4: """
-- A batch is in_progress until it ends and its results are collected, or
-- canceling from `grading batch cancel` until it ends and is canceled: the
-- results that came in before the cancel are stored, and the requests that
-- never ran are sent again by the next run.
CREATE TABLE batches_v4 (
    id           TEXT PRIMARY KEY,
    submitted_at TEXT NOT NULL,
    request_keys TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('in_progress', 'canceling', 'collected', 'canceled')),
    collected_at TEXT,
    canceled_at  TEXT
);
INSERT INTO batches_v4 (id, submitted_at, request_keys, status, collected_at)
    SELECT id, submitted_at, request_keys, status, collected_at FROM batches;
DROP TABLE batches;
ALTER TABLE batches_v4 RENAME TO batches;
""",
    # Cover sheets.
    5: """
-- Pages of the scan not stored at all, as JSON [{"scan_page": n, "reason":
-- "cover"}]: a cover sheet carries the student's name, so nothing else
-- about it is kept, not even its QR code's text.
ALTER TABLE scans ADD COLUMN skipped_pages TEXT;
""",
}


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        conn.execute("INSERT INTO meta VALUES ('schema_version', '1')")
        conn.commit()
        version = 1
    else:
        version = int(row["value"])
    if version > SCHEMA_VERSION:
        raise RuntimeError(f"{path} has schema version {version}, newer than this code ({SCHEMA_VERSION})")
    for n in range(version + 1, SCHEMA_VERSION + 1):
        _migrate(conn, n)
    return conn


def _migrate(conn, n):
    # executescript commits first, so run the statements one by one inside
    # an explicit transaction: a failed migration leaves version n - 1 intact.
    conn.execute("BEGIN")
    try:
        for stmt in _statements(MIGRATIONS[n]):
            conn.execute(stmt)
        conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(n),))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _statements(script):
    buf = ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            yield buf.strip()
            buf = ""
    if buf.strip():
        raise ValueError(f"incomplete SQL statement: {buf.strip()[:60]}")
