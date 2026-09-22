#!/usr/bin/env bash
# Unit test for reap_pods.sh's decision logic — the part that decides whether a pod is
# finished and whether it is safe to delete. It extracts the REAL classify() out of
# reap_pods.sh and drives it against fabricated pod logs, so no pod, volume or API is
# touched. Run it after any change to reap_pods.sh:  scripts/test_reap_pods.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
STUB="$(mktemp -d -t reaptest)"
trap 'rm -rf "$STUB"' EXIT

# classify() reads logs through tail_log; point that at files we write instead.
tail_log() { [ -f "$STUB/$1" ] || return 1; cat "$STUB/$1"; }
eval "$(awk '/^classify\(\) \{/,/^\}$/' "$HERE/reap_pods.sh")"
eval "$(awk '/^maybe_relaunch\(\) \{/,/^\}$/' "$HERE/reap_pods.sh")"

PASS=0; FAIL=0
mklog() { printf '%s\n' "$2" > "$STUB/$1"; }          # $1 key, $2 body
t() {  # $1 desc  $2 want  $3 name  $4 id  $5 keys(newline-sep)  [$6 REAP_FAILED]
  local desc="$1" want="$2"
  LOGKEYS="$5"; REAP_FAILED="${6:-}"; VERDICT=""; REASON=""
  classify "$3" "$4"
  if [ "$VERDICT" = "$want" ]; then
    PASS=$((PASS + 1)); printf '  ok   %-50s -> %-4s | %s\n' "$desc" "$VERDICT" "$REASON"
  else
    FAIL=$((FAIL + 1)); printf '  FAIL %-50s -> got %s, want %s | %s\n' "$desc" "$VERDICT" "$want" "$REASON"
  fi
}

DONE0='fetch=0 (exited) at 2026-09-09T00:37:37Z — terminating pod aaa
terminate HTTPError: 403
!! TERMINATION NOT CONFIRMED after retries'
DONE1='fetch=1 (exited) at 2026-09-09T00:37:37Z — terminating pod aaa'
MID='OK   eod    SRE.US: 6710 (+6710) [full] -> /workspace/data/ohlcv/SRE.json'
K1=20260909T003730Z-fetch_calendar.py-aaa.log
K2=20260909T004218Z-fetch_calendar.py-aaa.log

echo "--- fetchers: reaped on any exit code (work is over, logs are on the volume) ---"
mklog "$K1" "$DONE0"; t "fetcher exit 0"                    reap investopediaclaude-calendar aaa "$K1"
mklog "$K1" "$DONE1"; t "fetcher exit 1 — reaped but flagged" reap investopediaclaude-calendar aaa "$K1"
mklog "$K1" 'fetch=124 (WATCHDOG TIMEOUT) at X — terminating pod aaa'
                      t "fetcher watchdog timeout 124"      reap investopediaclaude-eodhd    aaa "$K1"
mklog "$K1" 'fetch=-9 (exited) at X — terminating pod aaa'
                      t "fetcher SIGKILL -9"                reap investopediaclaude-nasdaq   aaa "$K1"
mklog "$K1" "$MID";   t "fetcher mid-run"                   wait investopediaclaude-eodhd    aaa "$K1"

echo "--- gated jobs: the exit code must be present AND zero ---"
mklog "$K1" "$DONE0"; t "post exit 0"                       reap investopediaclaude-post     aaa "$K1"
mklog "$K1" "$DONE1"; t "post exit 1 -> HOLD"               hold investopediaclaude-post     aaa "$K1"
mklog "$K1" "$DONE1"; t "post exit 1 + --reap-failed"       reap investopediaclaude-post     aaa "$K1" 1
mklog "$K1" "$DONE1"; t "m1 exit 1 -> HOLD"                 hold investopediaclaude-m1       aaa "$K1"
mklog "$K1" "$DONE1"; t "validate exit 1 -> HOLD"           hold investopediaclaude-validate aaa "$K1"
mklog "$K1" 'post: waiting for fresh vendor manifests'
                      t "post still waiting on manifests"   wait investopediaclaude-post     aaa "$K1"

echo "--- predict / sync ---"
mklog "$K1" $'job=0 (exited) at X\npublish=0 (MongoDB updated)'
                      t "predict job=0, published"          reap investopediaclaude-predict-predict aaa "$K1"
mklog "$K1" $'job=0 (exited) at X\npublish=6 (FAILED)'
                      t "predict job=0 but publish failed"  reap investopediaclaude-predict-predict aaa "$K1"
mklog "$K1" 'job=1 (exited) at X'
                      t "predict job=1 -> HOLD"             hold investopediaclaude-predict-predict aaa "$K1"
mklog "$K1" 'job=124 (WATCHDOG TIMEOUT) at X'
                      t "predict watchdog -> HOLD"          hold investopediaclaude-predict-market  aaa "$K1"
mklog "$K1" 'predict bootstrap 2026-09-09 pod=aaa job=predict'
                      t "header 'job=predict' is not a code" wait investopediaclaude-predict-predict aaa "$K1"
mklog "$K1" 'sync done ec=0 at X'
                      t "sync ec=0"                         reap investopediaclaude-sync     aaa "$K1"
mklog "$K1" 'sync done ec=1 at X'
                      t "sync ec=1 -> HOLD"                 hold investopediaclaude-sync     aaa "$K1"

