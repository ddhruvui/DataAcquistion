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
# --relaunch makes the reaper RETRY a job the OOM-killer took, on a 16 GB pod, once. That is
# the one failure this run can fix without a human (nasdaq 2026-09-17 and 2026-09-22,
# build_m1 2026-09-17) and it lands well inside post's 240-min manifest gate, so a retried
# vendor still makes it into tonight's build. See reap_pods.sh's header for what is NOT retried.
REAPER_PID=""
stop_reaper() { [ -n "$REAPER_PID" ] && kill "$REAPER_PID" 2>/dev/null; return 0; }
trap stop_reaper EXIT INT TERM
if [ -z "${NO_REAPER:-}" ] && [ -z "${DRY_RUN:-}" ]; then
  scripts/reap_pods.sh --watch --reap-failed --relaunch &
  REAPER_PID=$!
  say "0/3 reaper: watching pods (pid $REAPER_PID) — finished pods are deleted, OOM-killed ones retried"
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
gone=0            # consecutive polls with no post pod AND a stale manifest
recovered=""      # the validate -> m1 rescue has been tried (once)
for i in $(seq 1 90); do    # poll 5-min, up to 7.5 h
  sleep 300
  PODS=$(curl -sS --max-time 30 https://rest.runpod.io/v1/pods \
    -H "Authorization: Bearer ${RUNPOD_API_KEY}" 2>/dev/null) || \
    { say "WARN: pods API unreachable — retrying"; continue; }
  if printf '%s' "$PODS" | grep -qE "investopediaclaude-(post|validate|m1)"; then
    say "post still running"; gone=0; continue
  fi
  # post pod gone -> only trust it if this run's m1 build actually landed
  MSTAMP=$(aws s3 ls $S3FLAGS "$BUCKET/m1/_manifest.json" 2>/dev/null | awk '{print $1" "$2}')
  if [ -n "$MSTAMP" ] && [ "$MSTAMP" \> "$LAUNCH_LOCAL" ]; then
    say "post done — m1 rebuilt at $MSTAMP (launch $LAUNCH_LOCAL)"; ok=1; break
  fi
  # No post pod and no fresh manifest means nothing is going to produce one: this used to
  # sit here re-polling for the full 7.5 h and then report FATAL, which is the shape of a
  # hang rather than a failure. Rescue it the way the runbook says to by hand — validate
  # then m1, in that order (build_m1 consumes validate's quarantine.json), neither of which
  # carries post's launch-time manifest gate. Once; a second miss is a real failure.
  # Two consecutive polls first, so S3 listing lag right after post exits is not mistaken
  # for a dead build.
  gone=$((gone + 1))
  say "post pod gone but m1 manifest is '${MSTAMP:-missing}' (launch $LAUNCH_LOCAL) [$gone/2]"
  if [ "$gone" -ge 2 ] && [ -z "$recovered" ]; then
    recovered=1
    say "RECOVERING: post produced no fresh m1 — relaunching validate -> m1"
    scripts/launch.sh validate || say "WARN: validate relaunch failed to place"
    scripts/launch.sh m1       || say "WARN: m1 relaunch failed to place"
    gone=0
  elif [ "$gone" -ge 2 ]; then
    say "FATAL: the validate -> m1 rescue ALSO left m1 stale — this is not a transient failure"
    break
  fi
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
