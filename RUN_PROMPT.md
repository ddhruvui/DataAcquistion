# Run prompt — nightly fetch

Paste one of these into Claude Code from this repo's root. The `daily-fetch` skill
(`.claude/skills/daily-fetch/`) carries the ordering rules and verification steps, so the
prompt itself can stay short. This repo only fills the RunPod network volume; the models
that read it live in the `InvestOpediaClaude` repo and have their own run prompt there.

## The one to use

> Run launch all and keep monitoring. Make sure the downloads from every service complete,
> verify them, and confirm post rebuilt the m1 tables. While the pods run, watch for errors
> and for pods stuck in a bad state. Do the needful.

Best fired **after 21:00 UTC** (17:00 ET). EODHD publishes the bulk day-file around 23:30 UTC,
so later in the evening is safer, not worse. (The fetcher re-pulls the trailing few sessions
each night, so an early or light pull self-heals the next night — but the models price against
tonight's pull, so launch late anyway. The same-night file always runs ~12% light: late fund-NAV
series, no equity names.)

## Variants

**Resume after a failure:**

> The fetch failed partway last night. Work out which vendor or stage broke, fix it, and carry
> the run through to a rebuilt m1. Tell me what actually failed and why.

**Just check on it:**

> Check the fetch: what pods are running, how far along are they, and is anything stuck?

**Post failed, data is fine:**

> The vendor manifests are fresh but post did not rebuild m1. Rerun validate then m1 and
> verify the manifest.

## What "done" looks like

Ask for these back, and treat anything missing as not-done:

- Per-vendor `jobs / ok / fail` — **fail must be 0** across every tree (watchlists included)
- The session that just closed present in `data/eod_bulk/US/`
- `validate exit=0` **and** `build_m1 exit=0`, with an `m1/_manifest.json` from this run
- No pod left running except, possibly, `intraday` (its multi-hour top-up is expected)

A pod exiting is not evidence a stage succeeded — a stage can be OOM-killed (`exit=-9`) while
its pod still self-terminates normally and leaves yesterday's output in place. Ask for exit
codes, not "it finished".

## Minute bars (`intraday-pull` skill)

The 1-minute intraday store (`data/tickdata/` on the volume; every stock we hold — the S&P list, the
data-only watchlist, the research names — plus SPY/QQQ, ~519 symbols, extended hours, from 2004) has
its own skill, `.claude/skills/intraday-pull/`. It is append-only (existing bars are never
rewritten) and the pod grows the volume 1 GB at a time when free space drops under 5 GB. The nightly
`launch all` tops it up; use the prompt below for a first backfill, after a universe change, or
whenever the intraday pod reported `fetch=75` (space) or `fetch=1` (failures).

**The one to use:**

> Run the intraday pull: launch the minute-bar fetcher on RunPod, monitor it, grow the volume by
> 1 GB if it runs out of space, verify the downloaded data, and make sure the pod is gone.

**Just check on it:**

> How far is the minute-bar backfill? Verify the intraday store and tell me what is missing.

**Done means:** `fetch=0`, the pod gone, the runner's append-only check with 0 lost rows and 0 shrunk
files, and the verifier printing `VERIFIED: minute store consistent — 519 symbols …`. While a new
batch of names is still backfilling (a cold symbol is ~350 credits, so ~400 new names take two
nights of credits), exit 5 `NOT FINISHED (consistent so far)` is the expected answer.

## Not included

`market`, `predict`, the MongoDB publish, and the `stage1`–`stage3` research stages are the
`InvestOpediaClaude` repo's job. Run them from there once this repo reports the volume current.
