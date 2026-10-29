<#
.SYNOPSIS
  Uninstall a TokenLean LITE release. The namespace, the Secrets and your Postgres data stay
  (usage rows, audit rows, proxy keys), so a later install picks up where this one stopped.

.DESCRIPTION
  uninstall.ps1 [-Namespace NS] [-Release NAME] [-Context NAME] [-Purge]

  -Namespace   default tokenlean-lite (or $env:TOKENLEAN_LITE_NAMESPACE)
  -Release     default tokenlean-lite (or $env:TOKENLEAN_LITE_RELEASE)
  -Context     the kubeconfig context to use (default: the current one)
  -Purge       also delete the Secrets install -CreateSecrets made, the bundled Redis's volume
               and the keys file smoke reads. Postgres is never touched.
#>
param(
    [string]$Namespace,
    [string]$Release,
    [string]$Context,
    [switch]$Purge
)
. (Join-Path $PSScriptRoot '_lib.ps1')

Initialize-Cloud -Namespace $Namespace -Release $Release -Context $Context
Assert-CloudTools
Write-Host "tokenlean-lite: uninstall $CloudRelease from namespace $CloudNamespace, context $(Get-CloudContextName)"
if ((Invoke-CloudHelm @('uninstall', $CloudRelease, '--namespace', $CloudNamespace, '--wait')).Code -ne 0) {
    Stop-Cloud 'helm uninstall failed'
}
if ($Purge) {
    $null = Invoke-CloudKubectl @('delete', 'secret', '-l', "$CloudCreatedBy,app.kubernetes.io/instance=$CloudRelease", '--ignore-not-found')
    $null = Invoke-CloudKubectl @('delete', 'pvc', '-l', "app.kubernetes.io/instance=$CloudRelease", '--ignore-not-found')
    $state = Get-CloudStateFile
    if (Test-Path $state) { Remove-Item -Force $state }
    Write-Host "tokenlean-lite: purged the release's generated Secrets, volumes and keys file"
}
Write-Host "tokenlean-lite: $CloudRelease uninstalled"
