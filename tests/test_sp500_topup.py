"""SP500 must top up a trailing window instead of re-pulling the whole table.

2026-10-16: moving to the 5-year Sharadar bundle. The whole-table pull (date.gte=1957) comes back
short on that plan and the snapshot shrink guard refuses it every night, freezing the constituents.
_sp500_topup re-pulls ~a quarter and splices it onto the held history.
"""
import importlib.util
import os
import sys

import pytest

SRC = os.path.join(os.path.dirname(__file__), "..", "src", "fetch_nasdaq.py")


def _load():
    spec = importlib.util.spec_from_file_location("fetch_nasdaq_sp500_under_test", SRC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


fn = _load()


def _row(date, action, ticker):
    return {"date": date, "action": action, "ticker": ticker, "name": ticker,
            "contraticker": None, "contraname": None, "note": None}


def _held():
    rows = [_row("1998-03-31", "historical", f"OLD{i}") for i in range(50)]
    rows += [_row("2026-06-30", "historical", f"T{i}") for i in range(50)]
    rows += [_row("2026-09-30", "historical", f"T{i}") for i in range(50)]
    rows += [_row("2026-10-06", "added", "NEWCO"), _row("2026-10-06", "removed", "T49")]
    rows += [_row("2026-10-13", "current", f"T{i}") for i in range(49)] + [_row("2026-10-13", "current", "NEWCO")]
    return rows


def _vendor(held, cut, extra=()):
    """What the API returns for date.gte=cut the next day: the window, re-dated current, new rows."""
    win = [r for r in held if r["action"] != "current" and r["date"] >= cut]
    cur = [dict(r, date="2026-10-14") for r in held if r["action"] == "current"]
    return win + cur + list(extra)


def test_topup_replaces_current_and_keeps_deep_history(monkeypatch):
    held = _held()
    seen = {}

    def fake_fetch(endpoint, params, label=None):
        seen.update(params)
        return _vendor(held, params["date.gte"], [_row("2026-10-14", "added", "XYZ")]), False

    monkeypatch.setattr(fn, "_fetch", fake_fetch)
    rows, added = fn._sp500_topup("sp500", held)

    assert seen["date.gte"] == "2026-06-08"                   # newest event 10-06 minus 120 days
    cur = [r for r in rows if r["action"] == "current"]
    assert len(cur) == 50 and {r["date"] for r in cur} == {"2026-10-14"}   # replaced, not stacked
    assert sum(r["date"] == "1998-03-31" for r in rows) == 50              # deep history kept
    assert any(r["ticker"] == "XYZ" for r in rows)
    assert added == 51                                        # 50 re-dated current + XYZ
    assert len(rows) == len(held) + 1


def test_topup_is_idempotent(monkeypatch):
    held = _held()
    monkeypatch.setattr(fn, "_fetch", lambda e, p, label=None: (_vendor(held, p["date.gte"]), False))
    once, _ = fn._sp500_topup("sp500", held)
    monkeypatch.setattr(fn, "_fetch", lambda e, p, label=None: (_vendor(once, p["date.gte"]), False))
    twice, added = fn._sp500_topup("sp500", once)
    assert len(twice) == len(once)
    assert added == 0                                         # same day: nothing new


@pytest.mark.parametrize("what", ["current", "window"])
def test_short_response_refused(monkeypatch, what):
    held = _held()

    def short(endpoint, params, label=None):
        rows = _vendor(held, params["date.gte"])
        drop = (lambda r: r["action"] == "current") if what == "current" else (lambda r: r["action"] != "current")
        return [r for r in rows if not drop(r)], False

    monkeypatch.setattr(fn, "_fetch", short)
    with pytest.raises(RuntimeError, match="history kept"):
        fn._sp500_topup("sp500", held)
