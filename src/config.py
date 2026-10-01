"""Load config/config.yaml into typed, read-only settings."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "config.yaml"


@dataclass(frozen=True)
class Sample:
    ratings_start: date
    ratings_end: date
    options_start: date
    options_end: date


@dataclass(frozen=True)
class Universe:
    gics_sector: str | None  # None = all sectors
    exclude_financials: bool
    include_mlps: bool | None  # None = open decision: keep and flag
    include_coal: bool | None


@dataclass(frozen=True)
class Linking:
    ccm_table: str
    ccm_linktypes: tuple[str, ...]
    ccm_linkprims: tuple[str, ...]
    optionm_link_table: str
    optionm_max_score: int


@dataclass(frozen=True)
class Options:
    maturities: tuple[int, ...]


@dataclass(frozen=True)
class Paths:
    raw: Path
    interim: Path
    processed: Path


@dataclass(frozen=True)
class Config:
    sample: Sample
    universe: Universe
    linking: Linking
    options: Options
    paths: Paths


def _as_date(value, key: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as e:
        raise ValueError(f"config: {key} must be a YYYY-MM-DD date, got {value!r}") from e


def _as_optional_bool(value, key: str) -> bool | None:
    if value is None or isinstance(value, bool):
        return value
    raise ValueError(f"config: {key} must be true, false or null, got {value!r}")


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> Config:
    with open(path) as f:
        raw = yaml.safe_load(f)

    s = raw["sample"]
    sample = Sample(**{k: _as_date(s[k], f"sample.{k}") for k in
                       ("ratings_start", "ratings_end", "options_start", "options_end")})
    if sample.ratings_start > sample.ratings_end or sample.options_start > sample.options_end:
        raise ValueError("config: sample start dates must precede end dates")

    u = raw["universe"]
    sector = u.get("gics_sector")
    if sector is not None:
        sector = str(sector).zfill(2)
        if not (len(sector) == 2 and sector.isdigit()):
            raise ValueError(f"config: universe.gics_sector must be a 2-digit code or null, got {u['gics_sector']!r}")
    universe = Universe(
        gics_sector=sector,
        exclude_financials=bool(u.get("exclude_financials", True)),
        include_mlps=_as_optional_bool(u.get("include_mlps"), "universe.include_mlps"),
        include_coal=_as_optional_bool(u.get("include_coal"), "universe.include_coal"),
    )

    lk = raw["linking"]
    linking = Linking(
        ccm_table=lk["ccm_table"],
        ccm_linktypes=tuple(lk["ccm_linktypes"]),
        ccm_linkprims=tuple(lk["ccm_linkprims"]),
        optionm_link_table=lk["optionm_link_table"],
        optionm_max_score=int(lk["optionm_max_score"]),
    )

    options = Options(maturities=tuple(int(m) for m in raw["options"]["maturities"]))

    p = raw["paths"]
    paths = Paths(**{k: (REPO_ROOT / p[k]).resolve() for k in ("raw", "interim", "processed")})

    return Config(sample=sample, universe=universe, linking=linking, options=options, paths=paths)
