#!/usr/bin/env python3
"""Download every EODHD dataset the v1.2 spec needs, for the universe in tickers.json.

Pure stdlib (no pip). Covers the ENTIRE EODHD column of
`../Data Acquisition Specification — FINAL v1.2.md` (the D-items EODHD is the source or
cross-check for). Non-EODHD vendors in the spec — Sharadar, the IBKR short-stock FTP file,
FinBERT weights, `exchange_calendars`, iBorrowDesk, Tiingo — are SEPARATE pullers (different
auth + rate models) and are intentionally NOT handled here.

TWO JOBS, ONE FETCHER (split 2026-09-29). A run pulls exactly the datasets its config names, and the
nightly EODHD pull is divided in two so that prices can change vendor, plan or token without touching
anything else:
    eodhd  src/fetch.py       + config/tickers.json   everything EXCEPT prices: dividends, splits,
                                                      fundamentals, estimates, news, SPY dividends,
                                                      index membership, calendars, symbol lists, the
                                                      earnings calendar, whole-market corporate actions
                                                      -> manifest DATA_DIR/_run.json
    ohlcv  src/fetch_ohlcv.py + config/ohlcv.json     prices: per-ticker eod, SPY, the bulk day-files
                                                      (it runs THIS file with RUN_TAG=ohlcv)
                                                      -> manifest DATA_DIR/_run_ohlcv.json
Both write the same DATA_DIR and the same subtrees as the one-pod pull did, so no reader changed. Only
the manifests differ, so neither run clobbers the other's and post.py waits for both.

PER-EQUITY (one file per ticker, driven by "stocks" + "datasets"; default datasets ["eod"]):
    eod          -> DATA_DIR/ohlcv/<TICKER>.json          D-01  date/open/high/low/close/
                                                          adjusted_close/volume (close is UNADJUSTED;
                                                          F_t = adjusted_close/close is the Q-002 factor)
    dividends    -> DATA_DIR/dividends/<TICKER>.json      D-03  ex-date cash dividends (value = per-share,
                                                          unadjusted where EODHD exposes unadjustedValue)
    splits       -> DATA_DIR/splits/<TICKER>.json         D-02  split ratios "A/B"
    fundamentals -> DATA_DIR/fundamentals/<TICKER>.json   D-05/06 cross-check + D-07 Earnings::History.
                                                          Full lossless EODHD object (Highlights, SharesStats,
                                                          Earnings.History/Trend, General.Sector, Financials)
    estimates    -> DATA_DIR/estimates/<TICKER>.json      D-14  Earnings::Trend analyst snapshots, APPEND-ONLY
                                                          (one dated row per pull — M1-01 immutable history)
    news         -> DATA_DIR/news/<TICKER>.json           D-08  timestamped, ticker-tagged articles (HEAVY;
                                                          own "news_from" window — depth is ~Dec-2020 onward)

MARKET / INDEX / EXCHANGE LEVEL (these need ".INDX"/exchange symbols the per-equity form can't express):
    market            -> DATA_DIR/market/<SYMBOL>.json            D-09  SPY.US daily level (index_prices, Q-019)
    market_dividends  -> DATA_DIR/market/dividends/<SYMBOL>.json  D-09  SPY dividends (total-return build)
    index_constituents-> DATA_DIR/universe/<INDEX>.json           D-15  survivorship-free S&P membership
                                                                 (Components + HistoricalTickerComponents)
    exchanges         -> DATA_DIR/calendar/<CODE>.json            D-11  EODHD exchange holidays (calendar CROSS-CHECK
                                                                 to the exchange_calendars source of truth)
    symbol_lists      -> DATA_DIR/symbols/<CODE>.json             D-13  full exchange symbol inventory incl. delisted
                                                                 (drives backfill completeness)
    earnings_upcoming -> DATA_DIR/earnings/upcoming.json          D-07  forward earnings calendar for the universe
                                                                 (Q-004 coverage check + F8 days_to_earnings)

BULK BACKFILL (D-01 PRIMARY — the spec's survivorship-bias-free price backfill; config block "eod_bulk"):
    eod_bulk          -> DATA_DIR/eod_bulk/<CODE>/YYYY-MM-DD.json  whole-exchange OHLCV per trading day, ALL tickers
                                                                 INCLUDING delisted. Resumes newest-first across runs
                                                                 (skips existing day-files), bounded by max_days_per_run
                                                                 to stay under the 100k-credit/day cap. With
                                                                 "complete_wait_minutes", the NEWEST session's file is
                                                                 re-pulled until EODHD has finished filling it (see
                                                                 _await_complete_day).

INCREMENTAL (default on; set "incremental": false in the config to force a full refetch). The
network volume persists DATA_DIR between RunPod launches, so the *append-only* streams — **news**
and **estimates** — read what's already on disk and only add new rows (news: rows dated on/after the
latest stored row, merged+deduped; estimates: one snapshot per pull day). Everything else refetches
in full: eod / dividends / splits / market are tiny AND EODHD rewrites `adjusted_close` retroactively
after a split/dividend so a naive append would go stale; fundamentals / universe / calendar / symbol
lists / earnings-calendar are point-in-time snapshots replaced whole.

PER-TICKER WINDOW (optional config block "from_overrides": {"<TICKER>": "YYYY-MM-DD"}). Because the
refetch-in-full streams REPLACE their file, a ticker whose history predates the config's `from`
is truncated to the window on its next run. An override gives those names a deeper `from` without
deepening the whole universe; it is applied only when EARLIER than the config's `from` (widening
only, so it can never truncate) and only to `from`-keyed datasets, never to `news_from`.

PARALLEL (config "workers", default 1). The per-ticker pass is latency-bound — sequentially ~3,100
requests one at a time, 85 of 87 min on 2026-09-29 — so tickers run on worker threads, each ticker's
datasets in order on one thread. "max_requests_per_minute" paces the process (EODHD: 1,000/min per
account, shared by every pod) and "merge_budget_mb" caps stored news held in memory at once. Env
WORKERS overrides the config for one run. Output and manifest are identical to the sequential pass.

SHARED UNIVERSE (optional config key "stocks_from": "<other config>.json", resolved next to this
config). Takes `stocks` and `from_overrides` from that file unless this one sets its own, so the two
EODHD jobs can never cover different names and a promoted name's override travels with it.

Self-termination is bootstrap.sh's job, so this runs/tests locally:

    DATA_DIR=./out CONFIG_PATH=config/tickers.json EODHD_API_TOKEN=xxx STORE_LOGS=true python src/fetch.py

Logging (env-controlled): the manifest (`_run.json`, or `_run_<RUN_TAG>.json`) is always written. A
full run log (`logs/run-<ts>.log`) is stored ONLY when `STORE_LOGS` is truthy. Failures
(`logs/error-<ts>.log`) and crashes (`logs/crash-<ts>.log`) are ALWAYS logged, regardless of
`STORE_LOGS`. A tagged run prefixes its log names with the tag (`logs/ohlcv-run-<ts>.log`).
Exit code: 0 if every job succeeded, 1 otherwise.
"""
import http.client
import json
import os
import ssl
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

