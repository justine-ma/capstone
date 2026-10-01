# Option-implied skew and credit risk

MFE capstone studying how option-implied distributions and skew relate to corporate credit
risk. The first deliverable is a matched firm-level panel linking single-name equity option
data (OptionMetrics IvyDB US) to S&P issuer credit ratings (Compustat), both accessed through
WRDS.

> **No data is committed to this repo.** WRDS data is licensed and must not be redistributed,
> and this repository is public. `.gitignore` excludes `data/`, parquet/CSV and other data
> files, `.env` and `.pgpass`. Notebook outputs are stripped on commit by `nbstripout` (see
> Setup), because printed tables are data too.

## Sample

- **Ratings.** The S&P long-term issuer credit rating (`splticrm`) from Compustat's monthly
  ratings file (`comp.adsprate`), January 1996 to February 2017. February 2017 is where the
  Compustat ratings file ends.
- **Options.** OptionMetrics, January 1996 (when its coverage begins) to December 2017. The
  extra year of options keeps post-event windows for rating changes late in the sample.
- **Universe.** US energy firms (GICS sector 10) to start. The sector is a setting in
  `config/config.yaml` (`universe.gics_sector`, `null` = all sectors), so widening the
  universe needs no code changes. The ratings pull is never sector-filtered; the filter is
  applied afterwards. The sector comes from `comp.company` and is each firm's *current*
  classification.
- **Panel.** A firm is in the panel in the months it has both a rating and option data.
  Firms that later defaulted, were acquired or were delisted stay in the sample, because
  they are the most informative observations.
- **Identifiers.** Ratings (`gvkey`) link to options (`secid`) through CRSP `permno`. Both
  links carry date windows, and a link is used only for months inside its window.

## Setup

1. **WRDS account.** Register at <https://wrds-www.wharton.upenn.edu/register/> with your
   university email, choosing your institution; its WRDS representative approves the account.
   WRDS requires Duo two-factor enrollment. The pipeline needs these libraries: `comp`
   (Compustat), `crsp` (CRSP, including the CCM link table), `optionm` (OptionMetrics) and
   `wrdsapps` (WRDS linking suite).

2. **Environment.**

   ```bash
   conda create -n capstone python=3.11
   conda activate capstone
   pip install -e ".[dev]"
   nbstripout --install --attributes .gitattributes   # strip notebook outputs on commit
   ```

3. **Credentials.** Nothing secret goes in tracked files.

   ```bash
   cp .env.example .env        # then set WRDS_USERNAME in .env
   python -m src.wrds_conn     # first run: enter your password, answer y to save ~/.pgpass
   ```

   The second command also reports whether each required library is available to your
   account. You may need to approve a Duo push when connecting.

## Running the pipeline

```bash
python scripts/build_panel.py                        # uses cached pulls where present
python scripts/build_panel.py --refresh              # re-query WRDS
python scripts/build_panel.py --link-through permno  # without OptionMetrics access
```

`--link-through` sets how far linking goes. `surface` is the default and needs `optionm`.
`secid` needs the OptionMetrics-CRSP link. `permno` needs only CRSP, and the coverage it
reports is then an upper bound on option coverage.

Each WRDS pull is cached as parquet under `data/` the first time it runs, so later runs
don't reconnect. Before querying, every stage checks that its tables and columns exist, and
it fails with a clear message if one is missing. The quality checks in `src/checks.py` run
inside the stages. The script ends by printing the coverage summary for the configured
universe and for all non-financial sectors.

`notebooks/01_coverage_check.ipynb` shows the same numbers, plus a per-step linking funnel,
the sector-filter diagnostics and the Chesapeake Energy spot check.

Tests (synthetic data, no WRDS connection): `pytest`.

**Status.** Stage 1 (ratings), Stage 2 (linking) and a coverage probe of the volatility
surface are built. Stage 3 (the full OptionMetrics pull) and Stage 4 (skew measures) wait on
a review of the coverage numbers, which decide the sample design. As of 2026-09-23, the
`jwm9980` account cannot read `optionm` or `wrdsapps.opcrsphist`. Until it can, coverage
runs with `--link-through permno`.

## Layout

```
config/config.yaml     sample period, universe, link filters, maturities, paths
src/config.py          typed access to the config
src/wrds_conn.py       connection, table/column verification, parquet cache
src/ratings.py         stage 1: Compustat ratings, numeric scale, rating events, universe
src/linking.py         stage 2: gvkey -> permno -> secid by date window
src/options.py         stage 3: OptionMetrics (coverage probe so far)
src/panel.py           stage 4: firm-month panel (coverage skeleton so far)
src/checks.py          data quality checks and the coverage summary
scripts/build_panel.py runs the pipeline
notebooks/01_coverage_check.ipynb  coverage review (kernel: "Python 3.11 (capstone)")
docs/data_dictionary.md  sources, columns and derived fields
```

## Open items

- [ ] Does NYU have Capital IQ S&P Credit Ratings? It would extend ratings past Feb 2017.
      The `jwm9980` account lists the `ciq` rating tables but cannot read them
      (`ciq_ratings` permission denied); only the `ciqsamp_ratings` sample is readable.
- [ ] Get OptionMetrics (`optionm`) and the OptionMetrics-CRSP link added to the WRDS account.
- [ ] Sector by current or by historical GICS? `comp.company` holds current codes;
      `comp.co_hgic` holds the codes in force at each date. The coverage notebook
      measures the difference.
- [ ] Do MLPs, LLC units and coal stay in the sample? (`universe.include_mlps`,
      `universe.include_coal`; `null` keeps them and flags them.)
- [ ] Widen to all rated firms, with energy as a case study?
- [ ] Leverage controls from Compustat fundamentals (measure not yet chosen).
- [ ] Commodity controls (oil implied volatility) to separate commodity risk from credit
      risk in skew.
