# Data dictionary

## Source tables

All columns below were verified with `db.describe_table()` on 2026-09-23. Each stage
re-verifies its tables at run time and fails with a clear message if a table is missing, a
column is missing, or the account cannot read the table.

| Table | Used for | Columns used | Access for `jwm9980` |
| --- | --- | --- | --- |
| `comp.adsprate` | S&P issuer credit ratings, monthly | `gvkey`, `datadate`, `splticrm` | readable |
| `comp.company` | name, current GICS and SIC codes | `gvkey`, `conm`, `gsector`, `gind`, `gsubind`, `sic` | readable |
| `comp.co_hgic` | GICS codes in force at each date (diagnostic) | `gvkey`, `indtype`, `gsector`, `gsubind`, `indfrom`, `indthru` | readable |
| `crsp.ccmxpf_lnkhist` | `gvkey` -> `permno` | `gvkey`, `lpermno`, `linktype`, `linkprim`, `linkdt`, `linkenddt` | readable |
| `crsp.stocknames` | CRSP share codes (units flag) | `permno`, `namedt`, `nameenddt`, `shrcd`, `exchcd`, `ticker`, `comnam` | readable |
| `wrdsapps.opcrsphist` | `permno` -> `secid` | `secid`, `sdate`, `edate`, `permno`, `score` | listed, **not readable** |
| `optionm.vsurfd{year}` | volatility surface | `secid`, `date`, `days`, `delta`, `impl_volatility`, `cp_flag` | **not available** |

Notes:

- **`comp.adsprate`.** One row per firm per month. `datadate` is always a month end, and
  each rating is carried forward until the next action. Strings seen from 1996 to Feb 2017:
  AAA through C, D, SD, and `N.M.` ("not meaningful"). `N.M.` appears on 311 firm-months
  (26 firms), all in 1996-98. The other columns are `spsdrm` (subordinated debt) and
  `spsticrm` (short-term); neither is used.
- **`comp.company` and `comp.co_hgic`.** `comp.company` carries *current* codes: Chesapeake
  (gvkey 027786) appears as "EXPAND ENERGY CORP". `comp.co_hgic` starts in June 1999, when
  GICS launched; its `indtype` is always `GICS`.
- **`crsp.ccmxpf_lnkhist`.** A null `linkenddt` means the link is still active. 6,383 of the
  33,324 LC/LU x P/C links are open-ended.
- **`wrdsapps.opcrsphist`.** This is a view on `wrdsapps_link_crsp_optionm`, which needs the
  OptionMetrics entitlement. WRDS documents `score` as lower-is-better, with 1 the best
  match. That can't be checked against the data until the table is readable.

## Cached files (`data/`, git-ignored)

| Path | Contents |
| --- | --- |
| `data/raw/comp_adsprate_{start}_{end}.parquet` | unfiltered ratings pull joined to `comp.company` |
| `data/raw/ccm_links_{linktypes}_{linkprims}.parquet` | CCM links for the configured link types |
| `data/raw/crsp_stocknames.parquet` | CRSP name history |
| `data/raw/comp_co_hgic.parquet` | historical GICS |
| `data/raw/wrdsapps_opcrsphist.parquet` | OptionMetrics-CRSP links, all scores |
| `data/interim/surface_probe/vsurfd{year}_{hash}.parquet` | surface coverage probe, per year, per secid set |
| `data/processed/coverage_{universe}.parquet` | coverage summaries written by `build_panel.py` |

## Firm-month frame (`panel.firm_months`)

One row per `(gvkey, datadate)` for every rated firm-month in any sector. Rows without a
link are kept with null ids. They are never dropped.

| Column | From | Meaning |
| --- | --- | --- |
| `gvkey`, `datadate` | adsprate | firm and month end (the key) |
| `splticrm` | adsprate | S&P long-term issuer credit rating |
| `conm`, `gsector`, `gind`, `gsubind`, `sic` | company | current name and classification |
| `rating_num` | derived | `ratings.RATING_SCALE`: AAA = 1 ... C = 21, D and SD = 22 |
| `rating_unmapped` | derived | string not in `RATING_SCALE`; the pipeline stops if any exist |
| `is_default_rating` | derived | rating is D or SD |
| `rating_change` | derived | notches versus the prior month, positive = worse; null unless the months are consecutive |
| `is_downgrade` | derived | `rating_change > 0`, excluding moves into default |
| `is_upgrade` | derived | `rating_change < 0` |
| `is_default_event` | derived | first month in D/SD after a non-default rating, even across a gap |
| `is_coal` | derived | current `gsubind` is 10102050 (Coal & Consumable Fuels) |
| `permno`, `linktype`, `linkprim`, `linkdt`, `linkenddt` | CCM | link valid at `datadate` |
| `namedt`, `nameenddt`, `shrcd`, `exchcd` | stocknames | CRSP name record valid at `datadate` |
| `is_unit` | derived | `shrcd` 70-79: MLP, LLC and trust units |
| `has_permno` | derived | CRSP link present |
| `secid`, `sdate`, `edate`, `score` | opcrsphist | OptionMetrics link valid at `datadate` (with `--link-through secid` or `surface`) |
| `has_secid` | derived | OptionMetrics link present |
| `n_days`, `has_options` | surface probe | trading days in the month with a 30-day ATM call on the surface (`surface` only) |
| `in_universe` | derived | in the configured universe (`ratings.universe_mask`) |

Rows coded `N.M.` are dropped before this frame is built (see `ratings.NOT_RATINGS`), with
a logged count.

## Coverage summary (`checks.coverage_summary`)

One row per calendar year plus an `all years` row. In that row, firm counts are distinct
firms over the whole sample, not sums of the yearly counts. "Matched" uses the finest
coverage flag present: `has_options`, then `has_secid`, then `has_permno`. The chosen flag is
recorded in `attrs["matched_on"]`. With `has_permno`, the counts are upper bounds on option
coverage.

| Column | Meaning |
| --- | --- |
| `rated_firms` | distinct rated firms in the universe |
| `matched_firms`, `matched_firm_months` | distinct firms, and firm-months, in the matched panel |
| `downgrades`, `upgrades` | rating events among rated firms |
| `downgrades_w_options`, `upgrades_w_options` | events where the firm was matched the month before |
| `defaults` | default events among rated firms |
| `defaults_w_options_12m` | default events where the firm was matched in one of the 12 months before |

The events come from month-end ratings, so a rating that lasts less than a month is not seen.
An example is a brief SD around a distressed exchange.
