[CmdletBinding()]
param([int]$Port = 8790)
$ErrorActionPreference = 'Stop'
$url = "http://127.0.0.1:$Port"
$health = Invoke-RestMethod -Uri "$url/health" -TimeoutSec 3
if ($health.app -ne 'agent-team') { throw 'This port is not 采来 — サイクル —.' }
Invoke-WebRequest -Uri "$url/" -SessionVariable teamSession -UseBasicParsing | Out-Null
Invoke-RestMethod -Method Post -Uri "$url/api/shutdown" -WebSession $teamSession -ContentType 'application/json' -Headers @{'X-Agent-Team-UI'='1'} -Body '{}' | Out-Null
Write-Output 'Shutdown requested. Interrupted tasks require review before retry.'
