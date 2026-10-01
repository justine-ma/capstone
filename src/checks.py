"""Data quality checks, called from inside the pipeline stages.

Numbered as in PROJECT_SPEC.md, Stage 5:
  1. assert_unique         no duplicate firm-month rows after a merge (raises)
  2. check_firm_counts     monthly firm counts, flagging jumps and cliffs (warns)
  3. check_rating_strings  every rating string maps to a number (raises, listing them)
  4. check_row_counts      a link join neither inflates nor deflates rows (raises)
  5. check_implied_vols    flags zero, negative and extreme implied vols (warns)
  6. check_chesapeake      known case: Chesapeake's 2014-2016 deterioration (raises)
Plus coverage_summary: firms and rating events per year for the coverage review.
"""

from __future__ import annotations

import logging

import pandas as pd

log = logging.getLogger(__name__)

FIRM_MONTH = ["gvkey", "datadate"]


class DataCheckError(AssertionError):
    """A data quality assertion failed; something upstream is wrong."""


# 1 -------------------------------------------------------------------------

def assert_unique(df: pd.DataFrame, keys: list[str], context: str) -> None:
    dup = df.duplicated(keys, keep=False)
    if dup.any():
        sample = df.loc[dup].sort_values(keys).head(10)
        raise DataCheckError(f"{context}: {int(dup.sum())} rows share a {keys} key.\n{sample}")


# 2 -------------------------------------------------------------------------

def check_firm_counts(df: pd.DataFrame, context: str, max_rel_change: float = 0.25,
                      min_abs_change: int = 5) -> pd.DataFrame:
    """Distinct firms per month; flags months whose count moves by more than
    `max_rel_change` (and at least `min_abs_change` firms) from the prior month."""
    counts = df.groupby("datadate")["gvkey"].nunique().rename("firms").to_frame()
    counts["change"] = counts["firms"].diff()
    counts["flag"] = ((counts["change"].abs() >= min_abs_change)
                      & (counts["change"].abs() > max_rel_change * counts["firms"].shift()))
    for date, row in counts[counts["flag"]].iterrows():
        log.warning("%s: firm count moves %+d to %d in %s; explain before relying on it",
                    context, row["change"], row["firms"], f"{date:%Y-%m}")
    log.info("%s: %d-%d firms per month, %d flagged months", context,
             counts["firms"].min(), counts["firms"].max(), int(counts["flag"].sum()))
    return counts


# 3 -------------------------------------------------------------------------

def check_rating_strings(df: pd.DataFrame) -> None:
    """Raise, listing every unmapped rating string, unless all map to a number."""
    unmapped = df.loc[df["rating_unmapped"], "splticrm"].value_counts(dropna=False)
    if not unmapped.empty:
        raise DataCheckError("rating strings not in ratings.RATING_SCALE (add them there, "
                             f"deliberately): {unmapped.to_dict()}")
    log.info("all %d rating strings map to RATING_SCALE", len(df))


# 4 -------------------------------------------------------------------------

def check_row_counts(n_before: int, n_after: int, context: str) -> None:
    log.info("%s: %d rows before, %d after", context, n_before, n_after)
    if n_before != n_after:
        raise DataCheckError(f"{context}: row count changed from {n_before} to {n_after}")


# 5 -------------------------------------------------------------------------

def check_implied_vols(df: pd.DataFrame, col: str = "impl_volatility",
                       upper: float = 3.0) -> pd.Series:
    """Count missing, zero, negative (OptionMetrics codes missing as -99.99) and
    implausibly large (> `upper`, annualized) implied vols. Flags, does not drop."""
    iv = df[col]
    counts = pd.Series({"rows": len(iv), "missing": int(iv.isna().sum()), "zero": int((iv == 0).sum()),
                        "negative": int((iv < 0).sum()), f"above_{upper:g}": int((iv > upper).sum())})
    bad = counts.drop(["rows", "missing"])
    if bad.any():
        log.warning("implied vol flags (%s): %s", col, bad[bad > 0].to_dict())
    return counts


# 6 -------------------------------------------------------------------------

# Found via CRSP names (ticker CHK, "CHESAPEAKE ENERGY CORP", permno 78877) -> CCM.
# comp.company.conm now reads "EXPAND ENERGY CORP" after the 2024 rename.
CHESAPEAKE_GVKEY = "027786"


