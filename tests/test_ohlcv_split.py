"""The EODHD pull as two jobs (split 2026-09-29): `eodhd` = src/fetch.py + config/tickers.json (everything
but prices) and `ohlcv` = src/fetch_ohlcv.py + config/ohlcv.json (per-ticker eod, SPY, bulk day-files).

Synthetic — no network, no volume, no credits: EODHD is faked at urllib's urlopen, so fetch.py's own
request, retry and parsing code still runs. What these pin down:
  - the live configs PARTITION the one-pod pull: nothing dropped, nothing fetched twice, the same
    universe and per-ticker overrides on both halves (watchlists too)
  - each job writes its own manifest into the shared tree and only its own subtrees, so neither
    erases the other's results and post.py can tell them apart
  - fetch_ohlcv.py really is fetch.py plus the tag, run end to end the way the pod runs it
  - the bulk completeness wait: re-pull the newest session until every name that printed per-ticker
    is in it, ignore names a settled file never carries, give up at the deadline keeping the fullest
    pull, and never wait on an older session or when unconfigured
"""
import importlib.util
import json
import os
import runpy
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

import pytest

from fake_eodhd import FakeEODHD, sessions_before_today

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SRC = os.path.join(ROOT, "src")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, os.path.join(SRC, path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _live(name):
    with open(os.path.join(ROOT, "config", name)) as f:
        return json.load(f)


# --- the live configs ---------------------------------------------------------------------------

LEVEL_KEYS = ("market", "market_dividends", "index_constituents", "exchanges", "symbol_lists",
              "earnings_upcoming", "eod_bulk", "eod_bulk_actions")


def _work(cfg):
    """Every unit of EODHD work a config asks for: per-ticker datasets + each market/exchange item."""
    out = {("dataset", d) for d in cfg.get("datasets") or []}
    for key in LEVEL_KEYS:
        val = cfg.get(key)
        if isinstance(val, dict):
            val = [key] if val.get("enabled") else []
        elif not isinstance(val, list):
            val = [key] if val else []
        out |= {(key, v) for v in val}
    return out


PRE_SPLIT = {
    ("dataset", "eod"), ("dataset", "dividends"), ("dataset", "splits"), ("dataset", "fundamentals"),
    ("dataset", "estimates"), ("dataset", "news"), ("market", "SPY.US"),
    ("market_dividends", "SPY.US"), ("index_constituents", "GSPC.INDX"), ("exchanges", "US"),
    ("symbol_lists", "US"), ("earnings_upcoming", "earnings_upcoming"), ("eod_bulk", "eod_bulk"),
    ("eod_bulk_actions", "eod_bulk_actions")}
PRICES = {("dataset", "eod"), ("market", "SPY.US"), ("eod_bulk", "eod_bulk")}


def test_the_two_jobs_partition_the_one_pod_pull():
    eodhd, ohlcv = _work(_live("tickers.json")), _work(_live("ohlcv.json"))
    assert not eodhd & ohlcv, f"fetched twice: {sorted(eodhd & ohlcv)}"
    assert eodhd | ohlcv == PRE_SPLIT, f"lost: {sorted(PRE_SPLIT - (eodhd | ohlcv))}"
    assert ohlcv == PRICES, "ohlcv should hold exactly the prices"


def test_the_watchlist_halves_partition_the_same_way():
    eodhd, ohlcv = _work(_live("watchlist_eodhd.json")), _work(_live("watchlist_ohlcv.json"))
    assert not eodhd & ohlcv
    assert ohlcv == {("dataset", "eod"), ("market", "SPY.US"), ("market", "QQQ.US")}
    assert ("market_dividends", "SPY.US") in eodhd and ("market_dividends", "QQQ.US") in eodhd


@pytest.mark.parametrize("price_cfg,universe_cfg", [("ohlcv.json", "tickers.json"),
                                                    ("watchlist_ohlcv.json", "watchlist_eodhd.json")])
def test_prices_cover_exactly_the_universe_of_the_rest(price_cfg, universe_cfg):
    """One list per universe: the price half resolves it, it does not carry a copy that can drift."""
    fetch = _load("fetch_live_cfg", "fetch.py")
    raw = _live(price_cfg)
    assert "stocks" not in raw and raw["stocks_from"] == universe_cfg
    resolved = fetch._resolve_stocks_from(raw, os.path.join(ROOT, "config", price_cfg))
    base = _live(universe_cfg)
    assert resolved["stocks"] == base["stocks"]
    assert resolved.get("from_overrides") == base.get("from_overrides")
    assert resolved["from"] == base["from"], "prices and corporate actions should share a window"


def test_ohlcv_waits_for_the_day_file_and_protects_the_concurrent_eodhd_pass():
    bulk = _live("ohlcv.json")["eod_bulk"]
    assert bulk["complete_wait_minutes"] >= bulk["complete_poll_minutes"] > 0
    assert bulk["reserve_credits"] >= 15000, "the concurrent eodhd pass spends ~14k a night"


def test_post_waits_for_both_eodhd_manifests(tmp_path, monkeypatch):
    post = _load("post_under_test", "post.py")
    assert "data" in post.WAIT_FOR and "data/_run_ohlcv.json" in post.WAIT_FOR
    monkeypatch.setattr(post, "VOLUME", str(tmp_path))
    floor = datetime.now(timezone.utc) - timedelta(minutes=5)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "_run.json").write_text(json.dumps({"ended_at": datetime.now(timezone.utc).isoformat()}))
    assert post._fresh("data", floor, None)
    assert not post._fresh("data/_run_ohlcv.json", floor, None), "eodhd finishing must not stand in for ohlcv"
    (tmp_path / "data" / "_run_ohlcv.json").write_text(json.dumps({"ended_at": datetime.now(timezone.utc).isoformat()}))
    assert post._fresh("data/_run_ohlcv.json", floor, None)


