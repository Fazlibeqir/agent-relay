# relay-awake.ps1 - Windows stand-in for "tmux new -s relay" + "systemd-inhibit --what=sleep:idle ./relay.sh ."
# Keeps the PC from sleeping only while relay.sh runs (a per-process request; no power settings are changed),
# then runs relay.sh in Git Bash. This window shows the live relay-status.ps1 dashboard (green = now, red = broke,
# yellow = next, with countdowns); relay.sh's own console output goes to relay-console.log / relay-console.err.
# Close the window or press Ctrl+C to stop the relay. Use -NoDashboard for the old raw relay.sh output.
#   powershell -NoProfile -ExecutionPolicy Bypass -File relay-awake.ps1
param([string]$Repo = $PSScriptRoot, [int]$MaxRounds = 0, [switch]$NoDashboard, [string]$Script = 'relay.sh')

Add-Type -Namespace Relay -Name Power -MemberDefinition '[DllImport("kernel32.dll")] public static extern uint SetThreadExecutionState(uint flags);'
$ES_CONTINUOUS = [uint32]'0x80000000'; $ES_SYSTEM_REQUIRED = [uint32]'0x00000001'
[Relay.Power]::SetThreadExecutionState($ES_CONTINUOUS -bor $ES_SYSTEM_REQUIRED) | Out-Null

$bash = 'C:\Program Files\Git\bin\bash.exe'
$command = "cd '$($Repo -replace '\\','/')' && ./$Script ."
try {
    $env:MAX_ROUNDS = "$MaxRounds"
    if ($NoDashboard -or -not (Test-Path (Join-Path $Repo 'relay-status.ps1'))) {
        & $bash -lc $command
        exit $LASTEXITCODE
    }
    # Same console, so closing this window or Ctrl+C still reaches relay.sh; only its output is redirected to files.
    $proc = Start-Process -FilePath $bash -ArgumentList '-lc', ('"' + $command + '"') -NoNewWindow -PassThru `
        -WorkingDirectory $Repo -RedirectStandardOutput (Join-Path $Repo 'relay-console.log') -RedirectStandardError (Join-Path $Repo 'relay-console.err')
    & (Join-Path $Repo 'relay-status.ps1') -Repo $Repo -WatchPid $proc.Id
    $proc.WaitForExit()
    exit $proc.ExitCode
} finally {
    [Relay.Power]::SetThreadExecutionState($ES_CONTINUOUS) | Out-Null
}
