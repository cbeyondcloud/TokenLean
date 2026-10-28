# Shared by up.ps1, down.ps1 and smoke.ps1. Windows PowerShell 5.1+ and PowerShell 7
# (Windows, macOS, Linux). Keep this file ASCII: 5.1 reads scripts without a BOM as ANSI.

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$LiteDir = $PSScriptRoot
$LiteEnvFile = Join-Path $LiteDir '.env.lite'
$LiteStateDir = Join-Path $LiteDir '.state'
$LiteComposeFile = Join-Path $LiteDir 'docker-compose.lite.yml'
# Every compose call names this project explicitly (-p), so nothing else can redirect it.
# $env:LITE_PROJECT (environment only, never .env.lite) runs a second LITE stack next to the
# first, e.g. a review checkout or a CI job; give it its own LITE_*_PORT values too.
$LiteProject = if ($env:LITE_PROJECT) { $env:LITE_PROJECT } else { 'tokenlean-lite' }
if ($LiteProject -cnotmatch '^[a-z0-9][a-z0-9_-]*$') {
    Write-Host 'lite: LITE_PROJECT may hold only a-z 0-9 _ - (starting with a letter or digit)' -ForegroundColor Red
    exit 1
}
# docker (default) or podman. Both are driven as '<engine> compose ...'.
$LiteEngine = if ($env:CONTAINER_ENGINE) { $env:CONTAINER_ENGINE } else { 'docker' }
$LiteAllProfiles = @('--profile', 'qdrant', '--profile', 'langfuse', '--profile', 'mock',
                     '--profile', 'grafana')

# Compose settings exported for another stack must never steer these scripts: the full
# TokenLean .env sets COMPOSE_PROJECT_NAME=token-opt, and with it 'down -Purge' would
# remove that stack's containers and volumes.
foreach ($name in @('COMPOSE_PROJECT_NAME', 'COMPOSE_FILE', 'COMPOSE_PROFILES')) {
    [Environment]::SetEnvironmentVariable($name, $null, 'Process')
}

function Stop-Lite([string]$Message) {
    Write-Host "lite: $Message" -ForegroundColor Red
    exit 1
}

function Assert-LiteEngine {
    if (-not (Get-Command $LiteEngine -ErrorAction SilentlyContinue)) {
        Stop-Lite "'$LiteEngine' not found (set CONTAINER_ENGINE=docker or podman)"
    }
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $LiteEngine compose version 2>$null | Out-Null } finally { $ErrorActionPreference = $old }
    if ($LASTEXITCODE -ne 0) {
        Stop-Lite "'$LiteEngine compose' is not available (Docker Compose v2, or podman with a compose provider)"
    }
}

# Set each KEY=value of .env.lite in this process's environment, literally. A variable
# already set to a non-empty value wins, as it does for compose itself. COMPOSE_* keys are
# ignored (see above).
function Import-LiteEnv {
    if (-not (Test-Path $LiteEnvFile)) { Stop-Lite '.env.lite not found - run up.ps1 first' }
    foreach ($line in [IO.File]::ReadAllLines($LiteEnvFile)) {
        $l = $line.TrimEnd("`r")
        if ($l -eq '' -or $l.StartsWith('#')) { continue }
        $i = $l.IndexOf('=')
        if ($i -lt 1) { continue }
        $key = $l.Substring(0, $i)
        if ($key -notmatch '^[A-Za-z0-9_]+$' -or $key.StartsWith('COMPOSE_')) { continue }
        $current = [Environment]::GetEnvironmentVariable($key, 'Process')
        if ([string]::IsNullOrEmpty($current)) {
            [Environment]::SetEnvironmentVariable($key, $l.Substring($i + 1), 'Process')
        }
    }
}

# Run '<engine> compose -f docker-compose.lite.yml --env-file <file> <args>'; returns the
# exit code. Output goes to the console. Native stderr (compose's progress) must not be
# taken for an error, so the call runs with ErrorActionPreference=Continue.
function Invoke-LiteCompose([string[]]$ComposeArgs, [string]$EnvFile = $LiteEnvFile) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $LiteEngine compose -p $LiteProject -f $LiteComposeFile --env-file $EnvFile @ComposeArgs | Out-Host
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $old
    }
    return $code
}

function Get-LiteBaseUrl {
    $port = if ($env:LITE_PROXY_PORT) { $env:LITE_PROXY_PORT } else { '4100' }
    return "http://127.0.0.1:$port"
}

function Write-LiteFile([string]$Path, [string]$Text) {
    # UTF-8 without a BOM and LF line ends: read by compose, bash and Python.
    [IO.File]::WriteAllText($Path, $Text.Replace("`r`n", "`n"), (New-Object System.Text.UTF8Encoding $false))
}

function New-LiteHex([int]$Bytes) {
    $buffer = New-Object byte[] $Bytes
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($buffer) } finally { $rng.Dispose() }
    return -join ($buffer | ForEach-Object { $_.ToString('x2') })
}

function Get-LiteSha256([string]$Text) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { $hash = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Text)) } finally { $sha.Dispose() }
    return -join ($hash | ForEach-Object { $_.ToString('x2') })
}
