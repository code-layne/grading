"""The config file: where the curricula, the scan inbox, the data folder and
roster-map.csv live, the ingest thresholds, the models and the API prices.

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


EFFORTS = ("low", "medium", "high", "xhigh", "max")


@dataclass
class ApiSettings:
    # One model transcribes the crops; the other scores rubrics and runs the
    # verify pass. Both default to the docs' general recommendation.
    read_model: str = "claude-opus-5-5"
    judge_model: str = "claude-opus-5-5"
    # output_config.effort. Opus 5.5 always thinks; effort sets how much.
    read_effort: str = "low"
    judge_effort: str = "medium"
    read_max_tokens: int = 4096
    judge_max_tokens: int = 8192
    # A run over more crops than this goes through the Batch API (half price,
    # within 24 hours); smaller runs use the standard Messages API. --now and
    # --batch override it for one run.
    batch_threshold: int = 20
    batch_poll_seconds: int = 30
    # Standard API calls in flight at once. A 429 or an overloaded error
    # halves it for the rest of the run, down to one.
    concurrency: int = 8
    # A live run whose estimate is above this many dollars is refused.
    max_run_usd: float = 10.0
    # Standard calls only (the Batch API rejects it): a request the model's
    # safety classifier declines is retried server-side on the fallback model
    # the API picks. The model that answered is recorded with each reading.
    refusal_fallback: bool = True
    keychain_service: str = "anthropic-grading"
    max_retries: int = 4
    # Output tokens (thinking included) assumed per call by the cost estimate.
    est_read_output_tokens: int = 800
    est_rubric_output_tokens: int = 2500
    est_verify_output_tokens: int = 1200


@dataclass
class Price:
    """US dollars per million tokens."""
    input: float
    output: float
    cache_write_5m: float
    cache_write_1h: float
    cache_read: float


# platform.claude.com/docs/en/about-claude/pricing, read 2026-10-22. The
# Batch API is 50% off every line; the discount stacks with cache pricing.
DEFAULT_PRICES = {
    "claude-opus-5-5": Price(4.00, 20.00, 5.00, 8.00, 0.20),
    "claude-sonnet-5-5": Price(2.00, 10.00, 2.50, 4.00, 0.10),
    "claude-haiku-5-5": Price(0.10, 0.50, 0.125, 0.20, 0.01),  # prompts up to 100k tokens
    # Where a declined Opus 5.5 request can fall back to.
    "claude-opus-5": Price(5.00, 25.00, 6.25, 10.00, 0.50),
    "claude-opus-4-8": Price(5.00, 25.00, 6.25, 10.00, 0.50),
}
BATCH_DISCOUNT = 0.5


@dataclass
class Config:
    curricula: list[Path]
    inbox: Path
    data: Path
    roster: Path
    ingest: IngestSettings = field(default_factory=IngestSettings)
    api: ApiSettings = field(default_factory=ApiSettings)
    prices: dict[str, Price] = field(default_factory=lambda: dict(DEFAULT_PRICES))
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


def _settings(target, raw: dict, section: str):
    known = target.__dataclass_fields__
    for key, value in raw.items():
        if key not in known:
            raise ConfigError(f"unknown setting {section}.{key}")
        kind = type(getattr(target, key))
        if kind is bool and not isinstance(value, bool):
            raise ConfigError(f"{section}.{key} must be true or false")
        setattr(target, key, kind(value))


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
    _settings(cfg.ingest, raw.get("ingest", {}), "ingest")
    _settings(cfg.api, raw.get("api", {}), "api")
    for key in ("read_effort", "judge_effort"):
        if getattr(cfg.api, key) not in EFFORTS:
            raise ConfigError(f"api.{key} must be one of {', '.join(EFFORTS)}")
    if cfg.api.concurrency < 1:
        raise ConfigError("api.concurrency must be at least 1")
    for model, entry in raw.get("prices", {}).items():
        fields = Price.__dataclass_fields__
        if not isinstance(entry, dict) or set(entry) != set(fields):
            raise ConfigError(f"prices.{model} needs exactly {', '.join(fields)}")
        cfg.prices[model] = Price(**{k: float(v) for k, v in entry.items()})

    # Scans, crops and the database never go near a repository: scans carry
    # student names, and the roster pairs names with tracking ids.
    for key in ("inbox", "data", "roster"):
        tree = git_work_tree(getattr(cfg, key))
        if tree is not None:
            raise ConfigError(f"paths.{key} ({getattr(cfg, key)}) is inside the git work tree "
                              f"{tree}; keep it outside every repository")
    return cfg
