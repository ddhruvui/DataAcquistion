"""A fake EODHD for fetch.py's synthetic tests: serves every endpoint fetch.py calls, faked at urllib's
urlopen so fetch.py's own request, retry and parsing code still runs. Not a test module itself."""
import json
import urllib.parse
from datetime import datetime, timedelta, timezone


def sessions_before_today(n=6):
    """The n weekdays before today, oldest first — never today, so the run's clock can't matter."""
    out, d = [], datetime.now(timezone.utc).date() - timedelta(days=1)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return sorted(out)


class _Resp:
    def __init__(self, payload):
        self.status, self._body = 200, json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeEODHD:
    """`printed` maps ticker -> dates it has a per-ticker bar; the bulk day-file for a date carries
    every printer except `never_in_bulk`, unless `bulk_plan` scripts that date: a list of
    (codes, filler_rows) served on successive calls, the last repeating. `on_request(path, q)`, if
    given, runs before each answer — a hook to hold a request (e.g. to force threads to interleave).
    Safe to call from several threads: `calls` only ever grows by list.append."""

    def __init__(self, sessions, printed, never_in_bulk=(), bulk_plan=None, filler=1200, on_request=None):
        self.sessions, self.printed = sessions, printed
        self.never_in_bulk, self.bulk_plan, self.filler = set(never_in_bulk), bulk_plan or {}, filler
        self.on_request = on_request
        self.calls = []

    def __call__(self, url, timeout=None, context=None):
        parts = urllib.parse.urlsplit(url)
        path = parts.path.split("/api/", 1)[1]
        q = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}
        self.calls.append((path, q))
        if self.on_request:
            self.on_request(path, q)
        return _Resp(self.route(path, q))

    def count(self, prefix):
        return sum(1 for p, _ in list(self.calls) if p.startswith(prefix))

    def bulk_calls(self, day):
        return sum(1 for p, q in list(self.calls)
                   if p.startswith("eod-bulk-last-day/") and q.get("date") == day and not q.get("type"))

    def route(self, path, q):
        today = datetime.now(timezone.utc).date().isoformat()
        if path == "user":
            return {"apiRequests": 10, "dailyRateLimit": 100000, "extraLimit": 0, "apiRequestsDate": today}
        if path.startswith("eod-bulk-last-day/"):
            if q.get("type"):
                return []
            day = q["date"]
            plan = self.bulk_plan.get(day)
            if plan:
                codes, filler = plan[min(self.bulk_calls(day), len(plan)) - 1]
            else:
                codes = {t for t, ds in self.printed.items() if day in ds} - self.never_in_bulk
                filler = self.filler
            names = sorted(codes) + [f"F{i:05d}" for i in range(filler)]
            return [dict(code=c, date=day, open=1, high=1, low=1, close=1, adjusted_close=1, volume=9)
                    for c in names]
        if path.startswith("eod/"):
            t = path[len("eod/"):].rsplit(".", 1)[0]
            days = self.printed.get(t, self.sessions)
            return [dict(date=d, open=1, high=1, low=1, close=1, adjusted_close=1, volume=9) for d in days]
        if path.startswith(("div/", "splits/")):
            return []
        if path == "news":
            t = q.get("s", "").rsplit(".", 1)[0]
            return [dict(date=f"{d}T12:00:00+00:00", title=f"{t} {d}", link=f"https://x/{t}/{d}",
                         content="c", symbols=[q.get("s")]) for d in self.sessions]
        if path.startswith("v1.1/fundamentals/"):
            return {"General": {"Code": path.rsplit("/", 1)[1]}, "Earnings": {"Trend": {"Quarterly": {}}}}
        if path.startswith("fundamentals/"):
            return {"Components": {"0": {"Code": "AAA"}}}
        if path.startswith("exchange-details/"):
            return {"ExchangeHolidays": {}}
        if path.startswith("exchange-symbol-list/"):
            return [{"Code": "AAA"}]
        if path == "calendar/earnings":
            return {"earnings": []}
        raise AssertionError(f"unexpected EODHD endpoint {path}")
