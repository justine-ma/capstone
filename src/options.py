"""Stage 3: OptionMetrics volatility surface and security prices.

Built so far: a coverage probe that finds which linked secids have any
volatility-surface data in each month. It aggregates server-side and returns
at most (secids x 12) rows per year, so it can run before the sample design is
settled.

Deliberately not built yet (PROJECT_SPEC "What to build first"): the full pull.
When built it must, for each year separately, query optionm.vsurfd{year}
filtered server-side by secid and days in cfg.options.maturities, keep the
full delta grid, cache the year to data/raw as parquet immediately, and run
checks.check_implied_vols. It also pulls the security price file (optionm.secprd)
for price, returns and shares outstanding.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable

import pandas as pd

from src.config import Config
from src.wrds_conn import WrdsSession, cached, query, verify_table

log = logging.getLogger(__name__)

SURFACE_COLUMNS = ["secid", "date", "days", "delta", "impl_volatility", "cp_flag"]


def surface_table(year: int) -> str:
    return f"optionm.vsurfd{year}"


def pull_surface_months(db, secids: tuple[int, ...], year: int, cfg: Config) -> pd.DataFrame:
    """Trading days per (secid, month) with a 30-day ATM call point on the surface."""
    ref = surface_table(year)
    verify_table(db, ref, SURFACE_COLUMNS)
    return query(db, f"""
        select secid, date_trunc('month', date)::date as month, count(*) as n_days
        from {ref}
        where secid in %(secids)s
          and date between %(start)s and %(end)s
          and days = 30 and delta = 50 and cp_flag = 'C'
        group by secid, date_trunc('month', date)
        """, params={"secids": secids, "start": cfg.sample.options_start, "end": cfg.sample.options_end},
        date_cols=["month"])


def load_surface_months(session: WrdsSession, cfg: Config, secids: Iterable[int],
                        refresh: bool = False) -> pd.DataFrame:
    """The coverage probe for every year of the options sample, cached per year in
    data/interim. Cache files are keyed by a hash of the secid set, so probing a
    different set never reuses a stale result."""
    ids = tuple(sorted({int(s) for s in secids}))
    tag = hashlib.sha1(",".join(map(str, ids)).encode()).hexdigest()[:10]
    frames = []
    for year in range(cfg.sample.options_start.year, cfg.sample.options_end.year + 1):
        path = cfg.paths.interim / "surface_probe" / f"vsurfd{year}_{tag}.parquet"
        frames.append(cached(path, lambda y=year: pull_surface_months(session.db, ids, y, cfg), refresh))
    out = pd.concat(frames, ignore_index=True)
    out["secid"] = out["secid"].astype("Int64")
    out["datadate"] = out["month"] + pd.offsets.MonthEnd(0)
    log.info("surface probe: %d secids probed, %d with data, %d secid-months",
             len(ids), out["secid"].nunique(), len(out))
    return out[["secid", "datadate", "n_days"]]
