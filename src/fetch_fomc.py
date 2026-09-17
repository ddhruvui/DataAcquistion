#!/usr/bin/env python3
"""FOMC meeting calendar -> the decision dates and STATEMENT TIMES the minute-bar work needs.

Source: federalreserve.gov, public HTML, no key, no credits.
  * /monetarypolicy/fomccalendars.htm            the current window (2021-2027 today; rows carry the
                                                  statement, press-conference and SEP links; a date
                                                  marked * has a Summary of Economic Projections)
  * /monetarypolicy/fomchistorical<YEAR>.htm      one page per past year (the Fed moves a year here
                                                  about five years on); its panel headings name the
                                                  meeting type: "Meeting", "(unscheduled) Meeting",
                                                  "Conference Call"
  * the statement page of every meeting            fetched ONCE (cached under raw/statements/) for the
                                                  "For release at 2:00 p.m. EST" line; older pages say
                                                  "For immediate release", so those meetings get the
                                                  time from config statement_time_rules (time_basis
                                                  "rule") — unscheduled ones without a page time: null

Why the time matters: a statement lands at 14:00 ET (14:15 before 2013, 12:30 on the 2011-12
press-conference meetings) and the reaction is a minute-scale event; a date alone cannot place it.
Why unscheduled meetings are kept but flagged: 2008-01-22 (08:20 ET), 2020-03-03 (10:00) and
2020-03-15 (Sunday 17:00) are real decisions, but not the pre-announced kind the pre-FOMC study is
about, so `scheduled` is the column a study filters on.

OUTPUT (DATA_DIR = /workspace/data_fomc):
    fomc.json         {generated_at_utc, source, from_year, to_year, n_meetings, n_scheduled,
                       n_unscheduled, meetings: [{year, label, start_date, end_date, scheduled,
                       kind (meeting|conference_call|notation_vote|cancelled), sep, press_conference,
                       statement_url, statement_date, statement_time_et, statement_tz, time_basis
                       (page|rule|none), minutes_url, source_page}]}   sorted by end_date
    raw/…             every page as fetched (calendars.html, historical/<YEAR>.html,
                      statements/<YYYYMMDDx>.html) so a parser fix can re-run offline
    _run.json         manifest: ok, per-page results, statements fetched/cached, per-year counts

Validation (fails the run): every year from from_year through the last year on the calendars page
must show the eight pre-announced meetings (held + cancelled) — the FOMC has scheduled eight a year
since 1981; 2020 held seven after March 17-18 gave way to the unscheduled March 15 meeting.

    DATA_DIR=./data_fomc CONFIG_PATH=config/fomc.json python3 src/fetch_fomc.py
Stdlib only. FOMC_BASE_URL overrides the host (tests, mirrors).
"""
import html as htmllib
import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import date, datetime, timezone

DATA_DIR = os.environ.get("DATA_DIR", "/workspace/data_fomc")
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/workspace/code/fomc.json")
BASE_URL = os.environ.get("FOMC_BASE_URL", "https://www.federalreserve.gov").rstrip("/")
STORE_LOGS = os.environ.get("STORE_LOGS", "").strip().lower() in ("1", "true", "yes", "on")
UA = "Mozilla/5.0 (compatible; DataAcquistion fetch_fomc; research use)"
CALENDARS_PATH = "/monetarypolicy/fomccalendars.htm"
HISTORICAL_PATH = "/monetarypolicy/fomchistorical{year}.htm"

MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August", "September",
     "October", "November", "December"], start=1)}
MONTHS.update({k[:3]: v for k, v in list(MONTHS.items())})
MONTHS["sept"] = 9

_LOG_LINES = []


def log(msg):
    print(msg, flush=True)
    _LOG_LINES.append(msg)


