# Daily use

## Every night: one command

After 21:00 UTC (best ~22:00 UTC / 18:00 ET):

```sh
caffeinate -i scripts/daily.sh
```

Leave it running until it prints `DONE — the volume is current` (~1.5 h). It downloads every
vendor, then validates and rebuilds `m1/` — the tables the models read. `caffeinate` keeps the
Mac awake; if the Mac sleeps, the run stalls.

Then run the models from the InvestOpediaClaude repo.

> Don't use `scripts/launch.sh all` by itself. It downloads but never rebuilds `m1/`, so the
> models refuse to run the next morning.

Or ask Claude: **"run the daily fetch"**.

## If something goes wrong

| You see | Do this |
|---|---|
| `FATAL: m1 was not rebuilt` | `scripts/launch.sh validate`, wait for it to finish, then `scripts/launch.sh m1` |
| You ran `launch.sh all` without `post` | Same as above, once every vendor pod has finished |
| A pod is still up after its job ended | `scripts/reap_pods.sh` |
| intraday `fetch=1`, only "09:30 bar open > 10 bps" | Ignore: known false alarm, the minute bars are fine |
| borrow "allowance spent … after 2026-10-01" | Normal: the iBorrowDesk backfill resumes on the 1st |

Check a pod's log (works after the pod is gone): `.claude/skills/daily-fetch/scripts/podlog <job>.py 30`

## Other commands

```sh
scripts/launch.sh <vendor>   # one vendor: eodhd nasdaq tiingo borrow calendar finbert intraday fomc
scripts/download.sh          # copy the whole volume to this repo
scripts/download.sh data/calendar/US.json   # ...or just one file / folder
scripts/storage_usage.sh     # what's on the volume
scripts/killpod.sh           # kill EVERY pod — not mid-run
```

## One-time setup

`cp runpod/.env.example runpod/.env` and fill in the keys. Everything else — vendors, datasets,
storage layout, vendor quirks — is in [README.md](README.md).