echo "--- restarts: the OLDEST log holds what the job actually did ---"
mklog "$K1" "$DONE0"
mklog "$K2" 'RESTART DETECTED — the fetcher already ran (exit 0); not re-running
fetch=0 (exited) at X — terminating pod aaa'
                      t "restarted fetcher, original exit 0" reap investopediaclaude-calendar aaa "$K1
$K2"
mklog "$K1" $'job=0 (exited) at X\npublish=0'
mklog "$K2" 'RESTART DETECTED (marker exists) — skipping job, terminating
job=98 (exited) at X'
                      t "restarted predict: 98 must not mask job=0" reap investopediaclaude-predict-predict aaa "$K1
$K2"
mklog "$K1" "$MID"; mklog "$K2" "$MID"
                      t "restarted mid-fetch, no code yet"  wait investopediaclaude-eodhd    aaa "$K1
$K2"

echo "--- never reaped ---"
mklog "$K1" $'job=0 (exited) at X\nKEEP_POD=1 — not terminating'
                      t "KEEP_POD=1 even with job=0"        hold investopediaclaude-predict-stage1 aaa "$K1"
                      t "no log here (exp pod, other volume)" wait investopediaclaude-predict-exp aaa '20260909T00Z-predict-exp-OTHER.log'
rm -f "$STUB/$K1";    t "log unreadable"                    wait investopediaclaude-eodhd    aaa "$K1"

echo "--- classify publishes the exit code the retry logic reads ---"
ec() {  # $1 desc  $2 want-EXITCODE  $3 name  $4 log body
  LOGKEYS="$K1"; REAP_FAILED=1; mklog "$K1" "$4"; EXITCODE="unset"
  classify "$3" aaa
  if [ "$EXITCODE" = "$2" ]; then
    PASS=$((PASS + 1)); printf '  ok   %-50s -> EXITCODE=%s\n' "$1" "$EXITCODE"
  else
    FAIL=$((FAIL + 1)); printf '  FAIL %-50s -> EXITCODE=%s, want %s\n' "$1" "$EXITCODE" "$2"
  fi
}
ec "OOM 137 surfaces as EXITCODE"   137 investopediaclaude-nasdaq \
   'fetch=137 (exited) at X — terminating pod aaa'
ec "SIGKILL -9 surfaces as EXITCODE" -9 investopediaclaude-post \
   'fetch=-9 (exited) at X — terminating pod aaa'
ec "clean exit surfaces as 0"         0 investopediaclaude-eodhd "$DONE0"
ec "mid-run has no EXITCODE"         "" investopediaclaude-eodhd "$MID"

echo "--- --relaunch: only memory deaths are retried, once, and post comes back as validate+m1 ---"
# Drive the REAL maybe_relaunch against a fake launcher that just records its arguments.
ROOT="$STUB"; mkdir -p "$STUB/scripts" "$STUB/runpod"
cat > "$STUB/scripts/launch.sh" <<'FAKE'
#!/usr/bin/env bash
printf '%s@%s\n' "$1" "${RUNPOD_VCPU:-default}" >> "$STUB_LAUNCHES"
FAKE
chmod +x "$STUB/scripts/launch.sh"
export STUB_LAUNCHES="$STUB/launches"

r() {  # $1 desc  $2 want (comma-sep "job@vcpu" list, "" = nothing)  $3 name  $4 code  [$5 keep RETRIED]
  local desc="$1" want="$2" got
  [ -n "${5:-}" ] || RETRIED=""
  : > "$STUB_LAUNCHES"
  maybe_relaunch "$3" "$4" > /dev/null
  got="$(paste -sd, - < "$STUB_LAUNCHES")"
  if [ "$got" = "$want" ]; then
    PASS=$((PASS + 1)); printf '  ok   %-50s -> %s\n' "$desc" "${got:-<nothing>}"
  else
    FAIL=$((FAIL + 1)); printf '  FAIL %-50s -> got [%s], want [%s]\n' "$desc" "$got" "$want"
  fi
}

RELAUNCH=1; DRY=""; RETRY_VCPU=8
r "nasdaq OOM 137 -> nasdaq at 8 vCPU"   "nasdaq@8"      investopediaclaude-nasdaq   137
r "nasdaq SIGKILL -9 -> retried too"     "nasdaq@8"      investopediaclaude-nasdaq   -9
r "post OOM -> validate+m1, NOT post"    "validate@8,m1@8" investopediaclaude-post   137
r "exit 1 is not a memory death"         ""              investopediaclaude-nasdaq   1
r "exit 0 is never retried"              ""              investopediaclaude-nasdaq   0
r "watchdog 124 is not a memory death"   ""              investopediaclaude-eodhd    124
r "empty code is never retried"          ""              investopediaclaude-nasdaq   ""
r "predict/sync are the model repo's"    ""              investopediaclaude-predict-market 137
RETRIED=""
r "first OOM retries"                    "nasdaq@8"      investopediaclaude-nasdaq   137
r "second OOM does NOT retry again"      ""              investopediaclaude-nasdaq   137 keep
RELAUNCH=""
r "no --relaunch -> never retries"       ""              investopediaclaude-nasdaq   137
RELAUNCH=1; DRY=1
r "--dry-run reports but launches nothing" ""            investopediaclaude-nasdaq   137
DRY=""

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" = "0" ]
