param(
    [string]$ProjectRoot = "C:\Users\sam\cena-ai-assistant",
    [string]$RepoRoot = "C:\Users\sam\Desktop\cenas-ezlive-tracking-live-work",
    [string]$DbPath = "C:\Users\sam\cena-ai-assistant\assistant_review.sqlite",
    [string]$TokenFile = "C:\Users\sam\cena-ai-assistant\secrets\assistant_runtime_token.txt",
    [string]$Hosts = "127.0.0.1",
    [int]$Port = 8782
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $RepoRoot)) {
    throw "RepoRoot not found: $RepoRoot"
}

if (-not (Test-Path -LiteralPath $TokenFile)) {
    throw "Assistant runtime token file not found: $TokenFile"
}

$env:ASSISTANT_REVIEW_DB = $DbPath
$env:ASSISTANT_RUNTIME_TOKEN = (Get-Content -LiteralPath $TokenFile -Raw).Trim()
$env:ASSISTANT_RUNTIME_HOSTS = $Hosts
$env:ASSISTANT_RUNTIME_PORT = [string]$Port
if ($env:PYTHONPATH) {
    $env:PYTHONPATH = "$RepoRoot;$env:PYTHONPATH"
} else {
    $env:PYTHONPATH = $RepoRoot
}

$anthropicFile = "C:\Users\sam\cena-secrets\anthropic_api_key.txt"
if (Test-Path -LiteralPath $anthropicFile) {
    $env:ANTHROPIC_API_KEY_FILE = $anthropicFile
}

$geminiCandidates = @(
    "C:\Users\sam\cena-secrets\gemini_api_key.txt",
    "C:\Users\sam\cena\.secrets\gemini_api_key.txt",
    "C:\Users\sam\cena-secrets\google_api_key.txt"
)
foreach ($candidate in $geminiCandidates) {
    if (Test-Path -LiteralPath $candidate) {
        $env:GEMINI_API_KEY_FILE = $candidate
        break
    }
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

$toastEnvFile = "C:\Users\sam\cena-secrets\toast_render_env.txt"
Set-EnvFromExportFile -Path $toastEnvFile -Names @(
    "TOAST_ANALYTICS_CLIENT_ID",
    "TOAST_ANALYTICS_CLIENT_SECRET",
    "TOAST_CLIENT_ID",
    "TOAST_CLIENT_SECRET",
    "TOAST_RESTAURANT_GUID_COPPERFIELD",
    "TOAST_RESTAURANT_GUID_TOMBALL"
)

if (-not $env:TOAST_ANALYTICS_CACHE_DIR) {
    $toastCacheDir = Join-Path $ProjectRoot "toast_analytics_cache"
    New-Item -ItemType Directory -Force -Path $toastCacheDir | Out-Null
    $env:TOAST_ANALYTICS_CACHE_DIR = $toastCacheDir
}

if (-not $env:TOAST_CACHE_DIR) {
    $toastOrderCacheDir = Join-Path $ProjectRoot "toast_cache"
    New-Item -ItemType Directory -Force -Path $toastOrderCacheDir | Out-Null
    $env:TOAST_CACHE_DIR = $toastOrderCacheDir
}

Set-Location -LiteralPath $RepoRoot
python ".\scripts\assistant_ck_runtime.py"
