Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue"

Set-Location $PSScriptRoot

function Write-Head($text) {
  Write-Host ""
  Write-Host ("==== " + $text + " ====") -ForegroundColor Cyan
}

function Show-Result($name, $ok, $detail) {
  if ($ok) {
    Write-Host ("[OK]   " + $name + " -> " + $detail) -ForegroundColor Green
  } else {
    Write-Host ("[FAIL] " + $name + " -> " + $detail) -ForegroundColor Red
  }
}

function Try-Invoke($scriptBlock) {
  try {
    & $scriptBlock
  } catch {
    return $_.Exception.Message
  }
  return $null
}

$apiHost = "api.cantex.io"
$apiUrl = "https://api.cantex.io"

Write-Head "Basic Environment"
$pyErr = Try-Invoke { python --version | Out-Host }
Show-Result "Python" ($null -eq $pyErr) ($(if($pyErr){$pyErr}else{"python command available"}))

Write-Head "DNS + TCP"
$dnsOk = $false
try {
  $dns = Resolve-DnsName $apiHost -Type A -ErrorAction Stop
  $ips = ($dns | Select-Object -ExpandProperty IPAddress)
  $dnsOk = $ips.Count -gt 0
  Show-Result "DNS resolve ($apiHost)" $dnsOk (($ips -join ", "))
} catch {
  Show-Result "DNS resolve ($apiHost)" $false $_.Exception.Message
}

try {
  $tcp = Test-NetConnection $apiHost -Port 443 -WarningAction SilentlyContinue
  Show-Result "TCP 443 ($apiHost)" ([bool]$tcp.TcpTestSucceeded) ("TcpTestSucceeded=" + $tcp.TcpTestSucceeded)
} catch {
  Show-Result "TCP 443 ($apiHost)" $false $_.Exception.Message
}

Write-Head "HTTPS Direct"
try {
  $resp = Invoke-WebRequest -UseBasicParsing -Uri $apiUrl -Method Get -TimeoutSec 15
  Show-Result "GET $apiUrl (direct)" $true ("HTTP " + [int]$resp.StatusCode)
} catch {
  $msg = $_.Exception.Message
  if ($_.Exception.Response -and $_.Exception.Response.StatusCode) {
    $msg = "HTTP " + [int]$_.Exception.Response.StatusCode
    # Any HTTP response means network path is reachable.
    Show-Result "GET $apiUrl (direct)" $true $msg
  } else {
    Show-Result "GET $apiUrl (direct)" $false $msg
  }
}

Write-Head "Proxy Detection (Clash Common Ports)"
$proxyPorts = @(7897, 7890, 7891, 1080)
$foundProxy = $null
foreach ($p in $proxyPorts) {
  try {
    $tnc = Test-NetConnection 127.0.0.1 -Port $p -WarningAction SilentlyContinue
    if ($tnc.TcpTestSucceeded) {
      $foundProxy = "http://127.0.0.1:$p"
      Show-Result "Local proxy port $p" $true "listening"
      break
    }
  } catch {}
}
if (-not $foundProxy) {
  Show-Result "Local proxy" $false "not found on 7897/7890/7891/1080"
}

Write-Head "HTTPS via Detected Proxy"
if ($foundProxy) {
  try {
    $respProxy = Invoke-WebRequest -UseBasicParsing -Uri $apiUrl -Method Get -Proxy $foundProxy -TimeoutSec 20
    Show-Result "GET $apiUrl via $foundProxy" $true ("HTTP " + [int]$respProxy.StatusCode)
  } catch {
    $msg = $_.Exception.Message
    if ($_.Exception.Response -and $_.Exception.Response.StatusCode) {
      $msg = "HTTP " + [int]$_.Exception.Response.StatusCode
      Show-Result "GET $apiUrl via $foundProxy" $true $msg
    } else {
      Show-Result "GET $apiUrl via $foundProxy" $false $msg
    }
  }
}

Write-Head "Recommended Startup"
if ($foundProxy) {
  Write-Host "If direct network is unstable, start UI with proxy env:" -ForegroundColor Yellow
  Write-Host ('$env:HTTPS_PROXY="' + $foundProxy + '"') -ForegroundColor Gray
  Write-Host ('$env:HTTP_PROXY="' + $foundProxy + '"') -ForegroundColor Gray
  Write-Host 'powershell -ExecutionPolicy Bypass -File .\run-ui.ps1' -ForegroundColor Gray
} else {
  Write-Host "No local proxy detected. If API timeout continues, enable Clash and rerun this check." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Done. Please send this output for troubleshooting." -ForegroundColor Cyan
