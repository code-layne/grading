"""`grading report`: step 9 of the design's pipeline.

The only command after the build that reads roster-map.csv. It joins the
scores, which the database keys by tracking id, to names, and writes them
only into a folder named for the run date, <data>/reports/<YYYY-MM-DD>/:

  scores.pdf, scores.csv   one table per section, students in gradebook
                           order, a column per assignment in the run
  feedback-sheets-<section>.pdf
                           a sheet per student and assignment, to staple
                           to the packet
  summary.pdf              score distribution, miss rate per question,
                           common wrong answers, students with no pages

What a report covers (its "run"): every submission (student and
assignment) no earlier final report has covered, or only those assignments
with --assignment, or everything with --all. When an assignment in the run
has never been reported, each section's whole roster is listed, with empty
cells for students with no pages; a run of late papers alone lists only
their students.

It refuses while any item of an assignment in the run is in review or not
graded yet, unless --partial. A partial report counts only settled items,
marks the cells it leaves short, and doesn't count as covering anything.

Nothing with a name is written anywhere but the report folder: the database
gets the folder's path and the tracking ids covered, and the terminal sees
tracking ids only.
"""

import csv
import datetime as dt
import json
import os
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.graphics.shapes import Drawing, Line, Rect, String
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from . import roster as roster_mod

NOT_IN_ROSTER = "Not in roster"


class ReportError(Exception):
    pass


@dataclass
class Cell:
    """One student's result on one assignment."""
    earned: float = 0.0
    possible: float = 0.0
    pending: int = 0                 # items in review or not graded yet (not counted in earned)
    missing: int = 0                 # items on pages that never came in
    received: str | None = None
    questions: list = field(default_factory=list)  # [{id, points, max, feedback, state}]

    @property
    def text(self) -> str:
        return f"{self.earned:g}/{self.possible:g}" + ("*" if self.pending else "") + ("†" if self.missing else "")


@dataclass
class Section:
    name: str
    students: list                   # roster.Student (or a stand-in for an unknown id), gradebook order
    cells: dict                      # (tracking id, assignment) -> Cell


@dataclass
class Report:
    run_date: str
    partial: bool
    assignments: list               # [{assignment, possible, questions: [{id, type, points}]}]
    sections: list                  # [Section]
    run: set                        # {(tracking id, assignment)}
    blocked: dict                   # assignment -> {"review": n, "ungraded": n}
    unknown: list                   # tracking ids not in the roster
    summary: dict


# ---- choosing the run ----------------------------------------------------

def select(conn, assignments=None, everything=False) -> set:
    subs = {(r["tracking_id"], r["assignment"]) for r in conn.execute("SELECT * FROM submissions")}
    if assignments:
        known = {a for _, a in subs}
        unknown = [a for a in assignments if a not in known]
        if unknown:
            raise ReportError(f"no pages received for {', '.join(unknown)}")
        subs = {s for s in subs if s[1] in assignments}
    if not everything:
        done = {(r["tracking_id"], r["assignment"]) for r in conn.execute("SELECT * FROM reported")}
        subs -= done
    if not subs:
        raise ReportError("nothing new to report: every submission is in an earlier report "
                          "(--all reports them again)")
    return subs


def blockers(conn, assignments) -> dict:
    """assignment -> items still in review, and items not graded yet."""
    out = {}
    for a in assignments:
        review = conn.execute("SELECT count(*) FROM review_queue WHERE assignment = ? AND resolved_at IS NULL",
                              (a,)).fetchone()[0]
        ungraded = conn.execute(
            "SELECT count(*) FROM crops c JOIN pages p ON p.id = c.page_id LEFT JOIN scores s ON s.crop_id = c.id "
            "WHERE p.status = 'placed' AND c.assignment = ? AND (s.status IS NULL OR s.status != 'final') "
            "AND c.id NOT IN (SELECT crop_id FROM review_queue WHERE resolved_at IS NULL)", (a,)).fetchone()[0]
        if review or ungraded:
            out[a] = {"review": review, "ungraded": ungraded}
    return out


# ---- building it ---------------------------------------------------------

def _qkey(q):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", q)]


