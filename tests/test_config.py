from datetime import date

import pytest
import yaml

from src.config import DEFAULT_CONFIG_PATH, REPO_ROOT, load_config


def write_variant(tmp_path, **universe):
    raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text())
    raw["universe"].update(universe)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path


def test_repo_config_loads_typed_values():
    cfg = load_config()
    assert cfg.sample.ratings_start == date(1996, 1, 1)
    assert cfg.sample.ratings_end == date(2017, 2, 28)
    assert cfg.sample.options_end == date(2017, 12, 31)
    assert cfg.universe.gics_sector == "10"
    assert cfg.universe.include_mlps is None and cfg.universe.include_coal is None
    assert cfg.options.maturities == (30, 91, 182, 365)
    assert cfg.paths.raw == REPO_ROOT / "data" / "raw"


def test_null_sector_means_all_sectors(tmp_path):
    assert load_config(write_variant(tmp_path, gics_sector=None)).universe.gics_sector is None


def test_integer_sector_is_normalized_to_string(tmp_path):
    assert load_config(write_variant(tmp_path, gics_sector=10)).universe.gics_sector == "10"


def test_open_decision_must_be_bool_or_null(tmp_path):
    with pytest.raises(ValueError, match="include_coal"):
        load_config(write_variant(tmp_path, include_coal="maybe"))
