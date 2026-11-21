<# Builds dist\AgentRelay\AgentRelay.exe (a folder, fast to start) and dist\AgentRelay-windows-x64.zip. Needs Python 3 and an internet connection once
   (PyInstaller and the window library go into .build\venv-win, not into your system Python). The window is WebView2, which is part of Windows 10/11. #>
$ErrorActionPreference = 'Stop'
$Kit = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Kit
$venv = '.build\venv-win\Scripts\python.exe'
$healthy = $false
if (Test-Path $venv) { & $venv -c "pass" 2>$null; $healthy = ($LASTEXITCODE -eq 0) }
if (-not $healthy) { python -m venv --clear .build\venv-win }
& $venv -m pip install --quiet --disable-pip-version-check pyinstaller pywebview segno tzdata
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
if (Test-Path dist\AgentRelay) { Remove-Item dist\AgentRelay -Recurse -Force }
& $venv -m PyInstaller --noconfirm --onedir --noconsole --name AgentRelay --icon "$Kit\assets\agent-relay.ico" `
	--distpath dist --workpath .build\work --specpath .build `
	--paths dashboard `
	--add-data "$Kit\dashboard\index.html;." --add-data "$Kit\dashboard\providers.json;." --add-data "$Kit\dashboard\review.js;." `
	--add-data "$Kit\dashboard\dialogs.js;." --add-data "$Kit\dashboard\dialogs.css;." --add-data "$Kit\dashboard\phone.html;." `
	--add-data "$Kit\template;template" --add-data "$Kit\relay.settings.example.json;." --add-data "$Kit\assets;assets" `
	--hidden-import appapi --hidden-import osops --hidden-import planner --hidden-import customai --hidden-import existing --hidden-import digest --hidden-import phone --hidden-import copilot_usage --hidden-import segno --collect-data tzdata `
	--collect-all webview `
	agent_relay_app.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
if (Test-Path dist\AgentRelay-windows-x64.zip) { Remove-Item dist\AgentRelay-windows-x64.zip -Force }
Compress-Archive -Path dist\AgentRelay -DestinationPath dist\AgentRelay-windows-x64.zip
Write-Host "`nBuilt: $Kit\dist\AgentRelay\AgentRelay.exe and dist\AgentRelay-windows-x64.zip" -ForegroundColor Green