def build(conn, students: dict, run: set, *, partial: bool, everything: bool, run_date: str) -> Report:
    names = sorted({a for _, a in run})
    blocked = blockers(conn, names)
    if blocked and not partial:
        lines = [f"  {a}: {b['review']} in review, {b['ungraded']} not graded yet" for a, b in blocked.items()]
        raise ReportError("items are still open, so no report (or pass --partial):\n" + "\n".join(lines) +
                          "\n  finish them with `grading review` (and `grading read` for the ungraded)")

    assignments = []
    for a in names:
        qs = [dict(r) for r in conn.execute("SELECT question AS id, type, points, page FROM questions "
                                            "WHERE assignment = ? ORDER BY rowid", (a,))]
        assignments.append({"assignment": a, "questions": qs, "possible": sum(q["points"] for q in qs)})

    reported_before = {r[0] for r in conn.execute("SELECT DISTINCT assignment FROM reported")}
    whole_class = everything or any(a not in reported_before for a in names)

    # Who is listed: everyone in a section the run touches when it holds a
    # class set, only the run's own students when it is late papers alone.
    run_ids = {t for t, _ in run}
    unknown = sorted(t for t in run_ids if t not in students)
    by_section: dict[str, list] = {}
    touched = {students[t].section for t in run_ids if t in students}
    for s in students.values():
        if s.section in touched and (whole_class or s.tracking_id in run_ids):
            by_section.setdefault(s.section, []).append(s)
    for t in unknown:
        by_section.setdefault(NOT_IN_ROSTER, []).append(
            roster_mod.Student(t, t, t, "", NOT_IN_ROSTER))

    received = {(r["tracking_id"], r["assignment"]): r["last_received_on"]
                for r in conn.execute("SELECT * FROM submissions")}
    sections = []
    for name in sorted(by_section, key=lambda n: (n == NOT_IN_ROSTER, n)):
        listed = sorted(by_section[name], key=lambda s: s.gradebook_key)
        cells = {}
        for s in listed:
            for a in assignments:
                k = (s.tracking_id, a["assignment"])
                if k in received:
                    cells[k] = _cell(conn, s.tracking_id, a, received[k])
        sections.append(Section(name, listed, cells))
    report = Report(run_date, partial, assignments, sections, run, blocked, unknown, {})
    report.summary = _summary(conn, report, students)
    return report


def _cell(conn, tid, a, received) -> Cell:
    scores = {r["question"]: r for r in conn.execute(
        "SELECT s.* FROM scores s JOIN crops c ON c.id = s.crop_id JOIN pages p ON p.id = c.page_id "
        "WHERE s.tracking_id = ? AND s.assignment = ? AND p.status = 'placed'", (tid, a["assignment"]))}
    on_pages = {r[0] for r in conn.execute(
        "SELECT c.question FROM crops c JOIN pages p ON p.id = c.page_id "
        "WHERE c.tracking_id = ? AND c.assignment = ? AND p.status = 'placed'", (tid, a["assignment"]))}
    cell = Cell(possible=a["possible"], received=received)
    for q in a["questions"]:
        row = scores.get(q["id"])
        entry = {"id": q["id"], "max": q["points"], "points": None, "feedback": None}
        if q["id"] not in on_pages:
            entry["state"] = "missing"
            cell.missing += 1
        elif row is None or row["status"] != "final" or row["points"] is None:
            entry["state"] = "pending"
            cell.pending += 1
        else:
            entry.update(state="scored", points=row["points"], feedback=row["feedback"])
            cell.earned += row["points"]
        cell.questions.append(entry)
    return cell


def _summary(conn, report: Report, students: dict) -> dict:
    out = {}
    run_ids = {t for t, _ in report.run}
    sections = {students[t].section for t in run_ids if t in students}
    for a in report.assignments:
        name = a["assignment"]
        cells = [sec.cells[(s.tracking_id, name)] for sec in report.sections for s in sec.students
                 if (s.tracking_id, name) in sec.cells and (s.tracking_id, name) in report.run]
        totals = [c.earned for c in cells]
        questions = []
        for q in a["questions"]:
            got = [e for c in cells for e in c.questions if e["id"] == q["id"] and e["state"] == "scored"]
            missed = sum(1 for e in got if e["points"] < e["max"] - 1e-9)
            wrong = []
            if q["type"] != "rubric":
                ids = [t for t, aa in report.run if aa == name]
                marks = ",".join("?" * len(ids))
                rows = conn.execute(
                    f"SELECT normalized_answer, count(*) AS n, min(detail) AS detail FROM scores "
                    f"WHERE assignment = ? AND question = ? AND status = 'final' AND points < max_points "
                    f"AND tracking_id IN ({marks}) GROUP BY normalized_answer ORDER BY n DESC, normalized_answer",
                    (name, q["id"], *ids)).fetchall()
                for r in rows:
                    wrong.append({"answer": _shown(r), "n": r["n"]})
            questions.append({"id": q["id"], "type": q["type"], "points": q["points"], "students": len(got),
                              "missed": missed, "wrong": wrong})
        have = {r[0] for r in conn.execute("SELECT tracking_id FROM submissions WHERE assignment = ?", (name,))}
        nothing = sorted((s for s in students.values() if s.section in sections and s.tracking_id not in have),
                         key=lambda s: (s.section, s.gradebook_key))
        out[name] = {"totals": totals, "possible": a["possible"], "questions": questions, "no_pages": nothing}
    return out


