Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

if (-not (Test-Path .venv312)) {
  py -3.12 -m venv .venv312
}

. .\.venv312\Scripts\Activate.ps1

if (-not (Test-Path config.json)) {
  Copy-Item config.json.example config.json
}

if (-not (Test-Path .env)) {
  Copy-Item .env.example .env
}

$env:UI_HOST = "0.0.0.0"
$env:UI_PORT = "39087"
$env:NO_PROXY = "127.0.0.1,localhost,::1"
$env:no_proxy = "127.0.0.1,localhost,::1"

& .\.venv312\Scripts\python.exe .\ui_server.py
