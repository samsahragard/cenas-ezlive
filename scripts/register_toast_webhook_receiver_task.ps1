param(
    [string]$TaskName = "CenasToastWebhookReceiver8784",
    [string]$RepoRoot = "C:\Users\sam\cenas-kitchen-runtime",
    [string]$ProjectRoot = "C:\Users\sam\cena-ai-assistant",
    [string]$Hosts = "127.0.0.1,100.73.38.82",
    [int]$Port = 8784
)

$ErrorActionPreference = "Stop"

$runScript = Join-Path $RepoRoot "scripts\toast_webhook_receiver_run.ps1"
if (-not (Test-Path -LiteralPath $runScript)) {
    throw "Run script not found: $runScript"
}

$logs = Join-Path $ProjectRoot "logs"
New-Item -ItemType Directory -Force -Path $logs | Out-Null

$outLog = Join-Path $logs "toast_webhook_receiver_8784.out.log"
$errLog = Join-Path $logs "toast_webhook_receiver_8784.err.log"
$args = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", "`"$runScript`"",
    "-ProjectRoot", "`"$ProjectRoot`"",
    "-RepoRoot", "`"$RepoRoot`"",
    "-Hosts", "`"$Hosts`"",
    "-Port", "$Port"
) -join " "

$cmdArgs = "/c powershell.exe $args 1>> `"$outLog`" 2>> `"$errLog`""
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $cmdArgs -WorkingDirectory $RepoRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal `
    -UserId $currentUser `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Force | Out-Null

Write-Output "Registered $TaskName"
Write-Output "Out log: $outLog"
Write-Output "Err log: $errLog"
