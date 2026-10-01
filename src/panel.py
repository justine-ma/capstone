"""Stage 4: the matched firm-month panel.

Built so far: the firm-month skeleton the coverage check runs on, which is every
rated firm-month with its CRSP and OptionMetrics links, universe flags and a
flag for volatility-surface data in that month. A firm is in the matched panel
in months where it has both a rating and option data. Defaulted and delisted
firms are never dropped.

Still to build after the coverage review: monthly skew measures at each
maturity (25-delta put minus ATM vol, the 25-delta risk reversal, and a slope
fitted across the delta grid), with daily option data kept alongside for event
studies around rating changes.

TODO: leverage controls from Compustat fundamentals (measure not chosen yet).
TODO: commodity controls (oil implied volatility) to separate commodity risk
from credit risk in skew.
"""

from __future__ import annotations

import logging

import pandas as pd

from src import checks, linking, options, ratings
from src.checks import FIRM_MONTH
from src.config import Config
from src.wrds_conn import WrdsSession

log = logging.getLogger(__name__)


def add_option_coverage(df: pd.DataFrame, surface_months: pd.DataFrame) -> pd.DataFrame:
    """Flag firm-months whose linked secid has volatility-surface data that month."""
    out = df.merge(surface_months, on=["secid", "datadate"], how="left", validate="many_to_one")
    checks.check_row_counts(len(df), len(out), "option coverage")
    checks.assert_unique(out, FIRM_MONTH, "option coverage")
    out["n_days"] = out["n_days"].fillna(0).astype(int)
    out["has_options"] = out["n_days"] > 0
    return out


LINK_LEVELS = ("permno", "secid", "surface")


def firm_months(session: WrdsSession, cfg: Config, refresh: bool = False,
                link_through: str = "surface") -> pd.DataFrame:
    """Stages 1-2 plus the options coverage probe, with the checks run along the way.

    `link_through` sets how far linking goes: "permno" stops after CRSP (needs no
    OptionMetrics access), "secid" adds the OptionMetrics link, and "surface" also
    probes the volatility surface for months with option data. Every rated
    firm-month is kept (all sectors); `in_universe` marks the configured universe.
    Pulls are served from cache unless `refresh` is set.
    """
    if link_through not in LINK_LEVELS:
        raise ValueError(f"link_through must be one of {LINK_LEVELS}, got {link_through!r}")
    rated = ratings.prepare_ratings(ratings.load_ratings(session, cfg, refresh))
    df = linking.link_crsp(rated, linking.load_ccm_links(session, cfg, refresh),
                           linking.load_stocknames(session, cfg, refresh))
    if link_through in ("secid", "surface"):
        om_links = linking.load_optionm_links(session, cfg, refresh)
        df = linking.link_secid(df, om_links, cfg.linking.optionm_max_score)
    if link_through == "surface":
        probed = options.load_surface_months(session, cfg, df["secid"].dropna(), refresh)
        df = add_option_coverage(df, probed)
    df["in_universe"] = ratings.universe_mask(df, cfg.universe)

    checks.check_chesapeake(df)
    matched_on = checks.matched_column(df)
    checks.check_firm_counts(df[df["in_universe"] & df[matched_on]],
                             f"firm-months with {matched_on}, configured universe")
    return df
