# Executed by manage.py with literal project/runtime paths supplied via environment.
# Never emit or persist the password. Only Windows Task Scheduler stores credentials.
$ErrorActionPreference = 'Stop'
$taskName = 'AgentTeam.Background'
$projectDir = $env:AGENT_TEAM_INSTALL_ROOT
$pythonExe = $env:AGENT_TEAM_INSTALL_PYTHON
if (-not (Test-Path -LiteralPath (Join-Path $projectDir 'background_runner.py'))) { throw 'Project missing.' }
if (-not (Test-Path -LiteralPath $pythonExe)) { throw 'Python missing.' }
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) { throw 'Task already exists. Inspect it before replacing it.' }
$account = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$credential = Get-Credential -UserName $account -Message '采来 — サイクル —: Windows password (not PIN). Stored only by Windows Task Scheduler.'
if ($null -eq $credential) { exit 1 }
if ($credential.UserName -ne $account) { throw 'Use the current Windows account to retain AI sign-in.' }
$passwordValue = $null
try {
    $action = New-ScheduledTaskAction -Execute $pythonExe -Argument ('-X utf8 "' + (Join-Path $projectDir 'background_runner.py') + '"') -WorkingDirectory $projectDir
    $triggers = @((New-ScheduledTaskTrigger -AtStartup), (New-ScheduledTaskTrigger -AtLogOn -User $account))
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 20 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    $principal = New-ScheduledTaskPrincipal -UserId $account -LogonType Password -RunLevel Limited
    $definition = New-ScheduledTask -Action $action -Trigger $triggers -Settings $settings -Principal $principal -Description '采来 — サイクル — local supervisor. Runs after sign-out; uses existing per-user AI sign-in. No public network listener.'
    $passwordValue = $credential.GetNetworkCredential().Password
    Register-ScheduledTask -TaskName $taskName -InputObject $definition -User $account -Password $passwordValue | Out-Null
    Start-ScheduledTask -TaskName $taskName
    Write-Output 'Registered AgentTeam.Background using Password logon and Limited privileges.'
} finally {
    $passwordValue = $null
    if ($credential) { $credential.Password.Dispose() }
    $credential = $null
}
