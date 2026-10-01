"""Run the pipeline end to end.

Currently: Stage 1 (ratings), Stage 2 (linking) and the options coverage probe,
then the coverage summary. Stage 3 (full options pull) and Stage 4 (skew panel)
come after the coverage review. Pulls are cached; pass --refresh to re-query.

    python scripts/build_panel.py [--refresh] [--link-through {permno,secid,surface}]

Without OptionMetrics access, use --link-through permno: coverage is then rated
firms with a CRSP permno, an upper bound on option coverage.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

from src import checks, panel, ratings  # noqa: E402
from src.config import load_config  # noqa: E402
from src.wrds_conn import SchemaError, WrdsSession  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--refresh", action="store_true", help="re-query WRDS instead of using cached pulls")
    parser.add_argument("--link-through", choices=panel.LINK_LEVELS, default="surface",
                        help="how far to link: CRSP permno, OptionMetrics secid, or surface data (default)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    cfg = load_config()
    try:
        with WrdsSession() as session:
            df = panel.firm_months(session, cfg, refresh=args.refresh, link_through=args.link_through)
    except SchemaError as e:
        sys.exit(f"\n{e}\nWithout OptionMetrics access, rerun with --link-through permno for "
                 "CRSP-linked coverage (an upper bound on option coverage).")

    all_sectors = dataclasses.replace(cfg.universe, gics_sector=None)
    universes = {
        f"sector_{cfg.universe.gics_sector or 'all'}": df["in_universe"],
        "all_sectors_ex_fin": ratings.universe_mask(df, all_sectors),
    }
    cfg.paths.processed.mkdir(parents=True, exist_ok=True)
    with pd.option_context("display.width", 200, "display.max_columns", 20):
        for name, mask in universes.items():
            summary = checks.coverage_summary(df, mask)
            summary.to_parquet(cfg.paths.processed / f"coverage_{name}.parquet")
            print(f"\nCoverage, {name} (matched on {summary.attrs['matched_on']}):\n{summary}")
    print("\nStages 3-4 (options pull, skew panel) wait on the coverage review.")


if __name__ == "__main__":
    main()