def _persist_log(kind, manifest=None):
    log_dir = os.path.join(DATA_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(log_dir, f"{kind}-{stamp}.log")
    with open(path, "w") as f:
        if _LOG_LINES:
            f.write("\n".join(_LOG_LINES) + "\n\n")
        if manifest is not None:
            f.write("--- manifest ---\n" + json.dumps(manifest, indent=2) + "\n")
    return path


def _write(out_path, data):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tmp = out_path + ".part"
    with open(tmp, "w") as f:
        if isinstance(data, str):
            f.write(data)
        else:
            json.dump(data, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, out_path)


# ---------------------------------------------------------------- http (stdlib, cached to raw/)
def http_get(url, retries=4, timeout=60):
    """Page text, or raises. Retries on 5xx/network errors; a 404 raises at once (a year with no
    historical page yet is normal and handled by the caller)."""
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise
            last = e
        except Exception as e:  # noqa: BLE001 — retried, then raised
            last = e
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"GET {url}: {last}")


def fetch_page(path, raw_rel, pause, refresh=True):
    """GET BASE_URL+path, keep a copy at raw/<raw_rel>. refresh=False reuses the cached copy
    (statement pages never change; calendar pages do)."""
    raw = os.path.join(DATA_DIR, "raw", raw_rel)
    if not refresh and os.path.exists(raw):
        with open(raw, encoding="utf-8") as f:
            return f.read(), "cached"
    text = http_get(BASE_URL + path)
    _write(raw, text)
    if pause:
        time.sleep(pause)
    return text, "fetched"


# ---------------------------------------------------------------- parsing
def _text(fragment):
    t = re.sub(r"<[^>]+>", " ", fragment)
    t = htmllib.unescape(t).replace("\xa0", " ")
    return re.sub(r"\s+", " ", t).strip()


def _month(name):
    m = MONTHS.get(name.strip().lower().rstrip("."))
    if not m:
        raise ValueError(f"unknown month {name!r}")
    return m


def _iso(y, m, d):
    return date(int(y), int(m), int(d)).isoformat()


def _links(fragment):
    return re.findall(r'<a\s[^>]*href="([^"]+)"[^>]*>(.*?)</a>', fragment, flags=re.S | re.I)


def _statement_link(fragment):
    """First link whose text is 'Statement' or the HTML link after a 'Statement:' label."""
    for href, text in _links(fragment):
        if _text(text).lower() == "statement":
            return href
    m = re.search(r'Statement:.*?href="([^"]*\.htm)"', fragment, flags=re.S)
    return m.group(1) if m else None


def _minutes_link(fragment):
    for href, text in _links(fragment):
        t = _text(text).lower()
        if t == "minutes" or (t == "html" and "minutes" in href.lower()):
            return href
    return None


def _sep(fragment):
    return bool(re.search(r"Projection Materials|Projection materials|SEPcompilation|SEPmaterial|"
                          r"Summary of Economic Projections|projtabl", fragment))


def _statement_date(url, fallback):
    m = re.search(r"(20\d{6}|19\d{6})", url or "")
    if not m:
        return fallback
    s = m.group(1)
    return _iso(s[:4], s[4:6], s[6:8])


def parse_calendars(html):
    """The current calendars page -> {year: [meeting dict]} (rows in page order)."""
    out = {}
    parts = re.split(r'<h4>\s*<a[^>]*>\s*(\d{4}) FOMC Meetings\s*</a>\s*</h4>', html)
    for i in range(1, len(parts) - 1, 2):
        year, block = int(parts[i]), parts[i + 1]
        rows = re.split(r'<div class="[^"]*\brow fomc-meeting\b[^"]*"', block)[1:]
        meetings = []
        for row in rows:
            mm = re.search(r'fomc-meeting__month[^>]*>\s*<strong>(.*?)</strong>', row, flags=re.S)
            dm = re.search(r'fomc-meeting__date[^>]*>(.*?)</div>', row, flags=re.S)
            if not mm or not dm:
                continue
            month_txt, date_txt = _text(mm.group(1)), _text(dm.group(1))
            months = [_month(x) for x in re.split(r"\s*/\s*", month_txt)]
            paren = re.search(r"\((.*?)\)", date_txt)
            note = paren.group(1).strip().lower() if paren else ""
            days = re.findall(r"\d{1,2}", re.sub(r"\(.*?\)", "", date_txt))
            if not days:
                continue
            d1, d2 = days[0], days[-1]
            m1, m2 = months[0], months[-1]
            kind, scheduled = _kind("notation vote" if "notation" in note else ("cancelled" if "cancel" in note else
                                    ("unscheduled" if "unscheduled" in note else None)), None)
            stmt = _statement_link(row)
            end = _iso(year, m2, d2)
            meetings.append({
                "year": year, "label": f"{month_txt} {date_txt}",
                "start_date": _iso(year, m1, d1), "end_date": end,
                "scheduled": scheduled, "kind": kind,
                "sep": "*" in date_txt or _sep(row), "press_conference": "Press Conference" in row,
                "statement_url": stmt, "statement_date": _statement_date(stmt, end),
                "minutes_url": _minutes_link(row), "source_page": "calendars",
            })
        out[year] = meetings
    return out


