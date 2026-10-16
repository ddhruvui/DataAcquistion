#!/usr/bin/env python3
"""OHLCV — daily prices as their own nightly job (`scripts/launch.sh ohlcv`), split out of EODHD.

WHAT it writes (config/ohlcv.json; the data-only watchlist's prices from config/watchlist_ohlcv.json
into data/watchlist/ first, like every vendor pod):
    data/ohlcv/<T>.json          D-01 per-ticker eod for the tickers.json universe
    data/market/SPY.US.json      D-09 SPY daily
    data/eod_bulk/US/<DATE>.json D-01 whole-exchange day-files, delisted names included
    data/_run_ohlcv.json         this job's manifest — post.py waits on it
Those are the paths the one-pod EODHD pull used, so validate.py, build_m1.py, fetch_intraday.py and
the models repo read them unchanged. Everything else EODHD provides (dividends, splits, fundamentals,
estimates, news, calendars...) is the `eodhd` job: src/fetch.py + config/tickers.json, manifest
data/_run.json.

HOW: every byte still comes from EODHD, so this runs fetch.py — same code, OHLCV config, its own
manifest name (RUN_TAG) — rather than carrying a copy. launch.sh uploads fetch.py next to it.

WHY a separate job: so prices can change vendor, plan or token without touching the rest.
EODHD_OHLCV_API_TOKEN in runpod/.env already runs it on a different EODHD key than the eodhd job.
To move prices off EODHD entirely, replace what this file runs; downstream depends only on the
paths above plus a manifest carrying `ended_at` and per-job `results` (post.py, verify_fetch.py).

    DATA_DIR=./data CONFIG_PATH=config/ohlcv.json EODHD_API_TOKEN=xxx python3 src/fetch_ohlcv.py
"""
import os
import runpy

HERE = os.path.dirname(os.path.abspath(__file__))

# Forced, not defaulted: without the tag, fetch.py would write _run.json and erase the eodhd job's
# manifest, which post.py would then read as the eodhd job having finished.
os.environ["RUN_TAG"] = "ohlcv"
os.environ.setdefault("CONFIG_PATH", "/workspace/code/ohlcv.json")
runpy.run_path(os.path.join(HERE, "fetch.py"), run_name="__main__")
