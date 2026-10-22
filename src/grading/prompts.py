"""The three kinds of request: read, rubric and verify.

Every request answers through one strict tool, so the response is JSON that
matches a schema and no prose is ever parsed. Opus 5.5 and Sonnet 5.5 reject
a forced tool_choice, so tool_choice is auto and the system prompt says to
call the tool; a response without the call is recorded as a failure.

Prompt order is tools, system, then the user turn. The stable part comes
first and the last system block carries the cache breakpoint: the
instructions plus, for rubric and verify, the question's key or rubric, so a
whole class shares one cached prefix per question. The crop and anything
about one student come after the breakpoint.

The read prompt deliberately carries no answer key: a reader that knows the
right answer drifts toward it, and a wrong answer read as the right one is
the one error review would never see.
"""

import base64
import json

LETTERS = [chr(c) for c in range(ord("A"), ord("Z") + 1)]
CONFIDENCE = {"type": "string", "enum": ["high", "medium", "low"]}

READ_SYSTEM = """\
You transcribe handwritten answers for a math teacher. Each image is one \
answer box cropped from a scanned student quiz. The scan is thresholded: the \
background is white and pencil or pen is gray to black. The printed border of \
the box shows at the edges; anything outside it has been cut away.

Transcribe exactly what the student wrote inside the box:
- Do not solve the problem, correct a mistake, finish a partial answer, \
simplify, or reorder terms. A wrong answer is transcribed as written.
- reading: plain-text math. Use ^ for exponents (x^2), / for fractions (3/2), \
sqrt(...) for roots, and the student's own signs and order. Keep any "x =" the \
student wrote. For written sentences, give the words as written, spelling \
included.
- latex: the same content in LaTeX, with nothing added.
- Ignore ink that is clearly crossed out. If two answers remain and you cannot \
tell which is final, transcribe both, separated by " | ", and use low confidence.
- confidence: high when you are certain of every character; medium when one \
character or sign could be read another way; low when more than that is uncertain.
- illegible: true only when the answer cannot be read at all; then reading \
and latex are empty strings.
- For a choice item the reading is the one capital letter the student wrote. \
If you see no letter, or more than one, set illegible true or confidence low.

Answer only by calling record_reading once."""

RUBRIC_SYSTEM = """\
You score a student's written response against a rubric for a math teacher. \
You get the rubric, the answer box image, and a transcription of it made \
earlier. The image is the evidence; if the transcription disagrees with the \
image, trust the image.

For each criterion:
- points: what the response earns on that criterion alone. Give credit only \
for what the response actually says, not for what the student probably meant.
- evidence: the exact words from the response that earn the points, quoted. \
An empty string when the criterion earns nothing.
- confidence: high, medium or low, for how sure you are of that criterion.

feedback: one short note to the student, addressed as "you". Point to what is \
missing or wrong with a question that sends the student back to it. Never \
state the correct answer, the corrected step, or the missing explanation. When \
everything is earned, name briefly what the response does well.

The graded examples in the rubric, when there are any, show how the teacher \
scores; follow them.

Answer only by calling score_rubric once."""

VERIFY_SYSTEM = """\
You check another grader's work for a math teacher. You get one question's \
manifest entry (its key or rubric), the student's answer box image, and the \
first grader's transcription and score. You do not see the first grader's \
reasoning.

Work independently:
1. Transcribe the box yourself before you look at the first reading, with the \
same rules: exactly what the student wrote, never solved or corrected; ^ for \
exponents, / for fractions; a choice item is one capital letter.
2. Then compare. agrees is true only when your transcription matches the \
first one and the score follows from it. Otherwise agrees is false and \
dispute says what differs, in one sentence. dispute is empty when you agree.

confidence and illegible mean the same as for the first reading: high only \
when you are certain of every character.

Answer only by calling {tool} once."""

RUBRIC_VERIFY_SYSTEM = VERIFY_SYSTEM + """

For a rubric question, also score every criterion yourself from the image, \
with the evidence words quoted, before comparing totals. agrees is true only \
when every criterion gets the same points as the first grader gave."""


def _object(properties: dict) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


def _reading_props(qtype: str) -> dict:
    reading = ({"type": "string", "enum": LETTERS + [""]} if qtype == "choice" else {"type": "string"})
    return {"reading": reading, "latex": {"type": "string"}, "confidence": CONFIDENCE,
            "illegible": {"type": "boolean"}}


def _steps(points: float) -> list:
    """Allowed points for a criterion: whole points, or halves when its value has a half."""
    step = 1.0 if float(points).is_integer() else 0.5
    n = int(round(float(points) / step))
    return [int(i * step) if (i * step).is_integer() else i * step for i in range(n + 1)]


