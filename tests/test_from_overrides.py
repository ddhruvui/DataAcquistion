"""Per-ticker `from_overrides` must widen a window and must never narrow one.

Regression for 2026-09-16: the 11 data-only watchlist names (AAL, AAOI, ARM, ASML, ASTS, BE,
MSTR, RKLB, SNOW, SOFI, TSM) were promoted out of config/watchlist_*.json — acquired there at
from=1990 — into tickers.json and tiingo.json, which run from=2000. eod/dividends/splits (EODHD)
and prices (Tiingo) are FULL refetches whose result REPLACES the file, so without an override the
first nightly run after promotion would have rewritten those files to the 2000 window: 2,170 price
rows (ASML 1995, TSM 1997, MSTR 1998) and the pre-2000 ASML/TSM splits that anchor those two
names' entire Q-002 adjustment chain. Tiingo would have deferred the same loss to whenever
skip_fresh_days next expired, which is worse — it looks fine for a month.
"""
import importlib.util
import json
import os
import sys

import pytest

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")
PROMOTED = ["AAL", "AAOI", "ARM", "ASML", "ASTS", "BE", "MSTR", "RKLB", "SNOW", "SOFI", "TSM"]
# The three that actually carry pre-2000 history, and what is at stake for each.
PRE_2000 = {"ASML": "1995-03-15", "TSM": "1997-10-08", "MSTR": "1998-06-11"}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "src", path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


eodhd = _load("fetch_under_test", "fetch.py")
tiingo = _load("fetch_tiingo_under_test", "fetch_tiingo.py")


def _cfg(name):
    with open(os.path.join(ROOT, "config", name)) as f:
        return json.load(f)


# --- the helper's contract -------------------------------------------------------------------

@pytest.mark.parametrize("ticker,expected", [
    ("ASML", "1990-01-01"),      # override is earlier than `from` -> it wins
    ("AAPL", "2000-01-01"),      # no override -> config window
])
def test_override_widens_only_for_named_tickers(ticker, expected):
    cfg = {"from": "2000-01-01", "from_overrides": {"ASML": "1990-01-01"}}
    assert eodhd._window_for(cfg, ticker, "from") == expected
    assert tiingo._window_for(cfg, ticker) == expected


def test_a_later_override_is_ignored_and_can_never_truncate():
    """The whole point: an override must not be able to narrow a window."""
    cfg = {"from": "2000-01-01", "from_overrides": {"NARROW": "2015-01-01"}}
    assert eodhd._window_for(cfg, "NARROW", "from") == "2000-01-01"
    assert tiingo._window_for(cfg, "NARROW") == "2000-01-01"


def test_news_window_is_untouched_by_overrides():
    """news is append-only and widening it triggers a paid backfill — overrides stay off it."""
    cfg = {"from": "2000-01-01", "news_from": "2020-12-01",
           "from_overrides": {"ASML": "1990-01-01"}}
    assert eodhd._window_for(cfg, "ASML", "news_from") == "2020-12-01"


def test_missing_or_empty_override_block_is_a_no_op():
    for cfg in ({"from": "2000-01-01"}, {"from": "2000-01-01", "from_overrides": None},
                {"from": "2000-01-01", "from_overrides": {}}):
        assert eodhd._window_for(cfg, "ASML", "from") == "2000-01-01"
        assert tiingo._window_for(cfg, "ASML") == "2000-01-01"


# --- the live configs ------------------------------------------------------------------------

@pytest.mark.parametrize("cfg_name,window_fn", [
    ("tickers.json", lambda c, t: eodhd._window_for(c, t, "from")),
    ("tiingo.json", lambda c, t: tiingo._window_for(c, t)),
])
def test_promoted_names_keep_their_pre_2000_history(cfg_name, window_fn):
    """Every promoted name is in the universe, and none of the three deep ones gets truncated."""
    cfg = _cfg(cfg_name)
    for t in PROMOTED:
        assert t in cfg["stocks"], f"{t} missing from {cfg_name} stocks"
    for t, first_row in PRE_2000.items():
        assert window_fn(cfg, t) <= first_row, (
            f"{cfg_name}: {t}'s window would truncate its history at {first_row}")


def test_promoted_names_left_the_watchlist_configs():
    """A name in both universes is fetched — and for the metered vendors billed — twice."""
    for name in ("watchlist_eodhd.json", "watchlist_sharadar.json", "watchlist_tiingo.json"):
        assert not set(_cfg(name)["stocks"]) & set(PROMOTED), f"{name} still holds promoted names"
    borrow = _cfg("watchlist_borrow.json")["iborrowdesk"]["stocks"]
    assert not set(borrow) & set(PROMOTED), "watchlist_borrow.json still holds promoted names"


def test_promoted_names_are_in_every_main_vendor_universe():
    for name in ("tickers.json", "sharadar.json", "tiingo.json"):
        assert set(PROMOTED) <= set(_cfg(name)["stocks"]), f"{name} is missing promoted names"