TOKEN = os.environ.get("EODHD_API_TOKEN", "")
DATA_DIR = os.environ.get("DATA_DIR", "/workspace/data")
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/workspace/code/tickers.json")
API = "https://eodhd.com/api"
EOD_FIELDS = ("date", "open", "high", "low", "close", "adjusted_close", "volume")
BULK_FIELDS = ("code", "date", "open", "high", "low", "close", "adjusted_close", "volume")  # + ticker key
PAGE_SIZE = 1000   # EODHD list-endpoint max page
MAX_PAGES = 100    # hard backstop so offset pagination can never loop forever
# News is walked in descending date WINDOWS (see _fetch_news): each window is up to
# MAX_PAGES * PAGE_SIZE rows, and a truncated window re-enters with to=<oldest row seen>.
# 40 windows x 100k rows is far beyond any real ticker's multi-year archive.
MAX_NEWS_WINDOWS = int(os.environ.get("MAX_NEWS_WINDOWS", "40"))
CAL_CHUNK = 100    # symbols per calendar/earnings call — keeps the URL well under any length limit
# D-11 session list written by fetch_calendar.py onto the same volume. Optional: when present,
# eod_bulk probes ONLY real NYSE sessions. Without it we fall back to Mon-Fri, which is what put
# ten holiday day-files (Labor Day, Thanksgiving, …) full of OTC/foreign rows on the volume —
# EODHD answers on market holidays, so "the exchange was shut" is not something the price feed
# can tell you. Missing file => fall back silently; the volume is cold on the very first run.
SESSIONS_PATH = os.environ.get("SESSIONS_PATH", "/workspace/data_calendar/XNYS.json")
# eod-bulk-last-day publishes the whole session at once ~15 min after the 20:00Z close; asked for
# an in-progress date it returns 0 rows (verified live 2026-08-11 15:31Z). Belt and braces: never
# even ask for TODAY before this UTC hour, so a vendor that starts serving a partial file cannot
# get one frozen into the volume — the resume logic treats any existing day-file as final forever.
BULK_TODAY_CUTOFF_UTC_HOUR = int(os.environ.get("BULK_TODAY_CUTOFF_UTC_HOUR", "21"))
# A session whose file comes back with far fewer rows than a normal session is a partial
# publication, not a quiet day. Don't freeze it; leave it for the next run.
BULK_MIN_ROWS_FRAC = float(os.environ.get("BULK_MIN_ROWS_FRAC", "0.5"))
# Consecutive partial-rejections before the floor itself is treated as the suspect.
BULK_PARTIAL_PATIENCE = int(os.environ.get("BULK_PARTIAL_PATIENCE", "8"))
# SETTLING WINDOW. A bulk day-file is NOT final on the evening of its session — EODHD keeps
# revising it for days. Measured 2026-08-12 on the 2026-08-10 file: pulled ~19 h after the close it
# had 44,767 rows; 34 h later the same date returned 50,368 (+5,601 codes, 5,563 of them ordinary
# equity tickers) and 4,744 changed closes. For the 503-name universe the CLOSE damage is trivial
# (5 sub-penny roundings) but VOLUME was revised on 458 of 502 names — understated in 98% of cases,
# median 4.5%, p95 38%, max 67%, and 9% low in aggregate.
#
# That matters: volume drives F6 turnover and the blueprint's default
# universe.mode = top1000_dollar_volume, so freezing evening volumes biases the universe itself.
# A day-file is therefore re-pulled when it was WRITTEN within this many days of its own session
# date. Keying on (mtime - session_date) rather than (today - session_date) also self-heals files
# frozen by earlier runs, and costs nothing once a file has been fetched after it settled.
BULK_RESETTLE_DAYS = int(os.environ.get("BULK_RESETTLE_DAYS", "5"))
# Vendor 5xx blips run minutes, not the ~6s get_json's in-request retries cover — so first-pass
# failures get one more attempt at end of run, after this pause (seconds; env-overridable).
RETRY_SWEEP_DELAY = int(os.environ.get("RETRY_SWEEP_DELAY", "60"))
# Env-gated: when truthy, a run log is stored on success. Errors/crashes log regardless (see below).
STORE_LOGS = os.environ.get("STORE_LOGS", "").strip().lower() in ("1", "true", "yes", "on")
# Which of the two EODHD jobs this run is (see "TWO JOBS" above). Both write the same DATA_DIR, so a
# shared manifest would let whichever finished last erase the other's results — and post.py's gate
# would read one job finishing as both. Untagged (eodhd) keeps _run.json, the name post.py has always
# gated `data` on; src/fetch_ohlcv.py sets RUN_TAG=ohlcv -> _run_ohlcv.json and logs/ohlcv-*.log.
RUN_TAG = os.environ.get("RUN_TAG", "").strip()

_ctx = None  # default = verified TLS; falls back to unverified if CA bundle is missing
_LOG_LINES = []  # captured stdout, persisted to logs/ on success (if STORE_LOGS) or always on failure
_LOG_LOCK = threading.Lock()  # one whole line at a time — the per-ticker pass logs from worker threads
_TLS = threading.local()  # per-worker state: the fundamentals cache (see _fundamentals)
_EARLY_CLOSES = set()  # D-11 half-day sessions; filled by _load_sessions, read by the bulk floor
_EOD_TAIL = {}  # {"AAPL.US": {last 5 bar dates}} from THIS run's per-ticker eod — the bulk completeness check
_PACER = None  # _Pacer shared by every request this process makes; set in main()
_MERGE_BUDGET = None  # _ByteBudget for append-only files loaded at once; set in main()


def log(msg):
    """Print to stdout (pod container log) AND capture for the persisted log file."""
    with _LOG_LOCK:
        sys.stdout.write(msg + "\n")
        sys.stdout.flush()
        _LOG_LINES.append(msg)


def _manifest_path():
    """This run's manifest: _run.json, or _run_<RUN_TAG>.json for a tagged job (see RUN_TAG)."""
    return os.path.join(DATA_DIR, f"_run_{RUN_TAG}.json" if RUN_TAG else "_run.json")


def _persist_log(kind, manifest=None):
    """Write captured output (+ manifest) to logs/[<tag>-]<kind>-<UTCstamp>.log; return the path."""
    log_dir = os.path.join(DATA_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(log_dir, f"{RUN_TAG + '-' if RUN_TAG else ''}{kind}-{stamp}.log")
    with open(path, "w") as f:
        if _LOG_LINES:
            f.write("\n".join(_LOG_LINES) + "\n\n")
        if manifest is not None:
            f.write("--- manifest ---\n" + json.dumps(manifest, indent=2) + "\n")
    return path


class _Pacer:
    """Spaces the STARTS of this process's requests at least 60/rpm seconds apart, across threads.

    EODHD allows 1,000 requests/min per ACCOUNT, shared by the eodhd, ohlcv and intraday pods. With
    workers a burst of near-empty calls (dividends, splits: ~0.6 s each) could briefly run far above
    the steady rate; this bounds each process no matter what the mix is. rpm <= 0 disables it."""

    def __init__(self, rpm):
        self.rpm = float(rpm) if rpm and rpm > 0 else 0.0
        self.gap = 60.0 / self.rpm if self.rpm else 0.0
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self):
        if not self.gap:
            return
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + self.gap
        if start > now:
            time.sleep(start - now)


class _ByteBudget:
    """Caps how many bytes of existing files the workers hold in memory at once.

    The append-only streams (news above all) load the WHOLE stored file to merge a few new rows, and
    Python needs ~3x the file size while parsing it (measured on a 40 MB news file: 3.0x peak, 2.0x
    once loaded). NVDA's news file is 560 MB and GOOG/GOOGL or AMD/AMZN sit next to each other in the
    ticker order, so unbounded workers could hold several of the largest at once on a 4 GB pod. A
    file bigger than the whole budget is admitted alone rather than never."""

    def __init__(self, limit):
        self.limit, self.used = max(1, int(limit)), 0
        self._cond = threading.Condition()

    def acquire(self, n):
        n = max(0, min(int(n), self.limit))
        with self._cond:
            while self.used and self.used + n > self.limit:
                self._cond.wait()
            self.used += n
        return n

    def release(self, n):
        with self._cond:
            self.used -= n
            self._cond.notify_all()


def get_json(url):
    """GET url -> (http_status, parsed_json_or_None). Retries transient errors."""
    global _ctx
    attempt = 0
    while True:
        if _PACER is not None:
            _PACER.wait()
        try:
            with urllib.request.urlopen(url, timeout=60, context=_ctx) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                attempt += 1
                wait = 2 * attempt
                if e.code == 429:
                    # EODHD caps an account at 1,000 requests/min across ALL its pods and says how
                    # long to back off; honour that (capped) rather than retrying into the same wall.
                    try:
                        wait = max(wait, min(60.0, float(e.headers.get("Retry-After") or 0)))
                    except (TypeError, ValueError, AttributeError):
                        pass
                time.sleep(wait)
                continue
            return e.code, None
        except (ssl.SSLError, urllib.error.URLError, http.client.HTTPException,
                ConnectionError, TimeoutError) as e:
            # http.client.HTTPException covers RemoteDisconnected / IncompleteRead — common on long
            # news pagination where the server drops a connection mid-stream. Retry, don't fail the job.
            err = e if isinstance(e, ssl.SSLError) else getattr(e, "reason", e)
            # Downgrade ONLY on a certificate-VERIFICATION failure (an image with no CA bundle),
            # never on a transient ssl.SSLError such as SSLEOFError or "bad record mac" — those
            # are common on long news pagination, and treating them the same would turn one flaky
            # frame into an unverified channel carrying api_token for the rest of the run.
            # Logged via log() so the downgrade reaches the run log and the manifest.
            if _ctx is None and isinstance(err, ssl.SSLCertVerificationError):
                log("WARN: TLS certificate verification failed (no usable CA bundle) — "
                    "retrying without verification for the rest of this run")
                _ctx = ssl._create_unverified_context()
                continue  # one-time TLS downgrade — does not consume a retry attempt
            if attempt < 4:
                attempt += 1
                time.sleep(2 * attempt)
                continue
            raise


def _get(path, params):
    """GET {API}/{path}?params -> parsed JSON, raising on auth/transport failure."""
    url = f"{API}/{path}?{urllib.parse.urlencode(params)}"
    status, payload = get_json(url)
    if status == 401:
        raise RuntimeError("401 Unauthorized — EODHD plan inactive or token wrong")
    if status != 200 or payload is None:
        raise RuntimeError(f"unexpected response (status={status})")
    return payload


