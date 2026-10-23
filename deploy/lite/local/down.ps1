<#
.SYNOPSIS
  Stop the TokenLean LITE stack (every optional service too). Data volumes are kept.

.DESCRIPTION
  down.ps1 [-Purge]

  -Purge  also delete the volumes (Postgres, Redis, Qdrant data). .env.lite is kept, so the
          next up.ps1 reuses the same keys and passwords on fresh databases.

  Engine: $env:CONTAINER_ENGINE = 'docker' (default) or 'podman'.
#>
param([switch]$Purge)
. (Join-Path $PSScriptRoot '_lib.ps1')

Assert-LiteEngine
$envFile = $LiteEnvFile
if (Test-Path $LiteEnvFile) {
    Import-LiteEnv
} else {
    # compose checks these even to stop; any value will do when there is no .env.lite
    $env:DB_PASSWORD = 'unused'
    $env:REDIS_PASSWORD = 'unused'
    $envFile = Join-Path $LiteDir '.env.lite.template'
}

$downArgs = @('down', '--remove-orphans')
if ($Purge) { $downArgs += '--volumes' }
$code = Invoke-LiteCompose ($LiteAllProfiles + $downArgs) $envFile
if ($code -ne 0) { Stop-Lite "compose down failed (exit $code)" }
if ($Purge) { Write-Host 'lite: stopped and volumes removed' } else { Write-Host 'lite: stopped' }
exit 0
