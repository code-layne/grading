# grading

Scan-to-score grading for the packet-based courses (Algebra 2, AP Statistics,
Precalculus, SAAR). The plan is `grading-system-design.md` in
[code-layne/math-shared](https://github.com/code-layne/math-shared), and the
build side (`\gradedblank`, `manifest.json`, personalized packets) lives there
too.

This repository is the pipeline that runs on the Mac: steps 3 to 9 of the
design's pipeline, from scanning to the score report and the feedback sheets.
Start with [Grading a class set](#grading-a-class-set).

## Grading a class set

From a stack of packets to stapled feedback sheets:

1. **Scan** the stack on the copier (300 dpi, one PDF, duplex if printed
   double-sided) to the inbox. Order and orientation don't matter.
2. **Ingest**: `grading ingest`. Pass `--received 2026-10-20` if the papers
   came in on another day. It prints how many pages were placed; anything
   unplaced waits for step 5.
3. **Read, score and verify**: `grading read`. It shows the number of calls
   and the estimated cost and asks before sending anything. A class set goes
   through the Batch API, usually within the hour; `--no-wait` lets you walk
   away, and running `grading read` again collects it.
4. **Review**: `grading review` opens the queue in the browser. Work down it
   with the keyboard (`?` shows the keys): approve with `a`, change a score
   with `s`, fix a misread answer with `r`, edit the feedback note with `f`,
   and confirm the blank batch at the end. Each approval is a ruling, so the
   same answer from any other student, now or later, is scored the same way.
5. **Unplaced pages** (`u` in review): for a page whose QR code couldn't be
   read, find it in the paper stack (`grading status` names the scan file
   and page), look up the student's tracking id in roster-map.csv, and place
   it. Then run `grading read` again to grade it.
6. **Report**: `grading report`. It refuses while anything is still in
   review; `--partial` reports anyway and marks what it left out. It writes
   `<data>/reports/<date>/`.
7. **Enter scores** in the Blackbaud gradebook from `scores.pdf` (or
   `scores.csv`), check the misses in `summary.pdf`, then **print**
   `feedback-sheets-<section>.pdf` and staple each sheet to its packet. The
   sheets are in gradebook order, the order `grading.mk` printed the packets
   in.

**Late papers** go through the same steps. Scan them alone or mixed into
another stack; rulings from the class set score their matching answers
without review, and the next `grading report` lists only the students whose
papers are new.

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
| `paths.roster` | `roster-map.csv`. Only `grading report` reads it. |
| `[ingest]` | Thresholds; the defaults are explained in `config.example.toml` |
| `[api]` | Models, effort, the batch threshold, the cost ceiling |
| `[prices]` | Dollars per million tokens, for the estimate and the report |

The API key lives in the macOS Keychain. Add it once (it prompts for the key):

```sh
security add-generic-password -a "$USER" -s anthropic-grading -w
```

Without a Keychain entry, `grading` uses `$ANTHROPIC_API_KEY`. The key is
passed to the SDK and nowhere else: it is never printed, logged or recorded.

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

## Read, score and verify

```sh
grading read                         # every crop on a placed page not graded yet
grading read --assignment alg2-u3-l1-gs
grading read --no-wait               # submit a batch and exit; rerun to collect it
grading read --record ~/Grading/recordings/2026-10-22
grading read --replay ~/Grading/recordings/2026-10-22
grading status                       # adds: scored final, to review, batches running
```

Every crop on a placed page (duplicates are flagged at ingest and skipped
here) goes through three passes. All three use the Anthropic Python SDK and
answer through a strict tool schema, so no prose is ever parsed.

1. **Read.** One call per non-blank crop asks for an exact transcription,
   never solved or corrected: `reading`, `latex`, `confidence` (high, medium,
   low) and `illegible`. A choice item's reading is a single letter (the
   schema allows only A to Z). The read prompt carries no answer key, so a
   misread can't drift toward the right answer.
2. **Score.** Rulings come first: a ruling for the item's normalized answer
   (`checks.normalize`) scores it and nothing else is asked.
   - **choice**: the letter against the key.
   - **short**: in code with SymPy (`checks.py`). `expression` is algebraic
     equivalence, `numeric` a value within the manifest's tolerance, `set`
     an unordered set of values, `text` a normalized string match. A single
     variable on the left is the student naming the unknown, so `x = 3/2`
     matches `3/2`. A reading that won't parse gets no score and goes to
     review, never to zero.
   - **rubric**: one call per group of identical normalized readings, with
     the rubric YAML, the crop and the transcription. It returns points, the
     quoted words that earn them and a confidence for each criterion, plus one
     feedback note that asks the student a question and never gives the
     answer. The rubric file is found from the manifest's `rubric` path,
     relative to the lesson directory (`<course>/<unit>/<lesson>`).
3. **Verify.** An independent second call per crop gets the crop, the
   manifest entry (key or rubric) and the first reading and score, but not
   the first call's reasoning. It transcribes again, rescores rubric criteria,
   and agrees or disputes.

The outcome is a row in `scores` (`status` final or review) and, unless the
item is plainly settled, a row in `review_queue`:

| Goes to review | Priority |
| --- | --- |
| Verify disagrees (reading, letter, or any rubric criterion) | 0 |
| Illegible, unparseable, a refused or failed call | 1 |
| A reading below high confidence | 2 |
| Every rubric item, in this phase | 3 |
| A blank box, until confirmed | 4 |

A choice or short item is final only when both readings match with high
confidence. A ruling for the item's normalized answer settles it with no
verify call and no review, and so does a reading fixed in review (the fixed
reading is scored like any other). A rerun never overwrites a score whose
source is `review`.

**Prompt caching.** The stable part of each prompt comes first and carries
the cache breakpoint: the instructions and, for rubric and verify calls, the
question's key or rubric. A class shares one cached prefix per question; the
crop and anything about one student come after it.

**Batches.** A run over more than `api.batch_threshold` crops (default 20)
goes through the Batch API at half price; smaller runs use the standard API.
The work runs in rounds (the reads; then the rubric calls and the choice and
short verifies; then the rubric verifies), one batch per round. The batch id
is stored in `batches`, so if the command is interrupted, or run with
`--no-wait`, the next `grading read` collects that batch instead of
submitting again. Everything is stored as it arrives, so a rerun only sends
what is still missing. A refused or malformed response is not retried; a
transient error (overloaded, expired) is retried on the next run.

**Cost guard.** Before a live run, `grading read` and `grading eval` print the
number of calls and an estimated cost by call type, and ask before sending
anything (`--yes` skips the question). A run estimated above
`api.max_run_usd` is refused outright. The estimate assumes no cache hits and
counts a rubric call per crop, so a real run should come in under it.

**Record and replay.** `--record DIR` saves every request (each image
replaced by its SHA-256) and response to `DIR/calls/<request id>.json`.
`--replay DIR` answers every request from those files with no network, and
reports any request that has changed since the recording (a prompt or a
crop). `grading read --record` refuses a folder inside a git work tree: a
recording of real papers holds student work.

## Review

```sh
grading review                       # http://127.0.0.1:5005/, opens the browser
grading review --port 5050 --no-browser
```

A Flask page on localhost. The queue is grouped by question, then by answer
group (identical normalized answers) with a count, in this order:

1. Disagreements, answers with no score (illegible, unparseable, a failed
   call) and low-confidence readings
2. Rubric items
3. The blank batch to confirm

Each group shows every crop in it with its tracking id, the reading (and the
verify pass's reading and verdict), the key, the proposed score, the rubric
evidence criterion by criterion next to the verify pass's scores, and the
feedback note.

| Key | Does |
| --- | --- |
| `j` `k` | next and previous group |
| `h` `l` | previous and next crop in the group |
| `a` | approve: save a ruling with the proposed score and the feedback as shown |
| `s` | type a score, `Enter` approves with it |
| `f` | edit the feedback note, `Ctrl`/`⌘ Enter` approves |
| `r` | fix the highlighted crop's reading, `Enter` saves it |
| `v` | take the verify pass's reading for the highlighted crop |
| `x` | in the blank batch: this box has writing |
| `u` `q` | unplaced pages, back to the queue |

- **A ruling** is keyed by question and normalized answer (`rulings`). It
  scores every matching answer at once, including ones already final, and
  every matching answer in later runs, late papers included, with no call
  and no review.
- **A fixed reading** (`corrections`) applies to that crop only. The crop
  is then scored by its corrected answer: by a ruling if there is one, in
  code for choice and short items, and otherwise it waits in a group of its
  own for a score.
- **No reading** (illegible, a failed call): there is nothing to rule on, so
  fix the reading or type a score for that crop alone (source `review`).
- **Blanks**: `a` confirms the batch (zero points). A box marked with `x`
  goes back to `grading read`.
- **Unplaced pages**: choose the assignment, page and tracking id (the page
  lists who has other pages of the assignment but not that one). The stored
  page is aligned by its registration marks and cropped like any other; a
  page whose marks can't be found must be rescanned. A second copy of a page
  already placed is stored as a duplicate. `d` dismisses a page that isn't a
  student's.

No names appear anywhere in the review app. Students are tracking ids, and
an unplaced page is shown by scan number, not file name (a file name might
hold one). The server listens on 127.0.0.1 only, refuses other host names,
and takes changes only as JSON with a token made at startup.

Every decision is stored as an input (a ruling, a correction, a confirmed
blank) and the scores and the queue are recomputed from the inputs, the same
code `grading read` runs. So review, a later `grading read` and the report
always agree.

## Reports

```sh
grading report                       # everything not in an earlier report
grading report --assignment alg2-u3-l1-gs
grading report --all                 # every submission again
grading report --partial             # even with items in review
```

This is the only command that reads `roster-map.csv`. It writes a folder
named for the run date, `<data>/reports/2026-10-22/` (`-2`, `-3` for a
second report that day), with every file mode 600:

| File | What it holds |
| --- | --- |
| `scores.pdf` | One table per section: students in gradebook order (last name) by legal name, one column per assignment in the run, cells `earned/possible`, empty when no pages came in |
| `scores.csv` | The same, with earned and possible in separate columns (a spreadsheet would read `7/12` as a date) |
| `feedback-sheets-<section>.pdf` | A sheet per student and assignment, in gradebook order: preferred and last name, the assignment, the total, one row per question with points and any feedback note |
| `summary.pdf` | Score distribution, miss rate per question with the student count, common wrong answers (two or more students) with counts, students with no pages received |

**The run.** A report covers every submission no earlier report covered. If
an assignment in it has never been reported, every student in the sections
it touches is listed, with empty cells for those with no pages. A report of
late papers alone lists only their students. A cell marked `†` is missing a
page; its items count zero.

**Refusals.** While any item of an assignment in the run is in review, or
crops are not graded yet, `grading report` refuses and says how many.
`--partial` writes the report anyway, counts only settled items, marks the
cells left short with `*`, and doesn't count as covering anything, so the
next full report still includes them.

The database records the report folder and the tracking ids each report
covered, never a name, and the terminal shows tracking ids only. Students in
the scans but not in roster-map.csv are listed by tracking id in a "Not in
roster" table.

## Eval

```sh
grading eval                         # the synthetic class set, live
grading eval --record tests/fixtures/recordings/synthetic
grading eval --replay tests/fixtures/recordings/synthetic
grading eval --fake                  # offline: answers from expected-scores.csv
```

`grading eval` ingests the synthetic class set (`tests/fixtures/synthetic`)
into a fresh data folder of its own (`<data>/eval/<date-time>`, or
`--workdir`), runs read, score and verify on it, and compares every item
with `expected-scores.csv`. It reports agreement by item type, every
disagreement with its crop path, how many items went to review and why, and
tokens and dollars by call type. An item with no proposed score (illegible,
unparseable) is counted apart from agreements and disagreements. The report
is also written to `eval-report.json` in the workdir. If an eval is
interrupted while a batch runs, rerun it with `--workdir` set to the same
folder: it collects the batch and carries on.

`--fake` answers from `expected-scores.csv` with four injected faults (one
illegible crop, one low-confidence reading, one verify disagreement, one
rubric score a point high), as the tests do.

## Data folder

```
grading.sqlite
pages/<scan sha>/<NNN>.png               aligned, strip-blanked, thresholded
crops/<assignment>/<tracking id>/<q>.png thresholded answer boxes
reports/<date>/                          the reports: the only files with names
```

The SQLite schema is in `src/grading/db.py`. Student records are keyed by
tracking id, and no table has a name column. The tables are `scans`, `pages`,
`crops`, `assignments` and `questions` (the manifest each assignment was read
against), and `submissions` (dates received, which never change a score).
Read, score and verify write `readings` (both passes), `judgments` (rubric
scores and verify verdicts), `scores`, `review_queue`, `api_calls` (every call
with its tokens) and `batches`. Review writes `rulings`, `corrections` and
`crops.blank_confirmed`, and places unplaced pages. The report writes
`reports` and `reported` (tracking ids only). The schema
changes only by migration (`db.MIGRATIONS`); an older database is migrated
when it is opened.

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
- **Read, score and verify** on the synthetic class set, against the fake
  client (`fake.py`: expected-scores.csv's responses with the four injected
  faults):
  - each fault lands in review for the right reason, and nothing else does
    except rubric items and the blank;
  - agreement is 48/48 choice, 35/36 short (the illegible item has no
    score), 11/12 rubric (the one scored a point high);
  - the two identical gs8 answers share one rubric call;
  - rulings score items before review and replace rubric calls;
  - `x = 3/2` scores as 3/2; an unparseable reading gets no score;
  - a refusal goes to review and isn't retried, a transient error is;
  - an interrupted batch is collected on the next run, not resubmitted;
  - a rerun sends nothing and keeps review decisions.
- **Record and replay**: a recorded run replays to the same report with no
  API client created; a changed prompt is flagged.
- **Cost guard**: the estimate, the confirmation (declining sends nothing)
  and the ceiling.
- **The live transport**, through the real SDK against a mocked HTTP layer:
  strict tools, `tool_choice` auto, the cache breakpoint, the refusal
  fallback, batch submission and results; and the Keychain lookup.
- **Review** (`test_review.py`, `test_webapp.py`), on the class set graded
  by the fake client:
  - the queue's order: disagreements and low confidence, then rubric items
    by answer group (the shared gs8 answer is one group of two), then the
    one blank batch;
  - approving saves a ruling that scores every matching answer, including
    ones already final, and a rerun sends nothing for them;
  - a fixed reading is scored by code and survives a rerun with no verify
    call; a fixed rubric reading waits in its own group for a score;
  - blanks confirm to zero; a blank with writing goes back to `grading read`;
  - an unplaced page placed by hand is aligned, cropped and graded; a second
    copy becomes a duplicate; bad ids, pages and assignments are refused;
  - a late paper (462HAN's page, its QR code damaged) placed by hand gets
    the class set's gs8 ruling with no rubric call, verify call or review;
  - the page renders every group and the unplaced queue, takes changes only
    with its token and on localhost, and shows no name from the roster,
    which it is never allowed to read.
- **Reports** (`test_report.py`), with algebra_2's `fake-roster.csv`:
  - refusal while items are in review; `--partial` marks and doesn't cover;
  - every student's total matches `expected-scores.csv` (with the one
    injected rubric fault approved as proposed), in gradebook order even
    from a shuffled roster;
  - the PDFs carry the invented names (score table order, one sheet per
    student with name, total and feedback), and no file written outside the
    report folder holds one, the database included, nor does the terminal;
  - a class report lists the missing student with an empty cell and under
    "no pages received"; after her late page, the next report lists only her;
  - tracking ids not in the roster are listed apart; only `report.py` reads
    the roster.
- **Code scoring** (`test_checks.py`), **the schema migration**, the CLI.
- **Duplicates, re-ingest, the inbox, config validation** (paths inside a
  git work tree are refused), **the schema** (no name columns) and
  **`.gitignore`**.
