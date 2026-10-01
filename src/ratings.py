"""Stage 1: S&P long-term issuer credit ratings from Compustat (comp.adsprate).

adsprate is monthly: each firm-month carries the rating in force at month end,
carried forward until the next rating action. The pull is not filtered by
sector. The universe filter (`universe_mask`) is applied downstream, so widening
the universe never requires a re-query.
"""

from __future__ import annotations

import logging

import pandas as pd

from src.checks import DataCheckError, FIRM_MONTH, assert_unique, check_rating_strings
from src.config import Config, Universe
from src.wrds_conn import WrdsSession, cached, query, verify_table

log = logging.getLogger(__name__)

RATINGS_TABLE = "comp.adsprate"
COMPANY_TABLE = "comp.company"

# S&P long-term issuer credit rating -> numeric scale, 1 = AAA ... 22 = D.
# SD (selective default: some but not all obligations missed) is a default
# designation, so it shares D's value. A string missing from this dict is
# flagged by map_ratings and fails check_rating_strings; it is never coerced.
RATING_SCALE: dict[str, int] = {
    "AAA": 1,
    "AA+": 2, "AA": 3, "AA-": 4,
    "A+": 5, "A": 6, "A-": 7,
    "BBB+": 8, "BBB": 9, "BBB-": 10,
    "BB+": 11, "BB": 12, "BB-": 13,
    "B+": 14, "B": 15, "B-": 16,
    "CCC+": 17, "CCC": 18, "CCC-": 19,
    "CC": 20, "C": 21,
    "D": 22, "SD": 22,
}
DEFAULT_RATINGS = frozenset({"D", "SD"})
# Codes meaning no rating is in force; their rows are dropped (and logged) as if
# the rating were null. 'N.M.' ("not meaningful") appears only in 1996-98, mostly
# around distressed restructurings, often before a D.
NOT_RATINGS = frozenset({"N.M."})

# comp.company carries the firm's *current* GICS codes, not those in force at the time.
FINANCIAL_SECTORS = ("40", "60")  # Real Estate (60) was part of Financials until 2016
COAL_SUBINDUSTRY = "10102050"  # Coal & Consumable Fuels


def pull_ratings(db, cfg: Config) -> pd.DataFrame:
    verify_table(db, RATINGS_TABLE, ["gvkey", "datadate", "splticrm"])
    verify_table(db, COMPANY_TABLE, ["gvkey", "conm", "gsector", "gind", "gsubind", "sic"])
    df = query(db, f"""
        select a.gvkey, a.datadate, a.splticrm,
               c.conm, c.gsector, c.gind, c.gsubind, c.sic
        from {RATINGS_TABLE} as a
        left join {COMPANY_TABLE} as c on a.gvkey = c.gvkey
        where a.splticrm is not null
          and a.datadate between %(start)s and %(end)s
        """, params={"start": cfg.sample.ratings_start, "end": cfg.sample.ratings_end},
        date_cols=["datadate"])
    if df.empty:
        raise RuntimeError(f"{RATINGS_TABLE} returned no rows for "
                           f"{cfg.sample.ratings_start}..{cfg.sample.ratings_end}")
    return df


def load_ratings(session: WrdsSession, cfg: Config, refresh: bool = False) -> pd.DataFrame:
    """The unfiltered ratings pull for the configured period, cached in data/raw."""
    s = cfg.sample
    path = cfg.paths.raw / f"comp_adsprate_{s.ratings_start:%Y%m%d}_{s.ratings_end:%Y%m%d}.parquet"
    return cached(path, lambda: pull_ratings(session.db, cfg), refresh)


def map_ratings(df: pd.DataFrame) -> pd.DataFrame:
    """Add rating_num (RATING_SCALE), rating_unmapped and is_default_rating."""
    out = df.copy()
    rating = out["splticrm"].astype("string").str.strip()
    out["rating_num"] = rating.map(RATING_SCALE).astype("Int64")
    out["rating_unmapped"] = out["rating_num"].isna()
    out["is_default_rating"] = rating.isin(DEFAULT_RATINGS).astype(bool)
    return out


def add_rating_changes(df: pd.DataFrame) -> pd.DataFrame:
    """Flag rating events within each firm's monthly series.

    rating_change   notches vs the previous month, positive = worse. Measured only
                    between consecutive calendar months: a re-rating after a gap
                    (rating withdrawn, then reinstated) is not a change.
    is_downgrade    rating_change > 0, excluding moves into default
    is_upgrade      rating_change < 0
    is_default_event  first month in D/SD after a non-default rating, gap or not.
                    A firm already in default at its first observation is not an event.
    """
    out = df.sort_values(FIRM_MONTH).reset_index(drop=True)
    month = out["datadate"].dt.year * 12 + out["datadate"].dt.month
    grp = out.groupby("gvkey", sort=False)
    consecutive = (month - month.groupby(out["gvkey"]).shift()) == 1
    out["rating_change"] = (out["rating_num"] - grp["rating_num"].shift()).where(consecutive)
    worse = out["rating_change"].gt(0).fillna(False).astype(bool)
    out["is_downgrade"] = worse & ~out["is_default_rating"]
    out["is_upgrade"] = out["rating_change"].lt(0).fillna(False).astype(bool)
    has_prev = grp.cumcount() > 0
    prev_default = grp["is_default_rating"].shift(fill_value=False).astype(bool)
    out["is_default_event"] = out["is_default_rating"] & has_prev & ~prev_default
    return out


