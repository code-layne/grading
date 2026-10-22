"""roster-map.csv: tracking ids with names, from math-shared's roster_import.py.

Only `grading report` calls load(). Ingest, read, review and eval know
students by tracking id alone.

    tracking_id,legal_name,preferred_name,class_year,section
    K7QM2P,"Example, Avery Jordan",Avery,2028,Algebra 2 - A
"""

import csv
from dataclasses import dataclass
from pathlib import Path

COLUMNS = ("tracking_id", "legal_name", "preferred_name", "class_year", "section")


class RosterError(Exception):
    pass


@dataclass(frozen=True)
class Student:
    tracking_id: str
    legal_name: str       # "Last, First Middle"
    preferred_name: str
    class_year: str
    section: str

    @property
    def last_name(self) -> str:
        return self.legal_name.split(",", 1)[0].strip()

    @property
    def first_names(self) -> str:
        parts = self.legal_name.split(",", 1)
        return parts[1].strip() if len(parts) > 1 else ""

    @property
    def gradebook_key(self):
        """Gradebook order: last name, then first names, ignoring case."""
        return (self.last_name.casefold(), self.first_names.casefold(), self.tracking_id)

    @property
    def sheet_name(self) -> str:
        """The feedback sheet's heading: preferred name, then last name, so
        two students with the same preferred name can't be confused."""
        return f"{self.preferred_name or self.first_names} {self.last_name}".strip()


def load(path: Path) -> dict[str, Student]:
    path = Path(path)
    if not path.is_file():
        raise RosterError(f"no roster at {path} (paths.roster): run math-shared's scripts/roster_import.py")
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        missing = set(COLUMNS) - set(reader.fieldnames or ())
        if missing:
            raise RosterError(f"{path} is missing the column(s) {', '.join(sorted(missing))}")
        out = {}
        for n, row in enumerate(reader, start=2):
            tid = (row["tracking_id"] or "").strip()
            if not tid:
                continue
            if tid in out:
                raise RosterError(f"{path} line {n}: tracking id {tid} appears twice")
            out[tid] = Student(tid, row["legal_name"].strip(), (row["preferred_name"] or "").strip(),
                               (row["class_year"] or "").strip(), (row["section"] or "").strip())
    return out
