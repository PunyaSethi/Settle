#!/usr/bin/env bash
#
# settle checkpoint gate.
#
#   scripts/gate.sh <checkpoint-name> <allowlist-file>
#
# Four checks, in order. Any failure exits non-zero and the checkpoint does not
# pass:
#
#   1. no file changed outside the checkpoint's allowlist
#   2. frozen files unchanged
#   3. pytest green
#   4. the named test IDs that actually ran are printed
#
# Check 4 is not decoration. A checkpoint that runs green while silently
# skipping a named test has not passed; printing the IDs makes that visible.
#
# The baseline for "changed" is $SETTLE_GATE_BASELINE, default HEAD.
#
set -uo pipefail

CHECKPOINT="${1:-}"
ALLOWLIST="${2:-}"
BASELINE="${SETTLE_GATE_BASELINE:-HEAD}"
# Frozen files are derived, not hardcoded (A65): any file in the tree whose
# name is one of these is frozen, wherever it sits.
FROZEN_NAMES=(SPEC.md DECISIONS.md PRIORS.md)

if [[ -z "$CHECKPOINT" || -z "$ALLOWLIST" ]]; then
  echo "usage: scripts/gate.sh <checkpoint-name> <allowlist-file>" >&2
  exit 2
fi
if [[ ! -f "$ALLOWLIST" ]]; then
  echo "gate: allowlist file not found: $ALLOWLIST" >&2
  exit 2
fi

cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 2

PY=./.venv/bin/python
[[ -x "$PY" ]] || PY=python3

FAILED=0
fail() { echo "  FAIL  $*"; FAILED=1; }
pass() { echo "  ok    $*"; }
warn() { echo "  WARN  $*"; }
info() { echo "  info  $*"; }

echo "gate: $CHECKPOINT   (baseline $BASELINE, allowlist $ALLOWLIST)"

# --- 1. allowlist ---------------------------------------------------------
echo
echo "[1/4] allowlist"

# Everything that differs from the baseline: modified tracked files plus
# untracked files git can see. Ignored files are not our business.
changed=$(
  {
    git diff --name-only "$BASELINE" 2>/dev/null
    git ls-files --others --exclude-standard
  } | sort -u
)

# An allowlist entry ending in "/" is a directory prefix. Anything else is an
# exact path. Blank lines and #-comments are skipped.
allowed() {
  local path="$1" entry
  while IFS= read -r entry; do
    entry="${entry%%#*}"
    entry="$(printf '%s' "$entry" | tr -d '[:space:]')"
    [[ -z "$entry" ]] && continue
    if [[ "$entry" == */ ]]; then
      [[ "$path" == "$entry"* ]] && return 0
    else
      [[ "$path" == "$entry" ]] && return 0
    fi
  done < "$ALLOWLIST"
  return 1
}

if [[ -z "$changed" ]]; then
  pass "no files changed since $BASELINE"
else
  while IFS= read -r path; do
    [[ -z "$path" ]] && continue
    if allowed "$path"; then
      pass "$path"
    else
      fail "$path — outside the $CHECKPOINT allowlist"
    fi
  done <<< "$changed"
fi

# --- 2. frozen files ------------------------------------------------------
echo
echo "[2/4] frozen files"

FROZEN=()
for name in "${FROZEN_NAMES[@]}"; do
  while IFS= read -r found; do
    [[ -n "$found" ]] && FROZEN+=("${found#./}")
  done < <(find . -name "$name" -not -path './.git/*' -not -path './.venv/*' 2>/dev/null | sort)
done

for f in "${FROZEN[@]}"; do
  if [[ ! -f "$f" ]]; then
    fail "$f — missing"
  elif git ls-files --error-unmatch "$f" >/dev/null 2>&1; then
    if git diff --quiet "$BASELINE" -- "$f"; then
      pass "$f unchanged since $BASELINE"
    elif allowed "$f"; then
      pass "$f changed, and this checkpoint's allowlist authorises it"
    else
      fail "$f — frozen file modified. Stop and ask before changing it."
    fi
  elif git check-ignore -q "$f"; then
    # Deliberately local-only. Not an anomaly, and it should not read like one
    # at every checkpoint.
    info "$f — intentionally local-only, gitignored and never committed."
  else
    warn "$f — untracked but not ignored. Nothing to verify it against."
  fi
done

# --- 3. tests -------------------------------------------------------------
echo
echo "[3/4] pytest"
# -m "" overrides pytest.ini's "not slow": the fast suite is the dev loop,
# the gate is the thing that has to be thorough.
if "$PY" -m pytest tests/ -q -m "" > /tmp/settle-gate-pytest.$$ 2>&1; then
  pass "$(tail -1 /tmp/settle-gate-pytest.$$)"
else
  fail "pytest red"
  sed 's/^/        /' /tmp/settle-gate-pytest.$$
fi

# --- 4. named test IDs ----------------------------------------------------
echo
echo "[4/4] named test IDs that ran"
collected=$("$PY" -m pytest tests/ --collect-only -q -m "" 2>/dev/null)
ids=$(printf '%s\n' "$collected" | grep -oE '[A-Z]{2,6}_[0-9]+' | sort -u -V)
if [[ -z "$ids" ]]; then
  fail "no named test IDs found — every checkpoint test must carry one"
else
  while IFS= read -r id; do
    n=$(printf '%s\n' "$collected" | grep -c "$id")
    printf '  %-8s %s test(s)\n' "${id/_/-}" "$n"
  done <<< "$ids"
fi

rm -f /tmp/settle-gate-pytest.$$

echo
if [[ "$FAILED" -eq 0 ]]; then
  echo "gate: $CHECKPOINT PASS"
else
  echo "gate: $CHECKPOINT FAIL"
fi
exit "$FAILED"
