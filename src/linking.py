"""Stage 2: identifier linking, gvkey -> permno (CCM) -> secid (OptionMetrics).

Link tables carry validity windows, and that is where silent merge errors come
from. Every join here goes through `interval_merge`: it keeps a link only if the
rating date falls inside [start, end] (a null end means the link is still
active), keeps unlinked firm-months with null ids rather than dropping them,
logs counts before and after, and asserts that no duplicate firm-month rows
were created.
"""

from __future__ import annotations

import logging

import pandas as pd

from src.checks import FIRM_MONTH, assert_unique, check_row_counts
from src.config import Config
from src.wrds_conn import WrdsSession, cached, query, verify_table

log = logging.getLogger(__name__)

CCM_COLUMNS = ["gvkey", "lpermno", "linktype", "linkprim", "linkdt", "linkenddt"]
OPTIONM_LINK_COLUMNS = ["secid", "sdate", "edate", "permno", "score"]
STOCKNAMES_TABLE = "crsp.stocknames"
STOCKNAMES_COLUMNS = ["permno", "namedt", "nameenddt", "shrcd", "exchcd", "ticker", "comnam"]


def interval_merge(left: pd.DataFrame, right: pd.DataFrame, *, on: str, date_col: str,
                   start_col: str, end_col: str, context: str,
                   keys: list[str] = FIRM_MONTH) -> pd.DataFrame:
    """Left-join `right` onto `left` where right[start_col] <= left[date_col] <= right[end_col].

    A null end date is treated as open-ended instead of letting the comparison
    drop the row. Rows of `left` without a valid link are kept with nulls.
    Raises if a key matches more than one link or the row count changes.
    """
    clash = (set(right.columns) - {on}) & set(left.columns)
    if clash:
        raise ValueError(f"{context}: columns {sorted(clash)} already exist on the left")
    if right[start_col].isna().any():
        raise ValueError(f"{context}: {int(right[start_col].isna().sum())} links have a null {start_col}")
    assert_unique(left, keys, f"{context} (input)")

    cols = list(dict.fromkeys([*keys, date_col, on]))
    cand = left[cols].dropna(subset=[on]).merge(right, on=on, how="inner")
    end = cand[end_col].fillna(pd.Timestamp.max)  # null end = link still active
    matched = cand[(cand[start_col] <= cand[date_col]) & (cand[date_col] <= end)]
    assert_unique(matched, keys, context)

    out = left.merge(matched.drop(columns=[c for c in cols if c not in keys]),
                     on=keys, how="left", validate="one_to_one")
    check_row_counts(len(left), len(out), context)
    has_key = left[on].notna()
    log.info("%s: linked %d of %d firm-months with a %s (%d of %d firms)", context,
             len(matched), int(has_key.sum()), on, matched["gvkey"].nunique(),
             left.loc[has_key, "gvkey"].nunique())
    return out


# CRSP/Compustat Merged ------------------------------------------------------

def pull_ccm_links(db, cfg: Config) -> pd.DataFrame:
    lk = cfg.linking
    verify_table(db, lk.ccm_table, CCM_COLUMNS)
    df = query(db, f"""
        select gvkey, lpermno as permno, linktype, linkprim, linkdt, linkenddt
        from {lk.ccm_table}
        where linktype in %(linktypes)s and linkprim in %(linkprims)s
        """, params={"linktypes": lk.ccm_linktypes, "linkprims": lk.ccm_linkprims},
        date_cols=["linkdt", "linkenddt"])
    if df.empty:
        raise RuntimeError(f"{lk.ccm_table} returned no links")
    df["permno"] = df["permno"].astype("Int64")
    return df


def load_ccm_links(session: WrdsSession, cfg: Config, refresh: bool = False) -> pd.DataFrame:
    lk = cfg.linking
    name = f"ccm_links_{'-'.join(lk.ccm_linktypes)}_{'-'.join(lk.ccm_linkprims)}.parquet"
    return cached(cfg.paths.raw / name, lambda: pull_ccm_links(session.db, cfg), refresh)