UNIVERSE = ["AAA", "BBB", "GONE", "GHOST"]


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A code dir holding both configs, a volume, an XNYS session file and a sleep that only counts.
    GONE stopped printing two sessions ago (delisted); GHOST prints per-ticker but no day-file has
    ever carried it — the vendor-code mismatch the settled-file filter exists for."""
    sessions = sessions_before_today()
    code, vol = tmp_path / "code", tmp_path / "data"
    code.mkdir()
    vol.mkdir()
    (tmp_path / "XNYS.json").write_text(json.dumps(
        {"calendar": "XNYS", "package_version": "test", "sessions": sessions, "early_closes": []}))
    (code / "tickers.json").write_text(json.dumps({
        "exchange": "US", "from": sessions[0], "news_from": sessions[0], "market_from": sessions[0],
        "from_overrides": {"AAA": "1990-01-01"}, "incremental": True,
        "datasets": ["dividends", "splits", "fundamentals", "estimates", "news"],
        "stocks": UNIVERSE, "workers": 4, "max_requests_per_minute": 0,
        "market_dividends": ["SPY.US"], "index_constituents": ["GSPC.INDX"],
        "exchanges": ["US"], "symbol_lists": ["US"], "earnings_upcoming": True,
        "eod_bulk_actions": {"enabled": True, "exchange": "US", "from": sessions[-1], "max_days_per_run": 5}}))
    ohlcv = {"exchange": "US", "stocks_from": "tickers.json", "from": sessions[0], "market_from": sessions[0],
             "min_credits": 3000, "workers": 4, "max_requests_per_minute": 0,
             "datasets": ["eod"], "market": ["SPY.US"],
             "eod_bulk": {"enabled": True, "exchange": "US", "from": sessions[0], "max_days_per_run": 50,
                          "complete_wait_minutes": 30, "complete_poll_minutes": 10}}
    (code / "ohlcv.json").write_text(json.dumps(ohlcv))

    # One settled day-file from before the window (written a week after its session), so the wait
    # knows which names a finished file carries: the real ones, not GHOST.
    bulk_dir = vol / "eod_bulk" / "US"
    bulk_dir.mkdir(parents=True)
    old = date.fromisoformat(sessions[0]) - timedelta(days=14)
    rows = [dict(code=c, date=old.isoformat(), close=1) for c in ["AAA", "BBB", "GONE"] + [f"F{i:05d}" for i in range(1200)]]
    settled = bulk_dir / f"{old.isoformat()}.json"
    settled.write_text(json.dumps(rows))
    ts = datetime(old.year, old.month, old.day, tzinfo=timezone.utc).timestamp() + 7 * 86400
    os.utime(settled, (ts, ts))

    printed = {"AAA": sessions, "BBB": sessions, "GHOST": sessions, "GONE": sessions[:-2], "SPY": sessions}
    sleeps = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))

    class W:
        pass
    w = W()
    w.sessions, w.code, w.vol, w.bulk_dir, w.printed, w.sleeps = sessions, code, vol, bulk_dir, printed, sleeps
    w.newest = sessions[-1]

    def fake(**kw):
        f = FakeEODHD(sessions, dict(printed), never_in_bulk={"GHOST"}, **kw)
        monkeypatch.setattr(urllib.request, "urlopen", f)
        return f
    w.fake = fake

    def run(config, tag="", **cfg_edits):
        """fetch.py's main() in-process on a fresh module, as the pod would run it for `config`."""
        if cfg_edits:
            path = code / config
            c = json.loads(path.read_text())
            for k, v in cfg_edits.items():
                c[k] = v
            path.write_text(json.dumps(c))
        mod = _load(f"fetch_{tag or 'eodhd'}_under_test", "fetch.py")
        for k, v in dict(DATA_DIR=str(vol), CONFIG_PATH=str(code / config), TOKEN="t", RUN_TAG=tag,
                         SESSIONS_PATH=str(tmp_path / "XNYS.json"), RETRY_SWEEP_DELAY=0).items():
            monkeypatch.setattr(mod, k, v)
        rc = mod.main()
        return rc, mod
    w.run = run
    return w


