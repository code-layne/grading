"""Scoring in code: choice letters and short answers.

The model only transcribes. Whether an answer is right is decided here, from
the manifest's key and check (expression, numeric, set or text). A reading
that can't be parsed gets no score at all, so it goes to review, never to zero.

normalize() is also the key that groups identical answers and that rulings
are stored under, so review (session 4b) must use it too.
"""

import re
import unicodedata
from dataclasses import dataclass

import sympy
from sympy.parsing.sympy_parser import (convert_xor, implicit_multiplication_application, parse_expr,
                                        standard_transformations)

TRANSFORMS = standard_transformations + (implicit_multiplication_application, convert_xor)
FUNCTIONS = {"sqrt": sympy.sqrt, "ln": sympy.log, "log": sympy.log, "exp": sympy.exp, "abs": sympy.Abs,
             "sin": sympy.sin, "cos": sympy.cos, "tan": sympy.tan, "pi": sympy.pi}
# Readings are model output from a photo of handwriting; parse_expr evaluates
# Python. Only these characters get that far.
SAFE = re.compile(r"^[0-9A-Za-z+\-*/^()., =]*$")
REPLACE = {"−": "-", "–": "-", "—": "-", "×": "*", "⋅": "*", "·": "*",
           "÷": "/", "√": "sqrt", "π": "pi", "²": "^2", "³": "^3",
           "[": "(", "]": ")"}


@dataclass
class Check:
    points: float | None        # None: could not be scored, goes to review
    correct: bool | None
    reason: str | None = None   # why there is no score


def _clean(text: str) -> str:
    text = text or ""
    for a, b in REPLACE.items():  # before NFKC, which turns x² into x2
        text = text.replace(a, b)
    text = unicodedata.normalize("NFKC", text)
    return " ".join(text.split())


def _strip_lhs(text: str) -> str:
    """'x = 3/2' -> '3/2': a single variable on the left is the student naming
    the unknown, not part of the answer."""
    m = re.fullmatch(r"\s*[A-Za-z]\s*=\s*(.+)", text)
    return m.group(1).strip() if m else text.strip()


def normalize(qtype: str, reading: str | None) -> str:
    text = _clean(reading or "")
    if qtype == "choice":
        return "".join(ch for ch in text.upper() if ch.isalpha())
    if qtype == "short":
        return _strip_lhs(text).replace(" ", "")
    return text.lower()


def parse(text: str):
    """A SymPy expression, or None when the text isn't one."""
    text = _strip_lhs(_clean(text))
    if not text or not SAFE.match(text) or "=" in text or "," in text:
        return None
    for word in re.findall(r"[A-Za-z]+", text):
        # Single letters are variables ("xy" is x*y); longer words must be functions.
        if len(word) > 2 and word not in FUNCTIONS:
            return None
    try:
        expr = parse_expr(text, local_dict=dict(FUNCTIONS, e=sympy.E), transformations=TRANSFORMS)
    except Exception:  # SymPy raises many kinds on bad input
        return None
    return expr if isinstance(expr, sympy.Basic) else None


def _same(a, b) -> bool:
    try:
        diff = sympy.simplify(sympy.expand(a - b))
        if diff == 0:
            return True
        return bool(diff.is_number and abs(complex(diff.evalf())) < 1e-9)
    except Exception:
        return False


def _elements(text: str):
    text = _clean(text).replace("{", "").replace("}", "")
    text = re.sub(r"\b(or|and)\b|;", ",", text)
    parts = [p.strip() for p in text.split(",")]
    if not parts or any(not p for p in parts):
        return None
    values = [parse(p) for p in parts]
    return None if any(v is None for v in values) else values


def _distinct(values):
    out = []
    for v in values:
        if not any(_same(v, u) for u in out):
            out.append(v)
    return out


def score(question: dict, reading: str) -> Check:
    """question: a manifest entry (or questions row) with type, points, answer,
    check and tolerance."""
    points = float(question["points"])
    qtype = question["type"]
    key = question.get("answer") or ""
    if qtype == "choice":
        got = normalize("choice", reading)
        if len(got) != 1:
            return Check(None, None, "not-a-letter")
        right = got == normalize("choice", key)
        return Check(points if right else 0.0, right)
    if qtype != "short":
        raise ValueError(f"{qtype} items are scored by rubric, not in code")

    kind = question.get("check") or question.get("check_kind") or "expression"
    if kind == "text":
        right = re.sub(r"[^a-z0-9 ]", "", _clean(reading).lower()) == re.sub(r"[^a-z0-9 ]", "", _clean(key).lower())
        return Check(points if right else 0.0, right)
    if kind == "set":
        got, want = _elements(reading), _elements(key)
        if want is None:
            raise ValueError(f"answer key {key!r} is not a set of values")
        if got is None:
            return Check(None, None, "unparseable")
        got, want = _distinct(got), _distinct(want)
        right = len(got) == len(want) and all(any(_same(g, w) for w in want) for g in got)
        return Check(points if right else 0.0, right)

    got, want = parse(reading), parse(key)
    if want is None:
        raise ValueError(f"answer key {key!r} does not parse")
    if got is None:
        return Check(None, None, "unparseable")
    if kind == "numeric":
        if not got.is_number:
            return Check(None, None, "not-a-number")
        tol = question.get("tolerance")
        tol = 1e-9 if tol is None else float(tol)
        try:
            right = abs(complex(got.evalf()) - complex(want.evalf())) <= tol + 1e-12
        except TypeError:
            return Check(None, None, "not-a-number")
        return Check(points if right else 0.0, right)
    if kind != "expression":
        raise ValueError(f"unknown check {kind!r}")
    right = _same(got, want)
    return Check(points if right else 0.0, right)
