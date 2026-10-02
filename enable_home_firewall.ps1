# Run elevated only after the user approves the narrow home Wi-Fi inbound rule.
$ErrorActionPreference = 'Stop'
$ruleName = 'Agent Team Home Wi-Fi'
$adapter = Get-NetAdapter -Name 'Wi-Fi' -ErrorAction Stop
if ($adapter.Status -ne 'Up') { throw 'Wi-Fi is not connected.' }
$profile = Get-NetConnectionProfile -InterfaceIndex $adapter.ifIndex -ErrorAction Stop
if ($profile.NetworkCategory -ne 'Private') { throw 'Wi-Fi network profile must be Private.' }
if (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue) { throw 'Rule already exists; inspect it before replacing.' }
$pythonExe = (Get-Command python.exe -ErrorAction Stop).Source
New-NetFirewallRule -DisplayName $ruleName -Description '采来 — サイクル — phone gateway: same home Wi-Fi subnet only.' -Direction Inbound -Action Allow -Enabled True -Profile Private -InterfaceAlias 'Wi-Fi' -RemoteAddress LocalSubnet -Protocol TCP -LocalPort 8788 -Program $pythonExe -ErrorAction Stop | Out-Null
Write-Output 'Added 采来 — サイクル — Home Wi-Fi rule for Private/Wi-Fi/LocalSubnet/TCP 8788.'
