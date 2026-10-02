[CmdletBinding()]
param([int]$Port = 8790)
$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
# Local-only decision measurement. No cloud fallback; retain existing effective decisions.
$env:DECISION_PROVIDER = 'ollama'
$env:OLLAMA_DECISION_ENABLED = 'true'
$env:DECISION_SHADOW_MODE = 'true'
$env:DECISION_API_CALLS_APPROVED = 'false'
$scriptPath = Join-Path $projectRoot 'server.py'
$pythonPath = (Get-Command python.exe -ErrorAction Stop).Source
$url = "http://127.0.0.1:$Port"
try {
    $existing = Invoke-RestMethod -Uri "$url/health" -TimeoutSec 2
    if ($existing.app -eq 'agent-team') {
        Write-Output "Already running: $url"
        return
    }
    throw 'Port is occupied by another service.'
} catch {
    if ($_.Exception.Message -eq 'Port is occupied by another service.') { throw }
}
$dataPath = Join-Path $projectRoot 'data'
New-Item -ItemType Directory -Path $dataPath -Force | Out-Null
$arguments = @('-X', 'utf8', ('"' + $scriptPath + '"'), '--port', [string]$Port)
$process = Start-Process -FilePath $pythonPath -ArgumentList $arguments -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $dataPath 'server.stdout.log') -RedirectStandardError (Join-Path $dataPath 'server.stderr.log')
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Milliseconds 300
    $process.Refresh()
    if ($process.HasExited) { throw 'Server exited. Read data/server.stderr.log.' }
    try {
        $health = Invoke-RestMethod -Uri "$url/health" -TimeoutSec 1
        if ($health.app -eq 'agent-team') {
            Write-Output "Started: $url (PID $($process.Id))"
            return
        }
    } catch {}
}
throw 'Startup was not confirmed. Check data/server.stderr.log before retrying.'
