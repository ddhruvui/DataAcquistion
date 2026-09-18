# Network volume layout — `crimtr8kbf`

What is actually on the RunPod network volume, derived from a full recursive listing
([arrange.txt](arrange.txt), `scripts/storage_usage.sh`) taken **2026-09-18**.

| | |
|---|---|
| Volume | `crimtr8kbf` (`eligible_purple_ermine`) |
| Region | EU-RO-1 |
| Allocated | 74 GB |
| Used | 58.3 GiB across **25,370** objects |
| Mount | `/workspace` on every pod; S3 API as `s3://crimtr8kbf` |

Thirteen top-level trees: one per vendor, plus the built landing layer, the quality report, the
uploaded code, the pod logs, and one tree this repo does not write (`results/`). Snapshot numbers
below are as-of that listing, not live state — re-run `scripts/storage_usage.sh` for current values.

## Top level

| tree | files | size | written by |
|---|---|---|---|
| `data/` | 20,573 | 55.28 GiB | EODHD — `src/fetch.py`, plus `src/fetch_intraday.py` under `tickdata/` |
| `data_nasdaq/` | 1,576 | 1.25 GiB | Sharadar — `src/fetch_nasdaq.py` |
| `data_tiingo/` | 1,048 | 0.78 GiB | Tiingo — `src/fetch_tiingo.py` |
| `data_borrow/` | 676 | 0.13 GiB | IBKR + iBorrowDesk — `src/fetch_borrow.py` |
| `m1/` | 581 | 0.31 GiB | `src/build_m1.py` (landing layer, not a fetcher) |
| `results/` | 570 | 0.13 GiB | **the models repo (InvestOpediaClaude)** — not produced here |
| `data_fomc/` | 212 | 0.015 GiB | federalreserve.gov — `src/fetch_fomc.py` |
| `_pod_logs/` | 92 | 0.002 GiB | every pod, via `src/bootstrap.sh` |
| `code/` | 24 | ~0 | `scripts/launch.sh` upload staging (skipped by `download.sh`) |
| `data_finbert/` | 8 | 0.41 GiB | `src/fetch_finbert.py` (D-16 weights) |
| `data_quality/` | 5 | 0.019 GiB | `src/validate.py` |
| `data_calendar/` | 5 | ~0 | `exchange_calendars` — `src/fetch_calendar.py` |

Four directories hold 96% of the bytes: `data/eod_bulk/US` (30.6 GiB), `data/tickdata/1m`
(11.9 GiB), `data/news` (11.3 GiB), `data_nasdaq` (1.2 GiB).

## Structure

Directories only; `[n]` is files directly inside. The repeated **517** is the per-ticker universe
(503 S&P names + the 2026-09-16 promotions + ETFs); **515** is the minute-bar symbol set.

