# Shared by install.ps1, upgrade.ps1, uninstall.ps1 and smoke.ps1. Windows PowerShell 5.1+ and
# PowerShell 7 (Windows, macOS, Linux). Keep this file ASCII: 5.1 reads scripts without a BOM as
# ANSI. helm (3.8+ or 4) and kubectl come from PATH, or from $env:HELM / $env:KUBECTL.

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$CloudDir = $PSScriptRoot
$CloudChart = Join-Path (Join-Path $CloudDir 'helm') 'tokenlean-lite'
# Owner-only files written by install -CreateSecrets / -Mock and read by smoke: the dev bootstrap
# keys and the mode of each release (<namespace>.<release>.env). Git-ignored.
$CloudStateDir = if ($env:TOKENLEAN_LITE_STATE_DIR) { $env:TOKENLEAN_LITE_STATE_DIR } else { Join-Path $CloudDir '.state' }
$CloudHelm = if ($env:HELM) { $env:HELM } else { 'helm' }
$CloudKubectl = if ($env:KUBECTL) { $env:KUBECTL } else { 'kubectl' }
# Labels the Secrets that install -CreateSecrets made, so uninstall -Purge finds only them.
$CloudCreatedBy = 'app.kubernetes.io/created-by=tokenlean-lite-install'
$CloudOnWindows = [IO.Path]::DirectorySeparatorChar -eq '\'

function Stop-Cloud([string]$Message) {
    Write-Host "tokenlean-lite: $Message" -ForegroundColor Red
    exit 1
}

function Test-CloudName([string]$What, [string]$Name, [int]$Max) {
    if ($Name -cnotmatch '^[a-z0-9]([a-z0-9-]*[a-z0-9])?$') {
        Stop-Cloud "the $What '$Name' may hold only a-z 0-9 - (starting and ending with a letter or digit)"
    }
    if ($Name.Length -gt $Max) { Stop-Cloud "the $What '$Name' is longer than $Max characters" }
}

# The settings every script shares, from its parameters or the environment.
function Initialize-Cloud([string]$Namespace, [string]$Release, [string]$Context) {
    $script:CloudNamespace = if ($Namespace) { $Namespace } elseif ($env:TOKENLEAN_LITE_NAMESPACE) { $env:TOKENLEAN_LITE_NAMESPACE } else { 'tokenlean-lite' }
    $script:CloudRelease = if ($Release) { $Release } elseif ($env:TOKENLEAN_LITE_RELEASE) { $env:TOKENLEAN_LITE_RELEASE } else { 'tokenlean-lite' }
    $script:CloudContext = $Context
    Test-CloudName 'namespace' $script:CloudNamespace 63
    Test-CloudName 'release' $script:CloudRelease 53
}

function Assert-CloudEnv([string]$Environment, [bool]$Mock, [bool]$CreateSecrets) {
    if (-not $Environment) { Stop-Cloud 'name the environment: dev, staging, prod or managed (Get-Help for details)' }
    if (@('dev', 'staging', 'prod', 'managed') -cnotcontains $Environment) {
        Stop-Cloud "the environment must be dev, staging, prod or managed (got '$Environment')"
    }
    if ($Mock -and $Environment -ne 'dev') {
        Stop-Cloud '-Mock is for dev only: it answers every model with the mock provider'
    }
    if ($CreateSecrets -and $Environment -ne 'dev') {
        Stop-Cloud "-CreateSecrets is for dev only: create $Environment's Secrets from your secret store"
    }
}

# registry[:port]/repository:tag -> @{ Repo; Tag }, refused unless the tag is an exact release.
function Split-CloudImage([string]$Image) {
    if (-not $Image) { Stop-Cloud '-Image (or TOKENLEAN_IMAGE) must name the proxy image, e.g. registry.example.com/tokenlean-proxy:1.0.0' }
    $last = $Image.Substring($Image.LastIndexOf('/') + 1)
    if (-not $last.Contains(':')) { Stop-Cloud "-Image $Image has no tag: name an exact release (repository:tag)" }
    $tag = $last.Substring($last.LastIndexOf(':') + 1)
    if (@('', 'latest', 'set-me') -contains $tag) { Stop-Cloud "-Image ${Image}: name an exact release, never latest" }
    return @{ Repo = $Image.Substring(0, $Image.Length - $tag.Length - 1); Tag = $tag }
}

