"""Tool Guard for model-written shell commands (Bash): the system-side layer that does not rely on the AI.

Security by Default:
- Commands that can read credential stores or 采来's own data are denied.
- Commands that can send data out (network, publish/push, install/download) always go to a person for approval,
  even when automatic approval of operations is on. Only an administrator's exception in
  data/security-policy.json (outside every project) lets a matching command through automatically.

This screens the command text. A script file that the command runs is not inspected; that remaining risk is
for the sandbox stage (agents in a container with an outbound allowlist, docs/SECURITY_BY_DEFAULT.md).
"""
import re

CREDENTIAL = re.compile(
    r'(?i)(?:[\\/]\.ssh\b|\bid_(?:rsa|ed25519|ecdsa)\b|\.claude-sairai|[\\/]\.claude[\\/]\.credentials|\.credentials\.json|\bauth\.json\b'
    r'|mobile-auth\.json|mobile-key\.pem|[\\/]\.aws\b|[\\/]\.azure\b|[\\/]\.config[\\/]gh\b|[\\/]\.git-credentials|\.netrc\b'
    r'|\bcmdkey\b|\bvaultcmd\b|Get-StoredCredential|ConvertFrom-SecureString|CredentialManager|\bProtectedData\b'
    r'|team\.sqlite3|handoff\.sqlite3|security-policy\.json|websearch-policy\.json|[\\/]Microsoft[\\/](?:Credentials|Protect)\b'
    r'|(?:^|[\s\'"\\/])\.env(?:\.[\w.-]+)?(?=$|[\s\'"])|\$env:(?:\w*(?:TOKEN|SECRET|PASSWORD|API_?KEY|AGENT_TEAM)\w*)'
    r'|\$\{?(?:\w*(?:TOKEN|SECRET|PASSWORD|API_?KEY|AGENT_TEAM)\w*)\}?|\bprintenv\b|(?:^|[;&|]\s*)env\s*(?:$|[;&|])|Get-ChildItem\s+env:|\bgci\s+env:|\bdir\s+env:)')

RULES = [
    ('publish', '公開・送信（push・デプロイ・メール等）', re.compile(
        r'(?i)\bgit\s+(?:push|remote\s+(?:add|set-url)|send-email|request-pull)\b|\bgh\s+\w+|\b(?:npm|pnpm|yarn)\s+publish\b|\btwine\s+upload\b'
        r'|\bdocker\s+(?:push|login)\b|\b(?:az|aws|gcloud|gsutil|firebase|vercel|netlify|heroku|kubectl|terraform)\s+\w+'
        r'|Send-MailMessage|Outlook\.Application|\bsmtplib\b|\bnet\s+use\b|\bNew-PSDrive\b|\brclone\b|\bazcopy\b')),
    ('install', 'ソフトウェアの導入・取得', re.compile(
        r'(?i)\b(?:pip3?|uv\s+pip)\s+install\b|\b-m\s+pip\s+install\b|\b(?:npm|pnpm)\s+(?:install|i|ci|add)\b|\byarn\s+(?:add|install)\b'
        r'|\bnpx\s+\S|\buvx\b|\bwinget\b|\bchoco\b|\bscoop\b|\bInstall-(?:Module|Package|Script)\b|\b(?:go|cargo|gem)\s+install\b'
        r'|\bconda\s+install\b|\bgit\s+(?:clone|fetch|pull|submodule\s+update)\b|\bdocker\s+(?:pull|run)\b')),
    # 2026-10-09: an assignee changed Developer Mode in HKLM on automatic approval (it failed only for lack of admin
    # rights). Changing OS or security settings always goes to a person, like sending data out.
    ('system', 'OS・セキュリティの設定の変更', re.compile(
        r'(?i)\b(?:Set|New|Remove|Rename|Clear)-Item(?:Property)?\b[^;|\n]*(?:\bHK(?:LM|CU|CR|U|CC)\b|HKEY_|Registry::)'
        r'|\breg(?:\.exe)?\s+(?:add|delete|import|load|unload|restore|copy|save)\b|\bregedit\b'
        r'|\bSet-ExecutionPolicy\b|\b(?:Set|Add|Remove)-MpPreference\b|\bnetsh\b|\bbcdedit\b'
        r'|\bsc(?:\.exe)?\s+(?:config|create|delete|stop|start|failure)\b|\b(?:Set|New|Remove|Stop|Start|Restart)-Service\b'
        r'|\bschtasks(?:\.exe)?\s+/(?:create|change|delete|run)\b|\b(?:Register|Unregister|Set|Enable|Disable)-ScheduledTask\b'
        r'|\b(?:Enable|Disable)-WindowsOptionalFeature\b|\bdism(?:\.exe)?\b|\b(?:New|Set|Remove|Enable|Disable)-NetFirewall\w*\b'
        r'|\bicacls\b[^;|\n]*/(?:grant|deny|setowner|reset)\b|\btakeown\b|\bSet-Acl\b|\brunas\b|-Verb\s+RunAs\b'
        r'|\bsecedit\b|\bgpupdate\b|\b(?:New|Set|Remove|Enable|Disable)-Local(?:User|Group\w*)\b|\bnet\s+(?:user|localgroup)\b'
        r'|\bSet-(?:Date|TimeZone)\b|\bsetx\b[^;|\n]*/m\b|\b(?:Stop-Computer|Restart-Computer|shutdown(?:\.exe)?)\b')),
    ('network', '外部との通信', re.compile(
        r'(?i)\b(?:curl|wget|ssh|scp|sftp|ftp|telnet|rsync|ncat|netcat|socat)\b|(?:^|[\s;&|(])nc\s|\bInvoke-(?:WebRequest|RestMethod)\b'
        r'|(?:^|[\s;&|(])(?:iwr|irm)\s|\bStart-BitsTransfer\b|\bbitsadmin\b|\bcertutil\b.*-urlcache|Net\.WebClient|\bHttpClient\b'
        r'|Net\.Sockets|\bTcpClient\b|\burllib\b|\brequests\.(?:get|post|put|patch|delete|request|Session)\b|\bhttpx\b|\baiohttp\b'
        r'|\bhttp\.client\b|\bsocket\.(?:socket|create_connection)\b|\bfetch\s*\(|\bXMLHttpRequest\b|\bwebbrowser\b'
        r'|\bhttps?://(?!(?:127\.0\.0\.1|localhost|\[::1\])(?:[:/]|$))|\bStart-Process\s+["\']?https?://(?!(?:127\.0\.0\.1|localhost|\[::1\])(?:[:/]|$))')),
]


