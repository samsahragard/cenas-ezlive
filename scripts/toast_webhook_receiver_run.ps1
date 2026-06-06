param(
    [string]$ProjectRoot = "C:\Users\sam\cena-ai-assistant",
    [string]$RepoRoot = "C:\Users\sam\cenas-kitchen-runtime",
    [string]$DbPath = "C:\Users\sam\cena-ai-assistant\toast_webhook\toast_webhook.sqlite",
    [string]$RelayTokenFile = "C:\Users\sam\cena-ai-assistant\secrets\toast_webhook_relay_token.txt",
    [string]$SigningSecretFile = "C:\Users\sam\cena-ai-assistant\secrets\toast_webhook_signing_secret.txt",
    [string]$SigningSecretsFile = "C:\Users\sam\cena-ai-assistant\secrets\toast_webhook_signing_secrets.json",
    [string]$EmployeeProfileDbDir = "C:\Users\sam\cena-ai-assistant\employee_profiles\toast",
    [string]$Hosts = "127.0.0.1,100.73.38.82",
    [int]$Port = 8784
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $RepoRoot)) {
    throw "RepoRoot not found: $RepoRoot"
}

if (-not (Test-Path -LiteralPath $RelayTokenFile)) {
    throw "Toast webhook relay token file not found: $RelayTokenFile"
}

if (-not (Test-Path -LiteralPath $SigningSecretFile)) {
    throw "Toast webhook signing secret file not found: $SigningSecretFile"
}

$toastWebhookRoot = Join-Path $ProjectRoot "toast_webhook"
New-Item -ItemType Directory -Force -Path $toastWebhookRoot | Out-Null
New-Item -ItemType Directory -Force -Path $EmployeeProfileDbDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $ProjectRoot "logs") | Out-Null

$env:TOAST_WEBHOOK_DB = $DbPath
$env:TOAST_RELAY_TOKEN_FILE = $RelayTokenFile
$env:TOAST_WEBHOOK_SIGNING_SECRET_FILE = $SigningSecretFile
if (Test-Path -LiteralPath $SigningSecretsFile) {
    $env:TOAST_WEBHOOK_SIGNING_SECRETS_FILE = $SigningSecretsFile
}
$env:TOAST_EMPLOYEE_PROFILE_DBS_AUTO_EXPORT = if ($env:TOAST_EMPLOYEE_PROFILE_DBS_AUTO_EXPORT) { $env:TOAST_EMPLOYEE_PROFILE_DBS_AUTO_EXPORT } else { "1" }
$env:TOAST_EMPLOYEE_PROFILE_DB_DIR = $EmployeeProfileDbDir
$env:TOAST_WEBHOOK_HOSTS = $Hosts
$env:TOAST_WEBHOOK_PORT = [string]$Port
$env:TOAST_WEBHOOK_SEED_ON_START = if ($env:TOAST_WEBHOOK_SEED_ON_START) { $env:TOAST_WEBHOOK_SEED_ON_START } else { "1" }

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

$toastEnvFile = "C:\Users\sam\cena-secrets\toast_render_env.txt"
Set-EnvFromExportFile -Path $toastEnvFile -Names @(
    "TOAST_ANALYTICS_CLIENT_ID",
    "TOAST_ANALYTICS_CLIENT_SECRET",
    "TOAST_CLIENT_ID",
    "TOAST_CLIENT_SECRET",
    "TOAST_RESTAURANT_GUID_COPPERFIELD",
    "TOAST_RESTAURANT_GUID_TOMBALL"
)

if (-not $env:TOAST_CACHE_DIR) {
    $toastCacheDir = Join-Path $ProjectRoot "toast_cache"
    New-Item -ItemType Directory -Force -Path $toastCacheDir | Out-Null
    $env:TOAST_CACHE_DIR = $toastCacheDir
}

Set-Location -LiteralPath $RepoRoot
python ".\scripts\toast_webhook_receiver.py"
