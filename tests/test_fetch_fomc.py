"""fetch_fomc.py parses both federalreserve.gov page formats, reads the statement time, and
refuses a year without eight scheduled meetings. Offline: the fixtures are trimmed copies of the
real markup (calendars page 2026 rows, historical pages 2008/2020/2023)."""
import importlib.util
import json
import os
import sys

import pytest

HERE = os.path.dirname(__file__)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, "..", "src", path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


ff = _load("fetch_fomc_under_test", "fetch_fomc.py")

CAL_ROW = '''
<div class="{cls}row fomc-meeting" ">
  <div class="fomc-meeting__month col-xs-5"><strong>{month}</strong></div>
  <div class="fomc-meeting__date col-xs-4">{day}</div>
  <div class="col-xs-12"><strong>Statement:</strong><br>
     <a href="/monetarypolicy/files/monetary{ymd}a1.pdf">PDF</a> | <a href="/newsevents/pressreleases/monetary{ymd}a.htm">HTML</a><br></div>
  <div class="col-xs-12"><a href="/monetarypolicy/fomcpresconf{ymd}.htm">Press Conference</a><br>{sep}</div>
  <div class="col-xs-12 fomc-meeting__minutes"><strong>Minutes:</strong><br>
     <a href="/monetarypolicy/files/fomcminutes{ymd}.pdf">PDF</a> | <a href="/monetarypolicy/fomcminutes{ymd}.htm">HTML</a></div>
</div>'''
SEP = '<strong>Projection Materials</strong><br><a href="/monetarypolicy/files/fomcprojtabl{ymd}.pdf">PDF</a>'


def cal_page(years):
    out = ""
    for y, rows in years.items():
        out += f'<div class="panel panel-default"><div class="panel-heading"><h4><a id="1">{y} FOMC Meetings</a></h4></div>'
        for month, day, ymd, sep in rows:
            out += CAL_ROW.format(cls="", month=month, day=day, ymd=ymd, sep=SEP.format(ymd=ymd) if sep else "")
        out += "</div>"
    return out


HIST_PANEL = '''<div class="panel panel-default"><div class="panel-heading"><h5>{title}</h5></div>
<div class="row"><div class="col-xs-12"><p><a href="{stmt}">Statement</a></p>{extra}
<p><a href="/monetarypolicy/fomcminutes{ymd}.htm">Minutes</a> (Released later)</p></div></div></div>'''


def test_calendars_page_rows_normal_sep_two_month_and_notation_vote():
    page = cal_page({2026: [("January", "27-28", "20260128", False), ("March", "17-18*", "20260318", True)],
                     2023: [("Oct/Nov", "31-1", "20231101", False), ("August", "22 (notation vote)", "20230822", False)]})
    cal = ff.parse_calendars(page)
    assert sorted(cal) == [2023, 2026]
    jan, mar = cal[2026]
    assert (jan["start_date"], jan["end_date"], jan["scheduled"], jan["sep"]) == ("2026-01-27", "2026-01-28", True, False)
    assert jan["statement_url"] == "/newsevents/pressreleases/monetary20260128a.htm"
    assert jan["statement_date"] == "2026-01-28" and jan["press_conference"]
    assert jan["minutes_url"] == "/monetarypolicy/fomcminutes20260128.htm"
    assert (mar["sep"], mar["end_date"]) == (True, "2026-03-18")
    octnov, notation = cal[2023]
    assert (octnov["start_date"], octnov["end_date"]) == ("2023-10-31", "2023-11-01")
    assert (notation["kind"], notation["scheduled"], notation["end_date"]) == ("notation_vote", False, "2023-08-22")


