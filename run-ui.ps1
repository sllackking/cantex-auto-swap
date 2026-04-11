Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

if (-not (Test-Path .venv312)) {
  try {
    py -3.12 -m venv .venv312
  } catch {
    py -3.11 -m venv .venv312
  }
}

$python = Join-Path $PSScriptRoot ".venv312\Scripts\python.exe"

if (-not (Test-Path config.json)) {
  Copy-Item config.json.example config.json
}

if (-not (Test-Path .env)) {
  Copy-Item .env.example .env
}

& $python -m pip install --disable-pip-version-check -q -U pip setuptools wheel

if (Test-Path requirements.txt) {
  $reqBody = (Get-Content requirements.txt -Raw)
  if ($reqBody -match '\S') {
    & $python -m pip install --disable-pip-version-check -r .\requirements.txt
  }
}

$sdkCandidates = @(
  (Join-Path $PSScriptRoot "cantex_sdk"),
  (Join-Path (Split-Path $PSScriptRoot -Parent) "cantex_sdk")
)
foreach ($sdkLocal in $sdkCandidates) {
  $sdkLocalSrc = Join-Path $sdkLocal "src"
  if (Test-Path $sdkLocalSrc) {
    & $python -m pip install --disable-pip-version-check -e $sdkLocal
    break
  }
}

# Ensure critical runtime deps exist even if sdk metadata install is incomplete.
& $python -m pip install --disable-pip-version-check aiohttp cryptography ecdsa pydantic typing_extensions

try {
  & $python -c "import cantex_sdk, aiohttp; print('deps_ok')"
} catch {
  Write-Host "Dependency check failed: missing cantex_sdk or aiohttp." -ForegroundColor Red
  Write-Host "Please ensure cantex_sdk exists under project folder (or parent folder) and Python >= 3.11." -ForegroundColor Yellow
  throw
}

$env:UI_HOST = "0.0.0.0"
$env:UI_PORT = "39087"
$env:NO_PROXY = "127.0.0.1,localhost,::1"
$env:no_proxy = "127.0.0.1,localhost,::1"

& .\.venv312\Scripts\python.exe .\ui_server.py
