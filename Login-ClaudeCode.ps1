[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath $PSScriptRoot
try {
    $env:CLAUDE_CONFIG_DIR = Join-Path $env:USERPROFILE '.claude-sairai'
    $claudePath = & python.exe -X utf8 -c "from team_config import discover; c=discover()['claude']; print(c[0] if c else '')"
    if ([string]::IsNullOrWhiteSpace($claudePath)) { throw 'Claude Code executable was not found.' }
    Write-Output 'This is the standalone Claude Code login used by 采来 — サイクル —.'
    Write-Output 'Complete login in your browser. Credentials are handled by Claude Code.'
    & $claudePath auth login --claudeai
    if ($LASTEXITCODE -ne 0) { throw 'Claude Code login did not finish successfully.' }
} finally {
    Pop-Location
}