```
_pod_logs/                                  [92]    <UTC>-<script>-<podid>.log
code/                                       [24]    uploaded fetcher + config per vendor
data/                                       [1]     _run.json
├── ohlcv/                                  [517]   D-01 per-ticker daily prices
├── eod_bulk/US/                            [6717]  D-01 whole-exchange day-files, 2000-01-03 → 2026-09-17
├── eod_bulk_actions/US/
│   ├── dividends/                          [165]   whole-market D-03, 2026-01-22 → 2026-09-17
│   └── splits/                             [165]   whole-market D-02, same span
├── dividends/                              [517]   D-03
├── splits/                                 [517]   D-02
├── fundamentals/                           [517]   D-05/06 + D-07 history
├── estimates/                              [517]   D-14, append-only snapshots
├── news/                                   [517]   D-08
├── market/                                 [1]     D-09 SPY price
│   └── dividends/                          [1]     D-09 SPY dividends
├── universe/                               [1]     D-15 GSPC.INDX
├── symbols/                                [1]     D-13 US.json
├── calendar/                               [1]     D-11 cross-check
├── earnings/                               [1]     upcoming.json
│   └── upcoming/                           [30]    dated snapshots
├── tickdata/                               [3]     _manifest / _run / _verify
│   ├── 1m/<515 symbol dirs>/<YYYY>.parquet [9839]  1-minute bars, 2004 → 2026
│   ├── _audit/                             [519]   per-symbol audit cache
│   └── logs/                               [4]
├── logs/                                   [2]
└── watchlist/                              [1]     data-only names (BRK-B + SPY/QQQ)
    ├── dividends/ splits/ ohlcv/ news/             mirrors data/, one file each
    ├── fundamentals/ estimates/
    ├── earnings/upcoming/                  [5]
    ├── market/dividends/                   [2]
    └── logs/                               [3]
data_nasdaq/                                [1]     _run.json
├── SEP/                                    [519]   D-12 per-ticker prices (+ _window.json)
├── SF1/                                    [518]   D-05/06 as-reported ARQ fundamentals
├── ACTIONS/                                [521]   D-04 corporate actions
├── TICKERS/                                [1]     D-13 whole-table entity master
├── SP500/                                  [1]     D-15 constituent history
├── logs/                                   [2]
└── watchlist/{ACTIONS,SEP,SF1,SFP,logs}/   [12]    BRK.B (dot form) + SFP for the ETFs
data_tiingo/                                [519]   <T>.json prices + _run + _coverage
├── metadata/                               [517]
├── market/                                 [1]
├── symbols/                                [1]
├── logs/                                   [2]
└── watchlist/{metadata,market,logs}/       [5]
data_borrow/                                [2]     _run.json + _run_history_v2.json
├── USA/                                    [41]    IBKR usa.txt snapshots (.json.gz, dated)
├── history/                                [517]   merged iBorrowDesk daily history
├── history_v2/                             [104]   raw v2 OHLC + the metered done-markers
├── logs/                                   [2]
└── watchlist/{history,history_v2,logs}/    [8]
data_calendar/                              [2]     XNYS.json + _run.json
└── logs/                                   [3]
data_fomc/                                  [2]     fomc.json + _run.json
├── raw/                                    [1]     cached source pages
│   ├── historical/                         [17]    one per year back to 2004
│   └── statements/                         [191]
└── logs/                                   [1]
data_finbert/                               [1]
├── 4556d13015211d73dccd3fdd39d39232506f3e43/ [5]   weights pinned by sha (config, model.bin, tokenizer)
└── logs/                                   [2]
data_quality/                               [3]     quarantine.json + report.json + shares_pit.json
└── logs/                                   [2]
m1/                                         [8]     _manifest + 7 single-file tables
├── raw_prices_eod/                         [28]    partitioned by year
├── adjustment_factors/                     [28]    part-<year>.parquet
└── qlib/                                   [517]   <TICKER>.csv
results/ResearchGate/                               written by the models repo, not this one
├── _pod_logs/                              [86]
├── app/{config,scripts,src/__pycache__}/   [35]    uploaded model code
├── code/                                   [2]
└── runs/pso_lssvm_v1/
    ├── latest/                             [7]     metrics / predictions / next_session / run_meta
    ├── daily/<UTC>/                        [4]
    ├── shards/20/{00..19}/                         per-shard run timestamps + latest/, 6 files each
    └── strategy/<UTC>/                     [2]
```

## Notes that the tree doesn't show

- **`data/` is two producers.** Everything except `tickdata/` comes from the EODHD pod; `tickdata/`
  is the intraday fetcher, which also **grows the volume** 1 GB at a time to keep `min_free_gb` free.
  The 74 GB allocation is a consequence of that, not a fixed size.
- **Not all of it is safe to read naively.** `eod_bulk/US/` holds 10 NYSE-holiday day-files (OTC rows
  only), its files are not immutable across pulls, and EODHD's `close` is retroactively rewritten —
  see *Reading this data correctly (before you build features on it)* in [README.md](README.md)
  before building features.
- **`watchlist/` subtrees are deliberately invisible to M1.** `build_m1.py` and `validate.py` glob
  non-recursively, so nothing under `<tree>/watchlist/` reaches the models.
- **`history_v2/<T>.json` doubles as a billing marker** for the metered iBorrowDesk allowance; moving
  a ticker without it re-bills the symbol.
- **`_run.json` per tree is the run manifest** — vendor, provenance, per-job results (borrow carries a
  second one, `_run_history_v2.json`, so the v2 backfill can't trip post.py's gate). `m1/_manifest.json`
  is the equivalent for the built tables; `data_quality/` has no `_run.json` — its artefacts *are* the
  report.
- **`download.sh` mirrors the whole volume** except `code/`. That is thousands of small files
  (`eod_bulk`) plus ~12 GiB of Parquet (`tickdata`) — name keys to fetch a subset instead.

Regenerate this tree from a fresh listing:

```sh
scripts/storage_usage.sh > arrange.txt
# the 680 unique directories, one per line
awk '/^[0-9]{4}-/ && NF==5 {print $5}' arrange.txt | awk -F/ -v OFS=/ '{NF--; print}' | sort -u
```