def prepare_ratings(raw: pd.DataFrame) -> pd.DataFrame:
    """Validate the raw pull, map ratings to numbers and flag rating events and coal."""
    assert_unique(raw, FIRM_MONTH, f"{RATINGS_TABLE} x {COMPANY_TABLE} pull")
    off = ~raw["datadate"].dt.is_month_end
    if off.any():
        raise DataCheckError(f"{int(off.sum())} ratings rows are not dated at a month end, "
                             f"e.g. {raw.loc[off, 'datadate'].head(3).tolist()}")
    not_rated = raw["splticrm"].str.strip().isin(NOT_RATINGS)
    if not_rated.any():
        log.info("dropping %d firm-months (%d firms) coded %s: no rating in force", int(not_rated.sum()),
                 raw.loc[not_rated, "gvkey"].nunique(), raw.loc[not_rated, "splticrm"].value_counts().to_dict())
    df = map_ratings(raw[~not_rated])
    check_rating_strings(df)
    df = add_rating_changes(df)
    df["is_coal"] = df["gsubind"].eq(COAL_SUBINDUSTRY).fillna(False).astype(bool)
    log.info("ratings: %d firm-months, %d firms, %s to %s", len(df), df["gvkey"].nunique(),
             f"{df['datadate'].min():%Y-%m}", f"{df['datadate'].max():%Y-%m}")
    return df


HIST_GICS_TABLE = "comp.co_hgic"


def pull_historical_gics(db) -> pd.DataFrame:
    verify_table(db, HIST_GICS_TABLE, ["gvkey", "indtype", "gsector", "gsubind", "indfrom", "indthru"])
    df = query(db, f"""
        select gvkey, indtype, gsector as gsector_hist, gsubind as gsubind_hist, indfrom, indthru
        from {HIST_GICS_TABLE}
        """, date_cols=["indfrom", "indthru"])
    if df.empty:
        raise RuntimeError(f"{HIST_GICS_TABLE} returned no rows")
    return df


def load_historical_gics(session: WrdsSession, cfg: Config, refresh: bool = False) -> pd.DataFrame:
    return cached(cfg.paths.raw / "comp_co_hgic.parquet", lambda: pull_historical_gics(session.db), refresh)


def add_historical_gics(df: pd.DataFrame, hist: pd.DataFrame) -> pd.DataFrame:
    """Attach the GICS sector in force at each rating date (gsector_hist). History
    starts in June 1999, when GICS launched, so earlier months stay null."""
    from src.linking import interval_merge

    return interval_merge(df, hist[["gvkey", "gsector_hist", "gsubind_hist", "indfrom", "indthru"]],
                          on="gvkey", date_col="datadate", start_col="indfrom", end_col="indthru",
                          context="historical GICS")


def universe_mask(df: pd.DataFrame, universe: Universe) -> pd.Series:
    """Boolean mask for the configured universe; logs how many firms each rule drops.

    Sector rules use comp.company's current GICS codes, so a firm reclassified
    since the sample period is assigned by its current sector. `is_unit` (MLP and
    LLC units) comes from linking.add_share_codes and is only needed when
    include_mlps is false. A null include_* setting keeps the firms (open decision).
    """
    rules: list[tuple[str, pd.Series]] = []
    if universe.gics_sector is not None:
        n_null = df.loc[df["gsector"].isna(), "gvkey"].nunique()
        log.info("universe: %d firms have no GICS sector in %s and fail the sector rule",
                 n_null, COMPANY_TABLE)
        rules.append((f"GICS sector {universe.gics_sector}", df["gsector"].eq(universe.gics_sector)))
    if universe.exclude_financials:
        rules.append(("exclude financials", ~df["gsector"].isin(FINANCIAL_SECTORS)))
    if universe.include_mlps is False:
        rules.append(("exclude MLP/LLC units", ~df["is_unit"]))
    if universe.include_coal is False:
        rules.append(("exclude coal", ~df["is_coal"]))

    keep = pd.Series(True, index=df.index)
    for name, rule in rules:
        before = df.loc[keep, "gvkey"].nunique()
        keep &= rule.fillna(False).astype(bool)
        after = df.loc[keep, "gvkey"].nunique()
        log.info("universe rule %-24s keeps %5d of %5d firms (drops %d)", name, after, before, before - after)
    return keep
