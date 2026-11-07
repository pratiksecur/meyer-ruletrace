#!/usr/bin/env bash
# Run RuleTrace (with the RuleMate Copilot) and start it again if it ever stops, e.g. during a live demo.
#   scripts/serve.sh            # http://localhost:8001/ruletrace
#   PORT=8002 scripts/serve.sh
cd "$(dirname "$0")/.." || exit 1
PY=${PYTHON:-.venv/bin/python}
[ -x "$PY" ] || PY=python
while true; do
  "$PY" -m uvicorn ruletrace.app:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8001}"
  echo "RuleTrace stopped (exit $?); restarting in 2 s. Press Ctrl+C twice to quit." >&2
  sleep 2
done
