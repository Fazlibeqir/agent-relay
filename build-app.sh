#!/usr/bin/env bash
# Builds dist/AgentRelay (one file) with PyInstaller on Linux. Needs python3 with venv and tkinter (sudo apt install python3-venv python3-tk).
set -euo pipefail
cd "$(dirname "$0")"
[ -x .build/venv/bin/python ] && .build/venv/bin/python -c pass 2>/dev/null || python3 -m venv --clear .build/venv
.build/venv/bin/python -m pip install --quiet --disable-pip-version-check pyinstaller
.build/venv/bin/python -m PyInstaller --noconfirm --onefile --name AgentRelay --distpath dist --workpath .build/work --specpath .build \
  --paths dashboard --add-data "dashboard/index.html:." --add-data "dashboard/providers.json:." --add-data "template:template" \
  --add-data "relay.settings.example.json:." --hidden-import appapi --hidden-import osops --hidden-import planner --hidden-import customai agent_relay_app.py
echo "Built: $(pwd)/dist/AgentRelay"