def _manifest(vol, name):
    return json.loads((vol / name).read_text())


def _bulk_entry(manifest):
    return next(r for r in manifest["results"] if r["dataset"] == "eod_bulk")


# --- each job writes only its own half ---------------------------------------------------------

def test_each_job_keeps_its_own_manifest_and_subtrees(world):
    world.fake()
    rc, _ = world.run("tickers.json")
    assert rc == 0
    assert (world.vol / "_run.json").exists() and not (world.vol / "_run_ohlcv.json").exists()
    for price_tree in ("ohlcv", "market/SPY.US.json"):
        assert not (world.vol / price_tree).exists(), f"eodhd wrote {price_tree}"
    assert sorted(p.name for p in world.bulk_dir.iterdir()) == [f"{date.fromisoformat(world.sessions[0]) - timedelta(days=14)}.json"]
    eodhd = _manifest(world.vol, "_run.json")
    assert eodhd["job"] == "eodhd" and eodhd["ok"]
    assert {r["dataset"] for r in eodhd["results"]} >= {"fundamentals", "news", "splits", "dividends"}
    assert "eod" not in {r["dataset"] for r in eodhd["results"]}

    world.fake()
    rc, _ = world.run("ohlcv.json", tag="ohlcv")
    assert rc == 0
    ohlcv = _manifest(world.vol, "_run_ohlcv.json")
    assert ohlcv["job"] == "ohlcv" and ohlcv["ok"]
    assert {r["dataset"] for r in ohlcv["results"]} == {"eod", "market", "eod_bulk"}
    assert _manifest(world.vol, "_run.json") == eodhd, "the ohlcv run must not touch the eodhd manifest"
    for t in UNIVERSE:
        assert (world.vol / "ohlcv" / f"{t}.json").exists()
    assert (world.vol / "market" / "SPY.US.json").exists()
    assert all((world.bulk_dir / f"{d}.json").exists() for d in world.sessions)


def test_stocks_from_brings_the_overrides_and_a_local_key_wins(world, tmp_path):
    fetch = _load("fetch_stocks_from", "fetch.py")
    cfg = fetch._resolve_stocks_from({"stocks_from": "tickers.json"}, str(world.code / "ohlcv.json"))
    assert cfg["stocks"] == UNIVERSE and cfg["from_overrides"] == {"AAA": "1990-01-01"}
    assert fetch._window_for(cfg, "AAA", "from") == "1990-01-01", "the override must reach the eod window"
    own = fetch._resolve_stocks_from({"stocks_from": "tickers.json", "stocks": ["ZZZ"]},
                                     str(world.code / "ohlcv.json"))
    assert own["stocks"] == ["ZZZ"]


def test_an_unreadable_stocks_from_fails_the_run_instead_of_fetching_nothing(world):
    world.fake()
    rc, _ = world.run("ohlcv.json", tag="ohlcv", stocks_from="missing.json")
    assert rc == 1
    assert not (world.vol / "_run_ohlcv.json").exists()


def test_fetch_ohlcv_py_is_fetch_py_with_the_tag(world, monkeypatch):
    """Run the real entry point the way the pod does — a fresh fetch.py via runpy, config from env."""
    world.fake()
    for k, v in dict(DATA_DIR=str(world.vol), CONFIG_PATH=str(world.code / "ohlcv.json"), EODHD_API_TOKEN="t",
                     SESSIONS_PATH=str(world.vol.parent / "XNYS.json"), RETRY_SWEEP_DELAY="0",
                     STORE_LOGS="true").items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("RUN_TAG", raising=False)
    try:
        with pytest.raises(SystemExit) as done:
            runpy.run_path(os.path.join(SRC, "fetch_ohlcv.py"), run_name="__main__")
        assert done.value.code == 0
    finally:
        os.environ.pop("RUN_TAG", None)          # the wrapper sets it on the process, by design
    assert _manifest(world.vol, "_run_ohlcv.json")["job"] == "ohlcv"
    assert not (world.vol / "_run.json").exists()
    logs = sorted(p.name for p in (world.vol / "logs").iterdir())
    assert logs and all(n.startswith("ohlcv-run-") for n in logs), logs


# --- the completeness wait ---------------------------------------------------------------------

