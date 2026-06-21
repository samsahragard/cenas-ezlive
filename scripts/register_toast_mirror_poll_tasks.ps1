param(
    [string]$FastTaskName = "CenasToastMirrorFastPoll",
    [string]$DimensionTaskName = "CenasToastMirrorDimensionSync",
    [string]$RepoRoot = "C:\Users\sam\cenas-kitchen-runtime",
    [string]$ProjectRoot = "C:\Users\sam\cena-ai-assistant",
    [string]$FastStart = "08:00",
    [string]$FastEnd = "23:59",
    [string]$DimensionStart = "07:00",
    [string]$DimensionEnd = "23:59"
)

$ErrorActionPreference = "Stop"

$runScript = Join-Path $RepoRoot "scripts\toast_mirror_poll_run.ps1"
if (-not (Test-Path -LiteralPath $runScript)) {
    throw "Run script not found: $runScript"
}
$fastLauncher = Join-Path $RepoRoot "scripts\toast_mirror_fast_poll.cmd"
if (-not (Test-Path -LiteralPath $fastLauncher)) {
    throw "Fast launcher not found: $fastLauncher"
}
$dimensionLauncher = Join-Path $RepoRoot "scripts\toast_mirror_dimension_sync.cmd"
if (-not (Test-Path -LiteralPath $dimensionLauncher)) {
    throw "Dimension launcher not found: $dimensionLauncher"
}

$logs = Join-Path $ProjectRoot "logs"
New-Item -ItemType Directory -Force -Path $logs | Out-Null

function Register-CenasMinuteTask {
    param(
        [string]$TaskName,
        [string]$Command,
        [string]$Start,
        [string]$End
    )

    $duration = Get-CenasDuration -Start $Start -End $End
    schtasks.exe /Create /TN $TaskName /TR $Command /SC DAILY /ST $Start /RI 5 /DU $duration /F /RL LIMITED | Out-Null
}

function Register-CenasHourlyTask {
    param(
        [string]$TaskName,
        [string]$Command,
        [string]$Start,
        [string]$End
    )

    $duration = Get-CenasDuration -Start $Start -End $End
    schtasks.exe /Create /TN $TaskName /TR $Command /SC DAILY /ST $Start /RI 60 /DU $duration /F /RL LIMITED | Out-Null
}

function Get-CenasDuration {
    param(
        [string]$Start,
        [string]$End
    )

    $startTime = [datetime]::ParseExact($Start, "HH:mm", $null)
    $endTime = [datetime]::ParseExact($End, "HH:mm", $null)
    if ($endTime -le $startTime) {
        $endTime = $endTime.AddDays(1)
    }
    $span = $endTime - $startTime
    return "{0:D2}:{1:D2}" -f [int]$span.TotalHours, $span.Minutes
}

$fastOutLog = Join-Path $logs "toast_mirror_fast_poll.out.log"
$fastErrLog = Join-Path $logs "toast_mirror_fast_poll.err.log"
$dimensionOutLog = Join-Path $logs "toast_mirror_dimension_sync.out.log"
$dimensionErrLog = Join-Path $logs "toast_mirror_dimension_sync.err.log"

$fastCommand = "`"$fastLauncher`""
Register-CenasMinuteTask -TaskName $FastTaskName -Command $fastCommand -Start $FastStart -End $FastEnd

$dimensionCommand = "`"$dimensionLauncher`""
Register-CenasHourlyTask -TaskName $DimensionTaskName -Command $dimensionCommand -Start $DimensionStart -End $DimensionEnd

Write-Output "Registered $FastTaskName"
Write-Output "Fast poll: every 5 minutes from $FastStart to $FastEnd"
Write-Output "Fast out log: $fastOutLog"
Write-Output "Fast err log: $fastErrLog"
Write-Output "Registered $DimensionTaskName"
Write-Output "Dimension sync: hourly from $DimensionStart to $DimensionEnd"
Write-Output "Dimension out log: $dimensionOutLog"
Write-Output "Dimension err log: $dimensionErrLog"
