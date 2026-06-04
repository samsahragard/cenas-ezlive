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

Set-Location -LiteralPath $RepoRoot
python ".\scripts\assistant_ck_runtime.py"
