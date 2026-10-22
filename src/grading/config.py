"""The config file: where the curricula, the scan inbox, the data folder and
roster-map.csv live, and the ingest thresholds.

    grading --config ~/.config/grading/config.toml ingest

Without --config, $GRADING_CONFIG, then ~/.config/grading/config.toml.
See config.example.toml for every key.
"""

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PATH = Path("~/.config/grading/config.toml")


class ConfigError(Exception):
    pass


@dataclass
class IngestSettings:
    dpi: int = 300
    # A pixel is ink when its gray level (0 black, 255 white) is BELOW this.
    # Show-through from the other side of the sheet measured 203 at its darkest
    # (isolated JPEG pixels) on the Brother test scan, printed ink about 30.
    ink_threshold: int = 160
    # Ink components smaller than this many pixels are dust or toner specks,
    # ignored by the blank check (not removed from the crop).
    speck_px: int = 40
    # A box with less ink than this, after specks, is marked blank.
    blank_ink_px: int = 80
    # Margin added on every side of an answer box when it is cropped.
    crop_margin_pt: float = 6.0
    # Largest allowed RMS distance between the four detected registration
    # marks and the fitted transform.
    max_mark_residual_px: float = 6.0


@dataclass
class Config:
    curricula: list[Path]
    inbox: Path
    data: Path
    roster: Path
    ingest: IngestSettings = field(default_factory=IngestSettings)
    source: Path | None = None

    @property
    def database(self) -> Path:
        return self.data / "grading.sqlite"


def git_work_tree(path: Path) -> Path | None:
    """The git work tree containing path, if any (path need not exist yet)."""
    p = path.expanduser().resolve()
    for d in (p, *p.parents):
        if (d / ".git").exists():
            return d
    return None


def _path(raw, key, base):
    if not isinstance(raw, str) or not raw:
        raise ConfigError(f"paths.{key} must be a path")
    p = Path(raw).expanduser()
    return p if p.is_absolute() else (base / p)


def load(path: str | os.PathLike | None = None) -> Config:
    if path is None:
        path = os.environ.get("GRADING_CONFIG") or DEFAULT_PATH
    path = Path(path).expanduser()
    if not path.is_file():
        raise ConfigError(f"no config file at {path} (copy config.example.toml there)")
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    return from_dict(raw, base=path.parent, source=path)


def from_dict(raw: dict, base: Path = Path("."), source: Path | None = None) -> Config:
    paths = raw.get("paths", {})
    missing = [k for k in ("curricula", "inbox", "data", "roster") if k not in paths]
    if missing:
        raise ConfigError(f"[paths] is missing {', '.join(missing)}")
    curricula = paths["curricula"]
    if isinstance(curricula, str):
        curricula = [curricula]
    cfg = Config(
        curricula=[_path(c, "curricula", base) for c in curricula],
        inbox=_path(paths["inbox"], "inbox", base),
        data=_path(paths["data"], "data", base),
        roster=_path(paths["roster"], "roster", base),
        source=source,
    )
    known = IngestSettings.__dataclass_fields__
    for key, value in raw.get("ingest", {}).items():
        if key not in known:
            raise ConfigError(f"unknown setting ingest.{key}")
        setattr(cfg.ingest, key, type(getattr(cfg.ingest, key))(value))

    # Scans, crops and the database never go near a repository: scans carry
    # student names, and the roster pairs names with tracking ids.
    for key in ("inbox", "data", "roster"):
        tree = git_work_tree(getattr(cfg, key))
        if tree is not None:
            raise ConfigError(f"paths.{key} ({getattr(cfg, key)}) is inside the git work tree "
                              f"{tree}; keep it outside every repository")
    return cfg
