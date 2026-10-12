param([string]$OutputPath,[int]$Seconds=2700)
$deadline = [DateTime]::UtcNow.AddSeconds($Seconds)
while ([DateTime]::UtcNow -lt $deadline) {
    if (Test-Path -LiteralPath "$OutputPath.stop") { break }
    $stamp = [DateTime]::UtcNow.ToString('o')
    Get-Process -Name python,ollama,codex,claude -ErrorAction SilentlyContinue | ForEach-Object {
        try { [pscustomobject]@{ utc=$stamp; pid=$_.Id; name=$_.ProcessName; started=$_.StartTime.ToUniversalTime().ToString('o'); cpu_seconds=$_.CPU; working_bytes=$_.WorkingSet64 } } catch { }
    } | Export-Csv -LiteralPath $OutputPath -Append -NoTypeInformation -Encoding utf8
    Start-Sleep -Seconds 1
}