def _criteria_schema(rubric) -> dict:
    return _object({str(c["id"]): _object({
        "points": {"type": "number", "enum": _steps(c["points"])},
        "evidence": {"type": "string"},
        "confidence": CONFIDENCE,
    }) for c in rubric.criteria})


def read_tool(qtype: str) -> dict:
    return {"name": "record_reading", "strict": True,
            "description": "Record the transcription of one answer box.",
            "input_schema": _object(_reading_props(qtype))}


def rubric_tool(rubric) -> dict:
    return {"name": "score_rubric", "strict": True,
            "description": "Record the score for each rubric criterion and one feedback note.",
            "input_schema": _object({"criteria": _criteria_schema(rubric), "feedback": {"type": "string"}})}


def verify_tool(qtype: str, rubric=None) -> dict:
    props = _reading_props(qtype)
    if rubric is not None:
        props["criteria"] = _criteria_schema(rubric)
    props.update(agrees={"type": "boolean"}, dispute={"type": "string"})
    return {"name": "verify_grade", "strict": True,
            "description": "Record your own reading and whether you agree with the first grader.",
            "input_schema": _object(props)}


def manifest_entry(q: dict) -> dict:
    """The question as the verify pass sees it: the manifest entry minus layout."""
    keys = ("id", "type", "points", "answer", "check", "tolerance", "rubric")
    return {k: q[k] for k in keys if q.get(k) is not None}


def _image(png: bytes) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                        "data": base64.standard_b64encode(png).decode("ascii")}}


def _cache(ttl: str | None) -> dict:
    return {"type": "ephemeral", "ttl": ttl} if ttl else {"type": "ephemeral"}


def _params(model, max_tokens, effort, tool, system_blocks, user_content, ttl):
    system = [{"type": "text", "text": t} for t in system_blocks]
    system[-1]["cache_control"] = _cache(ttl)
    return {
        "model": model,
        "max_tokens": max_tokens,
        "output_config": {"effort": effort},
        "tools": [tool],
        "tool_choice": {"type": "auto", "disable_parallel_tool_use": True},
        "system": system,
        "messages": [{"role": "user", "content": user_content}],
    }


def _describe(q: dict) -> str:
    kind = {"choice": "a multiple-choice or matching item (one letter)",
            "short": "a short answer",
            "rubric": "a written response"}[q["type"]]
    return f"Question {q['id']} is {kind}."


def read_request(api, q: dict, png: bytes, ttl: str | None) -> dict:
    return _params(api.read_model, api.read_max_tokens, api.read_effort, read_tool(q["type"]),
                   [READ_SYSTEM], [_image(png), {"type": "text", "text": f"{_describe(q)} "
                                                 "Transcribe the answer in this box."}], ttl)


def rubric_request(api, q: dict, rubric, png: bytes, reading: str, ttl: str | None) -> dict:
    key = f"Question {q['id']}, {q['points']:g} points. Rubric:\n\n{rubric.text}"
    user = [_image(png), {"type": "text", "text": "Transcription from the reading pass:\n\n"
                          f"{reading}\n\nScore the response in the image."}]
    return _params(api.judge_model, api.judge_max_tokens, api.judge_effort, rubric_tool(rubric),
                   [RUBRIC_SYSTEM, key], user, ttl)


def verify_request(api, q: dict, rubric, png: bytes, first: dict, ttl: str | None) -> dict:
    """first: {reading, confidence, points, criteria (rubric: {id: points})}."""
    tool = verify_tool(q["type"], rubric)
    entry = "Manifest entry:\n" + json.dumps(manifest_entry(q), indent=1)
    if rubric is not None:
        entry += f"\n\nRubric:\n\n{rubric.text}"
    score = "no score" if first["points"] is None else f"{first['points']:g} of {q['points']:g} points"
    lines = [f"First grader's reading: {first['reading']!r} (confidence {first['confidence']}).",
             f"First grader's score: {score}."]
    if first.get("criteria") and rubric is not None:
        given = first["criteria"]
        lines.append("By criterion: " + ", ".join(f"{c['id']} {given[str(c['id'])]:g}" for c in rubric.criteria
                                                  if str(c["id"]) in given) + ".")
    system = (RUBRIC_VERIFY_SYSTEM if rubric is not None else VERIFY_SYSTEM).format(tool=tool["name"])
    user = [_image(png), {"type": "text", "text": "\n".join(lines)}]
    return _params(api.judge_model, api.judge_max_tokens, api.judge_effort, tool, [system, entry], user, ttl)


TOOL_NAMES = {"read": "record_reading", "rubric": "score_rubric", "verify": "verify_grade"}
