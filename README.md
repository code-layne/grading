# grading

Scan-to-score grading for the packet-based courses (Algebra 2, AP Statistics,
Precalculus, SAAR). The plan is `grading-system-design.md` in
[code-layne/math-shared](https://github.com/code-layne/math-shared), and the
build side (`\gradedblank`, `manifest.json`, personalized packets) lives there
too.

This repository is the pipeline that runs on the Mac. So far it covers steps 3
and 4 of the design's pipeline, scanning and ingest. Reading, scoring, review
and the reports come next.

## Setup

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[test]'
mkdir -p ~/.config/grading && cp config.example.toml ~/.config/grading/config.toml
```

Edit the config:

| Key | What it is |
| --- | --- |
| `paths.curricula` | The course checkouts. Ingest finds each assignment's `manifest.json` in their build output. |
| `paths.inbox` | Where scan-to-PDF lands. Each PDF moves to `<inbox>/ingested/` once it is ingested. |
| `paths.data` | `grading.sqlite`, the page images and the crops |
| `paths.roster` | `roster-map.csv`. Only the report step will read it, and ingest never does. |
| `[ingest]` | Thresholds; the defaults are explained in `config.example.toml` |

The inbox, the data folder and `roster-map.csv` must be outside every git work
tree, or `grading` refuses to start. `.gitignore` also ignores every PDF,
image, database and CSV, with the test fixtures as the only exception.

## Scanning

Copier settings: 300 dpi, grayscale or color, one PDF per stack, and duplex
when the packets are printed double-sided. Pages can come in any order, a late
paper can be scanned alone or mixed into another stack, and a page fed upside
down is fine.

## Ingest

```sh
grading ingest                       # every PDF in the inbox
grading ingest ~/Desktop/late.pdf    # specific files, left where they are
grading ingest --received 2026-10-20 # the date the stack came in (default today)
grading status                       # counts, blanks to confirm, the unplaced queue
```

Each page of each scan goes through these steps:

1. **Render** at 300 dpi with PyMuPDF.
2. **Read the QR code** with zxing-cpp. It encodes `assignment|page|tracking id`.
   - Decoding runs on the thresholded page, so show-through from the other
     side of the sheet, which never reaches the ink threshold, can't be read.
   - The page's code must also sit where its manifest puts the QR code. A
     code anywhere else (a ghost dark enough to survive, a stray sheet) is
     recorded in `pages.rejected_codes` and ignored.
3. **Look up the manifest**: the `manifest.json` with that assignment id in
   the curriculum checkouts.
4. **Find the four registration marks** with OpenCV: solid squares of the
   mark's size, nearest each corner. A least-squares affine fit maps the
   manifest's layout onto the scan, correcting skew, copier scaling and
   offset. The fit's residual is checked.
5. **Warp the page onto the manifest's layout and blank the name strip.**
   Nothing is stored before this, so no stored image holds a name.
6. **Crop each answer box** with a 6 pt margin.
   - Paper feed is not perfectly even: the Brother test scan has a box 1.7 pt
     off the affine estimate. So each edge of the box is matched to its
     printed border within 4 pt before cropping.
   - Crops are thresholded: everything lighter than the ink threshold
     becomes white, and ink keeps its gray level. That is what is stored and
     what the model will see.
7. **Mark near-empty boxes blank.** A box is blank when, inside its border and
   ignoring specks, it holds less ink than `blank_ink_px`. Blank boxes get no
   API call and go to review as one batch to confirm.

Pages that can't be placed go to the **unplaced queue** (`pages.status =
'unplaced'`, with a reason). The reasons are `qr-unreadable`,
`unknown-assignment`, `marks-not-found`, `marks-inconsistent`, `qr-misplaced`,
`master-copy` and `rotated`. These pages are stored strip-blanked too. A
second copy of a page already placed for the same student and assignment is
stored as a **duplicate**, linked to the first copy and flagged. It never
replaces the first. A scan PDF already ingested (same SHA-256) is skipped.

The scan PDFs themselves are never copied into the data folder, because they
carry names. They stay in `<inbox>/ingested/` until you delete them.

## Data folder

```
grading.sqlite
pages/<scan sha>/<NNN>.png               aligned, strip-blanked, thresholded
crops/<assignment>/<tracking id>/<q>.png thresholded answer boxes
```

The SQLite schema is in `src/grading/db.py`. Student records are keyed by
tracking id, and no table has a name column. The tables are `scans`, `pages`,
`crops`, `assignments` and `questions` (the manifest each assignment was read
against), and `submissions` (dates received, which never change a score).
`readings`, `rulings` and `scores` are defined for the steps to come.

## Tests

```sh
pytest
```

- **Synthetic class set** (`tests/fixtures/synthetic`, from algebra_2's
  graded_sample eval), plus one extra page with its QR code damaged:
  - all 12 students' pages are placed on the right students;
  - the damaged page goes to the unplaced queue, strip-blanked;
  - every one of the 96 crops lands within 2 px (0.5 pt) of its box border;
  - exactly the one blank answer in `expected-scores.csv` is marked blank.
- **Real scan** (`tests/fixtures/real`, a Brother inkjet scan with
  show-through; see its README):
  - all three pages are placed, in reverse roster order;
  - each crop lands on its box despite the uneven feed;
  - all three empty rubric boxes read blank, although show-through text lies
    inside them;
  - the ghost QR code is never decoded, raw or thresholded. A pasted readable
    code at the ghost position is removed by the threshold when faint, and
    rejected by position when dark.
- **Duplicates, re-ingest, the inbox, config validation** (paths inside a
  git work tree are refused), **the schema** (no name columns) and
  **`.gitignore`**.