def _paginate(path, base_params, label=None):
    """Offset-paginate an EODHD list endpoint. Returns (rows, truncated).

    A multi-page pull (news can be 50+ pages / hundreds of MB) is the one place a run
    can sit silent for minutes, so when STORE_LOGS is on we emit a per-page heartbeat —
    it lands in the run log on success and in the error log if the pull dies mid-stream,
    pinpointing which page dropped.
    """
    rows = []
    offset = 0
    for page in range(1, MAX_PAGES + 1):
        params = dict(base_params, limit=PAGE_SIZE, offset=offset)
        payload = _get(path, params)
        if not isinstance(payload, list):
            raise RuntimeError(f"{path}: expected list, got {type(payload).__name__}")
        rows += payload
        if STORE_LOGS:
            log(f"     {path} {label or ''} page {page}: +{len(payload)} ({len(rows)} total)")
        if len(payload) < PAGE_SIZE:
            return rows, False
        offset += PAGE_SIZE
    return rows, True  # hit MAX_PAGES — more rows may exist


# --- low-level per-equity fetchers: (symbol, from_date) -> list to persist ----------------------

def _fetch_eod(symbol, from_date):
    # D-01: unadjusted OHLC + adjusted_close + volume. Delisted names stay in EODHD historically.
    params = {"api_token": TOKEN, "fmt": "json", "period": "d", "order": "a"}
    if from_date:
        params["from"] = from_date
    payload = _get(f"eod/{symbol}", params)
    if not isinstance(payload, list):
        raise RuntimeError("eod: expected list")
    # project to the canonical OHLCV field set (unadjusted OHLC + adjusted_close + volume)
    return [{k: row.get(k) for k in EOD_FIELDS} for row in payload]


def _fetch_eod_bulk_day(exchange, date):
    # D-01 backfill PRIMARY: whole-exchange OHLCV for one trading day (~100 credits/day-file).
    # Returns EVERY ticker that traded that day — INCLUDING delisted names — which the per-ticker
    # `eod` job (bounded to the fixed universe) can never reach. This is what makes the price
    # history survivorship-bias-free (spec §2 D-01, §7). `adjusted_close` here is as-of-pull and may
    # go stale after a later split/dividend — that's fine: the canonical Q-002 factor is derived from
    # D-02/D-03, and D-01's UNADJUSTED OHLCV (the source of truth) is immutable.
    params = {"api_token": TOKEN, "fmt": "json", "date": date}
    payload = _get(f"eod-bulk-last-day/{exchange}", params)
    if not isinstance(payload, list):
        raise RuntimeError("eod_bulk: expected list")
    return [{k: row.get(k) for k in BULK_FIELDS} for row in payload]


def _fetch_bulk_actions_day(exchange, date, kind):
    """D-02/D-03 WHOLE-MARKET corporate actions for one session (~100 credits each).

    The per-ticker splits/div pulls only ever cover the configured 503 names, while eod_bulk covers
    ~50k tickers including delisted ones — so a universe computed from the bulk prices
    (universe.mode = top1000_dollar_volume) had no split or dividend data for anything outside the
    503. These day-files close that: every split/dividend the whole exchange printed that session.
    Row shapes differ from the per-ticker feeds: splits give {code, date, split}, dividends give
    {code, date, dividend, unadjustedValue, currency, declarationDate, recordDate, paymentDate,
    period}."""
    params = {"api_token": TOKEN, "fmt": "json", "date": date, "type": kind}
    payload = _get(f"eod-bulk-last-day/{exchange}", params)
    if not isinstance(payload, list):
        raise RuntimeError(f"eod_bulk_actions[{kind}]: expected list")
    return payload


def _fetch_dividends(symbol, from_date):
    # D-03: ex-date, per-share value (EODHD exposes unadjustedValue on most names — kept as-is).
    params = {"api_token": TOKEN, "fmt": "json"}
    if from_date:
        params["from"] = from_date
    payload = _get(f"div/{symbol}", params)
    if not isinstance(payload, list):
        raise RuntimeError("dividends: expected list")
    return payload


def _fetch_splits(symbol, from_date):
    # D-02: split date + "A/B" ratio string.
    params = {"api_token": TOKEN, "fmt": "json"}
    if from_date:
        params["from"] = from_date
    payload = _get(f"splits/{symbol}", params)
    if not isinstance(payload, list):
        raise RuntimeError("splits: expected list")
    return payload


def _fetch_estimates(symbol, from_date):
    # D-14: point-in-time analyst-estimate snapshot. EODHD only ever returns the CURRENT Trend
    # object, so we stamp each pull with its UTC date and append (M1-01 immutable) — history accrues
    # forward from go-live, one row per pull day (from_date is intentionally ignored).
    #
    # Served from the per-ticker fundamentals cache. `filter=Earnings::Trend` narrows the RESPONSE,
    # not the PRICE: it is a fundamentals-class call and costs the full 10 credits (spec §4). Since
    # config/tickers.json runs both `fundamentals` and `estimates` over the same 503 names, calling
    # it separately billed 10,060 credits/run where 5,030 buys the identical bytes — verified live:
    # fundamentals/AAPL.US?filter=Earnings::Trend == fundamentals/AAPL.US -> ["Earnings"]["Trend"].
    #
    # Shape note: since the switch to v1.1 (see _fundamentals) this is {Quarterly: {...},
    # Annual: {...}}, not the old flat date-keyed map. Snapshots written before 2026-08-17 are the
    # flat v1 shape and their fiscal-Q4 entries hold the ANNUAL figure; build_m1 detects the shape
    # and tags the legacy rows `ambiguous_v1_flat` rather than pretending they are quarterly.
    payload = (_fundamentals(symbol).get("Earnings") or {}).get("Trend")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return [{"date": stamp, "trend": payload}]


def _fetch_news(symbol, from_date, to_date=None):
    """D-08: timestamped headlines, newest-first. Depth is ~Dec-2020 onward (API launch).

    WINDOWED, not one flat offset run. EODHD returns newest-first, so a plain offset walk that
    hits the MAX_PAGES * PAGE_SIZE ceiling (100,000 rows) drops the OLDEST articles in the window
    — and the next run's watermark is max(stored date), so it asks `from=<newest>` and never goes
    back for them. That hole is permanent and, worse, invisible.

    This is not hypothetical headroom: NVDA already returns 71,619 rows for a ONE-year window
    (MSFT 39,990, AAPL 37,290). Widening to the spec's multi-year backfill puts the busiest names
    several times over the ceiling. So when a window truncates we re-enter with `to=<oldest row
    fetched>` and keep walking backwards until a window comes back short."""
    out, seen = [], set()
    for window in range(1, MAX_NEWS_WINDOWS + 1):
        base = {"api_token": TOKEN, "fmt": "json", "s": symbol}
        if from_date:
            base["from"] = from_date
        if to_date:
            base["to"] = to_date
        rows, truncated = _paginate("news", base, label=f"{symbol} w{window}")
        fresh = 0
        oldest = None
        for r in rows:
            k = _news_key(r)
            if k not in seen:
                seen.add(k)
                out.append(r)
                fresh += 1
            d = (r.get("date") or "")[:10]
            if d and (oldest is None or d < oldest):
                oldest = d
        if not truncated:
            return out
        # Truncated: step the window back to the oldest row we actually received.
        if oldest is None or oldest == to_date or fresh == 0:
            # No progress — a single day exceeds the ceiling, or the vendor ignored `to`.
            log(f"WARN news {symbol}: window stalled at to={to_date} after {len(out)} rows "
                f"— older articles are NOT retrievable by this method")
            return out
        to_date = oldest
    log(f"WARN news {symbol}: hit MAX_NEWS_WINDOWS ({MAX_NEWS_WINDOWS}) at {len(out)} rows "
        f"(oldest reached {to_date}) — older articles may be truncated")
    return out


# --- snapshot fetchers (whole objects, always refetched) ----------------------------------------

def _fundamentals(symbol):
    """The 10-credit fundamentals object for `symbol`, fetched at most ONCE per ticker.

    Cached rather than re-derived from disk on purpose: reading DATA_DIR/fundamentals/<T>.json
    would couple `estimates` to a prior successful snapshot write and could serve a stale
    prior-run object into today's PIT snapshot. The cache is keyed by symbol and replaced as the
    per-ticker loop advances, so whichever of {fundamentals, estimates} runs first pays, the other
    is free, and the order in `datasets` stops mattering.

    ONE ENTRY PER WORKER THREAD, not one per process. A ticker's datasets run in order on a single
    worker (see the per-ticker pass in main), so its fundamentals and estimates always meet in that
    worker's cache. A process-wide one-entry cache would be evicted by the other workers between the
    two calls and re-bill the 10-credit object for most tickers (~5k credits a night).

    v1.1, NOT v1 — this is a correctness fix, not a version bump. The v1 endpoint returns
    `Earnings::Trend` as one flat date-keyed map, so a company's fiscal-Q4 row and its ANNUAL row
    share a key and the annual value wins. AAPL's Sep-2017 quarterly estimate is 1.87; v1 reports
    9.00 there, the FY number, ~5x too high, silently. 10 of AAPL's 39 quarterly periods are
    corrupted this way — every fiscal Q4 since 2017. v1.1 nests the block as
    {Quarterly: {...}, Annual: {...}} and returns 39 + 11 = 50 rows where v1 returns 40.
    Verified live 2026-08-17: every other top-level block is byte-identical between the two
    versions (13 keys, no field differences), so this costs nothing and breaks no other consumer."""
    cached = getattr(_TLS, "fund", None)
    if cached is None or cached[0] != symbol:
        payload = _get(f"v1.1/fundamentals/{symbol}", {"api_token": TOKEN})
        if not isinstance(payload, dict):
            raise RuntimeError("fundamentals: expected object")
        cached = _TLS.fund = (symbol, payload)   # replaces the last ticker's ~950 KB object
    return cached[1]


