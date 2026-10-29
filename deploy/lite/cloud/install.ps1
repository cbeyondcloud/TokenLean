<#
.SYNOPSIS
  Install TokenLean LITE on a Kubernetes cluster with the LITE Helm chart (an installed release
  is upgraded in place). Waits until the proxy has rolled out.

.DESCRIPTION
  install.ps1 <env> -Image <registry>/tokenlean-proxy:<release> [options]

  <env>           dev | staging | prod | managed: the values file values\<env>.yaml
  -Image          the proxy image, an exact release (or $env:TOKENLEAN_IMAGE)
  -Namespace      default tokenlean-lite (or $env:TOKENLEAN_LITE_NAMESPACE)
  -Release        default tokenlean-lite (or $env:TOKENLEAN_LITE_RELEASE)
  -Context        the kubeconfig context to use (default: the current one)
  -Values         your own values files over values\<env>.yaml: hosts, Secret names, sizes.
                  Never secret values: name Secrets instead.
  -Timeout        how long to wait for the rollout (default 10m)
  -Mock           dev only: answer every model with the bundled mock provider (no provider
                  keys, no spend)
  -CreateSecrets  dev only: create the Secrets values\dev.yaml names that are missing (Redis,
                  metrics, bootstrap keys, LLM_KEY_* from your environment), not Postgres's.
                  smoke.ps1 then finds the keys by itself.

  Needs helm 3.8+ (or 4) and kubectl (or $env:HELM / $env:KUBECTL), and the release's Secrets.
#>
param(
    [Parameter(Position = 0)][string]$Environment,
    [string]$Image,
    [string]$Namespace,
    [string]$Release,
    [string]$Context,
    [string[]]$Values,
    [string]$Timeout = '10m',
    [switch]$Mock,
    [switch]$CreateSecrets
)
. (Join-Path $PSScriptRoot '_lib.ps1')

Initialize-Cloud -Namespace $Namespace -Release $Release -Context $Context
Invoke-CloudDeploy -Mode 'install' -Environment $Environment -Image $Image -Values $Values `
    -Timeout $Timeout -Mock $Mock.IsPresent -CreateSecrets $CreateSecrets.IsPresent
