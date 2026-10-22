"""Rubric YAML files, found from the manifest that names them.

manifest.json gives a rubric path relative to the lesson directory. The
manifest itself is build output: <course>/target/<unit>/<lesson>/grading/
<component>/manifest.json, so the lesson directory is <course>/<unit>/<lesson>.
"""

from dataclasses import dataclass
from pathlib import Path

import yaml


class RubricError(Exception):
    pass


@dataclass
class Rubric:
    path: Path
    text: str          # the file as written, sent to the model
    data: dict

    @property
    def criteria(self) -> list[dict]:
        return self.data["criteria"]


def lesson_dirs(manifest_path: Path) -> list[Path]:
    """Where a rubric path in this manifest may be relative to, best first."""
    manifest_path = Path(manifest_path)
    parts = manifest_path.parts
    out = []
    if "target" in parts and "grading" in parts:
        t = len(parts) - 1 - parts[::-1].index("target")
        g = len(parts) - 1 - parts[::-1].index("grading")
        if t < g:
            out.append(Path(*parts[:t], *parts[t + 1:g]))
    out.extend(manifest_path.parents)
    return out


def find(manifest_path: Path, rel: str) -> Path:
    for d in lesson_dirs(manifest_path):
        if (d / rel).is_file():
            return d / rel
    raise RubricError(f"rubric {rel} (named in {manifest_path}) not found in the lesson directory")


def load(manifest_path: Path, rel: str) -> Rubric:
    path = find(manifest_path, rel)
    text = path.read_text()
    data = yaml.safe_load(text)
    criteria = data.get("criteria") if isinstance(data, dict) else None
    if not criteria or not all(isinstance(c, dict) and {"id", "points"} <= c.keys() for c in criteria):
        raise RubricError(f"{path}: needs a criteria list, each with an id and points")
    ids = [str(c["id"]) for c in criteria]
    if len(set(ids)) != len(ids):
        raise RubricError(f"{path}: criterion ids repeat")
    total = sum(float(c["points"]) for c in criteria)
    if "points" in data and abs(float(data["points"]) - total) > 1e-9:
        raise RubricError(f"{path}: criteria add up to {total}, not {data['points']}")
    return Rubric(path, text, data)
