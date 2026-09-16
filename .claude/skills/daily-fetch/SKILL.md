---
name: daily-fetch
description: Run and monitor the nightly vendor download on RunPod end to end — EODHD, Sharadar, Tiingo, borrow, calendar, FinBERT and the minute-bar top-up, then post (validate -> build_m1) — and verify every tree actually landed on the network volume. Use this whenever the user asks to run the daily fetch, "launch all", "run launch all and monitor", refresh the data, rebuild m1, check on running pods, or asks whether the downloads finished; and whenever a fetch needs babysitting, a pod looks stuck, or post failed and needs resuming. Prefer this over improvising with launch.sh directly, because the ordering constraints and the "looks finished but silently half-built" failure modes are not visible from the scripts themselves. The models (market/predict) live in the InvestOpediaClaude repo and are NOT run from here.
---

# Daily fetch: run and monitor

This repo owns the DATA half of the day: it fills the RunPod network volume and stops. The
modelling half (market → predict → publish) lives in the `InvestOpediaClaude` repo and reads
the volume this leaves behind; nothing here calls it. Your job is to launch the fetch, watch
it, **verify each stage actually did its work**, and report the state of the volume. The
recurring danger is not loud failure — it is a stage that exits, self-terminates, and leaves
stale output behind for the models to consume.

Bundled helpers (all read `runpod/.env`). They live in `.claude/skills/daily-fetch/scripts/` —
NOT the repo-root `scripts/` — so always call them by that full path
(`SK=.claude/skills/daily-fetch/scripts` and `$SK/pods` works well):

| script | what it does |
|---|---|
| `pods` | current pods, one per line; empty means none running |
| `vol` | `aws s3` against the volume — `vol ls data/…`, `vol cp data/_run.json /tmp/x --quiet` (note: `_pod_logs/` sits at the volume ROOT, not under `data/`) |
| `podlog <pattern> [n]` | newest matching `_pod_logs/` entry, tailed; works after the pod is gone |
| `verify_fetch.py [FLOOR_ISO]` | per-vendor manifest check; exit 0 only if all fresh and zero hard failures |
| `watch_pods.py` | change-only watchdog: `UP` / `DONE` / `STALL` / `IDLE` |

Reaping is a repo-root script, not one of these: `scripts/reap_pods.sh` deletes a pod once
that pod's OWN log shows its job finished. `scripts/daily.sh` runs it in the background for
the whole run, so a hand-driven launch is the case that needs it — start `reap_pods.sh --watch`
alongside the watchdog, or run it bare for a one-pass status. Do NOT use `killpod.sh` mid-run:
it kills every pod, including ones still working.

## The one-command form

```sh
scripts/daily.sh
```

does everything below (launch `all` + `post`, wait for post, verify the manifests) and ends
with `DONE — the volume is current`. The sections that follow are for driving or debugging it
by hand.

## Before launching

Check these — each has burned a real run:

1. **Time.** Launch after 21:00 UTC. EODHD publishes the bulk day-file around 23:30 UTC.
   The fetcher re-pulls the trailing few sessions every night (EODHD mutates recent day-files),
   so a same-night pull is not frozen forever — but it IS what the book prices against, so
   launching late still matters. Expect the same-night file ~12% lighter than its neighbors
   (~44k rows vs ~50k): the gap is late-publishing fund-NAV series (`0P…` codes), zero equity
   names, and the next night's re-pull tops it up. In the fetch log, `+N day-files … 1 deferred`
   is normal: the deferred one is the not-yet-published next session (or the 1999-12-31
   backfill boundary), and the deep-history backfill is complete (`~1999-12-31+ remaining`).
2. **No pods already running.** `$SK/pods`. A vendor whose pod is up is skipped, not
   doubled, so a stray pod silently means that vendor does not refresh.
3. **Baseline the volume** so you can prove movement later: newest `data/eod_bulk/US/*.json`,
   and the `_run.json` timestamp of each vendor tree. Note the newest day-file — the run must
   add the session that just closed.
4. **Sleep.** On macOS, sleeping suspends every background loop you start (a run once lost
   8.5 h this way). Hold the machine awake for the run only, tied to your chain's pid:
   `caffeinate -dims -w <pid> &`.

## Launch

```sh
scripts/launch.sh all && scripts/launch.sh post
```

Fire `post` immediately after `all`, never later. `post` waits for vendor manifests **newer
than its own launch** (minus a 10-min grace), so a `post` started after the fetchers finish
waits out its full 240-min timeout and then builds anyway with recorded gaps.

`launch.sh` verifies each pod actually started (its bootstrap log appears on the volume) and
retries a dead placement once. If EU-RO-1 has no CPU, it falls back automatically to the
cheapest available GPU — expect lines like `placed on: GPU NVIDIA RTX A4500`. That is normal
and correct; the job still runs the CPU image.

## Monitor

Start the watchdog and leave it attached to a Monitor — it only speaks on state changes:

```sh
POLL=180 STALL_CHECKS=5 python3 .claude/skills/daily-fetch/scripts/watch_pods.py
```

While pods run, check progress with `$SK/podlog`. Rough shape of a warm run: calendar and
finbert finish in seconds; nasdaq ~10 min; borrow ~30 min; tiingo ~35 min; **eodhd ~70-80 min**
and is always the long pole; `post` then takes ~7 min once its gate clears. `intraday` is the
exception: it can run for hours (credit- and time-capped), post does not wait for it, and
its own runbook is the `intraday-pull` skill.

