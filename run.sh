#!/usr/bin/env bash
# Contract: ./run.sh <SIM_URL> runs the controller until the scenario finishes.
# State goes to ./state/<scenario>.db (override with CTRL_STATE_DIR). Restarting resumes from that file.
# No Docker; needs Python 3.10+ with requirements.txt installed. All dependencies are already listed there.
set -euo pipefail
SIM_URL="${1:?usage: ./run.sh <SIM_URL>}"
cd "$(dirname "$0")"
if [ -x .venv/bin/python ]; then PY=.venv/bin/python
elif [ -x .venv/Scripts/python.exe ]; then PY=.venv/Scripts/python.exe
elif command -v python3 >/dev/null 2>&1; then PY=python3
else PY=python
fi
exec "$PY" -m controller --url "$SIM_URL"