def _shown(row) -> str:
    if row["normalized_answer"] in (None, ""):
        return "(blank)"
    detail = json.loads(row["detail"] or "{}")
    return (detail.get("read") or {}).get("reading") or row["normalized_answer"]


# ---- writing it ----------------------------------------------------------

def _fonts():
    """A Unicode TrueType font when one is installed (names can carry any
    accent), else the PDF base fonts, which cover Western European names."""
    candidates = [
        ("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
        ("/Library/Fonts/Arial Unicode.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ]
    for regular, bold in candidates:
        if os.path.isfile(regular) and os.path.isfile(bold):
            try:
                if "GradeSans" not in pdfmetrics.getRegisteredFontNames():
                    pdfmetrics.registerFont(TTFont("GradeSans", regular))
                    pdfmetrics.registerFont(TTFont("GradeSans-Bold", bold))
                return "GradeSans", "GradeSans-Bold"
            except Exception:  # an unreadable or unsupported font file: fall back
                continue
    return "Helvetica", "Helvetica-Bold"


INK, INK2, MUTED, LINE, BAR = (colors.HexColor(c) for c in ("#0b0b0b", "#52514e", "#898781", "#dddcd7", "#2a78d6"))


class _Styles:
    def __init__(self):
        self.font, self.bold = _fonts()
        self.title = ParagraphStyle("title", fontName=self.bold, fontSize=16, leading=20, textColor=INK, spaceAfter=4)
        self.h2 = ParagraphStyle("h2", fontName=self.bold, fontSize=12, leading=15, textColor=INK, spaceBefore=10,
                                 spaceAfter=4)
        self.body = ParagraphStyle("body", fontName=self.font, fontSize=9.5, leading=12, textColor=INK)
        self.small = ParagraphStyle("small", fontName=self.font, fontSize=8, leading=10, textColor=INK2)
        self.name = ParagraphStyle("name", fontName=self.bold, fontSize=22, leading=26, textColor=INK, spaceAfter=2)

    def table(self, data, widths=None, header=True, align_right_from=None):
        t = Table(data, colWidths=widths, repeatRows=1 if header else 0, hAlign="LEFT")
        style = [("FONT", (0, 0), (-1, -1), self.font, 9), ("TEXTCOLOR", (0, 0), (-1, -1), INK),
                 ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                 ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
        if header:
            style += [("FONT", (0, 0), (-1, 0), self.bold, 9), ("TEXTCOLOR", (0, 0), (-1, 0), INK2),
                      ("LINEBELOW", (0, 0), (-1, 0), 0.8, INK2)]
        if align_right_from is not None:
            style.append(("ALIGN", (align_right_from, 0), (-1, -1), "RIGHT"))
        t.setStyle(TableStyle(style))
        return t


def _p(text, style):
    return Paragraph(escape(str(text)), style)


def _doc(path, title):
    return SimpleDocTemplate(str(path), pagesize=letter, title=title, author="grading",
                             leftMargin=0.75 * inch, rightMargin=0.75 * inch, topMargin=0.7 * inch,
                             bottomMargin=0.7 * inch)


def _notes(report, st):
    out = []
    if any(c.pending for s in report.sections for c in s.cells.values()):
        out.append(_p("* Partial: items still in review or not graded yet are not counted.", st.small))
    if any(c.missing for s in report.sections for c in s.cells.values()):
        out.append(_p("† Some pages of this assignment never came in; their items count as zero.", st.small))
    return out


def write_scores_pdf(report, path, st):
    story = [_p(f"Scores, {report.run_date}" + (" (partial)" if report.partial else ""), st.title),
             _p("Points earned out of points possible. An empty cell: no pages received.", st.small)]
    names = [a["assignment"] for a in report.assignments]
    for i, sec in enumerate(report.sections):
        rows = [["Student"] + names]
        for s in sec.students:
            rows.append([s.legal_name] + [sec.cells[(s.tracking_id, a)].text if (s.tracking_id, a) in sec.cells
                                          else "" for a in names])
        avail = letter[0] - 1.5 * inch
        first = min(2.6 * inch, avail * 0.45)
        widths = [first] + [max((avail - first) / max(len(names), 1), 0.7 * inch)] * len(names)
        block = [_p(sec.name, st.h2), st.table(rows, widths, align_right_from=1)]
        story += block
        story += _notes(report, st)
        if i + 1 < len(report.sections):
            story.append(PageBreak())
    _doc(path, "Scores").build(story)


def write_scores_csv(report, path):
    names = [a["assignment"] for a in report.assignments]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        # Earned and possible in separate columns: a spreadsheet would read
        # "7/12" as a date.
        w.writerow(["section", "student"] + [f"{a} {k}" for a in names for k in ("earned", "possible")] + ["notes"])
        for sec in report.sections:
            for s in sec.students:
                row, notes = [sec.name, s.legal_name], []
                for a in report.assignments:
                    c = sec.cells.get((s.tracking_id, a["assignment"]))
                    row += [f"{c.earned:g}" if c else "", f"{a['possible']:g}"]
                    if c and c.pending:
                        notes.append(f"{a['assignment']}: {c.pending} items not counted (partial)")
                    if c and c.missing:
                        notes.append(f"{a['assignment']}: {c.missing} items on pages not received")
                w.writerow(row + ["; ".join(notes)])


def write_feedback_sheets(report, path, section, st):
    story = []
    for s in section.students:
        for a in report.assignments:
            c = section.cells.get((s.tracking_id, a["assignment"]))
            if c is None or (s.tracking_id, a["assignment"]) not in report.run:
                continue  # no packet to staple a sheet to, or not in this run
            if story:
                story.append(PageBreak())
            story += [_p(s.sheet_name, st.name), _p(a["assignment"], st.h2),
                      _p(f"Score: {c.earned:g} / {c.possible:g}" + (" (partial)" if c.pending else ""), st.title),
                      Spacer(1, 6)]
            rows = [["Question", "Points", "Feedback"]]
            for e in c.questions:
                if e["state"] == "scored":
                    pts = f"{e['points']:g} / {e['max']:g}"
                elif e["state"] == "missing":
                    pts = f"— / {e['max']:g}"
                else:
                    pts = f"? / {e['max']:g}"
                note = e["feedback"] or ("page not received" if e["state"] == "missing" else
                                         "not graded yet" if e["state"] == "pending" else "")
                rows.append([e["id"], pts, _p(note, st.body)])
            story.append(st.table(rows, [0.9 * inch, 0.9 * inch, 5.2 * inch]))
            story.append(Spacer(1, 10))
            story.append(_p(f"{section.name} · {s.tracking_id}", st.small))
    if not story:
        story = [_p(f"No sheets: no student in {section.name} has pages in this run.", st.body)]
    _doc(path, f"Feedback sheets, {section.name}").build(story)


def _histogram(totals, possible, st, width=6.5 * inch, height=1.6 * inch):
    """Students by points earned, one bar per point (or per tenth of the
    possible when there are many points)."""
    step = 1 if possible <= 20 else possible / 10
    nbins = int(possible // step) + 1
    counts = [0] * nbins
    for t in totals:
        counts[min(int(t // step), nbins - 1)] += 1
    d = Drawing(width, height)
    left, bottom, top_pad = 4, 16, 14
    plot_w, plot_h = width - left - 4, height - bottom - top_pad
    peak = max(counts) or 1
    bw = plot_w / nbins
    d.add(Line(left, bottom, left + plot_w, bottom, strokeColor=MUTED, strokeWidth=0.6))
    for i, n in enumerate(counts):
        x = left + i * bw
        h = plot_h * n / peak
        if n:
            d.add(Rect(x + 1, bottom, bw - 2, h, fillColor=BAR, strokeColor=None))
            d.add(String(x + bw / 2, bottom + h + 3, str(n), fontName=st.font, fontSize=7, fillColor=INK2,
                         textAnchor="middle"))
        label = f"{i * step:g}" if step == 1 else f"{i * step:.0f}+"
        d.add(String(x + bw / 2, bottom - 10, label, fontName=st.font, fontSize=7, fillColor=MUTED,
                     textAnchor="middle"))
    return d


def write_summary(report, path, st):
    story = [_p(f"Summary, {report.run_date}" + (" (partial)" if report.partial else ""), st.title),
             _p(f"{len({t for t, _ in report.run})} students with pages in this report.", st.small)]
    for a in report.assignments:
        s = report.summary[a["assignment"]]
        totals = s["totals"]
        story.append(_p(a["assignment"], st.h2))
        if totals:
            story.append(_p(f"Score distribution: {len(totals)} students, mean {statistics.mean(totals):.1f}, "
                            f"median {statistics.median(totals):g} of {s['possible']:g} points.", st.body))
            story.append(_histogram(totals, s["possible"], st))
        story.append(_p("Miss rate per question", st.h2))
        rows = [["Question", "Type", "Points", "Missed", "Students", "Miss rate"]]
        for q in s["questions"]:
            rate = f"{100 * q['missed'] / q['students']:.0f}%" if q["students"] else "—"
            rows.append([q["id"], q["type"], f"{q['points']:g}", str(q["missed"]), str(q["students"]), rate])
        story.append(st.table(rows, align_right_from=2))
        story.append(_p("Common wrong answers", st.h2))
        rows = [["Question", "Answer", "Students"]]
        for q in s["questions"]:
            for w in q["wrong"]:
                if w["n"] >= 2:
                    rows.append([q["id"], _p(w["answer"], st.body), str(w["n"])])
        singles = sum(1 for q in s["questions"] for w in q["wrong"] if w["n"] < 2)
        if len(rows) > 1:
            story.append(st.table(rows, [0.9 * inch, 4.6 * inch, 0.8 * inch], align_right_from=2))
        else:
            story.append(_p("No wrong answer was given by two or more students.", st.body))
        story.append(_p("Choice and short items only. " + (
            f"{singles} other wrong answer{'s were' if singles != 1 else ' was'} given by one student only."
            if singles else ""), st.small))
        story.append(_p("No pages received", st.h2))
        if s["no_pages"]:
            rows = [["Student", "Section"]] + [[x.legal_name, x.section] for x in s["no_pages"]]
            story.append(KeepTogether(st.table(rows)))
        else:
            story.append(_p("Every student in these sections has pages in.", st.body))
    if report.unknown:
        story.append(_p("Tracking ids not in roster-map.csv", st.h2))
        story.append(_p(", ".join(report.unknown), st.body))
    _doc(path, "Summary").build(story)


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "section"


def _folder(data: Path, run_date: str) -> Path:
    base = data / "reports"
    base.mkdir(parents=True, exist_ok=True)
    folder, n = base / run_date, 1
    while folder.exists():
        n += 1
        folder = base / f"{run_date}-{n}"
    folder.mkdir(mode=0o700)
    return folder


def run(cfg, conn, *, assignments=None, everything=False, partial=False, today: dt.date | None = None) -> dict:
    run_date = (today or dt.date.today()).isoformat()
    chosen = select(conn, assignments, everything)
    students = roster_mod.load(cfg.roster)
    report = build(conn, students, chosen, partial=partial, everything=everything, run_date=run_date)
    folder = _folder(cfg.data, run_date)
    st = _Styles()
    files = [folder / "scores.pdf", folder / "scores.csv", folder / "summary.pdf"]
    write_scores_pdf(report, files[0], st)
    write_scores_csv(report, files[1])
    write_summary(report, files[2], st)
    for sec in report.sections:
        if any(k in report.run for k in sec.cells):
            f = folder / f"feedback-sheets-{_slug(sec.name)}.pdf"
            write_feedback_sheets(report, f, sec, st)
            files.append(f)
    for f in files:
        os.chmod(f, 0o600)  # names
    now = dt.datetime.now().isoformat(timespec="seconds")
    with conn:
        rid = conn.execute("INSERT INTO reports (run_date, folder, partial, created_at) VALUES (?, ?, ?, ?)",
                           (run_date, str(folder.relative_to(cfg.data)), int(partial), now)).lastrowid
        if not partial:
            conn.executemany("INSERT INTO reported VALUES (?, ?, ?)", [(rid, t, a) for t, a in sorted(chosen)])
    return {"folder": folder, "files": files, "report": report}
