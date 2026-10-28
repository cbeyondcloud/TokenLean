<#
.SYNOPSIS
  Start the TokenLean LITE stack: proxy (LITE profile) + Postgres + Redis.

.DESCRIPTION
  up.ps1 [-Qdrant] [-Langfuse] [-Mock] [-Grafana] [-NoBuild]

  -Qdrant    also start Qdrant (for optimisations you later turn on that need it)
  -Langfuse  also start Langfuse and send traces to it (UI on LITE_LANGFUSE_PORT)
  -Mock      also start a local OpenAI-compatible stand-in and route every model to it:
             no provider key, no spend (what CI and smoke use)
  -Grafana   also start Grafana with the LITE dashboards, and the Prometheus they read
             (UI on LITE_GRAFANA_PORT; Per Call too with -Langfuse)
  -NoBuild   do not rebuild the proxy image

  First run: creates .env.lite from .env.lite.template and generates its secrets and proxy
  keys. Engine: $env:CONTAINER_ENGINE = 'docker' (default) or 'podman'.
  If scripts are blocked: powershell -ExecutionPolicy Bypass -File .\up.ps1
#>
param(
    [switch]$Qdrant,
    [switch]$Langfuse,
    [switch]$Mock,
    [switch]$Grafana,
    [switch]$NoBuild
)
. (Join-Path $PSScriptRoot '_lib.ps1')

Assert-LiteEngine

if (-not (Test-Path $LiteEnvFile)) {
    Write-LiteFile $LiteEnvFile ([IO.File]::ReadAllText((Join-Path $LiteDir '.env.lite.template')))
    Write-Host 'lite: created .env.lite (edit it to add provider keys)'
}

# Fill each generated value only where .env.lite has it empty or missing.
$lines = New-Object System.Collections.Generic.List[string]
foreach ($l in [IO.File]::ReadAllLines($LiteEnvFile)) { $lines.Add($l.TrimEnd("`r")) }

function Get-Value([string]$Key) {
    foreach ($l in $lines) { if ($l.StartsWith("$Key=")) { return $l.Substring($Key.Length + 1) } }
    return $null
}
function Set-IfEmpty([string]$Key, [string]$Value) {
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -eq "$Key=") { $lines[$i] = "$Key=$Value"; return }
        if ($lines[$i].StartsWith("$Key=")) { return }
    }
    $lines.Add("$Key=$Value")
}

$tenant = Get-Value 'LITE_TENANT_ID'
if ([string]::IsNullOrEmpty($tenant)) { $tenant = 'LITE-LOC-01' }
if ($tenant -notmatch '^[A-Za-z0-9._-]+$') { Stop-Lite 'LITE_TENANT_ID may hold only A-Z a-z 0-9 . _ -' }

Set-IfEmpty 'TOKENLEAN_API_KEY' ("tok-$tenant-" + (New-LiteHex 24))
Set-IfEmpty 'TOKENLEAN_ADMIN_KEY' ("tok-$tenant-admin-" + (New-LiteHex 24))
Set-IfEmpty 'DB_PASSWORD' (New-LiteHex 24)
Set-IfEmpty 'REDIS_PASSWORD' (New-LiteHex 24)
Set-IfEmpty 'QDRANT_API_KEY' (New-LiteHex 24)
Set-IfEmpty 'METRICS_SCRAPE_TOKEN' (New-LiteHex 24)
Set-IfEmpty 'LANGFUSE_PUBLIC_KEY' ('pk-lf-' + (New-LiteHex 16))
Set-IfEmpty 'LANGFUSE_SECRET_KEY' ('sk-lf-' + (New-LiteHex 24))
Set-IfEmpty 'LANGFUSE_NEXTAUTH_SECRET' (New-LiteHex 32)
Set-IfEmpty 'LANGFUSE_SALT' (New-LiteHex 32)
Set-IfEmpty 'LANGFUSE_INIT_USER_PASSWORD' (New-LiteHex 16)
Set-IfEmpty 'GRAFANA_ADMIN_PASSWORD' (New-LiteHex 16)
Write-LiteFile $LiteEnvFile (($lines -join "`n") + "`n")
Import-LiteEnv

