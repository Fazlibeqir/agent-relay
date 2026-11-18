<# Builds dist\AgentRelay.exe (one file, no console window). Needs Python 3 and an internet connection once (PyInstaller goes into .build\venv, not into your system Python). #>
$ErrorActionPreference = 'Stop'
$Kit = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Kit
$venv = '.build\venv\Scripts\python.exe'
$healthy = $false
if (Test-Path $venv) { & $venv -c "pass" 2>$null; $healthy = ($LASTEXITCODE -eq 0) }
if (-not $healthy) { python -m venv --clear .build\venv }
& $venv -m pip install --quiet --disable-pip-version-check pyinstaller
& $venv -m PyInstaller --noconfirm --onefile --noconsole --name AgentRelay `
	--distpath dist --workpath .build\work --specpath .build `
	--paths dashboard `
	--add-data "$Kit\dashboard\index.html;." --add-data "$Kit\dashboard\providers.json;." `
	--add-data "$Kit\template;template" --add-data "$Kit\relay.settings.example.json;." `
	--hidden-import appapi --hidden-import osops --hidden-import planner --hidden-import customai --hidden-import existing --hidden-import tkinter `
	agent_relay_app.py
Write-Host "`nBuilt: $Kit\dist\AgentRelay.exe" -ForegroundColor Green
