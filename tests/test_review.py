"""Review decisions on the synthetic class set, graded by the fake client:
the queue's order, rulings, fixed readings, blanks, and unplaced pages placed
by hand, including a late paper that picks up the class set's rulings."""

import json

import pytest

from grading import checks
from grading.fake import DEFAULT_FAULTS, FakeTransport
from grading.ingest import PlaceError, dismiss_page, place_by_hand
from grading.review import Review, ReviewError

from conftest import EXPECTED, LATE, SYNTHETIC, fake_run, read_csv, review_all

A = "alg2-u3-l1-gs"
SHARED_GS8 = ("BKTRTN", "462HAN")  # the two students with the same gs8 answer


def score(conn, tid, q):
    return conn.execute("SELECT * FROM scores WHERE tracking_id = ? AND question = ?", (tid, q)).fetchone()


def crop_id(conn, tid, q):
    return conn.execute("SELECT id FROM crops WHERE tracking_id = ? AND question = ?", (tid, q)).fetchone()[0]


def open_rows(conn):
    return conn.execute("SELECT count(*) FROM review_queue WHERE resolved_at IS NULL").fetchone()[0]


def test_queue_order_and_groups(graded):
    cfg, conn = graded
    groups = Review(cfg, conn).groups()
    tiers = [g["tier"] for g in groups]
    # Disagreements and low confidence, then rubric items, then the blank batch.
    assert tiers == sorted(tiers, key=["check", "rubric", "blanks"].index)
    assert tiers[-1] == "blanks" and tiers.count("blanks") == 1
    check = [g for g in groups if g["tier"] == "check"]
    assert {(g["question"], tuple(g["reasons"])) for g in check} == {
        ("gs1", ("verify-disagreement",)), ("gs5", ("low-confidence",)), ("gs6", ("illegible",)),
        ("gs8", ("rubric", "verify-disagreement"))}
    # Within a tier, by question, then by answer group with a count.
    rubric = [g for g in groups if g["tier"] == "rubric"]
    assert all(g["question"] == "gs8" for g in rubric)
    assert rubric[0]["count"] == 2 and sum(g["count"] for g in rubric) == 11
    assert sum(g["count"] for g in groups) == open_rows(conn) == 16


def test_group_shows_crop_reading_score_evidence_feedback(graded):
    cfg, conn = graded
    r = Review(cfg, conn)
    key = next(g["key"] for g in r.groups() if g["tier"] == "rubric" and g["count"] == 2)
    g = r.group(key)
    assert {i["tracking_id"] for i in g["items"]} == set(SHARED_GS8)
    assert g["proposed"] == 3 and g["feedback"]
    assert all(r.crop_path(i["crop_id"]).is_file() for i in g["items"])
    crit = g["items"][0]["criteria"]
    assert set(crit) == {str(c["id"]) for c in g["q"]["rubric"]["criteria"]}
    assert all("evidence" in v for v in crit.values())


def test_approve_saves_a_ruling_and_applies_it_to_every_match(graded):
    cfg, conn = graded
    r = Review(cfg, conn)
    key = next(g["key"] for g in r.groups() if g["tier"] == "rubric" and g["count"] == 2)
    n = r.approve(key, points=2, feedback="  Which terms does the minus reach?  ")
    assert n == 2
    ruling = conn.execute("SELECT * FROM rulings WHERE question = 'gs8' AND normalized_answer = ?",
                          (key[2],)).fetchone()
    assert (ruling["points"], ruling["feedback"]) == (2, "Which terms does the minus reach?")
    assert ruling["reading"].lower() == key[2]
    for tid in SHARED_GS8:
        row = score(conn, tid, "gs8")
        assert (row["points"], row["source"], row["status"]) == (2, "ruling", "final")
        assert row["feedback"] == "Which terms does the minus reach?"
    assert r.group(key) is None
    # A rerun sends nothing for them and keeps the ruling.
    s, t = fake_run(cfg, conn)
    assert sum(s.calls.values()) == 0
    assert score(conn, SHARED_GS8[0], "gs8")["points"] == 2


