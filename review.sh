#!/bin/sh
# The review path of this repository: nine offline test suites, then the worked example.
# No key, no network, no paid call: every command runs on its own stand-in providers
# (a stub model server on 127.0.0.1 and a simulated search adapter).
# Run from the repository root after the installation (README.md, section 4):  sh review.sh
PY="${PY:-.venv/bin/python}"
SUITES="tests/test_workflow.py tests/test_lifecycle.py tests/test_audit.py tests/test_concepts.py tests/test_evidence_packs.py tests/test_reuse_keys.py tests/test_tree_prune.py tests/test_currency_reread.py tests/test_evidence_pool.py"
COMMANDS="$SUITES examples/worked_example.py"
if [ ! -x "$PY" ]; then echo "review.sh: $PY not found; create the environment first (README.md, section 4)"; exit 2; fi
failed=0; total=0; total_start=$(date +%s)
for s in $COMMANDS; do
  total=$((total + 1)); start=$(date +%s)
  out=$(env NMB_TEST_FIXTURES=1 PYTHONDONTWRITEBYTECODE=1 "$PY" "$s" 2>&1); rc=$?
  end=$(date +%s)
  last=$(printf '%s\n' "$out" | tail -n 1 | cut -c1-110)
  if [ "$rc" -eq 0 ]; then printf 'ok    %-34s %3ss  %s\n' "$s" "$((end - start))" "$last"
  else failed=$((failed + 1)); printf 'FAIL  %-34s %3ss  exit %s\n' "$s" "$((end - start))" "$rc"; printf '%s\n' "$out" | tail -n 15; fi
done
echo "review path: $((total - failed)) of $total commands passed in $(( $(date +%s) - total_start )) seconds"
[ "$failed" -eq 0 ]