# "January 27-28 Meeting - 2004", "April/May 30-1 Meeting - 2013", "October 31-November 1 Meeting - 2017",
# "January 21 Conference Call - 2008", "March 15 (unscheduled) Meeting - 2020", "October 16 (unscheduled) - 2013",
# "March 17-18 (cancelled) Meeting - 2020", "March 19 (notation vote) - 2020"
_HEAD = re.compile(r"^(?P<m1>[A-Za-z]+\.?)(?:\s*/\s*(?P<m1b>[A-Za-z]+\.?))?\s+(?P<d1>\d{1,2})"
                   r"(?:\s*[-–]\s*(?:(?P<m2>[A-Za-z]+\.?)\s+)?(?P<d2>\d{1,2}))?"
                   r"\s*(?:\((?P<flag>unscheduled|cancelled|notation vote)\))?"
                   r"\s*(?P<kind>Meeting|Conference Call|Notation Vote)?"
                   r"\s*[-–]\s*(?P<y>\d{4})$", re.I)


def _kind(flag, kind_word):
    """meeting | conference_call | notation_vote | cancelled, and whether it counts as scheduled."""
    flag = (flag or "").lower(); kind_word = (kind_word or "").lower()
    if flag == "notation vote" or kind_word == "notation vote":
        return "notation_vote", False
    if flag == "cancelled":
        return "cancelled", False
    if kind_word == "conference call":
        return "conference_call", False
    return "meeting", flag != "unscheduled"


def parse_historical(html, year):
    """One fomchistorical<YEAR>.htm page -> [meeting dict]. Panels are headed
    '<Month> <d>[-<d>] [(unscheduled)] Meeting|Conference Call - <year>'."""
    heads = list(re.finditer(r"<h5[^>]*>(.*?)</h5>", html, flags=re.S))
    meetings = []
    for k, h in enumerate(heads):
        title = _text(h.group(1))
        m = _HEAD.match(title)
        if not m:
            continue
        body = html[h.end(): heads[k + 1].start() if k + 1 < len(heads) else len(html)]
        m1 = _month(m.group("m1"))
        m2 = _month(m.group("m2") or m.group("m1b") or m.group("m1"))
        d1 = m.group("d1"); d2 = m.group("d2") or d1
        kind, scheduled = _kind(m.group("flag"), m.group("kind"))
        stmt = _statement_link(body)
        end = _iso(year, m2, d2)
        meetings.append({
            "year": int(m.group("y")), "label": title,
            "start_date": _iso(year, m1, d1), "end_date": end,
            "scheduled": scheduled, "kind": kind,
            "sep": _sep(body), "press_conference": "Press Conference" in body,
            "statement_url": stmt, "statement_date": _statement_date(stmt, end),
            "minutes_url": _minutes_link(body), "source_page": f"historical{year}",
        })
    return meetings


_RELEASE = re.compile(r"For release at\s*(\d{1,2}):(\d{2})\s*([ap])\.?\s*m\.?\s*(E[SD]T)", re.I)