def test_historical_page_meetings_conference_calls_unscheduled_and_two_month():
    page = (HIST_PANEL.format(title="January 21 Conference Call - 2008", stmt="/newsevents/press/monetary/20080122b.htm", ymd="20080130", extra="")
            + HIST_PANEL.format(title="January 29-30 Meeting - 2008", stmt="/newsevents/press/monetary/20080130a.htm", ymd="20080130",
                                extra='<p><a href="/monetarypolicy/files/FOMC20080130SEPcompilation.pdf">SEP</a></p>')
            + '<h5 class="panel-heading">FOMC Search</h5><div>not a meeting</div>'
            + HIST_PANEL.format(title="March 15 (unscheduled) Meeting - 2020", stmt="/newsevents/pressreleases/monetary20200315a.htm", ymd="20200315", extra="")
            + HIST_PANEL.format(title="October 31-November 1 Meeting - 2017", stmt="/newsevents/pressreleases/monetary20171101a.htm", ymd="20171101", extra="")
            + HIST_PANEL.format(title="April/May 30-1 Meeting - 2013", stmt="/newsevents/pressreleases/monetary20130501a.htm", ymd="20130501", extra="")
            + HIST_PANEL.format(title="October 16 (unscheduled) - 2013", stmt="/newsevents/pressreleases/monetary20131016a.htm", ymd="20131016", extra="")
            + HIST_PANEL.format(title="March 17-18 (cancelled) Meeting - 2020", stmt="", ymd="20200318", extra="")
            + HIST_PANEL.format(title="March 19 (notation vote) - 2020", stmt="/newsevents/pressreleases/monetary20200319b.htm", ymd="20200319", extra=""))
    ms = ff.parse_historical(page, 2008)
    assert [m["label"] for m in ms] == ["January 21 Conference Call - 2008", "January 29-30 Meeting - 2008",
                                        "March 15 (unscheduled) Meeting - 2020", "October 31-November 1 Meeting - 2017",
                                        "April/May 30-1 Meeting - 2013", "October 16 (unscheduled) - 2013",
                                        "March 17-18 (cancelled) Meeting - 2020", "March 19 (notation vote) - 2020"]
    call, meet, unsched, octnov, aprmay, oct16, cancelled, notation = ms
    assert (aprmay["start_date"], aprmay["end_date"], aprmay["scheduled"]) == ("2008-04-30", "2008-05-01", True)
    assert (oct16["kind"], oct16["scheduled"], oct16["end_date"]) == ("meeting", False, "2008-10-16")
    assert (cancelled["kind"], cancelled["scheduled"], cancelled["start_date"], cancelled["end_date"]) == ("cancelled", False, "2008-03-17", "2008-03-18")
    assert (notation["kind"], notation["scheduled"]) == ("notation_vote", False)
    assert (call["kind"], call["scheduled"], call["end_date"], call["statement_date"]) == ("conference_call", False, "2008-01-21", "2008-01-22")
    assert (meet["kind"], meet["scheduled"], meet["sep"], meet["start_date"], meet["end_date"]) == ("meeting", True, True, "2008-01-29", "2008-01-30")
    assert (unsched["kind"], unsched["scheduled"]) == ("meeting", False)
    assert (octnov["start_date"], octnov["end_date"]) == ("2008-10-31", "2008-11-01")   # the year comes from the page requested


def test_statement_time_from_page_or_rules():
    assert ff.parse_statement_time("<p>For release at 2:00 p.m. EST</p>") == ("14:00", "EST")
    assert ff.parse_statement_time("<p>For release at&nbsp;10:00 a.m. EDT</p>") == ("10:00", "EDT")
    assert ff.parse_statement_time("<p>For immediate release</p>") is None
    rules = json.load(open(os.path.join(HERE, "..", "config", "fomc.json")))["statement_time_rules"]
    sched = {"scheduled": True, "end_date": "2004-06-30", "statement_date": "2004-06-30", "press_conference": False}
    assert ff.rule_time(rules, sched) == "14:15"
    assert ff.rule_time(rules, dict(sched, statement_date="2011-04-27", press_conference=True)) == "12:30"
    assert ff.rule_time(rules, dict(sched, statement_date="2011-06-22", press_conference=False)) == "14:15"
    assert ff.rule_time(rules, dict(sched, statement_date="2013-01-30", press_conference=False)) == "14:00"
    assert ff.rule_time(rules, dict(sched, scheduled=False)) is None       # unscheduled: no rule applies


