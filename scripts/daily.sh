#!/usr/bin/env bash
# THE one-command nightly fetch. Sequences the DATA half of the day on self-terminating
# pods, then stops. The modelling lives in the InvestOpediaClaude repo, whose own
# scripts/daily.sh picks up from the volume this leaves behind — nothing here knows
# what reads the data.
#
#   1. vendor fetch (launch.sh all: eodhd nasdaq tiingo borrow calendar finbert intraday fomc)
#      + post (validate -> build_m1), which self-sequences on tonight's vendor manifests
#   2. wait for post, and trust it only if m1/_manifest.json was rebuilt TODAY
#   3. verify every vendor manifest is fresh with zero hard failures
#
# The hand-off is the volume: data/ data_nasdaq/ data_tiingo/ data_borrow/ data_calendar/
# data_finbert/ data/tickdata/ data_fomc/ (raw vendor trees) and m1/ (the landing-layer tables).
#
# A REAPER runs in the background for the whole run: pods cannot be trusted to delete
# themselves (see reap_pods.sh), a survivor RE-RUNS its fetcher every ~5 min, and it
# blocks the next night's launch as "already running".
#
#   scripts/daily.sh                    # start ~22:00 UTC (18:00 ET); EODHD's bulk
#                                       # day-file lands ~23:30 UTC
#   SKIP_TIME_CHECK=1 scripts/daily.sh  # allow a launch before 21:00 UTC
#   NO_REAPER=1 scripts/daily.sh        # do not reap finished pods (debugging)
#   DRY_RUN=1 scripts/daily.sh          # print the launches, touch nothing
set -u
cd "$(dirname "$0")/.."
source scripts/_common.sh
say() { echo "[$(date -u +%H:%M:%SZ)] daily: $*"; }

# --reap-failed because a failed pod that stays up BLOCKS its own relaunch (launch.sh
# skips a vendor whose pod name is already running).
REAPER_PID=""
stop_reaper() { [ -n "$REAPER_PID" ] && kill "$REAPER_PID" 2>/dev/null; return 0; }
trap stop_reaper EXIT INT TERM
if [ -z "${NO_REAPER:-}" ] && [ -z "${DRY_RUN:-}" ]; then
  scripts/reap_pods.sh --watch --reap-failed &
  REAPER_PID=$!
  say "0/3 reaper: watching pods (pid $REAPER_PID) — finished pods are deleted from here"
fi

# EODHD's bulk day-file must not be pulled mid-session: tonight's book prices against
# tonight's pull (the trailing re-pull only heals it the NEXT night).
if [ -z "${SKIP_TIME_CHECK:-}" ] && [ "$(date -u +%H)" -lt 21 ]; then
  echo "!! before 21:00 UTC — the EODHD day-file may be mid-session." >&2
  echo "   SKIP_TIME_CHECK=1 to override." >&2
  exit 2
fi

# Freshness floor for the final check: launch time minus the same 10-min clock-skew
# grace post.py applies to its own gate. (macOS `date -v`, GNU `date -d` fallback.)
FLOOR=$(date -u -v-10M +%Y-%m-%dT%H:%M:%S+00:00 2>/dev/null \
     || date -u -d '-10 min' +%Y-%m-%dT%H:%M:%S+00:00)

say "1/3 fetch: vendor pods + post (post waits for tonight's manifests itself)"
scripts/launch.sh all
scripts/launch.sh post
if [ -n "${DRY_RUN:-}" ]; then say "DRY_RUN: skipping the post wait and verification"; exit 0; fi

say "2/3 post: waiting for validate -> build_m1"
# Trust post only if m1/_manifest.json was written AFTER this launch. Compared as local
# "YYYY-MM-DD HH:MM:SS" strings because `aws s3 ls` prints LastModified in local time —
# the old "dated today (UTC)" test failed whenever post finished after 00:00 UTC, which a
# ~22:00 UTC launch does most nights (2026-09-16: post finished 03:57 UTC).
LAUNCH_LOCAL=$(date +"%Y-%m-%d %H:%M:%S")
ok=""
for i in $(seq 1 90); do    # poll 5-min, up to 7.5 h
  sleep 300
  PODS=$(curl -sS --max-time 30 https://rest.runpod.io/v1/pods \
    -H "Authorization: Bearer ${RUNPOD_API_KEY}" 2>/dev/null) || \
    { say "WARN: pods API unreachable — retrying"; continue; }
  if printf '%s' "$PODS" | grep -q "investopediaclaude-post"; then
    say "post still running"; continue
  fi
  # post pod gone -> only trust it if this run's m1 build actually landed
  MSTAMP=$(aws s3 ls $S3FLAGS "$BUCKET/m1/_manifest.json" 2>/dev/null | awk '{print $1" "$2}')
  if [ -n "$MSTAMP" ] && [ "$MSTAMP" \> "$LAUNCH_LOCAL" ]; then
    say "post done — m1 rebuilt at $MSTAMP (launch $LAUNCH_LOCAL)"; ok=1; break
  fi
  say "post pod gone but m1 manifest is '${MSTAMP:-missing}' (launch $LAUNCH_LOCAL) — waiting/retrying"
done
[ -n "$ok" ] || { say "FATAL: m1 was not rebuilt today — read: .claude/skills/daily-fetch/scripts/podlog post.py 30"; exit 1; }

say "3/3 verify: every vendor manifest fresh (newer than $FLOOR), zero hard failures"
rc=0; python3 .claude/skills/daily-fetch/scripts/verify_fetch.py "$FLOOR" || rc=$?

# One last reaper pass so this never returns with a finished pod still billing. Any pod
# it reports as still running (typically intraday's multi-hour top-up) is left alone.
[ -z "${NO_REAPER:-}" ] && scripts/reap_pods.sh --reap-failed || true

if [ "$rc" = "0" ]; then
  say "DONE — the volume is current. InvestOpediaClaude's scripts/daily.sh can now run market -> predict."
else
  say "FATAL: a vendor tree is stale or has hard failures (see above) — fix before the models run"
  exit 1
fi