def test_waits_until_every_printer_is_in_the_newest_day_file(world):
    full = {"AAA", "BBB", "GONE"}
    f = world.fake(bulk_plan={world.sessions[-1]: [({"AAA"}, 1200), ({"AAA"}, 1300), ({"AAA", "BBB"}, 1400)]})
    rc, _ = world.run("ohlcv.json", tag="ohlcv")
    assert rc == 0
    assert f.bulk_calls(world.newest) == 3 and world.sleeps == [600, 600]
    stored = {r["code"] for r in json.loads((world.bulk_dir / f"{world.newest}.json").read_text())}
    assert {"AAA", "BBB"} <= stored
    info = _bulk_entry(_manifest(world.vol, "_run_ohlcv.json"))["newest_session"]
    assert info == {"session": world.newest, "rows": 1402, "expected": 2, "missing": [],
                    "waited_min": 20.0, "pulls": 3}
    # GONE is delisted (no bar that session) and GHOST no settled file carries: neither is waited on.
    assert "GONE" not in stored and full - stored == {"GONE"}
    # Only the newest session waits; the rest of the walk is one pull each, as before.
    assert all(f.bulk_calls(d) == 1 for d in world.sessions[:-1])


def test_gives_up_at_the_deadline_and_keeps_the_fullest_pull(world):
    f = world.fake(bulk_plan={world.sessions[-1]: [({"AAA"}, 1300), ({"AAA"}, 900)]})
    rc, _ = world.run("ohlcv.json", tag="ohlcv")
    assert rc == 0, "a short file at the deadline is stored as before, not a failed run"
    assert f.bulk_calls(world.newest) == 4 and world.sleeps == [600, 600, 600]   # 30 min / 10 min polls
    stored = json.loads((world.bulk_dir / f"{world.newest}.json").read_text())
    assert len(stored) == 1301, "a thinner re-pull must never replace a fuller one"
    info = _bulk_entry(_manifest(world.vol, "_run_ohlcv.json"))["newest_session"]
    assert info["missing"] == ["BBB"] and info["waited_min"] == 30.0 and info["pulls"] == 4


def test_a_complete_file_costs_no_wait(world):
    f = world.fake()
    rc, _ = world.run("ohlcv.json", tag="ohlcv")
    assert rc == 0 and world.sleeps == [] and f.bulk_calls(world.newest) == 1
    info = _bulk_entry(_manifest(world.vol, "_run_ohlcv.json"))["newest_session"]
    assert info["missing"] == [] and info["pulls"] == 1 and info["expected"] == 2


def test_without_a_settled_file_every_printer_is_expected(world):
    """Cold volume: nothing says GHOST never appears, so it is waited for — bounded by the deadline."""
    for p in world.bulk_dir.iterdir():
        p.unlink()
    f = world.fake()
    rc, _ = world.run("ohlcv.json", tag="ohlcv")
    assert rc == 0 and f.bulk_calls(world.newest) == 4
    info = _bulk_entry(_manifest(world.vol, "_run_ohlcv.json"))["newest_session"]
    assert info["missing"] == ["GHOST"] and info["expected"] == 3


def test_no_wait_unless_configured(world):
    """The one-pod shape (tickers-test.json) and anything else without the key behaves as before."""
    f = world.fake(bulk_plan={world.sessions[-1]: [({"AAA"}, 1200)]})
    bulk = json.loads((world.code / "ohlcv.json").read_text())["eod_bulk"]
    del bulk["complete_wait_minutes"]
    rc, _ = world.run("ohlcv.json", tag="ohlcv", eod_bulk=bulk)
    assert rc == 0 and world.sleeps == [] and f.bulk_calls(world.newest) == 1
    assert "newest_session" not in _bulk_entry(_manifest(world.vol, "_run_ohlcv.json"))


def test_the_launcher_knows_both_jobs():
    """`all` and a bare launch carry both halves; the new job ships the code it runs."""
    src = open(os.path.join(ROOT, "scripts", "launch.sh")).read()
    assert 'all)            VENDORS="eodhd ohlcv ' in src
    assert '"")             VENDORS="eodhd ohlcv"' in src
    block = src[src.index("    ohlcv)\n"):]
    block = block[:block.index(";;") + 2]
    for needle in ('FETCH_SCRIPT="fetch_ohlcv.py"', 'CONFIG_FILE="ohlcv.json"', 'WATCH_FILE="watchlist_ohlcv.json"',
                   'EXTRA_SRC="fetch.py"', 'EXTRA_CONFIGS="tickers.json watchlist_eodhd.json"',
                   "EODHD_OHLCV_API_TOKEN"):
        assert needle in block, needle
    out = subprocess.run(["bash", "-n", os.path.join(ROOT, "scripts", "launch.sh")], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
