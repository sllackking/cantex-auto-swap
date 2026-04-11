Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path .venv312)) {
  try { py -3.12 -m venv .venv312 } catch { try { py -3.11 -m venv .venv312 } catch { python -m venv .venv312 } }
}

$python = Join-Path $PSScriptRoot ".venv312\Scripts\python.exe"
if (-not (Test-Path config.json)) { Copy-Item config.json.example config.json }
if (-not (Test-Path .env)) { Copy-Item .env.example .env }

& $python -m pip install --disable-pip-version-check -q -U pip setuptools wheel
if (Test-Path (Join-Path $PSScriptRoot "cantex_sdk\src")) {
  & $python -m pip install --disable-pip-version-check -e (Join-Path $PSScriptRoot "cantex_sdk")
}
& $python -m pip install --disable-pip-version-check aiohttp cryptography ecdsa pydantic typing_extensions

& $python .\src\main.py --config .\config.json --dotenv .\.env