# CRSP share codes (flags MLP and LLC units) ---------------------------------

def pull_stocknames(db) -> pd.DataFrame:
    verify_table(db, STOCKNAMES_TABLE, STOCKNAMES_COLUMNS)
    df = query(db, f"select {', '.join(STOCKNAMES_COLUMNS)} from {STOCKNAMES_TABLE}",
               date_cols=["namedt", "nameenddt"])
    if df.empty:
        raise RuntimeError(f"{STOCKNAMES_TABLE} returned no rows")
    df["permno"] = df["permno"].astype("Int64")
    return df


def load_stocknames(session: WrdsSession, cfg: Config, refresh: bool = False) -> pd.DataFrame:
    return cached(cfg.paths.raw / "crsp_stocknames.parquet", lambda: pull_stocknames(session.db), refresh)


def add_share_codes(df: pd.DataFrame, stocknames: pd.DataFrame) -> pd.DataFrame:
    """Attach the CRSP share code in force at each rating date. Share codes 70-79
    are units (MLP units, LLC units, trust units): flagged as is_unit."""
    names = stocknames[["permno", "namedt", "nameenddt", "shrcd", "exchcd"]]
    out = interval_merge(df, names, on="permno", date_col="datadate",
                         start_col="namedt", end_col="nameenddt", context="CRSP share codes")
    out["is_unit"] = out["shrcd"].floordiv(10).eq(7).fillna(False).astype(bool)
    return out


# OptionMetrics-CRSP --------------------------------------------------------

def pull_optionm_links(db, cfg: Config) -> pd.DataFrame:
    """All scores are pulled; the score filter is applied in link_secid."""
    ref = cfg.linking.optionm_link_table
    verify_table(db, ref, OPTIONM_LINK_COLUMNS)
    df = query(db, f"select secid, sdate, edate, permno, score from {ref}", date_cols=["sdate", "edate"])
    if df.empty:
        raise RuntimeError(f"{ref} returned no links")
    for col in ("secid", "permno", "score"):
        df[col] = df[col].astype("Int64")
    return df


def load_optionm_links(session: WrdsSession, cfg: Config, refresh: bool = False) -> pd.DataFrame:
    name = cfg.linking.optionm_link_table.replace(".", "_") + ".parquet"
    return cached(cfg.paths.raw / name, lambda: pull_optionm_links(session.db, cfg), refresh)


def link_secid(df: pd.DataFrame, om_links: pd.DataFrame, max_score: int) -> pd.DataFrame:
    """Attach the OptionMetrics secid valid at each rating date. Lower scores are
    better matches; links scoring above `max_score` are discarded."""
    links = om_links[om_links["score"] <= max_score]
    log.info("OptionMetrics links: keeping %d of %d with score <= %d (score counts %s)",
             len(links), len(om_links), max_score, om_links["score"].value_counts().sort_index().to_dict())
    out = interval_merge(df, links[["permno", "secid", "sdate", "edate", "score"]], on="permno",
                         date_col="datadate", start_col="sdate", end_col="edate",
                         context="OptionMetrics permno->secid")
    # Two rated firms sharing one option series in a month would double-count it.
    assert_unique(out.dropna(subset=["secid"]), ["secid", "datadate"], "secid shared by two firms")
    out["has_secid"] = out["secid"].notna()
    return out


def link_crsp(ratings: pd.DataFrame, ccm: pd.DataFrame, stocknames: pd.DataFrame) -> pd.DataFrame:
    """gvkey -> permno (CCM), then the CRSP share code, each by date window."""
    df = interval_merge(ratings, ccm[["gvkey", "permno", "linktype", "linkprim", "linkdt", "linkenddt"]],
                        on="gvkey", date_col="datadate", start_col="linkdt", end_col="linkenddt",
                        context="CCM gvkey->permno")
    assert_unique(df.dropna(subset=["permno"]), ["permno", "datadate"], "permno shared by two firms")
    df = add_share_codes(df, stocknames)
    df["has_permno"] = df["permno"].notna()
    return df
