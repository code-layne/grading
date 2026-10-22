"""`grading report` on the synthetic class set, graded by the fake client and
reviewed, with algebra_2's fake-roster.csv (invented students) as the roster.

The report PDFs must carry the invented names; nothing written anywhere else
may."""

import csv
import datetime as dt
import random
from collections import defaultdict
from pathlib import Path

import pymupdf
import pytest

from grading import report
from grading.cli import main
from grading.fake import DEFAULT_FAULTS
from grading.ingest import place_by_hand
from grading.review import Review

from conftest import EXPECTED, FAKE_ROSTER, LATE, fake_run, read_csv, review_all, roster_names

A = "alg2-u3-l1-gs"
DAY = dt.date(2026, 10, 22)
FIXES = {DEFAULT_FAULTS["illegible"][0]: "2"}  # what the illegible crop says
ROSTER = {r["tracking_id"]: r for r in read_csv(FAKE_ROSTER)}


def with_roster(cfg, shuffle=False):
    rows = read_csv(FAKE_ROSTER)
    if shuffle:  # gradebook order must come from the names, not the file's order
        random.Random(4).shuffle(rows)
    cfg.roster.parent.mkdir(parents=True, exist_ok=True)
    with open(cfg.roster, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return cfg


def text(pdf: Path) -> list[str]:
    with pymupdf.open(pdf) as doc:
        return [page.get_text() for page in doc]


def gradebook():
    rows = sorted(ROSTER.values(), key=lambda r: r["legal_name"].split(",")[0].lower())
    return [r["legal_name"] for r in rows]


def expected_totals():
    totals = defaultdict(float)
    for r in read_csv(EXPECTED):
        totals[r["tracking_id"]] += float(r["points"])
    # The injected rubric fault (a point high) was approved as proposed.
    totals[DEFAULT_FAULTS["rubric_off_by_one"][0]] += 1
    return totals


def files_outside(root: Path, folders, roster: Path):
    for p in root.rglob("*"):
        if p.is_file() and not any(f in p.parents for f in folders) and p != roster:
            yield p


def assert_no_names_outside(root, folders, roster):
    names = roster_names()
    checked = 0
    for p in files_outside(root, folders, roster):
        if p.suffix == ".png":
            continue  # pixels; a 4-letter name could turn up in compressed bytes by chance
        data = p.read_bytes()
        for name in names:
            assert name.encode() not in data, f"{name!r} written to {p}"
        checked += 1
    assert checked  # the database at least


@pytest.fixture
def reviewed(graded):
    cfg, conn = graded
    review_all(Review(cfg, conn), FIXES)
    return with_roster(cfg, shuffle=True), conn


def test_refuses_while_items_are_in_review(graded):
    cfg, conn = with_roster(graded[0]), graded[1]
    with pytest.raises(report.ReportError, match=f"{A}: 16 in review, 0 not graded yet"):
        report.run(cfg, conn, today=DAY)
    assert not (cfg.data / "reports").exists() or not any((cfg.data / "reports").iterdir())
    assert conn.execute("SELECT count(*) FROM reports").fetchone()[0] == 0


def test_partial_report_marks_what_it_leaves_out(graded):
    cfg, conn = with_roster(graded[0]), graded[1]
    out = report.run(cfg, conn, partial=True, today=DAY)
    pages = text(out["folder"] / "scores.pdf")
    assert "(partial)" in pages[0] and "*" in pages[0]
    rows = read_csv(out["folder"] / "scores.csv")
    assert all("partial" in r["notes"] for r in rows)  # every student has a rubric item in review
    # A partial report covers nothing: the full one later still sees every submission.
    assert conn.execute("SELECT count(*) FROM reported").fetchone()[0] == 0
    review_all(Review(cfg, conn), FIXES)
    full = report.run(cfg, conn, today=DAY)
    assert full["folder"].name == "2026-10-22-2"
    assert len(full["report"].run) == 12


def test_report_files_and_scores(reviewed):
    cfg, conn = reviewed
    out = report.run(cfg, conn, today=DAY)
    folder = out["folder"]
    assert folder == cfg.data / "reports" / "2026-10-22"
    assert sorted(p.name for p in folder.iterdir()) == [
        "feedback-sheets-algebra-2-sample.pdf", "scores.csv", "scores.pdf", "summary.pdf"]
    assert all(oct(p.stat().st_mode & 0o777) == "0o600" for p in folder.iterdir())

    rows = read_csv(folder / "scores.csv")
    assert [r["student"] for r in rows] == gradebook()  # last name order, whatever the file's order
    by_name = {r["legal_name"]: t for t, r in ROSTER.items()}
    totals = expected_totals()
    for r in rows:
        assert r["section"] == "Algebra 2 - Sample"
        assert float(r[f"{A} earned"]) == totals[by_name[r["student"]]]
        assert r[f"{A} possible"] == "13" and r["notes"] == ""


def test_pdfs_carry_the_names(reviewed):
    cfg, conn = reviewed
    folder = report.run(cfg, conn, today=DAY)["folder"]
    (scores,) = text(folder / "scores.pdf")
    order = [scores.index(name) for name in gradebook()]
    assert order == sorted(order)
    assert "Algebra 2 - Sample" in scores and A in scores

    sheets = text(folder / "feedback-sheets-algebra-2-sample.pdf")
    assert len(sheets) == 12
    tids = [t for t, _ in sorted(ROSTER.items(), key=lambda kv: kv[1]["legal_name"].split(",")[0].lower())]
    totals = expected_totals()
    for page, tid in zip(sheets, tids):
        r = ROSTER[tid]
        assert page.startswith(f"{r['preferred_name']} {r['legal_name'].split(',')[0]}")
        assert A in page and f"Score: {totals[tid]:g} / 13" in page
        assert all(f"gs{n}" in page for n in range(1, 9))
    arden = sheets[0]
    feedback = conn.execute("SELECT feedback FROM scores WHERE tracking_id = '44QB73' AND question = 'gs8'"
                            ).fetchone()[0]
    assert feedback and " ".join(feedback.split()) in " ".join(arden.split())

    summary = " ".join(text(folder / "summary.pdf"))
    assert "Score distribution: 12 students" in summary
    assert "Miss rate per question" in summary and "Common wrong answers" in summary
    assert "Every student in these sections has pages in." in summary


def test_nothing_outside_the_report_folder_has_a_name(reviewed, tmp_path, capsys):
    cfg, conn = reviewed
    conn.close()
    conf = tmp_path / "config.toml"
    conf.write_text(f'[paths]\ncurricula = ["{cfg.curricula[0]}"]\ninbox = "{cfg.inbox}"\n'
                    f'data = "{cfg.data}"\nroster = "{cfg.roster}"\n')
    assert main(["--config", str(conf), "report"]) == 0
    printed = capsys.readouterr()
    (folder,) = (cfg.data / "reports").iterdir()
    for name in roster_names():
        assert name not in printed.out + printed.err
    assert str(folder) in printed.out
    assert_no_names_outside(tmp_path, [folder], cfg.roster)
    # Every report PDF has them.
    for pdf in folder.glob("*.pdf"):
        assert "Arden" in " ".join(text(pdf)) or pdf.name == "summary.pdf"


def test_nothing_new_to_report(reviewed):
    cfg, conn = reviewed
    report.run(cfg, conn, today=DAY)
    with pytest.raises(report.ReportError, match="nothing new"):
        report.run(cfg, conn, today=DAY)
    again = report.run(cfg, conn, everything=True, today=DAY)
    assert len(again["report"].run) == 12
    with pytest.raises(report.ReportError, match="no pages received for alg2-nope"):
        report.run(cfg, conn, assignments=["alg2-nope"], everything=True, today=DAY)


def test_late_papers(late_paper, capsys):
    cfg, conn, page_id = late_paper
    with_roster(cfg)
    late_name = ROSTER[LATE]["legal_name"]

    # The class set: everyone listed, the missing student's cell empty and
    # her name under "no pages received".
    first = report.run(cfg, conn, today=dt.date(2026, 10, 21))
    rows = {r["student"]: r for r in read_csv(first["folder"] / "scores.csv")}
    assert len(rows) == 12 and rows[late_name][f"{A} earned"] == ""
    assert len(text(first["folder"] / "feedback-sheets-algebra-2-sample.pdf")) == 11
    summary = " ".join(text(first["folder"] / "summary.pdf"))
    assert "No pages received" in summary and late_name in summary

    # Her page comes in late, is placed by hand, graded and reviewed.
    place_by_hand(cfg, conn, page_id, A, 1, LATE)
    with pytest.raises(report.ReportError, match="not graded yet"):
        report.run(cfg, conn, today=dt.date(2026, 10, 23))
    fake_run(cfg, conn)
    review_all(Review(cfg, conn), FIXES)
    late = report.run(cfg, conn, today=dt.date(2026, 10, 23))

    # Only she is listed, and only she gets a sheet.
    assert late["folder"].name == "2026-10-23"
    rows = read_csv(late["folder"] / "scores.csv")
    assert [r["student"] for r in rows] == [late_name]
    total = sum(float(r["points"]) for r in read_csv(EXPECTED) if r["tracking_id"] == LATE)
    assert float(rows[0][f"{A} earned"]) == total
    (scores,) = text(late["folder"] / "scores.pdf")
    assert late_name in scores and sum(n in scores for n in gradebook()) == 1
    (sheet,) = text(late["folder"] / "feedback-sheets-algebra-2-sample.pdf")
    assert sheet.startswith(f"{ROSTER[LATE]['preferred_name']} Ingleby")
    assert "Every student in these sections has pages in." in " ".join(text(late["folder"] / "summary.pdf"))

    assert_no_names_outside(cfg.data.parent, [first["folder"], late["folder"]], cfg.roster)


def test_unknown_tracking_id_is_listed_apart(reviewed):
    cfg, conn = reviewed
    rows = [r for r in read_csv(FAKE_ROSTER) if r["tracking_id"] != "WR2FYP"]
    with open(cfg.roster, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    out = report.run(cfg, conn, today=DAY)
    assert out["report"].unknown == ["WR2FYP"]
    csv_rows = read_csv(out["folder"] / "scores.csv")
    assert csv_rows[-1]["section"] == report.NOT_IN_ROSTER and csv_rows[-1]["student"] == "WR2FYP"


def test_missing_roster_is_an_error(reviewed):
    cfg, conn = reviewed
    cfg.roster.unlink()
    with pytest.raises(Exception, match="no roster"):
        report.run(cfg, conn, today=DAY)


def test_only_the_report_reads_the_roster():
    src = Path(report.__file__).parent
    for py in src.glob("*.py"):
        code = py.read_text()
        uses = "roster.load" in code or "roster_mod" in code or "from . import roster" in code \
            or "from .roster" in code or ".roster)" in code
        if py.name not in ("report.py", "roster.py", "cli.py"):
            assert not uses, f"{py.name} reaches for the roster"
    # The CLI only imports its error, to print it.
    assert "roster.load" not in (src / "cli.py").read_text()
