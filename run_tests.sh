#!/bin/bash
# Every suite, in one command. Exits non-zero if anything fails.
cd "$(dirname "$0")" || exit 1
PY=.venv/bin/python
[ -x "$PY" ] || PY=python3
total=0; failed=0
for suite in tests/test_*.py; do
  out=$("$PY" "$suite" 2>&1)
  p=$(echo "$out" | grep -cE "^PASS"); f=$(echo "$out" | grep -cE "^FAIL")
  total=$((total+p)); failed=$((failed+f))
  printf "%-28s %2d passed  %d failed\n" "$(basename "$suite")" "$p" "$f"
  [ "$f" != "0" ] && echo "$out" | grep -E "^FAIL"
done
echo "────────────────────────────────────────────"
echo "$total passed, $failed failed"
exit $((failed > 0))