A `STALL` line means the pod is alive but its log has not grown — read the log before acting;
it may be a slow vendor rather than a hang. `post` is deliberately exempt, because it prints
its gate line once and then polls in silence for as long as the fetchers take.

A pod that stays up AFTER its log ends in `fetch=<rc>` has failed to delete itself. That is
not cosmetic: RunPod relaunches the container, so the job RE-RUNS every ~5 min (calendar and
finbert each ran 5x on 2026-09-09, burning vendor calls), and the next night `launch.sh` skips
that vendor as "already running". Reap it — `scripts/reap_pods.sh` — rather than waiting it out.

## Verify the downloads

Do not treat "pod gone" as success. Run:

```sh
python3 .claude/skills/daily-fetch/scripts/verify_fetch.py <launch-ISO-minus-10min>
```

Every tree must be `FRESH` with `fail=0`, and `STALE`/`HARD FAILURES` must both be none.
Then confirm the session that just closed actually landed:

```sh
.claude/skills/daily-fetch/scripts/vol ls data/eod_bulk/US/ | tail -3
```

Tiingo `DEFER` entries are budget deferrals, not failures — they resume next run.

The four `…/watchlist` trees are the data-only watchlist (`config/watchlist_*.json`), fetched
inside the eodhd/nasdaq/tiingo/borrow pods BEFORE their main pass (borrow's is iBorrowDesk
history only — the IBKR snapshot already covers every name) — each of those pod logs shows a
`watchlist=<rc>` line, and a watchlist failure also turns the final `fetch=` nonzero. They must be
FRESH with `fail=0` like the rest, but nothing downstream reads them, so a watchlist failure is
never a reason to hold post. They add ~12 min to the tiingo pod every night (13 symbols
refetched daily at 72 s pacing), so tiingo no longer exits in ~25 s on a skip-fresh night.

`finbert` reporting `added=0` is normal, not a shirked job: it only verifies the pinned
model files (size+sha) are on the volume, so a warm run adds nothing.

**Vendor restatements look like pipeline bugs but aren't.** If build_m1's row counts move by
thousands day-over-day while validate stays green (e.g. `raw_prices_eod` SHRINKING despite a
new session), suspect EODHD restating a single name's history. Diagnose it in one step: diff
the `OK   eod <TICKER>.US: N` lines between the two nights' `fetch.py` pod logs — the ticker
whose count jumped is your answer. Seen 2026-08-28: DD lost its entire pre-2017 (pre-DowDuPont)
tape, −4,444 rows, every other name +0/+1 (restored 2026-09-15). Note it in the report: the
model repo's daily predict (panel tail 2021+) doesn't care, its stage1–3 rerun does.

`borrow` is the one job whose misses are permanent: the IBKR snapshot is a live file with no
history. Confirm its `borrow usa: N rows [snapshot …]` line appears. Its iBorrowDesk half only
refreshes ~80 of 506 names per run, which is by design (each fetch returns a rolling year that
closes the gap), so a large "stale" count there is not an error.

Both borrow passes end with the **iBorrowDesk v2 all-time backfill** (`collect_history_v2`, keyed
by `IBORROWDESK_API_KEY`), which reports under `_run.json` → `history_v2` and never touches `ok`
or `fetch=`. It is breadth-first, so until it completes (~Nov 2026) `allowance spent (0 units left) —
the rest continues on the first run after 2026-10-01` and `N partial` are the EXPECTED state, not
failures: the Patreon allowance is 500 units/month and resets on the 1st. What needs attention is
`FAIL borrow_history_v2` or a `v2 validation: … failing K [...]` with K > 0 (each failing name gets a
`WARN v2 validation` line). Once done it logs `complete for all N names — nothing requested`.
`BORROW_V2_ONLY=1 scripts/launch.sh borrow` runs just that backfill (manifest `_run_history_v2.json`).

## Verify post — the hand-off to the models

`post` runs validate then build_m1 **inside one pod**, so they are not separate pod logs:

```sh
.claude/skills/daily-fetch/scripts/podlog post.py 30
```

Require **three** things, not one: `validate exit=0`, `build_m1 exit=0`, and an `m1/_manifest.json`
whose timestamp is from this run. A negative exit code is a signal death — `exit=-9` is the OOM
killer. It has struck twice (once leaving 3 of 8 m1 tables rewritten, once — 2026-08-26's run —
killing BOTH stages on a 2-vCPU pod) while the pod still terminated normally and the manifest
stayed a day old. `launch.sh` refuses `post`/`m1`/`validate` below 4 vCPU / 8 GB
(`ALLOW_SMALL_POD=1` overrides — don't, unless you accept a possible half-build). If 8 GB ever
OOMs again, relaunch with `RUNPOD_VCPU=8`; the GPU fallback (A4500 = 62 GB) also moots it.

If post failed, rerun `launch.sh validate` then `launch.sh m1` **in that order** (build_m1
consumes validate's quarantine.json; neither has post's launch-time gate) and re-verify all
three conditions above. Until they hold, the models must not run: the model repo's
`scripts/daily.sh` checks `m1/_manifest.json` is dated today before it starts, and refuses otherwise.

**That is the end of this repo's job.** What the models do next — `market`, `predict`, the
MongoDB publish — is the `InvestOpediaClaude` repo (`scripts/daily.sh` there, and its
`daily-pipeline` skill). Do not launch those from here.

## Reporting back

Give the user the evidence, not reassurance: a per-vendor table of jobs/ok/fail, the exit code
of validate and build_m1, whether today's day-file landed, the `m1/_manifest.json` timestamp,
and the newest `eod_bulk` day-file (what the models will price against). If something failed,
say which stage, what the log showed, and what you did about it.