def parse_statement_time(html):
    """('HH:MM', 'EST'|'EDT') from the 'For release at 2:00 p.m. EST' line; None when the page
    says 'For immediate release' (every statement before ~2014) or carries no line."""
    t = _text(html)
    m = _RELEASE.search(t)
    if not m:
        return None
    hh, mi, ap, tz = int(m.group(1)), m.group(2), m.group(3).lower(), m.group(4).upper()
    if ap == "p" and hh != 12:
        hh += 12
    if ap == "a" and hh == 12:
        hh = 0
    return f"{hh:02d}:{mi}", tz


def rule_time(rules, meeting):
    """Statement time from config statement_time_rules for a SCHEDULED meeting, else None."""
    if not meeting.get("scheduled"):
        return None
    when = meeting.get("statement_date") or meeting["end_date"]
    pick = None
    for r in sorted(rules, key=lambda r: r["from"]):
        if r["from"] <= when:
            pick = r
    if not pick:
        return None
    if meeting.get("press_conference") and pick.get("time_et_press_conference"):
        return pick["time_et_press_conference"]
    return pick.get("time_et")


def validate(meetings, from_year, to_year):
    """Every year in [from_year, to_year] must show the eight pre-announced meetings: held
    (`scheduled`) plus cancelled (2020: March 17-18 gave way to the unscheduled March 15 meeting)."""
    per_year = {}
    for m in meetings:
        y = int(m["end_date"][:4])
        row = per_year.setdefault(y, {"scheduled": 0, "unscheduled": 0, "cancelled": 0})
        row["scheduled" if m["scheduled"] else ("cancelled" if m["kind"] == "cancelled" else "unscheduled")] += 1
    problems = []
    for y in range(from_year, to_year + 1):
        row = per_year.get(y, {})
        n = row.get("scheduled", 0) + row.get("cancelled", 0)
        if n != 8:
            problems.append(f"{y}: {n} scheduled meetings (expected 8)")
    return per_year, problems


