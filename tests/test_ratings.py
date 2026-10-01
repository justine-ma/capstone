import pandas as pd
import pytest

from src import ratings
from src.checks import DataCheckError, check_rating_strings
from src.config import Universe


def rated(rows):
    """rows: (gvkey, 'YYYY-MM', rating) -> mapped frame with rating events."""
    df = pd.DataFrame(rows, columns=["gvkey", "month", "splticrm"])
    df["datadate"] = pd.PeriodIndex(df.pop("month"), freq="M").to_timestamp(how="end").normalize()
    return ratings.add_rating_changes(ratings.map_ratings(df))


def events(df, gvkey, col):
    sel = df[(df["gvkey"] == gvkey) & df[col]]
    return sel["datadate"].dt.strftime("%Y-%m").tolist()


def test_scale_runs_from_aaa_1_to_d_22():
    scale = ratings.RATING_SCALE
    assert scale["AAA"] == 1 and scale["BBB-"] == 10 and scale["D"] == 22
    assert sorted(set(scale.values())) == list(range(1, 23))
    assert scale["SD"] == scale["D"]


def test_unmapped_strings_are_flagged_not_coerced():
    out = ratings.map_ratings(pd.DataFrame({"splticrm": ["BBB", "N.M.", "SD"]}))
    assert out["rating_unmapped"].tolist() == [False, True, False]
    assert out["rating_num"].iloc[2] == 22
    with pytest.raises(DataCheckError, match="N.M."):
        check_rating_strings(out)


def test_not_ratings_are_dropped_but_unknown_strings_still_fail():
    raw = pd.DataFrame({
        "gvkey": ["A", "A", "A"],
        "datadate": pd.to_datetime(["2000-01-31", "2000-02-29", "2000-03-31"]),
        "splticrm": pd.array(["B", "N.M.", "D"], dtype="string"),
        "gsubind": pd.array(["10102020"] * 3, dtype="string"),
    })
    out = ratings.prepare_ratings(raw)
    assert out["splticrm"].tolist() == ["B", "D"]
    assert out["is_default_event"].tolist() == [False, True]  # D after the N.M. gap still counts
    raw.loc[1, "splticrm"] = "NR?"
    with pytest.raises(DataCheckError, match="NR"):
        ratings.prepare_ratings(raw)


def test_changes_only_count_between_consecutive_months():
    df = rated([
        ("A", "2010-01", "BB"),
        ("A", "2010-02", "B"),     # downgrade
        ("A", "2010-05", "BB"),    # after a gap: new spell, not an upgrade
        ("A", "2010-06", "BB-"),   # downgrade
        ("B", "2010-01", "A"),
        ("B", "2010-02", "A+"),    # upgrade
    ])
    assert events(df, "A", "is_downgrade") == ["2010-02", "2010-06"]
    assert events(df, "A", "is_upgrade") == []
    assert events(df, "B", "is_upgrade") == ["2010-02"]


def test_default_events():
    df = rated([
        ("A", "2015-01", "CCC"),
        ("A", "2015-02", "D"),     # default, not also a downgrade
        ("A", "2015-03", "D"),     # same default episode
        ("B", "2015-01", "D"),     # already in default at first observation
        ("C", "2015-01", "CCC-"),
        ("C", "2015-04", "SD"),    # default after a gap still counts
        ("E", "2015-01", "SD"),
        ("E", "2015-02", "CCC+"),  # re-rated after distressed exchange
        ("E", "2015-03", "D"),     # second default event
    ])
    assert events(df, "A", "is_default_event") == ["2015-02"]
    assert events(df, "A", "is_downgrade") == []
    assert events(df, "B", "is_default_event") == []
    assert events(df, "C", "is_default_event") == ["2015-04"]
    assert events(df, "E", "is_default_event") == ["2015-03"]
    assert events(df, "E", "is_upgrade") == ["2015-02"]


@pytest.fixture
def universe_frame():
    return pd.DataFrame({
        "gvkey":   ["en", "unit", "coal", "fin", "reit", "tech", "nogics"],
        "gsector": ["10", "10", "10", "40", "60", "45", None],
        "is_unit": [False, True, False, False, False, False, False],
        "is_coal": [False, False, True, False, False, False, False],
    }).astype({"gsector": "string"})


def kept(df, universe):
    return df.loc[ratings.universe_mask(df, universe), "gvkey"].tolist()


def test_sector_filter_is_config_driven(universe_frame):
    assert kept(universe_frame, Universe("10", True, None, None)) == ["en", "unit", "coal"]
    assert kept(universe_frame, Universe(None, True, None, None)) == ["en", "unit", "coal", "tech", "nogics"]
    assert kept(universe_frame, Universe(None, False, None, None)) == universe_frame["gvkey"].tolist()


def test_open_decisions_keep_firms_and_false_drops_them(universe_frame):
    assert kept(universe_frame, Universe("10", True, False, None)) == ["en", "coal"]
    assert kept(universe_frame, Universe("10", True, True, False)) == ["en", "unit"]