def fetch_fundamentals(symbol):
    # D-05/06 cross-check + D-07 Earnings::History. Stored lossless: the full nested object
    # (General/Highlights/Valuation/SharesStats/Earnings/Financials/...). Downstream PIT extraction
    # selects the blocks it needs; keeping it whole avoids silently dropping a field a model wants.
    return _fundamentals(symbol)


def fetch_index_constituents(index_symbol):
    # D-15: index fundamentals carry General + Components (current) + HistoricalTickerComponents
    # (add/remove dates) — the survivorship-free membership source. Stored lossless.
    payload = _get(f"fundamentals/{index_symbol}", {"api_token": TOKEN})
    if not isinstance(payload, dict):
        raise RuntimeError("index_constituents: expected object")
    return payload


def fetch_exchange_details(code):
    # D-11: EODHD exchange holidays — the cross-check to the exchange_calendars source of truth.
    payload = _get(f"exchange-details/{code}", {"api_token": TOKEN, "fmt": "json"})
    if not isinstance(payload, dict):
        raise RuntimeError("exchange_calendar: expected object")
    return payload


def fetch_symbol_list(code):
    # D-13: full symbol inventory for the exchange INCLUDING delisted tickers (delisted=1) —
    # drives backfill completeness / delisted handling (T-12).
    payload = _get(f"exchange-symbol-list/{code}", {"api_token": TOKEN, "fmt": "json", "delisted": 1})
    if not isinstance(payload, list):
        raise RuntimeError("symbol_list: expected list")
    return payload


def fetch_earnings_upcoming(symbols, from_date, to_date):
    # D-07 (upcoming): forward earnings calendar for the universe. `symbols` is chunked so the URL
    # stays well within limits; the per-chunk `earnings` arrays are concatenated.
    out = []
    for i in range(0, len(symbols), CAL_CHUNK):
        chunk = symbols[i:i + CAL_CHUNK]
        params = {"api_token": TOKEN, "fmt": "json", "symbols": ",".join(chunk)}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        payload = _get("calendar/earnings", params)
        rows = payload.get("earnings", []) if isinstance(payload, dict) else payload
        if isinstance(rows, list):
            out += rows
    return out


# Dedup keys for the append-only incremental streams (a stable per-record identity).
def _news_key(r):
    return r.get("link") or f"{r.get('date', '')}|{r.get('title', '')}"


def _estimates_key(r):
    return r.get("date")  # one snapshot per pull day; a same-day re-run overwrites it


# dataset -> (low_level_fetch, subdir, from_cfg_key, incremental_key_fn or None).
# incremental_key_fn is set ONLY for append-only streams; None => always full refetch (small /
# retroactively-revised: eod, dividends, splits).
# dataset -> (low_level_fetch, subdir, from_cfg_key, incremental_key_fn, supports_backfill).
# supports_backfill: the vendor can serve an OLDER window on request, so widening the config's
# start date re-pulls the gap. False for `estimates`, whose history is our own pull log — there is
# no earlier snapshot to go and get.
SERIES = {
    "eod":       (_fetch_eod,       "ohlcv",      "from",      None,            False),
    "dividends": (_fetch_dividends, "dividends",  "from",      None,            False),
    "splits":    (_fetch_splits,    "splits",     "from",      None,            False),
    "estimates": (_fetch_estimates, "estimates",  "from",      _estimates_key,  False),
    "news":      (_fetch_news,      "news",       "news_from", _news_key,       True),
}
# dataset -> (fetch(symbol), subdir, count_fn). Point-in-time objects, always refetched whole.
SNAPSHOT = {
    "fundamentals": (fetch_fundamentals, "fundamentals", None),
}
VALID_DATASETS = set(SERIES) | set(SNAPSHOT)


def _window_for(cfg, ticker, from_key):
    """The `from` one ticker's window-bearing datasets should use — see "PER-TICKER WINDOW" above.

    The three `from`-keyed streams with incr_key=None are FULL refetches: what the window returns
    REPLACES the file. So a ticker carrying history older than the config's `from` is truncated to
    the window on its next run, quietly and permanently. Promoting the data-only watchlist
    (from=1990) into tickers.json (from=2000) would have cost 2,170 price rows — ASML 1995, TSM
    1997, MSTR 1998 — and with them the pre-2000 ASML/TSM splits anchoring those names' whole
    Q-002 adjustment chain, which is the damage that outlives the rows.

    WIDENING ONLY: an override applies only when EARLIER than the config's own `from`, so it can
    add history and never remove it. `news_from` and friends are left alone deliberately — news is
    append-only and already on disk, and widening its window triggers a real backfill at real cost.
    """
    base = cfg.get(from_key, cfg.get("from"))
    if from_key != "from":
        return base
    ov = (cfg.get("from_overrides") or {}).get(ticker)
    return ov if ov and (not base or ov < base) else base


def _resolve_stocks_from(cfg, config_path):
    """`stocks_from`: take `stocks` and `from_overrides` from another config instead of repeating them.

    The two EODHD jobs split one universe: ohlcv.json takes its names from tickers.json, and the price
    watchlist from watchlist_eodhd.json. A second copy of a 517-name list would drift on the next index
    turnover, leaving prices and fundamentals covering different names. `from_overrides` comes with the
    list because it is a property of the names: eod is a full refetch that REPLACES the file, so a
    promoted name that arrived without its override would be truncated to `from` on its first price run
    (the 2026-09-16 promotion's 2,170 pre-2000 rows — see _window_for). A key this config sets itself
    wins. The path is relative to this config's directory, as with borrow.json's `stocks_from`.
    Raises on an unreadable file: running a price job over an empty universe is worse than not running."""
    src = cfg.get("stocks_from")
    if not src:
        return cfg
    path = src if os.path.isabs(src) else os.path.join(os.path.dirname(config_path), src)
    with open(path) as f:
        base = json.load(f)
    out = dict(cfg)
    for key in ("stocks", "from_overrides"):
        if key not in out and key in base:
            out[key] = base[key]
    return out


def _file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _read_existing(path):
    """Existing list payload on disk, or [] if absent/unreadable."""
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, ValueError):
        return []


def _oldest_date(rows):
    dates = [(r.get("date") or "")[:10] for r in rows if r.get("date")]
    return min(dates) if dates else None


def _latest_date(rows):
    dates = [(r.get("date") or "")[:10] for r in rows if r.get("date")]
    return max(dates) if dates else None


def _merge(existing, new, keyfn):
    """Union existing + new by keyfn (new wins on collision); sorted by date. Returns (rows, added)."""
    by = {keyfn(r): r for r in existing}
    added = 0
    for r in new:
        k = keyfn(r)
        if k not in by:
            added += 1
        by[k] = r
    rows = sorted(by.values(), key=lambda r: r.get("date") or "")
    return rows, added


def _write(out_path, data, indent=None):
    """Atomically write JSON: dump to a sibling .part, flush+fsync, then os.replace() into place.
    A pod killed mid-write can then never leave a truncated file — critical for eod_bulk, whose
    resume logic treats any existing day-file as complete and skips it forever."""
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tmp = out_path + ".part"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=indent)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, out_path)


def _load_sessions():
    """D-11 session set from the volume, or None when fetch_calendar.py hasn't run yet."""
    try:
        with open(SESSIONS_PATH) as f:
            payload = json.load(f)
        sessions = set(payload.get("sessions") or [])
        if sessions:
            global _EARLY_CLOSES
            _EARLY_CLOSES = set(payload.get("early_closes") or ())
            log(f"     eod_bulk: using D-11 session calendar {payload.get('calendar')} "
                f"({len(sessions)} sessions, {len(_EARLY_CLOSES)} early closes, "
                f"exchange_calendars {payload.get('package_version')})")
            return sessions
    except (FileNotFoundError, ValueError, AttributeError):
        pass
    log(f"     eod_bulk: no D-11 calendar at {SESSIONS_PATH} — falling back to Mon-Fri "
        f"(market holidays will be probed and may store non-session files)")
    return None


def _bulk_unsettled(path, session_date):
    """True if this day-file was written before its session had settled, so it must be re-pulled.

    Compares the file's MTIME against its own session date (not against today): a file fetched
    2 days after its session is suspect forever until re-fetched later, which is what lets this
    repair day-files frozen by earlier runs instead of only protecting future ones."""
    if BULK_RESETTLE_DAYS <= 0:
        return False
    try:
        written = datetime.fromtimestamp(os.path.getmtime(path), timezone.utc).date()
    except OSError:
        return False
    return (written - session_date).days < BULK_RESETTLE_DAYS


