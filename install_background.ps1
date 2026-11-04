# Executed by manage.py with literal project/runtime paths supplied via environment.
# Never emit or persist the password. Only Windows Task Scheduler stores credentials.
$ErrorActionPreference = 'Stop'
$taskName = 'AgentTeam.Background'
$projectDir = $env:AGENT_TEAM_INSTALL_ROOT
$pythonExe = $env:AGENT_TEAM_INSTALL_PYTHON
if (-not (Test-Path -LiteralPath (Join-Path $projectDir 'background_runner.py'))) { throw 'Project missing.' }
if (-not (Test-Path -LiteralPath $pythonExe)) { throw 'Python missing.' }
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) { throw 'Task already exists. Inspect it before replacing it.' }
# Do not register a supervisor that would start a server against an unusable DB pair (read-only check).
& $pythonExe -I -X utf8 (Join-Path $projectDir 'team_deployment.py') check --data (Join-Path $projectDir 'data') | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'DB pair check refused registration. Nothing was registered.' }
$account = [Security.Principal.WindowsIdentity]::GetCurrent().Name
# Get-Credential prompts inside the console and shows nothing from a child process, so use the Windows sign-in dialog (CredUI).
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class SairaiCredPrompt {
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    struct CREDUI_INFO { public int cbSize; public IntPtr hwndParent; public string pszMessageText; public string pszCaptionText; public IntPtr hbmBanner; }
    [DllImport("credui.dll", CharSet = CharSet.Unicode)]
    static extern int CredUIPromptForWindowsCredentials(ref CREDUI_INFO info, int authError, ref uint authPackage, IntPtr inBuf, uint inSize, out IntPtr outBuf, out uint outSize, ref bool save, int flags);
    [DllImport("credui.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool CredUnPackAuthenticationBuffer(int flags, IntPtr buf, uint size, StringBuilder user, ref int userSize, StringBuilder domain, ref int domainSize, StringBuilder pass, ref int passSize);
    public static string[] Prompt(string caption, string message) {
        CREDUI_INFO info = new CREDUI_INFO();
        info.cbSize = Marshal.SizeOf(typeof(CREDUI_INFO));
        info.pszCaptionText = caption;
        info.pszMessageText = message;
        uint package = 0; bool save = false; IntPtr outBuf; uint outSize;
        int rc = CredUIPromptForWindowsCredentials(ref info, 0, ref package, IntPtr.Zero, 0, out outBuf, out outSize, ref save, 1);
        if (rc == 1223) { return null; }
        if (rc != 0) { throw new InvalidOperationException("CredUI error " + rc); }
        try {
            StringBuilder user = new StringBuilder(512), domain = new StringBuilder(512), pass = new StringBuilder(512);
            int us = 512, ds = 512, ps = 512;
            if (!CredUnPackAuthenticationBuffer(0, outBuf, outSize, user, ref us, domain, ref ds, pass, ref ps)) { throw new InvalidOperationException("Unpack failed " + Marshal.GetLastWin32Error()); }
            string identity = String.IsNullOrWhiteSpace(domain.ToString()) ? user.ToString() : domain.ToString() + "\\" + user.ToString();
            return new string[] { identity, pass.ToString() };
        } finally {
            byte[] zero = new byte[outSize];
            Marshal.Copy(zero, 0, outBuf, (int)outSize);
            Marshal.FreeCoTaskMem(outBuf);
        }
    }
}
'@
$answer = [SairaiCredPrompt]::Prompt('采来 — サイクル —', "Enter the Windows password for $account (not a PIN). It is stored only by Windows Task Scheduler.")
if ($null -eq $answer) { exit 1 }
$enteredUser = $answer[0]
$passwordValue = $answer[1]
$answer = $null
$accountShort = $account.Split('\')[-1]
if (($enteredUser -ne $account) -and ($enteredUser -ne $accountShort)) { $passwordValue = $null; throw 'Use the current Windows account to retain AI sign-in.' }
try {
    $action = New-ScheduledTaskAction -Execute $pythonExe -Argument ('-X utf8 "' + (Join-Path $projectDir 'background_runner.py') + '"') -WorkingDirectory $projectDir
    $triggers = @((New-ScheduledTaskTrigger -AtStartup), (New-ScheduledTaskTrigger -AtLogOn -User $account))
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 20 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    $principal = New-ScheduledTaskPrincipal -UserId $account -LogonType Password -RunLevel Limited
    $definition = New-ScheduledTask -Action $action -Trigger $triggers -Settings $settings -Principal $principal -Description '采来 — サイクル — local supervisor. Runs after sign-out; uses existing per-user AI sign-in. No public network listener.'
    Register-ScheduledTask -TaskName $taskName -InputObject $definition -User $account -Password $passwordValue | Out-Null
    Start-ScheduledTask -TaskName $taskName
    Write-Output 'Registered AgentTeam.Background using Password logon and Limited privileges.'
} finally {
    $passwordValue = $null
}