# ---------------------------------------------------------------- main
def main():
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    from_year = int(cfg.get("from_year", 2004))
    pause = float(cfg.get("request_pause_sec", 0.3))
    rules = cfg.get("statement_time_rules") or []
    want_times = bool(cfg.get("fetch_statement_pages", True))
    results, problems = [], []

    # 1) the current calendars page (this year and its neighbours)
    by_year = {}
    try:
        html, how = fetch_page(CALENDARS_PATH, "calendars.html", pause)
        cal = parse_calendars(html)
        for y, ms in cal.items():
            by_year[y] = ms
        results.append({"symbol": "calendars", "dataset": "fomc", "ok": True, "count": sum(len(v) for v in cal.values()),
                        "years": sorted(cal), "error": None})
        log(f"OK   calendars page: years {min(cal)}..{max(cal)}, {sum(len(v) for v in cal.values())} meetings ({how})")
    except Exception as e:
        results.append({"symbol": "calendars", "dataset": "fomc", "ok": False, "count": 0, "error": f"{type(e).__name__}: {e}"})
        log(f"FAIL calendars page: {type(e).__name__}: {e}")
    to_year = int(cfg.get("to_year") or (max(by_year) if by_year else datetime.now(timezone.utc).year))

    # 2) one historical page per year; where a year is on both, the historical page wins (it names
    #    unscheduled meetings and conference calls explicitly)
    for y in range(from_year, to_year + 1):
        try:
            html, how = fetch_page(HISTORICAL_PATH.format(year=y), f"historical/{y}.html", pause)
        except urllib.error.HTTPError as e:
            if e.code == 404 and y in by_year:
                continue                      # not yet moved to the historical pages: calendars page covers it
            results.append({"symbol": str(y), "dataset": "fomc", "ok": False, "count": 0, "error": f"HTTP {e.code}"})
            log(f"FAIL historical {y}: HTTP {e.code}")
            continue
        except Exception as e:
            results.append({"symbol": str(y), "dataset": "fomc", "ok": False, "count": 0, "error": f"{type(e).__name__}: {e}"})
            log(f"FAIL historical {y}: {type(e).__name__}: {e}")
            continue
        ms = parse_historical(html, y)
        if ms:
            by_year[y] = ms
            results.append({"symbol": str(y), "dataset": "fomc", "ok": True, "count": len(ms), "error": None})
            log(f"OK   historical {y}: {len(ms)} entries ({how})")
        elif y not in by_year:
            results.append({"symbol": str(y), "dataset": "fomc", "ok": False, "count": 0, "error": "page parsed to zero meetings"})
            log(f"FAIL historical {y}: page parsed to zero meetings")

    meetings = sorted((m for ms in by_year.values() for m in ms), key=lambda m: (m["end_date"], m["start_date"]))

    # 3) statement time: the page's own 'For release at' line, else the rule table
    n_fetched = n_cached = n_page = n_rule = n_none = 0
    for m in meetings:
        t = None
        if want_times and m.get("statement_url"):
            key = re.sub(r"[^0-9A-Za-z]+", "", m["statement_url"].rsplit("/", 2)[-2] + "_" + m["statement_url"].rsplit("/", 1)[-1])[:60]
            try:
                page, how = fetch_page(m["statement_url"], f"statements/{key}.html", pause, refresh=False)
                n_fetched += how == "fetched"; n_cached += how == "cached"
                t = parse_statement_time(page)
            except Exception as e:  # noqa: BLE001 — a missing statement page is not fatal
                log(f"     statement page {m['statement_url']}: {type(e).__name__}: {e}")
        if t:
            m["statement_time_et"], m["statement_tz"], m["time_basis"] = t[0], t[1], "page"; n_page += 1
        else:
            rt = rule_time(rules, m)
            m["statement_time_et"], m["statement_tz"] = rt, ("ET" if rt else None)
            m["time_basis"] = "rule" if rt else "none"
            n_rule += bool(rt); n_none += not rt
    log(f"     statement times: {n_page} from the page, {n_rule} from rules, {n_none} unknown "
        f"({n_fetched} pages fetched, {n_cached} cached)")

    per_year, val_problems = validate(meetings, from_year, to_year)
    problems += val_problems
    for p in val_problems:
        log(f"FAIL validation: {p}")
    if not meetings:
        problems.append("no meetings parsed")

    payload = {
        "source": BASE_URL, "spec_item": "FOMC calendar (Two Auctions a Day)",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "from_year": from_year, "to_year": to_year,
        "n_meetings": len(meetings), "n_scheduled": sum(m["scheduled"] for m in meetings),
        "n_unscheduled": sum(not m["scheduled"] for m in meetings),
        "per_year": {str(y): per_year[y] for y in sorted(per_year)},
        "statement_time_rules": rules,
        "meetings": meetings,
    }
    _write(os.path.join(DATA_DIR, "fomc.json"), payload)
    log(f"OK   fomc.json: {len(meetings)} meetings {from_year}..{to_year} "
        f"({payload['n_scheduled']} scheduled, {payload['n_unscheduled']} unscheduled)")

    all_ok = bool(results) and all(r["ok"] for r in results) and not problems
    manifest = {
        "vendor": "federalreserve.gov (public HTML, free)",
        "spec": "Data Acquisition Specification — FINAL v1.2",
        "spec_items": ["FOMC calendar"],
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "from_year": from_year, "to_year": to_year,
        "n_meetings": len(meetings), "per_year": payload["per_year"],
        "statements": {"fetched": n_fetched, "cached": n_cached, "time_from_page": n_page, "time_from_rule": n_rule, "time_unknown": n_none},
        "ok": all_ok, "problems": problems, "results": results,
    }
    os.makedirs(DATA_DIR, exist_ok=True)
    _write(os.path.join(DATA_DIR, "_run.json"), manifest)
    if not all_ok:
        log(f"FAILED — error log: {_persist_log('error', manifest)}")
        return 1
    if STORE_LOGS:
        log(f"run log: {_persist_log('run', manifest)}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        try:
            _LOG_LINES.append(traceback.format_exc())
            _persist_log("crash")
        finally:
            traceback.print_exc()
        sys.exit(1)
