param(
    [ValidateSet("fast", "dimensions")]
    [string]$Mode = "fast",
    [string]$ProjectRoot = "C:\Users\sam\cena-ai-assistant",
    [string]$RepoRoot = "C:\Users\sam\cenas-kitchen-runtime",
    [string]$DbPath = "C:\Users\sam\cena-ai-assistant\toast_webhook\toast_webhook.sqlite",
    [string]$ReviewDbPath = "C:\Users\sam\cena-ai-assistant\toast_webhook\toast_shift_reviews.sqlite",
    [string]$ToastEnvFile = "C:\Users\sam\cena-secrets\toast_render_env.txt"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $RepoRoot)) {
    throw "RepoRoot not found: $RepoRoot"
}

$logs = Join-Path $ProjectRoot "logs"
$toastWebhookRoot = Join-Path $ProjectRoot "toast_webhook"
$toastCacheDir = Join-Path $ProjectRoot "toast_cache"
New-Item -ItemType Directory -Force -Path $logs | Out-Null
New-Item -ItemType Directory -Force -Path $toastWebhookRoot | Out-Null
New-Item -ItemType Directory -Force -Path $toastCacheDir | Out-Null

$lockPath = Join-Path $logs "toast_mirror_$Mode.lock"
if (Test-Path -LiteralPath $lockPath) {
    $lock = Get-Item -LiteralPath $lockPath
    if ($lock.LastWriteTime -lt (Get-Date).AddMinutes(-10)) {
        Remove-Item -LiteralPath $lockPath -Force -Recurse
    } else {
        Write-Output "$(Get-Date -Format o) $Mode poll already running; skipped."
        exit 0
    }
}
New-Item -ItemType Directory -Path $lockPath -ErrorAction Stop | Out-Null

$env:TOAST_WEBHOOK_DB = $DbPath
$env:TOAST_SHIFT_REVIEW_DB = $ReviewDbPath
$env:TOAST_CACHE_DIR = $toastCacheDir
if ($env:PYTHONPATH) {
    $env:PYTHONPATH = "$RepoRoot;$env:PYTHONPATH"
} else {
    $env:PYTHONPATH = $RepoRoot
}

function Set-EnvFromExportFile {
    param(
        [string]$Path,
        [string[]]$Names
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }

    foreach ($line in Get-Content -LiteralPath $Path) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) {
            continue
        }
        $match = [regex]::Match($trimmed, '^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$')
        if (-not $match.Success) {
            continue
        }
        $name = $match.Groups[1].Value
        if ($Names -notcontains $name) {
            continue
        }
        $value = $match.Groups[2].Value.Trim()
        if (
            ($value.StartsWith('"') -and $value.EndsWith('"')) -or
            ($value.StartsWith("'") -and $value.EndsWith("'"))
        ) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        [Environment]::SetEnvironmentVariable($name, $value, "Process")
    }
}

Set-EnvFromExportFile -Path $ToastEnvFile -Names @(
    "TOAST_ANALYTICS_CLIENT_ID",
    "TOAST_ANALYTICS_CLIENT_SECRET",
    "TOAST_CLIENT_ID",
    "TOAST_CLIENT_SECRET",
    "TOAST_RESTAURANT_GUID_COPPERFIELD",
    "TOAST_RESTAURANT_GUID_TOMBALL"
)

Set-Location -LiteralPath $RepoRoot

function Invoke-ShiftReviewProjection {
    param([int]$Days)

    $reviewLockPath = Join-Path $logs "toast_shift_review_projection.lock"
    if (Test-Path -LiteralPath $reviewLockPath) {
        $reviewLock = Get-Item -LiteralPath $reviewLockPath
        if ($reviewLock.LastWriteTime -lt (Get-Date).AddMinutes(-30)) {
            Remove-Item -LiteralPath $reviewLockPath -Force -Recurse
        } else {
            Write-Output "$(Get-Date -Format o) shift-review projection already running; skipped."
            return
        }
    }
    New-Item -ItemType Directory -Path $reviewLockPath -ErrorAction Stop | Out-Null
    try {
        python -m scripts.toast_shift_review_projection `
            --source-db $DbPath `
            --review-db $ReviewDbPath `
            --days $Days
    } finally {
        Remove-Item -LiteralPath $reviewLockPath -Force -Recurse -ErrorAction SilentlyContinue
    }
}

try {
    if ($Mode -eq "fast") {
        python -m scripts.toast_webhook_backfill `
            --db $DbPath `
            --toast-env-file $ToastEnvFile `
            --sync-labor-days 1 `
            --backfill-orders-days 2 `
            --refresh
        Invoke-ShiftReviewProjection -Days 2
    } else {
        python -m scripts.toast_webhook_backfill `
            --db $DbPath `
            --toast-env-file $ToastEnvFile `
            --sync-dimensions `
            --refresh
        Invoke-ShiftReviewProjection -Days 30
    }
} finally {
    Remove-Item -LiteralPath $lockPath -Force -Recurse -ErrorAction SilentlyContinue
}
