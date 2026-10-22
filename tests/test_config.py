import subprocess
from pathlib import Path

import pytest

from grading import config

ROOT = Path(__file__).resolve().parents[1]


def paths(tmp_path, **override):
    p = {"curricula": [str(tmp_path / "Mathematics" / "algebra_2")], "inbox": str(tmp_path / "inbox"),
         "data": str(tmp_path / "data"), "roster": str(tmp_path / "rosters" / "roster-map.csv")}
    p.update(override)
    return {"paths": p}


def test_loads_paths_and_settings(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text('[paths]\ncurricula = ["~/Mathematics/algebra_2"]\ninbox = "inbox"\n'
                 'data = "data"\nroster = "rosters/roster-map.csv"\n[ingest]\nink_threshold = 150\n')
    cfg = config.load(f)
    assert cfg.curricula == [Path("~/Mathematics/algebra_2").expanduser()]
    assert cfg.data == tmp_path / "data" and cfg.database == tmp_path / "data" / "grading.sqlite"
    assert cfg.ingest.ink_threshold == 150 and cfg.ingest.dpi == 300


def test_example_config_parses():
    raw = config.tomllib.loads((ROOT / "config.example.toml").read_text())
    assert set(raw["paths"]) == {"curricula", "inbox", "data", "roster"}
    assert set(raw["ingest"]) <= set(config.IngestSettings.__dataclass_fields__)
    assert set(raw["api"]) == set(config.ApiSettings.__dataclass_fields__)
    cfg = config.from_dict({**raw, "paths": paths(Path("/tmp/grading-example"))["paths"]})
    for model, price in raw["prices"].items():
        assert config.Price(**price) == config.DEFAULT_PRICES[model] == cfg.prices[model]


@pytest.mark.parametrize("key", ["data", "roster", "inbox"])
def test_refuses_paths_inside_a_git_work_tree(tmp_path, key):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    target = repo / ("roster-map.csv" if key == "roster" else key)
    with pytest.raises(config.ConfigError, match="git work tree"):
        config.from_dict(paths(tmp_path, **{key: str(target)}))


def test_unknown_setting_is_an_error(tmp_path):
    raw = paths(tmp_path)
    raw["ingest"] = {"ink_treshold": 150}
    with pytest.raises(config.ConfigError, match="ink_treshold"):
        config.from_dict(raw)


def test_api_settings_and_prices(tmp_path):
    raw = paths(tmp_path)
    raw["api"] = {"read_model": "claude-sonnet-5-5", "batch_threshold": 40, "max_run_usd": 3,
                  "refusal_fallback": False}
    raw["prices"] = {"claude-sonnet-5-5": {"input": 2, "output": 10, "cache_write_5m": 2.5,
                                           "cache_write_1h": 4, "cache_read": 0.1}}
    cfg = config.from_dict(raw)
    assert cfg.api.read_model == "claude-sonnet-5-5" and cfg.api.judge_model == "claude-opus-5-5"
    assert cfg.api.batch_threshold == 40 and cfg.api.max_run_usd == 3.0 and cfg.api.refusal_fallback is False
    assert cfg.prices["claude-sonnet-5-5"].cache_read == 0.1
    assert cfg.prices["claude-opus-5-5"].input == 4.0


@pytest.mark.parametrize("section, value, match", [
    ("api", {"read_effort": "extreme"}, "read_effort"),
    ("api", {"refusal_fallback": "yes"}, "true or false"),
    ("api", {"batch_treshold": 5}, "batch_treshold"),
    ("prices", {"claude-opus-5-5": {"input": 4}}, "needs exactly"),
])
def test_bad_api_settings(tmp_path, section, value, match):
    raw = paths(tmp_path)
    raw[section] = value
    with pytest.raises(config.ConfigError, match=match):
        config.from_dict(raw)
