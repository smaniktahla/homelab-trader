#!/usr/bin/env bash
# Diff one architecture map against a base ref (default: origin/main) and
# render the result. Used at both PR level (base = origin/main, i.e. "what
# did this branch change") and epic level (base = the commit the epic
# branched from, i.e. "what did the whole epic change").
#
# Usage: architecture/scripts/diff-map.sh <name> [base-ref]
#   e.g. architecture/scripts/diff-map.sh order-risk-path
#        architecture/scripts/diff-map.sh order-risk-path 2562669   # epic-level: diff vs epic start
set -euo pipefail

NAME="${1:?Usage: diff-map.sh <name> [base-ref]}"
BASE_REF="${2:-origin/main}"
REPO_ROOT="$(git rev-parse --show-toplevel)"
MAP="architecture/${NAME}.json"
ARCHIFY="${ARCHIFY_BIN:-$HOME/.agents/skills/archify/bin/archify.mjs}"

if [ ! -f "$REPO_ROOT/$MAP" ]; then
  echo "No such map: $MAP" >&2
  exit 1
fi

TMP_BASE="$(mktemp -t archify-base-XXXXXX.json)"
trap 'rm -f "$TMP_BASE"' EXIT

if ! git -C "$REPO_ROOT" show "${BASE_REF}:${MAP}" > "$TMP_BASE" 2>/dev/null; then
  echo "Map $MAP does not exist at $BASE_REF -- nothing to diff (it's new)." >&2
  exit 0
fi

OUT_DIR="$REPO_ROOT/architecture/generated"
mkdir -p "$OUT_DIR"
OUT_HTML="$OUT_DIR/${NAME}.diff.html"

node "$ARCHIFY" compare architecture "$TMP_BASE" "$REPO_ROOT/$MAP" "$OUT_HTML" \
  --repo-root "$REPO_ROOT" --json
