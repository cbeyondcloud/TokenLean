<#
.SYNOPSIS
  Upgrade an installed TokenLean LITE release: a new proxy image, changed values, or a newer
  chart. The migration hook runs first; the proxy rolls out one pod at a time.

.DESCRIPTION
  upgrade.ps1 <env> -Image <registry>/tokenlean-proxy:<release> [options]

  <env>        dev | staging | prod | managed: the values file values\<env>.yaml
  -Image       the proxy image, an exact release (or $env:TOKENLEAN_IMAGE)
  -Namespace   default tokenlean-lite (or $env:TOKENLEAN_LITE_NAMESPACE)
  -Release     default tokenlean-lite (or $env:TOKENLEAN_LITE_RELEASE)
  -Context     the kubeconfig context to use (default: the current one)
  -Values      your own values files over values\<env>.yaml
  -Timeout     how long to wait for the rollout (default 10m)
  -Mock        dev only: keep answering every model with the mock provider

  Stops when the release is not installed (use install.ps1). A failed upgrade leaves the
  previous revision serving; 'helm rollback <release> -n <namespace>' returns to it fully.
#>
param(
    [Parameter(Position = 0)][string]$Environment,
    [string]$Image,
    [string]$Namespace,
    [string]$Release,
    [string]$Context,
    [string[]]$Values,
    [string]$Timeout = '10m',
    [switch]$Mock
)
. (Join-Path $PSScriptRoot '_lib.ps1')

Initialize-Cloud -Namespace $Namespace -Release $Release -Context $Context
Invoke-CloudDeploy -Mode 'upgrade' -Environment $Environment -Image $Image -Values $Values `
    -Timeout $Timeout -Mock $Mock.IsPresent -CreateSecrets $false
