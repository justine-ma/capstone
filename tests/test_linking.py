import pandas as pd
import pytest

from src import linking
from src.checks import DataCheckError

T = pd.Timestamp


def firm_months(*rows):
    return pd.DataFrame(rows, columns=["gvkey", "datadate"]).astype({"datadate": "datetime64[ns]"})


def ccm(*rows):
    df = pd.DataFrame(rows, columns=["gvkey", "permno", "linkdt", "linkenddt"])
    return df.astype({"permno": "Int64", "linkdt": "datetime64[ns]", "linkenddt": "datetime64[ns]"})


def merge_ccm(left, links):
    return linking.interval_merge(left, links, on="gvkey", date_col="datadate", start_col="linkdt",
                                  end_col="linkenddt", context="test")


def test_null_end_date_means_link_still_active():
    out = merge_ccm(firm_months(("A", T("2016-12-31"))), ccm(("A", 1, T("2000-01-01"), None)))
    assert out["permno"].tolist() == [1]


def test_window_is_inclusive_and_dates_outside_are_unlinked_not_dropped():
    left = firm_months(("A", T("1999-12-31")), ("A", T("2000-01-31")),
                       ("A", T("2000-03-31")), ("A", T("2000-04-30")))
    out = merge_ccm(left, ccm(("A", 1, T("2000-01-31"), T("2000-03-31"))))
    assert len(out) == 4
    assert out["permno"].isna().tolist() == [True, False, False, True]


def test_permno_switch_follows_the_date_windows():
    left = firm_months(("A", T("2005-01-31")), ("A", T("2005-02-28")))
    out = merge_ccm(left, ccm(("A", 1, T("2000-01-01"), T("2005-01-31")), ("A", 2, T("2005-02-01"), None)))
    assert out["permno"].tolist() == [1, 2]


def test_overlapping_links_raise_instead_of_duplicating_rows():
    links = ccm(("A", 1, T("2000-01-01"), None), ("A", 2, T("2004-01-01"), None))
    with pytest.raises(DataCheckError, match="share a"):
        merge_ccm(firm_months(("A", T("2005-01-31"))), links)


def test_link_secid_discards_poor_scores():
    left = firm_months(("A", T("2010-01-31")), ("B", T("2010-01-31")))
    left["permno"] = pd.array([1, 2], dtype="Int64")
    om = pd.DataFrame({"secid": [10, 20], "permno": [1, 2], "score": [1, 3],
                       "sdate": [T("1996-01-01")] * 2, "edate": [T("2020-12-31")] * 2})
    om = om.astype({"secid": "Int64", "permno": "Int64", "score": "Int64"})
    out = linking.link_secid(left, om, max_score=1)
    assert out["secid"].tolist()[0] == 10 and pd.isna(out["secid"].iloc[1])


def test_two_firms_on_one_secid_raise():
    left = firm_months(("A", T("2010-01-31")), ("B", T("2010-01-31")))
    left["permno"] = pd.array([1, 2], dtype="Int64")
    om = pd.DataFrame({"secid": [10, 10], "permno": [1, 2], "score": [1, 1],
                       "sdate": [T("1996-01-01")] * 2, "edate": [T("2020-12-31")] * 2})
    om = om.astype({"secid": "Int64", "permno": "Int64", "score": "Int64"})
    with pytest.raises(DataCheckError, match="secid shared"):
        linking.link_secid(left, om, max_score=1)