# The proxy's key file: sha256(key) -> tenant. Only hashes; the keys stay in .env.lite.
New-Item -ItemType Directory -Force -Path $LiteStateDir | Out-Null
$userHash = Get-LiteSha256 $env:TOKENLEAN_API_KEY
$adminHash = Get-LiteSha256 $env:TOKENLEAN_ADMIN_KEY
$keys = "{`n  `"$userHash`": {`"tenant_id`": `"$tenant`", `"tier`": `"free`"},`n" +
        "  `"$adminHash`": {`"tenant_id`": `"$tenant`", `"tier`": `"free`", `"admin`": true}`n}`n"
Write-LiteFile (Join-Path $LiteStateDir 'local-keys.json') $keys
if ($IsLinux -or $IsMacOS) {
    # PowerShell 7 off Windows: the proxy runs as uid 1000, whoever owns this checkout.
    & chmod 755 $LiteStateDir
    & chmod 644 (Join-Path $LiteStateDir 'local-keys.json')
}

# Prometheus (-Grafana) presents METRICS_SCRAPE_TOKEN to /metrics: owner-only file, read by
# the Prometheus container as root. Written on every up, so an edited token is picked up.
$promDir = Join-Path $LiteStateDir 'prometheus'
New-Item -ItemType Directory -Force -Path $promDir | Out-Null
$tokenFile = Join-Path $promDir 'metrics-scrape-token'
Write-LiteFile $tokenFile $env:METRICS_SCRAPE_TOKEN
if ($IsLinux -or $IsMacOS) {
    & chmod 700 $promDir
    & chmod 600 $tokenFile
}

# Overlays (paths inside the proxy container) and compose profiles for this run.
$overlays = '/app/lite/config/agentic.yaml'
$profiles = @()
if ($Qdrant) { $profiles += @('--profile', 'qdrant') }
if ($Langfuse) {
    $profiles += @('--profile', 'langfuse')
    $overlays += ',/app/lite/config/langfuse.yaml'
}
$env:LITE_GRAFANA_LANGFUSE = 'off'
if ($Grafana) {
    $profiles += @('--profile', 'grafana')
    # Per Call and its Langfuse datasource only when Langfuse runs.
    if ($Langfuse) { $env:LITE_GRAFANA_LANGFUSE = 'on' }
}
$mode = 'live'
if ($Mock) {
    $profiles += @('--profile', 'mock')
    $overlays += ',/app/lite/mock/mock-provider.yaml'
    $mode = 'mock'
}
if ($env:LITE_EXTRA_OVERLAYS) { $overlays += ",$($env:LITE_EXTRA_OVERLAYS)" }
$env:LITE_CONFIG_OVERLAYS = $overlays
Write-LiteFile (Join-Path $LiteStateDir 'stack.env') "LITE_MODE=$mode`n"

$upArgs = @('up', '-d', '--remove-orphans')
if ($env:TOKENLEAN_IMAGE) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $LiteEngine image inspect $env:TOKENLEAN_IMAGE 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) { & $LiteEngine pull $env:TOKENLEAN_IMAGE | Out-Host }
    } finally { $ErrorActionPreference = $old }
    if ($LASTEXITCODE -ne 0) { Stop-Lite "could not pull $($env:TOKENLEAN_IMAGE)" }
    $upArgs += '--no-build'
} elseif (-not $NoBuild) {
    $upArgs += '--build'
}

Write-Host "lite: starting ($mode mode) with $LiteEngine ..."
$code = Invoke-LiteCompose ($profiles + $upArgs)
if ($code -ne 0) { Stop-Lite "compose up failed (exit $code)" }

$url = (Get-LiteBaseUrl) + '/health'
$timeout = if ($env:LITE_HEALTH_TIMEOUT) { [int]$env:LITE_HEALTH_TIMEOUT } else { 240 }
$deadline = (Get-Date).AddSeconds($timeout)
Write-Host -NoNewline "lite: waiting for $url "
$last = ''
while ($true) {
    try {
        $last = (Invoke-WebRequest -UseBasicParsing -Uri $url -TimeoutSec 5).Content
        if ($last -match '"status":"ok"') { break }
    } catch {
        $last = $_.Exception.Message
    }
    if ((Get-Date) -gt $deadline) {
        Write-Host ''
        Invoke-LiteCompose ($profiles + @('ps')) | Out-Null
        Invoke-LiteCompose @('logs', '--tail', '60', 'proxy') | Out-Null
        Stop-Lite "the proxy did not report healthy within ${timeout}s (last: $last)"
    }
    Write-Host -NoNewline '.'
    Start-Sleep -Seconds 3
}
Write-Host ' ok'

# Proxy keys live in Postgres (the LITE profile's key_store). The keys of .env.lite are copied
# in only while its table is empty (the first up on fresh volumes), so check that the admin
# key still opens /admin/keys.
$code = 0
try {
    $r = Invoke-WebRequest -UseBasicParsing -Uri ((Get-LiteBaseUrl) + "/admin/keys?tenant_id=$tenant") `
        -Headers @{ Authorization = "Bearer $($env:TOKENLEAN_ADMIN_KEY)" } -TimeoutSec 10
    $code = [int]$r.StatusCode
} catch {
    if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
}
if ($code -ne 200) {
    Write-Host "lite: warning: TOKENLEAN_ADMIN_KEY was refused by /admin/keys (HTTP $code)." -ForegroundColor Yellow
    Write-Host 'lite:   The key store copies the keys of .env.lite only into an empty table; a key changed' -ForegroundColor Yellow
    Write-Host 'lite:   in .env.lite since is not there. Issue keys through /admin/keys, or start over' -ForegroundColor Yellow
    Write-Host 'lite:   with down.ps1 -Purge (deletes the data) and up.ps1.' -ForegroundColor Yellow
}
Write-Host "lite: proxy  $(Get-LiteBaseUrl)  (OpenAI /v1/chat/completions, Anthropic /v1/messages, Gemini /v1beta)"
Write-Host 'lite: admin  /admin/keys (proxy keys, team + user binding), /admin/routing (team caps, routing), /admin/usage-export'
Write-Host "lite: key    TOKENLEAN_API_KEY in $LiteEnvFile"
if ($Langfuse) {
    $lfPort = if ($env:LITE_LANGFUSE_PORT) { $env:LITE_LANGFUSE_PORT } else { '3110' }
    Write-Host "lite: langfuse http://127.0.0.1:$lfPort (password in .env.lite)"
}
if ($Grafana) {
    $gfPort = if ($env:LITE_GRAFANA_PORT) { $env:LITE_GRAFANA_PORT } else { '3120' }
    Write-Host "lite: grafana http://127.0.0.1:$gfPort (admin, GRAFANA_ADMIN_PASSWORD in .env.lite; folder TokenLean LITE)"
}
Write-Host 'lite: next   .\smoke.ps1'
exit 0
