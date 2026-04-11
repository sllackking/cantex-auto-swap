Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function New-VenvIfNeeded {
  if (Test-Path .venv312) { return }
  try {
    py -3.12 -m venv .venv312
    return
  } catch {}
  try {
    py -3.11 -m venv .venv312
    return
  } catch {}
  python -m venv .venv312
}

New-VenvIfNeeded
$python = Join-Path $PSScriptRoot ".venv312\Scripts\python.exe"

if (-not (Test-Path config.json)) { Copy-Item config.json.example config.json }
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
if (-not (Test-Path wallets.json)) { '[]' | Set-Content wallets.json -Encoding UTF8 }
if (-not (Test-Path bot.log)) { '' | Set-Content bot.log -Encoding UTF8 }

& $python -m pip install --disable-pip-version-check -q -U pip setuptools wheel

if (Test-Path requirements.txt) {
  $reqBody = (Get-Content requirements.txt -Raw)
  if ($reqBody -match '\S') {
    & $python -m pip install --disable-pip-version-check -r .\requirements.txt
  }
}

$sdkLocal = Join-Path $PSScriptRoot "cantex_sdk"
if (Test-Path (Join-Path $sdkLocal "src")) {
  & $python -m pip install --disable-pip-version-check -e $sdkLocal
}

& $python -m pip install --disable-pip-version-check aiohttp cryptography ecdsa pydantic typing_extensions

try {
  & $python -c "import cantex_sdk, aiohttp; print('deps_ok')"
} catch {
  Write-Host "依赖检查失败：cantex_sdk 或 aiohttp 不可用。" -ForegroundColor Red
  throw
}

$env:UI_HOST = "0.0.0.0"
$env:UI_PORT = "39087"
$env:NO_PROXY = "127.0.0.1,localhost,::1"
$env:no_proxy = "127.0.0.1,localhost,::1"

& $python .\ui_server.py
