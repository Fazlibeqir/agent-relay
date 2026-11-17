#!/usr/bin/env bash
# Runs Agent Relay from source on Linux (needs python3). The packaged binary comes from build-app.sh, built on a Linux machine.
exec python3 "$(dirname "$0")/agent_relay_app.py" "$@"
