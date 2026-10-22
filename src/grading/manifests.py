"""Find each assignment's manifest.json in the curriculum checkouts.

The curriculum builds write manifest.json next to the blank and key PDFs
(math-shared's grading.mk: target/<unit>/<lesson>/grading/<component>/ in a
course). The QR code on a page carries the assignment id; this index maps
that id to the manifest the same build produced.
"""

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path


class ManifestError(Exception):
    pass


@dataclass
class Manifest:
    assignment: str
    path: Path
    sha256: str
    data: dict

    @property
    def pages(self) -> int:
        return self.data["pages"]

    def questions_on(self, page: int) -> list[dict]:
        return [q for q in self.data["questions"] if q["page"] == page]


def _walk(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        if "manifest.json" in filenames:
            yield Path(dirpath) / "manifest.json"


def _read(path: Path) -> Manifest | None:
    raw = path.read_bytes()
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict) or not {"assignment", "questions", "marks", "qr", "strip"} <= data.keys():
        return None
    if data.get("key"):
        return None  # key-manifest.json and the like: scans align to the blank
    return Manifest(data["assignment"], path, hashlib.sha256(raw).hexdigest(), data)


class ManifestIndex:
    def __init__(self, roots):
        self.roots = [Path(r) for r in roots]
        self._by_id: dict[str, Manifest] | None = None

    def _load(self):
        found: dict[str, Manifest] = {}
        for root in self.roots:
            if not root.is_dir():
                raise ManifestError(f"curriculum checkout {root} does not exist")
            for path in _walk(root):
                m = _read(path)
                if m is None:
                    continue
                prev = found.get(m.assignment)
                if prev and prev.sha256 != m.sha256:
                    raise ManifestError(f"assignment {m.assignment} has two different manifests: "
                                        f"{prev.path} and {m.path}")
                found.setdefault(m.assignment, m)
        self._by_id = found

    def get(self, assignment: str) -> Manifest | None:
        if self._by_id is None:
            self._load()
        return self._by_id.get(assignment)

    def __len__(self):
        if self._by_id is None:
            self._load()
        return len(self._by_id)