def test_approving_a_choice_ruling_reaches_answers_already_final(graded):
    cfg, conn = graded
    r = Review(cfg, conn)
    # The low-confidence gs5 reading is right; approve it with a changed score.
    key = next(g["key"] for g in r.groups() if g["question"] == "gs5")
    finals = conn.execute("SELECT count(*) FROM scores WHERE question = 'gs5' AND normalized_answer = ? "
                          "AND status = 'final'", (key[2],)).fetchone()[0]
    assert finals >= 1
    assert r.approve(key, points=1.5) == finals + 1
    assert {row["points"] for row in conn.execute(
        "SELECT points FROM scores WHERE question = 'gs5' AND normalized_answer = ?", (key[2],))} == {1.5}


def test_score_must_be_in_range(graded):
    cfg, conn = graded
    r = Review(cfg, conn)
    key = next(g["key"] for g in r.groups() if g["question"] == "gs5")
    with pytest.raises(ReviewError):
        r.approve(key, points=3)
    with pytest.raises(ReviewError):
        r.approve(key, points="two")


def test_fixing_an_illegible_reading_scores_it_and_survives_a_rerun(graded):
    cfg, conn = graded
    r = Review(cfg, conn)
    tid, q = DEFAULT_FAULTS["illegible"]
    g = next(g for g in r.groups() if g["question"] == q and g["individual"])
    with pytest.raises(ReviewError):
        r.approve(g["key"])  # nothing to rule on
    assert r.correct(crop_id(conn, tid, q), "x = 3/2") is None  # code scores it: settled
    row = score(conn, tid, q)
    assert (row["points"], row["source"], row["status"], row["normalized_answer"]) == (1, "code", "final", "3/2")
    assert json.loads(row["detail"])["corrected_from"] == ""
    s, t = fake_run(cfg, conn)
    assert sum(s.calls.values()) == 0  # a fixed reading needs no verify call
    assert score(conn, tid, q)["points"] == 1


def test_taking_the_verify_reading_on_a_disagreement(graded):
    cfg, conn = graded
    r = Review(cfg, conn)
    tid, q = DEFAULT_FAULTS["verify_disagreement"]
    g = r.group(next(g["key"] for g in r.groups() if g["question"] == q))
    (it,) = g["items"]
    assert it["verify_reading"] != it["reading"]
    r.correct(it["crop_id"], it["reading"])  # the first reading was right
    row = score(conn, tid, q)
    assert row["status"] == "final" and row["normalized_answer"] == checks.normalize("choice", it["reading"])


def test_fixed_rubric_reading_moves_to_its_own_group(graded):
    cfg, conn = graded
    r = Review(cfg, conn)
    cid = crop_id(conn, "TYME2T", "gs8")
    key = r.correct(cid, "Every term changes sign.")
    assert key == [A, "gs8", "every term changes sign."]
    g = r.group(key)
    assert g["proposed"] is None and g["items"][0]["reasons"] == ["reading-corrected"]
    with pytest.raises(ReviewError):
        r.approve(key)  # no score proposed: type one
    r.approve(key, points=1)
    assert (score(conn, "TYME2T", "gs8")["points"], score(conn, "TYME2T", "gs8")["source"]) == (1, "ruling")


def test_deciding_one_crop_alone(graded):
    cfg, conn = graded
    r = Review(cfg, conn)
    tid, q = DEFAULT_FAULTS["illegible"]
    cid = crop_id(conn, tid, q)
    r.decide(cid, 0, "Write your answer inside the box.")
    row = score(conn, tid, q)
    assert (row["points"], row["source"], row["feedback"]) == (0, "review", "Write your answer inside the box.")
    assert conn.execute("SELECT count(*) FROM rulings").fetchone()[0] == 0
    fake_run(cfg, conn)
    assert score(conn, tid, q)["source"] == "review"


def test_blank_batch(graded):
    cfg, conn = graded
    r = Review(cfg, conn)
    (blank,) = [x for x in read_csv(EXPECTED) if x["category"] == "blank"]
    cid = crop_id(conn, blank["tracking_id"], blank["item"])
    assert r.group(["blanks"])["items"][0]["crop_id"] == cid
    r.confirm_blanks([cid])
    row = score(conn, blank["tracking_id"], blank["item"])
    assert (row["points"], row["status"]) == (0, "final")
    assert r.group(["blanks"]) is None


