import pandas as pd
import pytest

from src import checks
from src.checks import DataCheckError


def month_end(ym):
    return pd.Period(ym, freq="M").to_timestamp(how="end").normalize()


def test_assert_unique_and_row_counts():
    df = pd.DataFrame({"gvkey": ["A", "A"], "datadate": [month_end("2010-01")] * 2})
    with pytest.raises(DataCheckError):
        checks.assert_unique(df, checks.FIRM_MONTH, "test")
    with pytest.raises(DataCheckError):
        checks.check_row_counts(10, 11, "test")


def test_firm_count_cliff_is_flagged():
    rows = [(f"F{i}", month_end("2010-01")) for i in range(20)]
    rows += [(f"F{i}", month_end("2010-02")) for i in range(20)]
    rows += [(f"F{i}", month_end("2010-03")) for i in range(8)]  # cliff
    counts = checks.check_firm_counts(pd.DataFrame(rows, columns=checks.FIRM_MONTH), "test")
    assert counts["flag"].tolist() == [False, False, True]


def test_implied_vol_flags():
    counts = checks.check_implied_vols(pd.DataFrame({"impl_volatility": [0.3, 0.0, -99.99, 4.2, None]}))
    assert counts[["zero", "negative", "above_3", "missing"]].tolist() == [1, 1, 1, 1]


def test_coverage_summary_counts_firms_and_events_with_options():
    months = ["2010-01", "2010-02", "2010-03", "2011-01", "2011-02"]
    df = pd.DataFrame({
        "gvkey": ["A"] * 5 + ["B"] * 2,
        "datadate": [month_end(m) for m in months] + [month_end("2010-01"), month_end("2010-02")],
        "has_permno":  [True, True, True, True, True, True, True],
        "has_options": [True, False, True, True, True, False, False],
        "is_downgrade":     [False, False, True, False, True, False, True],
        "is_upgrade":       [False] * 7,
        "is_default_event": [False] * 7,
    })
    everyone = pd.Series(True, index=df.index)
    out = checks.coverage_summary(df, everyone)
    assert out.attrs["matched_on"] == "has_options"
    assert out.loc["2010", "rated_firms"] == 2 and out.loc["2010", "matched_firms"] == 1
    # A's 2010-03 downgrade had no options in 2010-02; its 2011-02 downgrade did in 2011-01.
    assert out.loc["2010", ["downgrades", "downgrades_w_options"]].tolist() == [2, 0]
    assert out.loc["2011", ["downgrades", "downgrades_w_options"]].tolist() == [1, 1]
    assert out.loc["all years", "matched_firms"] == 1
    assert out.loc["all years", "matched_firm_months"] == 4

    # Without the surface probe, matching falls back to the CRSP link.
    crsp_only = checks.coverage_summary(df.drop(columns="has_options"), everyone)
    assert crsp_only.attrs["matched_on"] == "has_permno"
    assert crsp_only.loc["2010", ["matched_firms", "downgrades_w_options"]].tolist() == [2, 2]


def test_chesapeake_check_requires_deterioration():
    def frame(ratings_2016):
        dates = [month_end("2014-01"), month_end("2016-06")]
        return pd.DataFrame({"gvkey": "X", "datadate": dates,
                             "splticrm": pd.array(["BB+", ratings_2016[0]], dtype="string"),
                             "rating_num": pd.array([11, ratings_2016[1]], dtype="Int64"),
                             "permno": 1, "secid": 2})
    path = checks.check_chesapeake(frame(("CCC", 18)), gvkey="X")
    assert path["splticrm"].tolist() == ["BB+", "CCC"]
    with pytest.raises(DataCheckError, match="did not deteriorate"):
        checks.check_chesapeake(frame(("BBB", 9)), gvkey="X")
