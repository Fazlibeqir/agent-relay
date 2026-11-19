#!/usr/bin/env bash
# Builds dist/AgentRelay/ (a folder with the AgentRelay program, fast to start) with PyInstaller on Linux, using the bundled Qt web engine for the window.
# Needs python3 with venv. Packages the Qt web engine needs at run time (Debian/Ubuntu names): libnss3 libxkbcommon-x11-0 libxcb-cursor0 libasound2 libgl1.
set -euo pipefail
cd "$(dirname "$0")"
K="$(pwd)"    # absolute paths: PyInstaller resolves relative ones from the spec folder (.build)
[ -x .build/venv/bin/python ] && .build/venv/bin/python -c pass 2>/dev/null || python3 -m venv --clear .build/venv
.build/venv/bin/python -m pip install --quiet --disable-pip-version-check pyinstaller "pywebview[qt]"
rm -rf dist/AgentRelay
.build/venv/bin/python -m PyInstaller --noconfirm --onedir --name AgentRelay --distpath dist --workpath .build/work --specpath .build \
  --paths dashboard --add-data "$K/dashboard/index.html:." --add-data "$K/dashboard/providers.json:." --add-data "$K/dashboard/review.js:." --add-data "$K/template:template" \
  --add-data "$K/dashboard/dialogs.js:." --add-data "$K/dashboard/dialogs.css:." \
  --add-data "$K/relay.settings.example.json:." --add-data "$K/assets:assets" --hidden-import appapi --hidden-import osops --hidden-import planner --hidden-import customai \
  --hidden-import existing --hidden-import digest --collect-submodules webview --hidden-import webview.platforms.qt agent_relay_app.py
echo "Built: $(pwd)/dist/AgentRelay/AgentRelay"
