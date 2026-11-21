#!/usr/bin/env bash
# Builds dist/AgentRelay/ (a folder with the AgentRelay program, fast to start) with PyInstaller on Linux, using the bundled Qt web engine for the window.
# Needs python3 with venv. Qt runtime dependencies include the system C++, GBM and OpenGL libraries.
set -euo pipefail
cd "$(dirname "$0")"
K="$(pwd)"    # absolute paths: PyInstaller resolves relative ones from the spec folder (.build)
[ -x .build/venv/bin/python ] && .build/venv/bin/python -c pass 2>/dev/null || python3 -m venv --clear .build/venv
.build/venv/bin/python -m pip install --quiet --disable-pip-version-check pyinstaller "pywebview[qt]" segno
rm -rf dist/AgentRelay
.build/venv/bin/python -m PyInstaller --noconfirm --onedir --name AgentRelay --distpath dist --workpath .build/work --specpath .build \
  --paths dashboard --add-data "$K/dashboard/index.html:." --add-data "$K/dashboard/providers.json:." --add-data "$K/dashboard/review.js:." --add-data "$K/template:template" \
  --add-data "$K/dashboard/dialogs.js:." --add-data "$K/dashboard/dialogs.css:." --add-data "$K/dashboard/phone.html:." \
  --add-data "$K/relay.settings.example.json:." --add-data "$K/assets:assets" --hidden-import appapi --hidden-import osops --hidden-import planner --hidden-import customai \
  --hidden-import existing --hidden-import digest --hidden-import phone --hidden-import copilot_usage --hidden-import segno --collect-submodules webview --hidden-import webview.platforms.qt agent_relay_app.py
# Mesa loads the host driver. Pair it with the host C++ runtime and GBM so it
# sees the matching symbols and driver directories on Fedora and other Linux hosts.
rm -f -- dist/AgentRelay/_internal/libstdc++.so.6 dist/AgentRelay/_internal/libgbm.so.1
echo "Built: $(pwd)/dist/AgentRelay/AgentRelay"