def check_chesapeake(df: pd.DataFrame, gvkey: str | None = None) -> pd.DataFrame:
    """Chesapeake Energy's rating must deteriorate from 2014 to 2016, and it must
    link to CRSP and OptionMetrics throughout. Returns its rating path."""
    gvkey = gvkey or CHESAPEAKE_GVKEY
    if gvkey is None:
        raise DataCheckError("checks.CHESAPEAKE_GVKEY is not set")
    chk = df[(df["gvkey"] == gvkey) & df["datadate"].dt.year.between(2014, 2016)].sort_values("datadate")
    years = chk["datadate"].dt.year
    if not {2014, 2016} <= set(years):
        raise DataCheckError(f"Chesapeake (gvkey {gvkey}) lacks ratings in 2014 or 2016: years {sorted(set(years))}")
    first_2014 = chk.loc[years == 2014, "rating_num"].iloc[0]
    worst_2016 = chk.loc[years == 2016, "rating_num"].max()
    if pd.isna(first_2014) or pd.isna(worst_2016) or not worst_2016 > first_2014:
        raise DataCheckError(f"Chesapeake rating did not deteriorate 2014->2016 "
                             f"({first_2014} -> worst {worst_2016}); something upstream is wrong")
    for col in ("permno", "secid"):
        if col in chk and chk[col].isna().any():
            months = chk.loc[chk[col].isna(), "datadate"].dt.strftime("%Y-%m").tolist()
            raise DataCheckError(f"Chesapeake has no {col} link in {months}")
    if "has_options" in chk and not chk["has_options"].all():
        months = chk.loc[~chk["has_options"], "datadate"].dt.strftime("%Y-%m").tolist()
        raise DataCheckError(f"Chesapeake has no volatility-surface data in {months}")
    changed = chk["splticrm"].ne(chk["splticrm"].shift()).fillna(True)  # first row compares to NA
    path = chk.loc[changed, ["datadate", "splticrm", "rating_num"]]
    log.info("Chesapeake 2014-2016 path: %s", ", ".join(
        f"{d:%Y-%m} {r}" for d, r in zip(path["datadate"], path["splticrm"])))
    return path


# Coverage ------------------------------------------------------------------

COVERAGE_LEVELS = ("has_options", "has_secid", "has_permno")  # finest first


def matched_column(df: pd.DataFrame) -> str:
    """The finest coverage flag present: surface data, else an OptionMetrics
    secid, else a CRSP permno (an upper bound on option coverage)."""
    return next(c for c in COVERAGE_LEVELS if c in df)


def _had_coverage_before(events: pd.DataFrame, covered: pd.DataFrame, window: int) -> pd.Series:
    """For each event row, True if the firm was covered in one of the `window`
    months strictly before the event month."""
    if events.empty:
        return pd.Series(False, index=events.index)
    ev = events[["gvkey", "month_id"]].reset_index().sort_values("month_id")
    cov = covered[["gvkey", "month_id"]].rename(columns={"month_id": "covered_month"}).sort_values("covered_month")
    hit = pd.merge_asof(ev, cov, left_on="month_id", right_on="covered_month", by="gvkey",
                        allow_exact_matches=False, direction="backward")
    ok = (hit["month_id"] - hit["covered_month"]) <= window
    return pd.Series(ok.to_numpy(), index=hit["index"]).reindex(events.index)


def coverage_summary(df: pd.DataFrame, universe: pd.Series) -> pd.DataFrame:
    """Firms and rating events per year for one universe.

    A firm-month is *matched* on the finest coverage flag available (see
    matched_column); `attrs["matched_on"]` records which. Upgrades and downgrades
    count as "with options" if the firm was matched in the month before the
    event; defaults if it was matched in any of the 12 months before, since the
    stock often stops trading ahead of the default itself.
    """
    d = df.loc[universe].copy()
    matched_col = matched_column(d)
    d["year"] = d["datadate"].dt.year
    d["month_id"] = d["datadate"].dt.year * 12 + d["datadate"].dt.month
    covered = d[d[matched_col]]
    for event, window in (("downgrade", 1), ("upgrade", 1), ("default", 12)):
        flag = "is_default_event" if event == "default" else f"is_{event}"
        d[f"{event}_w_options"] = False
        ev = d[d[flag]]
        d.loc[ev.index, f"{event}_w_options"] = _had_coverage_before(ev, covered, window)

    g = d.groupby("year")
    out = pd.DataFrame({
        "rated_firms": g["gvkey"].nunique(),
        "matched_firms": covered.groupby("year")["gvkey"].nunique(),
        "matched_firm_months": covered.groupby("year").size(),
        "downgrades": g["is_downgrade"].sum(),
        "downgrades_w_options": g["downgrade_w_options"].sum(),
        "upgrades": g["is_upgrade"].sum(),
        "upgrades_w_options": g["upgrade_w_options"].sum(),
        "defaults": g["is_default_event"].sum(),
        "defaults_w_options_12m": g["default_w_options"].sum(),
    }).fillna(0).astype(int)
    out.index = out.index.astype(str)
    total = out.sum()
    for col, source in (("rated_firms", d), ("matched_firms", covered)):
        total[col] = source["gvkey"].nunique()  # distinct firms over the sample, not a sum
    out.loc["all years"] = total
    out.index.name = "year"
    out.attrs["matched_on"] = matched_col
    return out
