$ErrorActionPreference = "Stop"

Set-Location "e:\Downloads\BOT-main\BOT-main"

if ([string]::IsNullOrWhiteSpace($env:TELEGRAM_BOT_TOKEN)) {
    throw "TELEGRAM_BOT_TOKEN is not set. Run: `$env:TELEGRAM_BOT_TOKEN = 'your-token'"
}

if ([string]::IsNullOrWhiteSpace($env:GEMINI_API_KEYS) -and
    [string]::IsNullOrWhiteSpace($env:GEMINI_API_KEY)) {
    throw "GEMINI_API_KEYS or GEMINI_API_KEY is not set."
}

Write-Host "Starting bot..."
Write-Host "Token present: $($env:TELEGRAM_BOT_TOKEN.Length)"
if ($env:GEMINI_API_KEYS) {
    Write-Host "Gemini keys: $((($env:GEMINI_API_KEYS -split ',').Count))"
} else {
    Write-Host "Gemini keys: 1"
}

py -3 -u bot.py