function Assert-CloudTools {
    foreach ($tool in @($CloudHelm, $CloudKubectl)) {
        if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
            Stop-Cloud "'$tool' not found: install helm 3.8+ (or 4) and kubectl, or set HELM / KUBECTL"
        }
    }
}

# Run a native tool. Native stderr (progress, warnings) must not be taken for an error, so the
# call runs with ErrorActionPreference=Continue. Returns @{ Code; Output }.
function Invoke-CloudNative([string]$Exe, [string[]]$Arguments, [switch]$Quiet, [switch]$Capture) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $out = @()
    try {
        if ($Capture) { $out = @(& $Exe @Arguments 2>$null) }
        elseif ($Quiet) { & $Exe @Arguments 2>$null | Out-Null }
        else { & $Exe @Arguments | Out-Host }
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $old
    }
    return @{ Code = $code; Output = $out }
}

function Invoke-CloudHelm([string[]]$Arguments, [switch]$Quiet) {
    $all = @($Arguments)
    if ($script:CloudContext) { $all += @('--kube-context', $script:CloudContext) }
    return Invoke-CloudNative $CloudHelm $all -Quiet:$Quiet
}

function Invoke-CloudKubectl([string[]]$Arguments, [switch]$Quiet, [switch]$Capture, [switch]$NoNamespace) {
    $all = @()
    if ($script:CloudContext) { $all += @('--context', $script:CloudContext) }
    if (-not $NoNamespace) { $all += @('-n', $script:CloudNamespace) }
    $all += $Arguments
    return Invoke-CloudNative $CloudKubectl $all -Quiet:$Quiet -Capture:$Capture
}

function Get-CloudSelector([string]$Component) {
    return "app.kubernetes.io/instance=$($script:CloudRelease),app.kubernetes.io/component=$Component"
}

function Get-CloudContextName {
    if ($script:CloudContext) { return $script:CloudContext }
    $r = Invoke-CloudNative $CloudKubectl @('config', 'current-context') -Capture
    if ($r.Code -eq 0 -and $r.Output.Count) { return [string]$r.Output[0] }
    return '(the current one)'
}

function Write-CloudFile([string]$Path, [string]$Text) {
    # UTF-8 without a BOM and LF line ends: read by kubectl, bash and Python.
    [IO.File]::WriteAllText($Path, $Text.Replace("`r`n", "`n"), (New-Object System.Text.UTF8Encoding $false))
}

function Protect-CloudPath([string]$Path, [string]$Mode) {
    # Owner-only on macOS/Linux. On Windows the file inherits its folder's ACL (your profile or
    # checkout).
    if (-not $CloudOnWindows) { & chmod $Mode $Path }
}

function Get-CloudStateFile {
    return Join-Path $CloudStateDir "$($script:CloudNamespace).$($script:CloudRelease).env"
}

function Get-CloudState([string]$Key) {
    $file = Get-CloudStateFile
    if (-not (Test-Path $file)) { return '' }
    foreach ($line in [IO.File]::ReadAllLines($file)) {
        if ($line.StartsWith("$Key=")) { return $line.Substring($Key.Length + 1).TrimEnd("`r") }
    }
    return ''
}

function Set-CloudState([string]$Key, [string]$Value) {
    if (-not (Test-Path $CloudStateDir)) {
        $null = New-Item -ItemType Directory -Path $CloudStateDir
        Protect-CloudPath $CloudStateDir '700'
    }
    $file = Get-CloudStateFile
    $lines = @()
    if (Test-Path $file) { $lines = @([IO.File]::ReadAllLines($file) | Where-Object { -not $_.StartsWith("$Key=") }) }
    $lines += "$Key=$Value"
    Write-CloudFile $file (($lines -join "`n") + "`n")
    Protect-CloudPath $file '600'
}

function New-CloudHex([int]$Bytes) {
    $buffer = New-Object byte[] $Bytes
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($buffer) } finally { $rng.Dispose() }
    return -join ($buffer | ForEach-Object { $_.ToString('x2') })
}

function Get-CloudSha256([string]$Text) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { $hash = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Text)) } finally { $sha.Dispose() }
    return -join ($hash | ForEach-Object { $_.ToString('x2') })
}