SEARCH_COMMAND = re.compile(r'(?i)^\s*(?:[\w.-]+=\S*\s+)*(?:\S*[\\/])?(?:grep|egrep|fgrep|rg|findstr(?:\.exe)?|Select-String|sls)\b')
QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")


def _segments(text):
    """Split at ; & | newline outside quotes (a | inside a search pattern is not a pipe). Separators are kept."""
    parts, current, quote = [], '', None
    for char in text:
        if quote:
            current += char
            if char == quote:
                quote = None
        elif char in '\'"':
            quote = char
            current += char
        elif char in ';&|\n':
            parts += [current, char]
            current = ''
        else:
            current += char
    return parts + [current]


def _without_search_terms(text):
    """The command with the quoted search terms of search commands blanked (2026-10-09, user decision).

    `grep -nE "socket|urllib|http" app.py` only reads a file, but the name urllib in its pattern made the network
    rule ask the user. A quoted term of grep/rg/findstr/Select-String is not run, so it is not judged. A double-quoted
    term that the shell would expand ($(...) or a backtick) is kept and judged. Everything else is judged as before,
    including real network commands in the same line (`grep "x" a && curl ...`)."""
    parts = _segments(text)
    for index in range(0, len(parts), 2):
        # rg --pre runs another program on each file, so such a search is judged in full.
        if SEARCH_COMMAND.match(parts[index]) and not re.search(r'(?i)--pre\b', parts[index]):
            parts[index] = QUOTED.sub(lambda m: m.group(0) if m.group(0).startswith('"') and ('$(' in m.group(0) or '`' in m.group(0))
                                      else '""', parts[index])
    return ''.join(parts)


def classify(command):
    """Return None (no outbound effect found) or (category, label, action) with action 'deny' or 'manual'."""
    text = str(command or '')
    if CREDENTIAL.search(text):  # credentials and 采来's data: judged on the whole command, search terms included
        return 'credential', '認証情報・采来の内部データの読み取り', 'deny'
    screened = _without_search_terms(text)
    for category, label, pattern in RULES:
        if pattern.search(screened):
            return category, label, 'manual'
    return None


def excepted(command, category, policy):
    """An administrator's exception: same category and the command contains the allowed text exactly."""
    text = str(command or '')
    for rule in policy.get('outbound_exceptions') or []:
        if not isinstance(rule, dict):
            continue
        allowed = str(rule.get('contains') or '').strip()
        if rule.get('category') == category and len(allowed) >= 8 and allowed == text.strip():
            return str(rule.get('reason') or '管理者が許可した例外')[:200]
    return None