def _bulk_row_floor(bulk_dir, near_date=None, sample=12):
    """Median row count of stored day-files NEAREST IN DATE to `near_date`, times the fraction.

    ERA-LOCAL ON PURPOSE. The first version took the 20 newest files, which measures a backfill
    against today's market breadth. Walking into 2008 that put the floor at 25,205 while a real
    2008 session carries ~21,000 rows, so 879 complete days were fetched, rejected and re-queued
    in ONE run — ~88,000 credits spent to store 2 files, and it would have repeated every night
    with the backfill permanently stalled at 2007-10-31. US listed-security counts roughly halve
    going back two decades, so any fixed floor is wrong at one end of the range.

    Returns 0 (no floor) until enough neighbours exist to make a median meaningful — a cold
    backfill must not refuse its own first days."""
    try:
        names = [n for n in os.listdir(bulk_dir) if n.endswith(".json") and not n.startswith("_")]
    except OSError:
        return 0
    if near_date:
        # Nearest by date distance, not lexicographic order.
        names.sort(key=lambda n: abs((date.fromisoformat(n[:-5]) - near_date).days)
                   if _isodate(n[:-5]) else 10 ** 6)
    else:
        names.sort(reverse=True)
    counts = []
    for n in names[:sample]:
        try:
            with open(os.path.join(bulk_dir, n)) as f:
                rows = json.load(f)
            if isinstance(rows, list) and len(rows) > 1000:  # skip cached holiday/empty files
                counts.append(len(rows))
        except (OSError, ValueError):
            continue
    if len(counts) < 3:
        return 0
    counts.sort()
    return int(counts[len(counts) // 2] * BULK_MIN_ROWS_FRAC)


def _bulk_frontier(bulk_dir):
    """Oldest stored day-file date — where a backwards backfill actually resumes.

    This, not the window end, is the era the next fetch lands in. Seeding the floor from `bend`
    (the newest date) reproduced exactly the 2026-relative baseline it was meant to replace."""
    try:
        ds = [n[:-5] for n in os.listdir(bulk_dir)
              if n.endswith(".json") and not n.startswith("_") and _isodate(n[:-5])]
    except OSError:
        return None
    return date.fromisoformat(min(ds)) if ds else None


def _isodate(s):
    try:
        date.fromisoformat(s)
        return True
    except ValueError:
        return False


def _bulk_codes(rows):
    return {r.get("code") for r in rows or ()}


def _settled_codes(bulk_dir):
    """Codes carried by the newest SETTLED day-file (see _bulk_unsettled), or None on a cold volume.

    The completeness wait only waits for names this file carries. A name whose per-ticker feed prints
    but which the bulk file never lists (a vendor code mismatch, a class share filed differently)
    would otherwise hold the pod for the full wait every night; a settled file is the one sample of a
    finished publication on hand. Delisted names need no help here — they stop printing per-ticker."""
    try:
        names = sorted((n for n in os.listdir(bulk_dir)
                        if n.endswith(".json") and not n.startswith("_") and _isodate(n[:-5])),
                       reverse=True)
    except OSError:
        return None
    for n in names:
        path = os.path.join(bulk_dir, n)
        if _bulk_unsettled(path, date.fromisoformat(n[:-5])):
            continue
        try:
            with open(path) as f:
                rows = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(rows, list) and len(rows) > 1000:  # skip cached holiday/empty files
            return _bulk_codes(rows)
    return None


def _printed_on(iso):
    """Tickers whose per-ticker eod feed has a bar dated `iso`, as pulled earlier in THIS run."""
    return {sym.rsplit(".", 1)[0] for sym, dates in _EOD_TAIL.items() if iso in dates}


def _await_complete_day(exchange, iso, rows, expected, wait_min, poll_min):
    """Re-pull the newest session's day-file until it carries every name in `expected`, or time runs out.

    WHY. EODHD "publishes" the bulk day-file ~23:30 UTC and then keeps FILLING it for hours: pulls at
    01:05-01:40 UTC on 2026-09-03, -04 and -09 froze ~35.9k rows with 28-29 S&P names absent (AAPL,
    WMT, MCD...), and the file only plateaued ~02:50 UTC. The row floor cannot see this — half an
    era-correct floor still accepts a file 28% short. Before the split the per-ticker pass (~86 min)
    ran first in the same pod, so the walk reached the day-file late by accident; the ohlcv job gets
    there ~3 min after launch (517 eod calls on 8 workers), so it has to wait on purpose. The per-ticker feed, same vendor, has
    the full bar by 17:00 ET, which makes it the authority on who printed: `expected` is the universe
    names with a bar dated `iso` in this run's eod pull, narrowed to those a settled file carries.

    Polls every `poll_min` up to `wait_min`, 100 credits a pull. A thinner re-pull never replaces a
    fuller one. Still short at the deadline -> the fullest pull is returned, and the caller stores it
    as before; the settling window re-pulls the file for BULK_RESETTLE_DAYS, so it heals on its own.
    Returns (rows, info) — info lands in the manifest's eod_bulk entry as `newest_session`."""
    missing = expected - _bulk_codes(rows)
    waited, pulls = 0.0, 1
    while missing and poll_min > 0 and waited + poll_min <= wait_min:
        shown = ", ".join(sorted(missing)[:8]) + (", …" if len(missing) > 8 else "")
        log(f"     eod_bulk {exchange} {iso}: {len(rows):,} rows, {len(missing)} of {len(expected)} "
            f"universe names not in it yet ({shown}) — EODHD is still filling it; re-pull in "
            f"{poll_min:g} min ({waited:g}/{wait_min:g} min waited)")
        time.sleep(poll_min * 60)
        waited += poll_min
        try:
            fresh = _fetch_eod_bulk_day(exchange, iso)
            pulls += 1
        except Exception as e:
            log(f"WARN eod_bulk {exchange} {iso}: re-pull failed ({type(e).__name__}: {e}) — "
                f"keeping the {len(rows):,}-row pull")
            continue
        if len(fresh) >= len(rows):
            rows = fresh
        missing = expected - _bulk_codes(rows)
    if missing:
        shown = ", ".join(sorted(missing)[:12]) + (", …" if len(missing) > 12 else "")
        log(f"WARN eod_bulk {exchange} {iso}: still missing {len(missing)} of {len(expected)} universe "
            f"names after {waited:g} min ({shown}) — storing the fullest pull ({len(rows):,} rows); the "
            f"settling window re-pulls it for {BULK_RESETTLE_DAYS} days")
    elif pulls > 1:
        log(f"     eod_bulk {exchange} {iso}: complete after {waited:g} min "
            f"({pulls} pulls, {len(rows):,} rows, all {len(expected)} universe names present)")
    return rows, {"session": iso, "rows": len(rows), "expected": len(expected),
                  "missing": sorted(missing), "waited_min": waited, "pulls": pulls}


def _credit_budget():
    """(used, cap) from EODHD's own counter, or (None, None) if unavailable.

    The /user call is itself free. The counter resets lazily on the first request of a new GMT day,
    so `apiRequestsDate` is authoritative for *which* day the number belongs to."""
    try:
        d = _get("user", {"api_token": TOKEN, "fmt": "json"})
        used, cap = int(d["apiRequests"]), int(d["dailyRateLimit"])
        # Purchased add-on calls land in a separate `extraLimit` field, not in `dailyRateLimit`
        # (verified live 2026-08-21: 99,999/100,000 used + extraLimit 100,000 -> the gate read
        # "1 left" and skipped the run while 100k paid credits sat unused). Spend against the sum.
        extra = int(d.get("extraLimit") or 0)
        if extra:
            log(f"     credit add-on: extraLimit {extra:,} on top of the {cap:,} daily cap")
        cap += extra
        # The counter resets LAZILY, on the first billable request of a new GMT day — a /user call
        # does not trigger it. So just after midnight it still reports YESTERDAY's date and
        # yesterday's (often exhausted) total. Taking that at face value would make this preflight
        # skip the whole run on exactly the day it is supposed to start. `apiRequestsDate` is the
        # authority: if it is not today, the budget is really full.
        if str(d.get("apiRequestsDate", ""))[:10] != datetime.now(timezone.utc).date().isoformat():
            log(f"     credit counter still dated {d.get('apiRequestsDate')} "
                f"({used:,} used) — it resets on the first billable call; treating budget as full")
            return 0, cap
        return used, cap
    except Exception:
        return None, None


def main():
    global _PACER, _MERGE_BUDGET
    if not TOKEN:
        print("FATAL: EODHD_API_TOKEN not set", file=sys.stderr)
        return 1
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    try:
        cfg = _resolve_stocks_from(cfg, CONFIG_PATH)
    except (OSError, ValueError) as e:
        print(f"FATAL: stocks_from {cfg.get('stocks_from')!r} unreadable: {type(e).__name__}: {e}",
              file=sys.stderr)
        return 1
    # CONCURRENCY for the per-ticker pass (section 1). `workers` defaults to 1 — the sequential pass
    # — so only a config that asks for threads gets them. Env WORKERS overrides it for one launch
    # (launch.sh passes EODHD_WORKERS), so `EODHD_WORKERS=1 scripts/launch.sh` is the no-edit
    # fallback. The pacer covers every request this process makes; 0 disables it.
    workers = max(1, int(os.environ.get("WORKERS") or cfg.get("workers") or 1))
    _PACER = _Pacer(float(cfg.get("max_requests_per_minute", 300)))
    _MERGE_BUDGET = _ByteBudget(float(cfg.get("merge_budget_mb", 600)) * 1e6)

    # CREDIT PREFLIGHT. Running twice in one GMT day is the failure mode this prevents: the second
    # run starts with the remainder, gets a few tickers in, and then every remaining job 402s —
    # 2,993 of 3,026 jobs "failed" that way on 2026-08-14 while changing no data at all, because a
    # failed job simply does not write. That is a alarming-looking manifest for a no-op. Bail early
    # and cleanly instead, so a same-day re-run is a cheap skip rather than a fake catastrophe.
    # The floor is per pass: env MIN_CREDITS (bootstrap sets it for the watchlist pass), else the
    # config's `min_credits` (a price-only pass is ~2k), else 20k — what a full eodhd pass spends.
    used, cap = _credit_budget()
    if used is not None and cap:
        left = cap - used
        need = int(os.environ.get("MIN_CREDITS") or cfg.get("min_credits") or 20000)
        log(f"     credits: {used:,}/{cap:,} used, {left:,} left (need >= {need:,} to start)")
        if left < need:
            log(f"SKIP run: only {left:,} credits remain today — a full pass needs ~{need:,}. "
                f"The counter resets at midnight GMT; re-run then. Nothing was fetched.")
            return 0
    exchange = cfg.get("exchange", "US")
    datasets = cfg.get("datasets") or ["eod"]
    stocks = cfg.get("stocks", [])
    market = cfg.get("market", [])                       # fully-qualified symbols (SPY.US)
    market_dividends = cfg.get("market_dividends", [])   # SPY.US div for the total-return build
    index_constituents = cfg.get("index_constituents", [])
    exchanges = cfg.get("exchanges", [])
    symbol_lists = cfg.get("symbol_lists", [])           # D-13 exchange-symbol-list (incl. delisted)
    earnings_upcoming = cfg.get("earnings_upcoming", False)  # D-07 forward calendar (bool)
    earnings_days = cfg.get("earnings_calendar_days", 90)    # horizon for the forward calendar
    market_from = cfg.get("market_from", cfg.get("from"))  # HMM/Q-019 want market history back to 2000
    incremental = cfg.get("incremental", True)

    unknown = [d for d in datasets if d not in VALID_DATASETS]
    if unknown:
        print(f"FATAL: unknown dataset(s) {unknown}; valid: {sorted(VALID_DATASETS)}", file=sys.stderr)
        return 1

    os.makedirs(DATA_DIR, exist_ok=True)
    results = []
    retry_queue = []  # (index into results, attempt fn) per first-pass failure — end-of-run sweep

    def record(job):
        """Append one job's (entry, attempt) to the manifest. Main thread only, so the order of
        `results` — and the retry sweep's indices into it — never depends on worker timing."""
        entry, attempt = job
        if not entry["ok"]:
            retry_queue.append((len(results), attempt))
        results.append(entry)

    def series_job(dataset, symbol, out, low_fetch, default_from, incr_key, backfill=False):
        """Full refetch, or — for append-only streams when incremental — fetch the delta and merge.
        Runs now, on the calling thread; returns (entry, attempt) for record()."""
        def attempt():
            entry = {"symbol": symbol, "dataset": dataset, "ok": False, "count": 0, "added": 0, "error": None}
            # An append-only stream holds its whole stored file in memory from here to the write, so
            # it waits for room in the shared budget first (see _ByteBudget). 0 for a new file.
            held = (_MERGE_BUDGET.acquire(_file_size(out))
                    if (_MERGE_BUDGET and incremental and incr_key) else 0)
            try:
                existing = _read_existing(out) if (incremental and incr_key) else []
                if existing:
                    since = _latest_date(existing) or default_from
                    new = low_fetch(symbol, since)
                    # WIDENING THE WINDOW MUST ACTUALLY WIDEN IT. The tail watermark only ever
                    # moves forward, so if the config's start date is now EARLIER than the oldest
                    # row on disk, an incremental run would fetch only the new tail and silently
                    # ignore the extra history the operator just asked for — the config changes and
                    # nothing happens. Detect that gap and pull it too.
                    oldest = _oldest_date(existing)
                    mode = f"incr≥{since}"
                    if backfill and default_from and oldest and default_from < oldest:
                        new += low_fetch(symbol, default_from, to_date=oldest)
                        mode = f"incr≥{since} +backfill {default_from}..{oldest}"
                    merged, added = _merge(existing, new, incr_key)
                else:
                    new = low_fetch(symbol, default_from)
                    merged, added = _merge([], new, incr_key) if incr_key else (new, len(new))
                    mode = f"full≥{default_from}"
                _write(out, merged)
                if dataset == "eod":   # who printed which recent session — see _await_complete_day
                    _EOD_TAIL[symbol] = {(r.get("date") or "")[:10] for r in merged[-5:]}
                entry.update(ok=True, count=len(merged), added=added)
                log(f"OK   {dataset:<18} {symbol}: {len(merged)} (+{added}) [{mode}] -> {out}")
            except Exception as e:
                entry["error"] = f"{type(e).__name__}: {e}"
                log(f"FAIL {dataset:<18} {symbol}: {entry['error']}")
            finally:
                if held:
                    _MERGE_BUDGET.release(held)
            return entry
        return attempt(), attempt

    def snapshot_job(dataset, symbol, out, fetch, count_fn=None):
        """A point-in-time object, replaced whole. Runs now; returns (entry, attempt) for record()."""
        def attempt():
            entry = {"symbol": symbol, "dataset": dataset, "ok": False, "count": 0, "added": 0, "error": None}
            try:
                data = fetch()
                _write(out, data)
                c = count_fn(data) if count_fn else (len(data) if isinstance(data, (list, dict)) else 0)
                entry.update(ok=True, count=c, added=c)
                log(f"OK   {dataset:<18} {symbol}: {c} [snapshot] -> {out}")
            except Exception as e:
                entry["error"] = f"{type(e).__name__}: {e}"
                log(f"FAIL {dataset:<18} {symbol}: {entry['error']}")
            return entry
        return attempt(), attempt

    # 1) per-equity datasets — the bulk of the run. Measured sequentially on 2026-09-29 it was 85 of
    #    87 min: ~3,100 requests one at a time, each ~0.6 s even when the answer is empty (news 3.5 s,
    #    fundamentals 2.6, eod 2.5), so it is latency-bound, not credit- or bandwidth-bound. Tickers
    #    therefore run on `workers` threads. Each ticker's datasets stay in config order on ONE
    #    worker: fundamentals and estimates meet in that worker's cache (_fundamentals), and a
    #    ticker's files only ever have one writer. Results are recorded here on the main thread in
    #    ticker order, so the manifest reads exactly as it did sequentially.
    def ticker_jobs(ticker):
        symbol = f"{ticker}.{exchange}"
        jobs = []
        for ds in datasets:
            if ds in SERIES:
                low, subdir, from_key, incr_key, backfill = SERIES[ds]
                out = os.path.join(DATA_DIR, subdir, f"{ticker}.json")
                jobs.append(series_job(ds, symbol, out, low, _window_for(cfg, ticker, from_key),
                                       incr_key, backfill))
            else:
                fetch, subdir, count_fn = SNAPSHOT[ds]
                out = os.path.join(DATA_DIR, subdir, f"{ticker}.json")
                jobs.append(snapshot_job(ds, symbol, out,
                                         lambda fetch=fetch, symbol=symbol: fetch(symbol), count_fn))
        return jobs

    workers = min(workers, max(1, len(stocks)))
    if workers > 1:
        log(f"     per-ticker pass: {len(stocks)} tickers on {workers} workers ("
            + (f"requests paced to {_PACER.rpm:g}/min" if _PACER.rpm else "requests unpaced")
            + f", stored files in memory capped at {_MERGE_BUDGET.limit / 1e6:,.0f} MB)")
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ticker") as pool:
        for jobs in (pool.map(ticker_jobs, stocks) if workers > 1 else map(ticker_jobs, stocks)):
            for job in jobs:
                record(job)

    # 2) market-level price series (SPY.US) — full refetch (adjusted_close is retroactively revised)
    for sym in market:
        out = os.path.join(DATA_DIR, "market", f"{sym}.json")
        record(series_job("market", sym, out, _fetch_eod, market_from, None))

    # 2b) market-level dividends (SPY.US) for the D-09 total-return build
    for sym in market_dividends:
        out = os.path.join(DATA_DIR, "market", "dividends", f"{sym}.json")
        record(series_job("market_dividends", sym, out, _fetch_dividends, market_from, None))

    # 3) survivorship-free index membership (D-15 snapshot)
    for idx in index_constituents:
        out = os.path.join(DATA_DIR, "universe", f"{idx}.json")
        record(snapshot_job("index_constituents", idx, out, lambda idx=idx: fetch_index_constituents(idx),
                            count_fn=lambda d: len(d.get("Components") or {})))

    # 4) exchange trading calendar / holidays (D-11 EODHD cross-check snapshot)
    for code in exchanges:
        out = os.path.join(DATA_DIR, "calendar", f"{code}.json")
        record(snapshot_job("exchange_calendar", code, out, lambda code=code: fetch_exchange_details(code),
                            count_fn=lambda d: len(d.get("ExchangeHolidays") or {})))

    # 5) full symbol inventory incl. delisted (D-13 snapshot)
    for code in symbol_lists:
        out = os.path.join(DATA_DIR, "symbols", f"{code}.json")
        record(snapshot_job("symbol_list", code, out, lambda code=code: fetch_symbol_list(code)))

    # 6) forward earnings calendar for the universe (D-07 upcoming).
    #    APPEND-ONLY, one dated file per pull (M1-01): F8's `days_to_earnings` at time t must use
    #    the calendar AS KNOWN AT t. A single overwritten upcoming.json only ever holds today's
    #    view, so a backtest reading it would see announcement dates that were published after t —
    #    look-ahead, and the silent kind. upcoming.json is still written as a "latest" convenience
    #    copy; the dated files under earnings/upcoming/ are the point-in-time record.
    if earnings_upcoming and stocks:
        symbols = [f"{t}.{exchange}" for t in stocks]
        today = datetime.now(timezone.utc).date()
        frm = today.isoformat()
        to = (today + timedelta(days=earnings_days)).isoformat()
        dated = os.path.join(DATA_DIR, "earnings", "upcoming", f"{frm}.json")
        latest = os.path.join(DATA_DIR, "earnings", "upcoming.json")

        def _fetch_upcoming():
            rows = fetch_earnings_upcoming(symbols, frm, to)
            payload = {"pulled_at_utc": datetime.now(timezone.utc).isoformat(),
                       "vendor": "EODHD", "source_endpoint": f"{API}/calendar/earnings",
                       "spec_item": "D-07", "from": frm, "to": to,
                       "n_symbols": len(symbols), "earnings": rows}
            _write(latest, payload)
            return payload

        record(snapshot_job("earnings_upcoming", f"{len(symbols)} symbols {frm}..{to}", dated,
                            _fetch_upcoming, count_fn=lambda d: len(d.get("earnings") or [])))

    # 7) D-01 whole-exchange bulk backfill (survivorship-bias-free; INCLUDES delisted tickers).
    #    Date-partitioned: one file per trading day = every ticker. The volume persists, so each run
    #    resumes by SKIPPING day-files already present, newest-first, bounded by max_days_per_run to
    #    stay under the 100k-credit/day cap — a cold 2000→now backfill completes over several runs.
    # Shared by both bulk blocks; hoisted so eod_bulk_actions works with eod_bulk disabled.
    now_utc = datetime.now(timezone.utc)
    today_utc = now_utc.date()
    bulk = cfg.get("eod_bulk") or {}
    if bulk.get("enabled"):
        bex = bulk.get("exchange", exchange)
        bstart = datetime.strptime(bulk.get("from") or cfg.get("from") or "2000-01-01", "%Y-%m-%d").date()
        bend = (datetime.strptime(bulk["to"], "%Y-%m-%d").date() if bulk.get("to")
                else datetime.now(timezone.utc).date())
        # Size the run. "auto" means: use whatever credits are left after the per-ticker pass.
        # int() must never see the sentinel — an earlier version converted first and crashed the
        # whole run with ValueError: invalid literal for int() with base 10: 'auto', after the
        # per-ticker data had been fetched but before eod_bulk or the manifest.
        # The reserve is what must survive the backfill: 6,000 covered eod_bulk_actions when it ran
        # next in the same pod. In the ohlcv job the whole eodhd pass runs CONCURRENTLY in another
        # pod on the same key, so ohlcv.json raises it (`reserve_credits`) to cover that pass too.
        raw_max = bulk.get("max_days_per_run", 500)
        if str(raw_max).strip().lower() == "auto":
            used_now, cap_now = _credit_budget()
            if used_now is not None and cap_now:
                reserve = int(os.environ.get("BULK_CREDIT_RESERVE") or bulk.get("reserve_credits")
                              or 6000)
                max_days = max(0, (cap_now - used_now - reserve) // 100)
                log(f"     eod_bulk auto-size: {cap_now - used_now:,} credits left, "
                    f"reserving {reserve:,} -> {max_days:,} day-files this run")
            else:
                max_days = 500
                log("     eod_bulk auto-size: credit counter unavailable, falling back to 500")
        else:
            max_days = int(raw_max)
        entry = {"symbol": f"{bex} bulk {bstart}..{bend}", "dataset": "eod_bulk",
                 "ok": True, "count": 0, "added": 0, "error": None}
        bulk_dir = os.path.join(DATA_DIR, "eod_bulk", bex)
        sessions = _load_sessions()
        _frontier = _bulk_frontier(bulk_dir)
        row_floor = _bulk_row_floor(bulk_dir, near_date=_frontier or bend)
        log(f"     eod_bulk row floor: {row_floor:,} "
            f"(seeded from files near {_frontier or bend}, frac {BULK_MIN_ROWS_FRAC})")
        seed_floor = row_floor  # frontier-era floor — restored when the walk jumps eras
        last_stored = None      # date of the last file STORED this run, to detect era jumps
        recent_counts = []      # rolling row counts stored THIS run, to track breadth backwards
        partial_counts = []     # row counts REJECTED as partial — evidence the floor is wrong
        fetched = skipped = consec_fail = nonsession = deferred = 0
        more = False
        # Completeness wait for the newest session (see _await_complete_day). Off unless configured.
        wait_min = float(bulk.get("complete_wait_minutes") or 0)
        poll_min = float(bulk.get("complete_poll_minutes") or 10)
        settled = _settled_codes(bulk_dir) if wait_min > 0 else None
        newest_seen = False     # the wait applies to the FIRST session this walk fetches, only
        d = bend
        while d >= bstart:
            iso = d.isoformat()
            # Real NYSE sessions only when the D-11 calendar is available; Mon-Fri otherwise.
            is_session = (iso in sessions) if sessions is not None else (d.weekday() < 5)
            if is_session:
                out = os.path.join(bulk_dir, f"{iso}.json")
                if os.path.exists(out) and not _bulk_unsettled(out, d):
                    skipped += 1
                elif d == today_utc and now_utc.hour < BULK_TODAY_CUTOFF_UTC_HOUR:
                    # Today's session hasn't settled — don't spend the credit, don't risk freezing
                    # a partial file. Tomorrow's run (or a post-cutoff re-run) picks it up.
                    deferred += 1
                    more = True
                elif fetched >= max_days:
                    more = True
                    break  # per-run budget spent; a later run resumes from bend, skipping what's done
                else:
                    check_complete = wait_min > 0 and not newest_seen
                    newest_seen = True
                    try:
                        rows = _fetch_eod_bulk_day(bex, iso)
                        if check_complete:
                            expected = _printed_on(iso)
                            if settled is not None:
                                expected &= settled
                            rows, entry["newest_session"] = _await_complete_day(
                                bex, iso, rows, expected, wait_min, poll_min)
                        consec_fail = 0
                        # ERA JUMP. The walk re-pulls a handful of RECENT files (settling window)
                        # before leaping decades back to the frontier tail, and rolling breadth
                        # from the recent era must not police the old one: measured 2026-08-21,
                        # five ~50k-row 2026 re-pulls re-based the floor to 25,007, which then
                        # rejected 38 complete ~18.6k-row 2004 sessions as "partial" (3,800
                        # credits wasted, 38 holes). On a jump, fall back to the frontier seed.
                        if last_stored is not None and abs((last_stored - d).days) > 90:
                            recent_counts = []
                            if row_floor != seed_floor:
                                log(f"     eod_bulk era jump {last_stored} -> {iso}: "
                                    f"floor reset {row_floor:,} -> seed {seed_floor:,}")
                            row_floor = seed_floor
                        # A half-day prints roughly half the names, so the full-session floor
                        # rejects every early close forever (measured 2026-08-21: 2008-07-03 has
                        # 18,815 rows vs floor 25,043 — re-fetched and re-billed nightly, never
                        # stored). Judge early closes against half the floor.
                        floor_here = row_floor // 2 if iso in _EARLY_CLOSES else row_floor
                        if rows and floor_here and len(rows) < floor_here:
                            # Partial publication: a session file this short is not a quiet day.
                            # Freezing it would be permanent (resume skips any existing file).
                            log(f"WARN eod_bulk {bex} {iso}: only {len(rows)} rows "
                                f"(< floor {floor_here}) — looks partial, not storing; retry next run")
                            deferred += 1
                            more = True
                            partial_counts.append(len(rows))
                            # SELF-CORRECTION. N consecutive "partials" is overwhelming evidence the
                            # FLOOR is wrong, not that the vendor published N bad days in a row. Left
                            # unchecked this rejected 879 complete sessions and spent ~88,000 credits
                            # to store one file — twice, because the first fix seeded the floor from
                            # the wrong end of the window. Re-base on what the vendor is actually
                            # returning and carry on, loudly.
                            if len(partial_counts) >= BULK_PARTIAL_PATIENCE:
                                mid = sorted(partial_counts)[len(partial_counts) // 2]
                                new_floor = int(mid * BULK_MIN_ROWS_FRAC)
                                if new_floor < row_floor:
                                    log(f"WARN eod_bulk {bex}: {len(partial_counts)} consecutive "
                                        f"rejections — the floor ({row_floor:,}) does not match this "
                                        f"era (median {mid:,}). Re-basing to {new_floor:,}.")
                                    row_floor = new_floor
                                    recent_counts = []  # wrong-era evidence — one store must not re-base it back up
                                partial_counts = []
                        elif rows:
                            _write(out, rows); fetched += 1
                            last_stored = d
                            # Re-base the floor on what this era actually looks like. Without it a
                            # single seed from the resume point drifts wrong over a multi-year walk.
                            # Early closes are excluded: their halved breadth would drag the median.
                            partial_counts = []
                            if iso not in _EARLY_CLOSES:
                                recent_counts.append(len(rows))
                            if len(recent_counts) >= 3:
                                recent_counts = recent_counts[-25:]
                                mid = sorted(recent_counts)[len(recent_counts) // 2]
                                row_floor = int(mid * BULK_MIN_ROWS_FRAC)
                        elif (today_utc - d).days > 5:
                            _write(out, rows); fetched += 1  # settled no-data day — cache [] so we never re-probe
                            last_stored = d
                        else:
                            deferred += 1
                            more = True  # recent empty day (not yet posted) — retry next run
                    except Exception as e:
                        consec_fail += 1
                        more = True
                        if consec_fail >= 3:
                            entry["error"] = (f"stopped after 3 consecutive failures "
                                              f"(likely 100k/day credit cap): {type(e).__name__}: {e}")
                            log(f"WARN eod_bulk {bex}: {entry['error']} — re-run to resume")
                            break
            elif sessions is not None and d.weekday() < 5:
                nonsession += 1  # weekday the exchange was shut — no call, no credit, no phantom file
            d -= timedelta(days=1)
        entry.update(count=fetched, added=fetched, deferred=deferred,
                     non_sessions_skipped=nonsession, row_floor=row_floor)
        # Bulk is a long-horizon, resumable backfill, so merely DEFERRING work (per-run cap spent,
        # today not settled, a recent day not yet published) stays NON-FATAL — a run must not be
        # marked FAILED for pacing itself. But a run that BAILED on consecutive errors is a real
        # break (bulk not activated on the plan — §8-4 — a revoked token, or the 100k/day cap) and
        # must fail the run: leaving it ok=True meant a completely dead bulk endpoint reported
        # success every day while the price backfill silently stopped advancing.
        # ...but only when the run made NO progress. Hitting the 100k/day cap partway through a
        # multi-day backfill sets the same `error` and is entirely normal — failing the run every
        # day for the duration of a planned backfill would train you to ignore the flag. Zero
        # day-files plus consecutive errors is the shape of a genuine break.
        if entry["error"] and fetched == 0:
            entry["ok"] = False
        results.append(entry)
        status = f"~{d.isoformat()}+ remaining, re-run to continue" if more else "complete"
        log(f"OK   {'eod_bulk':<18} {bex}: +{fetched} day-files (skipped {skipped} existing, "
            f"{nonsession} non-sessions, {deferred} deferred; {status}) -> {bulk_dir}/")

    # 7b) D-02/D-03 whole-market corporate actions, date-partitioned like eod_bulk.
    #     Separate `from` because the credit profile is different: 200/session (splits+dividends)
    #     on top of eod_bulk's 100, so a full 5-year backfill is ~253k credits / 3 days of cap.
    #     Defaults to forward-only from `from` — the per-ticker feeds already cover the 503 names
    #     historically; what was missing is the rest of the bulk universe. Widen deliberately.
    bulk_act = cfg.get("eod_bulk_actions") or {}
    if bulk_act.get("enabled"):
        bex = bulk_act.get("exchange", exchange)
        astart = datetime.strptime(bulk_act.get("from") or cfg.get("from") or "2000-01-01",
                                   "%Y-%m-%d").date()
        aend = (datetime.strptime(bulk_act["to"], "%Y-%m-%d").date() if bulk_act.get("to")
                else datetime.now(timezone.utc).date())
        amax = int(bulk_act.get("max_days_per_run", 60))
        kinds = bulk_act.get("types") or ["splits", "dividends"]
        sessions_a = _load_sessions()
        entry = {"symbol": f"{bex} bulk-actions {astart}..{aend}", "dataset": "eod_bulk_actions",
                 "ok": True, "count": 0, "added": 0, "error": None}
        got = askip = adefer = 0
        d = aend
        while d >= astart and got < amax:
            iso = d.isoformat()
            is_sess = (iso in sessions_a) if sessions_a is not None else (d.weekday() < 5)
            if is_sess:
                if d == today_utc and now_utc.hour < BULK_TODAY_CUTOFF_UTC_HOUR:
                    adefer += 1
                else:
                    for kind in kinds:
                        out = os.path.join(DATA_DIR, "eod_bulk_actions", bex, kind, f"{iso}.json")
                        if os.path.exists(out):
                            askip += 1
                            continue
                        try:
                            rows = _fetch_bulk_actions_day(bex, iso, kind)
                            # An action-free session is a real answer, so [] is cached — unlike
                            # eod_bulk, where an empty price file means "not published yet".
                            _write(out, rows)
                            got += 1
                        except Exception as e:
                            entry["error"] = f"{type(e).__name__}: {e}"
                            log(f"WARN eod_bulk_actions {kind} {iso}: {entry['error']}")
                            got = amax
                            break
            d -= timedelta(days=1)
        entry.update(count=got, added=got, deferred=adefer)
        if entry["error"] and got == 0:
            entry["ok"] = False
        results.append(entry)
        log(f"OK   {'eod_bulk_actions':<18} {bex}: +{got} day-file(s) over {kinds} "
            f"(skipped {askip} existing, {adefer} deferred) -> "
            f"{os.path.join(DATA_DIR, 'eod_bulk_actions', bex)}/")

    # End-of-run retry sweep: each first-pass failure gets one fresh attempt before the run is
    # judged. record_series/record_snapshot are idempotent (atomic writes, incremental re-reads
    # disk), so a re-run is safe; eod_bulk is excluded — it is non-fatal and resumes across runs.
    if retry_queue:
        log(f"RETRY {len(retry_queue)} failed task(s) after {RETRY_SWEEP_DELAY}s pause ...")
        time.sleep(RETRY_SWEEP_DELAY)
        healed = 0
        for idx, attempt in retry_queue:
            entry = attempt()
            entry["retried"] = True  # manifest: distinguishes healed-on-retry from clean first pass
            healed += 1 if entry["ok"] else 0
            results[idx] = entry
        log(f"RETRY sweep healed {healed}/{len(retry_queue)}")

    all_ok = bool(results) and all(r["ok"] for r in results)
    manifest = {
        "vendor": "EODHD",
        "job": RUN_TAG or "eodhd",
        "spec": "Data Acquisition Specification — FINAL v1.2",
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "exchange": exchange,
        "from": cfg.get("from"),
        "news_from": cfg.get("news_from"),
        "market_from": market_from,
        "incremental": incremental,
        "datasets": datasets,
        "market": market,
        "market_dividends": market_dividends,
        "index_constituents": index_constituents,
        "exchanges": exchanges,
        "symbol_lists": symbol_lists,
        "earnings_upcoming": bool(earnings_upcoming),
        "eod_bulk": cfg.get("eod_bulk") or {"enabled": False},
        "ok": all_ok,
        "results": results,
    }
    _write(_manifest_path(), manifest, indent=2)

    if not all_ok:
        # Errors are ALWAYS logged, regardless of STORE_LOGS.
        log(f"FAILED — error log: {_persist_log('error', manifest)}")
        return 1
    if STORE_LOGS:
        log(f"run log: {_persist_log('run', manifest)}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Last-resort: persist the traceback to the volume so it survives termination.
        # ALWAYS written, regardless of STORE_LOGS.
        try:
            _LOG_LINES.append(traceback.format_exc())
            _persist_log("crash")
        finally:
            traceback.print_exc()
        sys.exit(1)