# Create the Secret from files (never from values on the command line) and label it, unless it
# exists. Returns $true when it was created.
function New-CloudSecret([string]$Name, [string[]]$KeyFiles) {
    if ((Invoke-CloudKubectl @('get', 'secret', $Name) -Quiet).Code -eq 0) {
        Write-Host "tokenlean-lite: kept the existing Secret $Name"
        return $false
    }
    $create = @('create', 'secret', 'generic', $Name)
    foreach ($kf in $KeyFiles) { $create += "--from-file=$kf" }
    if ((Invoke-CloudKubectl $create -Quiet).Code -ne 0) { Stop-Cloud "could not create the Secret $Name" }
    $null = Invoke-CloudKubectl @('label', 'secret', $Name, $CloudCreatedBy, "app.kubernetes.io/instance=$($script:CloudRelease)") -Quiet
    Write-Host "tokenlean-lite: created the Secret $Name"
    return $true
}

# Dev only: the Secrets values/dev.yaml names, except Postgres's (it belongs to your dev
# Postgres): random passwords, bootstrap proxy keys (hashes in the Secret, the keys in the
# owner-only state file for smoke), and the provider keys set as LLM_KEY_* in the environment.
function New-CloudDevSecrets {
    $tenant = if ($env:LITE_TENANT_ID) { $env:LITE_TENANT_ID } else { 'LITE-DEV-01' }
    if ($tenant -notmatch '^[A-Za-z0-9._-]+$') { Stop-Cloud 'LITE_TENANT_ID may hold only A-Z a-z 0-9 . _ -' }
    if ((Invoke-CloudKubectl @('get', 'namespace', $script:CloudNamespace) -Quiet).Code -ne 0) {
        $null = Invoke-CloudKubectl @('create', 'namespace', $script:CloudNamespace) -Quiet -NoNamespace
    }
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("tokenlean-lite-" + (New-CloudHex 8))
    $null = New-Item -ItemType Directory -Path $tmp
    Protect-CloudPath $tmp '700'
    try {
        $api = "tok-$tenant-$(New-CloudHex 24)"
        $admin = "tok-$tenant-admin-$(New-CloudHex 24)"
        $keysFile = Join-Path $tmp 'local-keys.json'
        Write-CloudFile $keysFile ("{`n  `"$(Get-CloudSha256 $api)`": {`"tenant_id`": `"$tenant`", `"tier`": `"free`"},`n" +
            "  `"$(Get-CloudSha256 $admin)`": {`"tenant_id`": `"$tenant`", `"tier`": `"free`", `"admin`": true}`n}`n")
        if (New-CloudSecret 'tokenlean-lite-bootstrap-keys' @("local-keys.json=$keysFile")) {
            Set-CloudState 'TOKENLEAN_API_KEY' $api
            Set-CloudState 'TOKENLEAN_ADMIN_KEY' $admin
            Set-CloudState 'LITE_TENANT_ID' $tenant
            Write-Host "tokenlean-lite: the bootstrap keys are in $(Get-CloudStateFile) (owner-only)"
        } elseif (-not (Get-CloudState 'TOKENLEAN_API_KEY')) {
            Write-Host 'tokenlean-lite: note: no keys on file for this release; smoke needs TOKENLEAN_API_KEY and TOKENLEAN_ADMIN_KEY' -ForegroundColor Yellow
        }
        $redis = Join-Path $tmp 'redis-password'
        Write-CloudFile $redis (New-CloudHex 24)
        $null = New-CloudSecret 'tokenlean-lite-redis' @("password=$redis")
        $token = Join-Path $tmp 'scrape-token'
        Write-CloudFile $token (New-CloudHex 24)
        $null = New-CloudSecret 'tokenlean-lite-metrics' @("scrape-token=$token")
        $provider = @()
        foreach ($item in (Get-ChildItem env: | Where-Object { $_.Name -cmatch '^LLM_KEY_[A-Z0-9_]+$' -and $_.Value })) {
            $file = Join-Path $tmp $item.Name
            Write-CloudFile $file $item.Value
            $provider += "$($item.Name)=$file"
        }
        $null = New-CloudSecret 'tokenlean-lite-provider-keys' $provider
    } finally {
        Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    }
}

# What a failed rollout looks like: the release's pods, recent events, the proxy's last logs.
function Show-CloudDiagnosis {
    Write-Host "tokenlean-lite: pods of release $($script:CloudRelease):"
    $null = Invoke-CloudKubectl @('get', 'pods', '-l', "app.kubernetes.io/instance=$($script:CloudRelease)")
    $events = Invoke-CloudKubectl @('get', 'events', '--sort-by=.lastTimestamp') -Capture
    $events.Output | Select-Object -Last 15 | Out-Host
    $null = Invoke-CloudKubectl @('logs', '-l', (Get-CloudSelector 'proxy'), '--tail', '40')
    $null = Invoke-CloudKubectl @('logs', '-l', (Get-CloudSelector 'migrations'), '--tail', '40')
}

# install / upgrade: helm upgrade [--install] with values/<env>.yaml, then wait for the rollout.
function Invoke-CloudDeploy([string]$Mode, [string]$Environment, [string]$Image, [string[]]$Values,
                            [string]$Timeout, [bool]$Mock, [bool]$CreateSecrets) {
    Assert-CloudEnv $Environment $Mock $CreateSecrets
    if (-not $Image) { $Image = $env:TOKENLEAN_IMAGE }
    $img = Split-CloudImage $Image
    Assert-CloudTools
    $valuesFile = Join-Path (Join-Path $CloudDir 'values') "$Environment.yaml"
    if (-not (Test-Path $valuesFile)) { Stop-Cloud "$valuesFile not found" }
    foreach ($v in @($Values | Where-Object { $_ })) {
        if (-not (Test-Path $v)) { Stop-Cloud "-Values ${v}: file not found" }
    }
    if ($Mode -eq 'upgrade') {
        if ((Invoke-CloudHelm @('status', $script:CloudRelease, '--namespace', $script:CloudNamespace) -Quiet).Code -ne 0) {
            Stop-Cloud "no release $($script:CloudRelease) in namespace $($script:CloudNamespace): run install.ps1 first"
        }
    }
    Write-Host "tokenlean-lite: $Mode $($script:CloudRelease) ($Environment, $Image) in namespace $($script:CloudNamespace), context $(Get-CloudContextName)"
    if ($CreateSecrets) { New-CloudDevSecrets }

    $helmArgs = @('upgrade')
    if ($Mode -eq 'install') { $helmArgs += '--install' }
    $helmArgs += @($script:CloudRelease, $CloudChart, '--namespace', $script:CloudNamespace)
    if ($Mode -eq 'install') { $helmArgs += '--create-namespace' }
    $helmArgs += @('-f', $valuesFile)
    foreach ($v in @($Values | Where-Object { $_ })) { $helmArgs += @('-f', $v) }
    $helmArgs += @('--set', "image.repository=$($img.Repo)", '--set', "image.tag=$($img.Tag)")
    if ($Mock) { $helmArgs += @('--set', 'mockProvider.enabled=true') }
    $helmArgs += @('--wait', '--timeout', $Timeout)
    if ((Invoke-CloudHelm $helmArgs).Code -ne 0) {
        Show-CloudDiagnosis
        Stop-Cloud "helm $Mode failed (the release keeps its previous revision; helm history $($script:CloudRelease) -n $($script:CloudNamespace))"
    }

    $found = Invoke-CloudKubectl @('get', 'deployment', '-l', (Get-CloudSelector 'proxy'), '-o', 'name') -Capture
    $deployment = @($found.Output | Where-Object { $_ }) | Select-Object -First 1
    if (-not $deployment) { Stop-Cloud "no proxy Deployment found for release $($script:CloudRelease)" }
    if ((Invoke-CloudKubectl @('rollout', 'status', [string]$deployment, '--timeout', $Timeout)).Code -ne 0) {
        Show-CloudDiagnosis
        Stop-Cloud "the proxy did not roll out within $Timeout"
    }
    Set-CloudState 'LITE_MODE' $(if ($Mock) { 'mock' } else { 'live' })
    Write-Host "tokenlean-lite: $($script:CloudRelease) is up in namespace $($script:CloudNamespace)"
    Write-Host "tokenlean-lite: next   smoke.ps1 -Namespace $($script:CloudNamespace) -Release $($script:CloudRelease)"
}