def test_a_blank_with_writing_goes_back_to_read(graded):
    cfg, conn = graded
    r = Review(cfg, conn)
    (blank,) = [x for x in read_csv(EXPECTED) if x["category"] == "blank"]
    cid = crop_id(conn, blank["tracking_id"], blank["item"])
    r.confirm_blanks([cid], not_blank=[cid])
    assert conn.execute("SELECT blank_confirmed FROM crops WHERE id = ?", (cid,)).fetchone()[0] == 0
    assert r.group(["blanks"]) is None
    assert score(conn, blank["tracking_id"], blank["item"])["status"] == "review"
    s, t = fake_run(cfg, conn, FakeTransport(EXPECTED, overrides={(blank["tracking_id"], blank["item"]): "3x^3"}))
    assert s.calls["read"] == 1


def test_review_all_empties_the_queue(graded):
    cfg, conn = graded
    r = Review(cfg, conn)
    review_all(r, {DEFAULT_FAULTS["illegible"][0]: "2"})
    assert r.groups() == [] and open_rows(conn) == 0
    assert conn.execute("SELECT count(*) FROM scores WHERE status != 'final'").fetchone()[0] == 0


# ---- unplaced pages and late papers ---------------------------------------

def test_unplaced_page_is_listed_by_scan_not_file_name(late_paper):
    cfg, conn, page_id = late_paper
    r = Review(cfg, conn)
    (p,) = r.unplaced()
    assert p["id"] == page_id and p["reason"] == "qr-unreadable" and "filename" not in p
    assert r.candidates(A, 1) == {"missing_this_page": [], "known": sorted(
        {x["tracking_id"] for x in read_csv(SYNTHETIC / "scan-pages.csv")} - {LATE})}


def test_placing_by_hand_checks_its_input(late_paper):
    cfg, conn, page_id = late_paper
    with pytest.raises(PlaceError, match="not a tracking id"):
        place_by_hand(cfg, conn, page_id, A, 1, "O0O0O0")
    with pytest.raises(PlaceError, match="no manifest"):
        place_by_hand(cfg, conn, page_id, "alg2-nope", 1, LATE)
    with pytest.raises(PlaceError, match="pages 1 to 1"):
        place_by_hand(cfg, conn, page_id, A, 2, LATE)


def test_late_paper_placed_by_hand_gets_the_rulings(late_paper):
    cfg, conn, page_id = late_paper
    res = place_by_hand(cfg, conn, page_id, A, 1, LATE.lower())
    assert (res.status, res.tracking_id) == ("placed", LATE)
    page = conn.execute("SELECT * FROM pages WHERE id = ?", (page_id,)).fetchone()
    assert page["status"] == "placed" and page["resolved_at"] and page["reason"] == "qr-unreadable"
    assert conn.execute("SELECT count(*) FROM crops WHERE page_id = ?", (page_id,)).fetchone()[0] == 8
    sub = conn.execute("SELECT * FROM submissions WHERE tracking_id = ?", (LATE,)).fetchone()
    assert sub["first_received_on"] == "2026-10-23"
    assert Review(cfg, conn).unplaced() == []

    s, t = fake_run(cfg, conn)
    # BKTRTN's gs8 answer was ruled on in the class set: the late copy of it
    # gets that ruling with no rubric call, no verify call and no review.
    assert s.calls["rubric"] == 0
    assert f"verify-{A}-{LATE}-gs8" not in t.store["requests"]
    row = score(conn, LATE, "gs8")
    assert (row["source"], row["status"], row["points"]) == ("ruling", "final", 3)
    assert row["feedback"] == score(conn, "BKTRTN", "gs8")["feedback"]
    assert conn.execute("SELECT count(*) FROM review_queue WHERE tracking_id = ? AND resolved_at IS NULL",
                        (LATE,)).fetchone()[0] == 0


def test_a_second_copy_placed_by_hand_is_a_duplicate(late_paper):
    cfg, conn, page_id = late_paper
    res = place_by_hand(cfg, conn, page_id, A, 1, "BKTRTN")
    assert res.status == "duplicate"
    first = conn.execute("SELECT id FROM pages WHERE tracking_id = 'BKTRTN' AND status = 'placed'").fetchone()[0]
    assert conn.execute("SELECT duplicate_of FROM pages WHERE id = ?", (page_id,)).fetchone()[0] == first


def test_dismissing_an_unplaced_page(late_paper):
    cfg, conn, page_id = late_paper
    dismiss_page(conn, page_id)
    assert Review(cfg, conn).unplaced() == []
    with pytest.raises(PlaceError):
        place_by_hand(cfg, conn, page_id, A, 1, LATE)