def test_validation_requires_eight_scheduled_meetings_a_year():
    ms = [{"end_date": f"2010-{m:02d}-15", "scheduled": True, "kind": "meeting"} for m in (1, 3, 4, 6, 8, 9, 11, 12)]
    ms.append({"end_date": "2010-05-09", "scheduled": False, "kind": "conference_call"})
    ms += [{"end_date": f"2011-{m:02d}-15", "scheduled": True, "kind": "meeting"} for m in (1, 3, 4, 6, 8, 9, 11)]
    ms.append({"end_date": "2011-12-14", "scheduled": False, "kind": "cancelled"})   # a cancelled slot still counts
    per_year, problems = ff.validate(ms, 2010, 2012)
    assert per_year[2010] == {"scheduled": 8, "unscheduled": 1, "cancelled": 0}
    assert per_year[2011] == {"scheduled": 7, "unscheduled": 0, "cancelled": 1}
    assert problems == ["2012: 0 scheduled meetings (expected 8)"]


def test_end_to_end_writes_payload_manifest_and_caches_statement_pages(tmp_path, monkeypatch):
    monkeypatch.setattr(ff, "DATA_DIR", str(tmp_path / "data_fomc"))
    monkeypatch.setattr(ff, "CONFIG_PATH", str(tmp_path / "fomc.json"))
    (tmp_path / "fomc.json").write_text(json.dumps({"from_year": 2025, "request_pause_sec": 0,
        "statement_time_rules": [{"from": "2013-01-30", "time_et": "14:00"}]}))
    months = [("January", "28-29", "20250129"), ("March", "18-19*", "20250319"), ("May", "6-7", "20250507"), ("June", "17-18*", "20250618"),
              ("July", "29-30", "20250730"), ("September", "16-17*", "20250917"), ("Oct", "28-29", "20251029"), ("December", "9-10*", "20251210")]
    cal = cal_page({2025: [(m, d, ymd, "*" in d) for m, d, ymd in months],
                    2026: [(m, d, ymd.replace("2025", "2026"), "*" in d) for m, d, ymd in months]})
    calls = []
    def fake_get(url, **kw):
        calls.append(url)
        if url.endswith("fomccalendars.htm"):
            return cal
        if "fomchistorical" in url:
            raise ff.urllib.error.HTTPError(url, 404, "not yet", {}, None)
        if "pressreleases/monetary" in url:
            return "<html>For release at 2:00 p.m. EDT</html>"
        raise AssertionError(url)
    monkeypatch.setattr(ff, "http_get", fake_get)
    assert ff.main() == 0
    out = json.load(open(tmp_path / "data_fomc" / "fomc.json"))
    assert out["n_meetings"] == 16 and out["n_scheduled"] == 16 and out["per_year"] == {"2025": {"scheduled": 8, "unscheduled": 0, "cancelled": 0}, "2026": {"scheduled": 8, "unscheduled": 0, "cancelled": 0}}
    assert out["meetings"][0]["statement_time_et"] == "14:00" and out["meetings"][0]["time_basis"] == "page"
    assert [m["end_date"] for m in out["meetings"]][:3] == ["2025-01-29", "2025-03-19", "2025-05-07"]
    run = json.load(open(tmp_path / "data_fomc" / "_run.json"))
    assert run["ok"] and run["statements"] == {"fetched": 16, "cached": 0, "time_from_page": 16, "time_from_rule": 0, "time_unknown": 0}
    n_first = len(calls)
    assert ff.main() == 0                                   # second run: statement pages come from raw/statements/
    assert len(calls) == n_first + 3                        # calendars page + two 404 historical probes only
    assert json.load(open(tmp_path / "data_fomc" / "_run.json"))["statements"]["cached"] == 16


def test_a_year_short_of_eight_meetings_fails_the_run(tmp_path, monkeypatch):
    monkeypatch.setattr(ff, "DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setattr(ff, "CONFIG_PATH", str(tmp_path / "cfg.json"))
    (tmp_path / "cfg.json").write_text(json.dumps({"from_year": 2026, "request_pause_sec": 0, "fetch_statement_pages": False}))
    cal = cal_page({2026: [("January", "27-28", "20260128", False)]})
    monkeypatch.setattr(ff, "http_get", lambda url, **kw: cal if url.endswith("fomccalendars.htm")
                        else (_ for _ in ()).throw(ff.urllib.error.HTTPError(url, 404, "x", {}, None)))
    assert ff.main() == 1
    run = json.load(open(tmp_path / "d" / "_run.json"))
    assert run["ok"] is False and run["problems"] == ["2026: 1 scheduled meetings (expected 8)"]
