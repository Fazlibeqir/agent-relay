@echo off
rem Runs Agent Relay from source (needs Python 3). The packaged app is dist\AgentRelay.exe (build it with build-app.ps1).
python "%~dp0agent_relay_app.py" %*
