<#
.SYNOPSIS
  Smoke-test a TokenLean LITE release on a cluster: the same checks as
  deploy\lite\local\smoke.ps1 (health, chat, stream, messages, cost headers, usage rows, a
  team-bound key with its cap and its revocation), run against the ingress URL or a
  port-forward. Exit 0 when all pass.

.DESCRIPTION
  smoke.ps1 [-Url URL] [-Namespace NS] [-Release NAME] [-Context NAME] [-LocalPort N]

  -Url         the release's address (its ingress, https://...). Without it, the proxy Service
               is port-forwarded to 127.0.0.1:<LocalPort> (default 4180).
  -Namespace   default tokenlean-lite (or $env:TOKENLEAN_LITE_NAMESPACE)
  -Release     default tokenlean-lite (or $env:TOKENLEAN_LITE_RELEASE)
  -Context     the kubeconfig context to use (default: the current one)

  Keys: $env:TOKENLEAN_API_KEY and $env:TOKENLEAN_ADMIN_KEY (and $env:LITE_TENANT_ID, their
  tenant), else the ones install.ps1 -CreateSecrets wrote. Model: $env:SMOKE_MODEL, else the
  mock when the release was installed with -Mock. A private CA must be in the system store.
#>
param(
    [string]$Url,
    [string]$Namespace,
    [string]$Release,
    [string]$Context,
    [int]$LocalPort = 4180
)
. (Join-Path $PSScriptRoot '_lib.ps1')

Initialize-Cloud -Namespace $Namespace -Release $Release -Context $Context
$apiKey = if ($env:TOKENLEAN_API_KEY) { $env:TOKENLEAN_API_KEY } else { Get-CloudState 'TOKENLEAN_API_KEY' }
$adminKey = if ($env:TOKENLEAN_ADMIN_KEY) { $env:TOKENLEAN_ADMIN_KEY } else { Get-CloudState 'TOKENLEAN_ADMIN_KEY' }
$tenant = if ($env:LITE_TENANT_ID) { $env:LITE_TENANT_ID } else { Get-CloudState 'LITE_TENANT_ID' }
if (-not $tenant) { $tenant = 'LITE-DEV-01' }
if (-not $apiKey) { Stop-Cloud 'TOKENLEAN_API_KEY is not set: set it and TOKENLEAN_ADMIN_KEY, or install a dev release with -CreateSecrets' }
if (-not $adminKey) { Stop-Cloud 'TOKENLEAN_ADMIN_KEY is not set (the team-key checks use /admin/keys)' }
$model = $env:SMOKE_MODEL
if (-not $model) {
    if ((Get-CloudState 'LITE_MODE') -eq 'mock') { $model = 'lite-mock-1' }
    else { Stop-Cloud 'set SMOKE_MODEL to a model the release answers (or install a dev release with -Mock)' }
}

$tmp = Join-Path ([IO.Path]::GetTempPath()) ("lite-cloud-smoke-" + (New-CloudHex 8))
$null = New-Item -ItemType Directory -Path $tmp
Protect-CloudPath $tmp '700'
$forward = $null
$code = 1
try {
    if (-not $Url) {
        Assert-CloudTools
        $found = Invoke-CloudKubectl @('get', 'service', '-l', (Get-CloudSelector 'proxy'), '-o', 'name') -Capture
        $svc = @($found.Output | Where-Object { $_ }) | Select-Object -First 1
        if (-not $svc) { Stop-Cloud "no proxy Service for release $CloudRelease in namespace $CloudNamespace" }
        $pfArgs = @()
        if ($CloudContext) { $pfArgs += @('--context', $CloudContext) }
        $pfArgs += @('-n', $CloudNamespace, 'port-forward', [string]$svc, "${LocalPort}:http")
        $forward = Start-Process -FilePath $CloudKubectl -ArgumentList $pfArgs -PassThru -NoNewWindow `
            -RedirectStandardOutput (Join-Path $tmp 'port-forward.out') -RedirectStandardError (Join-Path $tmp 'port-forward.err')
        $Url = "http://127.0.0.1:$LocalPort"
        for ($i = 0; $i -lt 30; $i++) {
            try {
                $null = Invoke-WebRequest -Uri "$Url/health" -UseBasicParsing -TimeoutSec 2
                break
            } catch {
                if ($forward.HasExited) { Stop-Cloud ('port-forward stopped: ' + [IO.File]::ReadAllText((Join-Path $tmp 'port-forward.err'))) }
                Start-Sleep -Seconds 1
            }
        }
    }

    # The keys reach the local smoke in an env file in a private folder, never on a command line.
    $envFile = Join-Path $tmp 'smoke.env'
    Write-CloudFile $envFile "TOKENLEAN_API_KEY=$apiKey`nTOKENLEAN_ADMIN_KEY=$adminKey`nLITE_TENANT_ID=$tenant`n"
    Protect-CloudPath $envFile '600'
    Write-Host "tokenlean-lite smoke: release $CloudRelease in namespace $CloudNamespace at $Url"
    foreach ($name in @('TOKENLEAN_API_KEY', 'TOKENLEAN_ADMIN_KEY', 'LITE_TENANT_ID')) {
        [Environment]::SetEnvironmentVariable($name, $null, 'Process')
    }
    $env:LITE_ENV_FILE = $envFile
    $env:LITE_BASE_URL = $Url
    $env:SMOKE_MODEL = $model
    $shell = (Get-Process -Id $PID).Path
    $localSmoke = Join-Path (Join-Path (Split-Path $CloudDir -Parent) 'local') 'smoke.ps1'
    & $shell -NoProfile -ExecutionPolicy Bypass -File $localSmoke
    $code = $LASTEXITCODE
} finally {
    if ($forward -and -not $forward.HasExited) { Stop-Process -Id $forward.Id -Force -ErrorAction SilentlyContinue }
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}
exit $code
