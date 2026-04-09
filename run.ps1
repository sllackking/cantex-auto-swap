Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

$venvPath = ".venv312"

if (-not (Test-Path $venvPath)) {
  py -3.12 -m venv $venvPath
}

. "$venvPath\Scripts\Activate.ps1"

if (-not (Test-Path config.json)) {
  Copy-Item config.json.example config.json
}

if (-not (Test-Path .env)) {
  Copy-Item .env.example .env
}

$hasSdk = (& python -c "import importlib.util; print('1' if importlib.util.find_spec('cantex_sdk') else '0')").Trim()
if ($hasSdk -ne "1") {
  $localSdkCandidates = @(
    "D:\\CCnetwork\\cantex_sdk",
    (Join-Path $PSScriptRoot "vendor\\cantex_sdk")
  )
  foreach ($sdkPath in $localSdkCandidates) {
    if (Test-Path $sdkPath) {
      Write-Host "Installing local cantex_sdk from: $sdkPath"
      python -m pip install -e $sdkPath
      break
    }
  }
}

$hasSdk = (& python -c "import importlib.util; print('1' if importlib.util.find_spec('cantex_sdk') else '0')").Trim()
if ($hasSdk -ne "1") {
  Write-Host ""
  Write-Host "cantex_sdk is not installed."
  Write-Host "Please place SDK source at D:\\CCnetwork\\cantex_sdk or D:\\CCnetwork\\cantex-auto-swap\\vendor\\cantex_sdk"
  Write-Host "Then run this script again."
  exit 1
}

python .\src\main.py --config .\config.json --dotenv .\.env
