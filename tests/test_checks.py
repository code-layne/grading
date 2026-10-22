"""Code scoring: the model reads, code decides."""

import pytest

from grading import checks

EXPR = {"type": "short", "points": 2, "answer": "3x^3 + 4x^2 - 5x - 4", "check": "expression"}
NUMERIC = {"type": "short", "points": 1, "answer": "3/2", "check": "numeric", "tolerance": 0.01}
SET = {"type": "short", "points": 2, "answer": "5, 3", "check": "set"}
TEXT = {"type": "short", "points": 1, "answer": "reject H0", "check": "text"}
CHOICE = {"type": "choice", "points": 1, "answer": "C"}


@pytest.mark.parametrize("q, reading, points", [
    (EXPR, "3x^3 + 4x^2 - 5x - 4", 2), (EXPR, "3x^3+4x^2-5x-4", 2), (EXPR, "-4 - 5x + 4x^2 + 3x^3", 2),
    (EXPR, "3x³ + 4x² − 5x − 4", 2), (EXPR, "3x^3 - 2x^2 - 3x + 8", 0),
    (NUMERIC, "3/2", 1), (NUMERIC, "1.5", 1), (NUMERIC, "x = 3/2", 1), (NUMERIC, "1.51", 1),
    (NUMERIC, "1.52", 0), (NUMERIC, "14", 0), (NUMERIC, "2", 0),
    ({**NUMERIC, "check": "expression"}, "x = 3/2", 1), ({**NUMERIC, "check": "expression"}, "1.5", 1),
    (SET, "3, 5", 2), (SET, "{5, 3}", 2), (SET, "x = 3 or x = 5", 2), (SET, "5, 3, 3", 2),
    (SET, "6, 3", 0), (SET, "5", 0), (SET, "3, 5, 6", 0),
    (TEXT, "Reject H₀.", 1), (TEXT, "fail to reject H0", 0),
    (CHOICE, "C", 1), (CHOICE, "c", 1), (CHOICE, "(C)", 1), (CHOICE, "B", 0),
])
def test_scores(q, reading, points):
    assert checks.score(q, reading).points == points


@pytest.mark.parametrize("q, reading", [
    (EXPR, ""), (EXPR, "see my work below"), (EXPR, "__import__('os').system('true')"), (EXPR, "x = "),
    (EXPR, "3x^3 | 3x^2"), (NUMERIC, "x + 1"), (SET, "3,,5"), (SET, ""), (CHOICE, "AB"), (CHOICE, ""),
])
def test_what_does_not_parse_gets_no_score(q, reading):
    result = checks.score(q, reading)
    assert result.points is None and result.correct is None and result.reason


def test_normalize_groups_the_same_answer():
    assert checks.normalize("short", "x = 3/2") == checks.normalize("short", "3/2")
    assert checks.normalize("short", "3x^3+4x^2-5x-4") == checks.normalize("short", "3x^3 + 4x^2 - 5x - 4")
    assert checks.normalize("choice", " b ") == "B"
    assert checks.normalize("rubric", "The  first\nstep") == checks.normalize("rubric", "the first step")


def test_rubric_items_are_not_scored_in_code():
    with pytest.raises(ValueError):
        checks.score({"type": "rubric", "points": 4}, "anything")
