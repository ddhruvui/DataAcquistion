"""fetch.py's per-ticker pass on worker threads. Synthetic — the fake EODHD sits at urlopen.

Why each test exists:
  - threads must change nothing but speed: same files, same manifest, same order as sequential
  - fundamentals + estimates share a 10-credit object through a cache; a process-wide one-entry
    cache would be evicted by other workers between the two calls and re-bill it for most tickers
  - news merges hold whole stored files in memory (NVDA's is 560 MB); the byte budget bounds that
  - EODHD allows 1,000 requests/min per ACCOUNT across all pods; the pacer bounds each process, and
    a 429 backs off for as long as the vendor's Retry-After says
  - log lines come from many threads and must stay whole (reap_pods.sh and podlog parse them)
"""
import importlib.util
import io
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

from fake_eodhd import FakeEODHD, sessions_before_today

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "fetch.py")
TICKERS = [f"T{i:02d}" for i in range(24)]


def _fresh_fetch():
    spec = importlib.util.spec_from_file_location("fetch_parallel_under_test", SRC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Both EODHD configs over 24 tickers, a session file, and a sleep that only records."""
    sessions = sessions_before_today()
    code = tmp_path / "code"
    code.mkdir()
    (tmp_path / "XNYS.json").write_text(json.dumps(
        {"calendar": "XNYS", "package_version": "t", "sessions": sessions, "early_closes": []}))
    base = {"exchange": "US", "from": sessions[0], "news_from": sessions[0], "market_from": sessions[0],
            "incremental": True, "workers": 8, "max_requests_per_minute": 0}
    (code / "tickers.json").write_text(json.dumps(dict(
        base, stocks=TICKERS, datasets=["dividends", "splits", "fundamentals", "estimates", "news"],
        market_dividends=["SPY.US"], index_constituents=["GSPC.INDX"], exchanges=["US"],
        symbol_lists=["US"], earnings_upcoming=True)))
    (code / "ohlcv.json").write_text(json.dumps(dict(
        base, stocks_from="tickers.json", datasets=["eod"], market=["SPY.US"],
        eod_bulk={"enabled": True, "exchange": "US", "from": sessions[0], "max_days_per_run": 50})))
    sleeps = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    printed = {t: sessions for t in TICKERS + ["SPY"]}

    class E:
        pass
    e = E()
    e.tmp, e.code, e.sessions, e.sleeps = tmp_path, code, sessions, sleeps

    def fake(**kw):
        f = FakeEODHD(sessions, dict(printed), **kw)
        monkeypatch.setattr(urllib.request, "urlopen", f)
        return f
    e.fake = fake

    def run(config, vol, tag="", workers=None, **cfg_edits):
        if cfg_edits:
            c = json.loads((code / config).read_text())
            c.update(cfg_edits)
            (code / config).write_text(json.dumps(c))
        if workers is not None:
            monkeypatch.setenv("WORKERS", str(workers))
        mod = _fresh_fetch()
        for k, v in dict(DATA_DIR=str(vol), CONFIG_PATH=str(code / config), TOKEN="t", RUN_TAG=tag,
                         SESSIONS_PATH=str(tmp_path / "XNYS.json"), RETRY_SWEEP_DELAY=0).items():
            monkeypatch.setattr(mod, k, v)
        rc = mod.main()
        monkeypatch.delenv("WORKERS", raising=False)
        return rc, mod
    e.run = run
    return e


def _tree(vol):
    """Every file under vol except the parts that legitimately differ run to run (timestamps)."""
    out = {}
    for root, _, files in os.walk(vol):
        for n in files:
            rel = os.path.relpath(os.path.join(root, n), vol)
            if rel.startswith(("logs", "earnings")) or n.startswith("_run"):
                continue
            with open(os.path.join(root, n)) as f:
                out[rel] = f.read()
    return out


def test_threads_change_nothing_but_speed(env):
    seq, par = env.tmp / "seq", env.tmp / "par"
    for vol, workers in ((seq, 1), (par, 8)):
        for config, tag in (("tickers.json", ""), ("ohlcv.json", "ohlcv")):
            env.fake()
            rc, mod = env.run(config, vol, tag=tag, workers=workers)
            assert rc == 0
            said = [ln for ln in mod._LOG_LINES if "per-ticker pass" in ln]
            assert bool(said) == (workers > 1), said
    assert _tree(seq) == _tree(par)
    for manifest in ("_run.json", "_run_ohlcv.json"):
        a = json.loads((seq / manifest).read_text())["results"]
        b = json.loads((par / manifest).read_text())["results"]
        assert a == b, f"{manifest}: results differ in content or order"


def test_fundamentals_are_billed_once_per_ticker_when_workers_interleave(env):
    """Hold the first two fundamentals requests until both are in flight, so two workers have each
    cached a different ticker before either reaches estimates. A shared one-entry cache would lose
    one of them and re-bill it."""
    gate = threading.Barrier(2, timeout=10)
    seen, lock = [0], threading.Lock()

    def hold_first_two(path, q):
        if path.startswith("v1.1/fundamentals/"):
            with lock:
                seen[0] += 1
                first_two = seen[0] <= 2
            if first_two:
                gate.wait()
    f = env.fake(on_request=hold_first_two)
    rc, _ = env.run("tickers.json", env.tmp / "vol")
    assert rc == 0
    assert f.count("v1.1/fundamentals/") == len(TICKERS)
    est = json.loads((env.tmp / "vol" / "estimates" / "T00.json").read_text())
    assert est[-1]["trend"] == {"Quarterly": {}}, "estimates must come from that ticker's own object"


def test_news_merges_respect_the_memory_budget(env):
    """With a budget smaller than any stored file, stored news files are held one at a time — and
    with the default budget they are not, which shows the test can see the difference."""
    vol = env.tmp / "vol"
    env.fake()
    assert env.run("tickers.json", vol)[0] == 0            # first run: creates the news files

    def watch(budget_mb):
        live, peak, lock = [0], [0], threading.Lock()

        def in_news(path, q):
            if path == "news":
                with lock:
                    live[0] += 1
                    peak[0] = max(peak[0], live[0])
                threading.Event().wait(0.02)            # real time: time.sleep is only recorded
                with lock:
                    live[0] -= 1
        env.fake(on_request=in_news)
        assert env.run("tickers.json", vol, merge_budget_mb=budget_mb)[0] == 0
        return peak[0]
    assert watch(0.000001) == 1
    assert watch(600) > 1


def test_byte_budget_admits_by_size_and_an_oversize_file_alone():
    fetch = _fresh_fetch()
    budget = fetch._ByteBudget(600)
    live, peak, alone, lock = [0], [0], [], threading.Lock()

    def job(size):
        held = budget.acquire(size)
        with lock:
            live[0] += held
            peak[0] = max(peak[0], live[0])
            if size > 600:
                alone.append(live[0] == held)
        threading.Event().wait(0.03)
        with lock:
            live[0] -= held
        budget.release(held)
    threads = [threading.Thread(target=job, args=(s,)) for s in (300, 300, 300, 50, 50, 900, 200, 10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert peak[0] <= 600 and alone == [True] and budget.used == 0


def test_pacer_spaces_request_starts_across_threads():
    fetch = _fresh_fetch()
    pacer = fetch._Pacer(1200)                  # one start every 0.05 s
    starts, lock = [], threading.Lock()

    def worker():
        for _ in range(3):
            pacer.wait()
            with lock:
                starts.append(time.monotonic())
    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    starts.sort()
    # The pacer spaces the SCHEDULED starts exactly; when each thread actually wakes jitters by a few
    # ms, so a single gap can read 43 ms. What it must guarantee is the rate: 12 starts can never
    # fit in less than 11 gaps (0.55 s), whatever the wake-up jitter. Unpaced they take ~0 s.
    span = starts[-1] - starts[0]
    assert len(starts) == 12 and span >= 11 * 0.05 - 0.02, (span, starts)
    assert fetch._Pacer(0).gap == 0 and fetch._Pacer(None).gap == 0


def test_429_waits_as_long_as_retry_after_says(monkeypatch):
    fetch = _fresh_fetch()
    calls, sleeps = [], []

    class Ok:
        status = 200

        def read(self):
            return b"[]"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(url, timeout=None, context=None):
        calls.append(url)
        if len(calls) == 1:
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", {"Retry-After": "7"}, None)
        return Ok()
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    assert fetch.get_json("https://eodhd.com/api/x") == (200, [])
    assert sleeps == [7.0]


class _YieldingOut(io.StringIO):
    """stdout that hands the GIL to another thread after every write — the moment a line printed in
    two writes (message, then newline) gets split by another thread's line."""

    def write(self, s):
        n = super().write(s)
        time.sleep(0)
        return n


def test_log_lines_stay_whole_across_threads(monkeypatch):
    fetch = _fresh_fetch()
    out = _YieldingOut()
    monkeypatch.setattr(sys, "stdout", out)

    def worker(i):
        for j in range(300):
            fetch.log(f"OK   eod                W{i}-{j:03d}.US: 6 (+6) [full] -> /x/{'y' * 40}")
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    lines = out.getvalue().splitlines()
    pat = re.compile(r"^OK   eod {16}W\d-\d{3}\.US: 6 \(\+6\) \[full\] -> /x/y{40}$")
    assert len(lines) == 2400 and all(pat.match(ln) for ln in lines)
    assert len(fetch._LOG_LINES) == 2400
