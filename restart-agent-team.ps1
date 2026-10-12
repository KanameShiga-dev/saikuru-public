[CmdletBinding()]
param(
    [ValidateSet('restart')]
    [string]$Action = 'restart',
    [ValidateRange(1024, 65535)]
    [int]$Port = 8790
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$stopScript = Join-Path $PSScriptRoot 'Stop-AgentTeam.ps1'
$startScript = Join-Path $PSScriptRoot 'Start-AgentTeam.ps1'
foreach ($scriptPath in @($stopScript, $startScript)) {
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
        throw "Agent team control script was not found: $scriptPath"
    }
}

$healthUri = "http://127.0.0.1:$Port/health"
$health = $null
try {
    $health = Invoke-RestMethod -Uri $healthUri -TimeoutSec 3
}
catch {
    # A stopped service is safe to start below.
}

if ($null -ne $health) {
    if ($health.app -ne 'agent-team') {
        throw "Port $Port is occupied by another service."
    }

    & $stopScript -Port $Port | Out-Null
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Milliseconds 300
        try {
            $probe = Invoke-RestMethod -Uri $healthUri -TimeoutSec 1
            if ($probe.app -ne 'agent-team') {
                break
            }
        }
        catch {
            break
        }
    }
}

& $startScript -Port $Port | Out-Null
$confirmed = $false
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    Start-Sleep -Milliseconds 300
    try {
        $probe = Invoke-RestMethod -Uri $healthUri -TimeoutSec 1
        if ($probe.app -eq 'agent-team') {
            $confirmed = $true
            break
        }
    }
    catch {
    }
}

if (-not $confirmed) {
    throw "Agent team restart was not confirmed on port $Port."
}

Write-Output "Agent team restart confirmed on port $Port."
