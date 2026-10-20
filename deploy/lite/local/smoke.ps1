<#
.SYNOPSIS
  Smoke-test a running TokenLean LITE stack. Exit 0 when every check passes.

.DESCRIPTION
  smoke.ps1   model: $env:SMOKE_MODEL, else the mock (after up.ps1 -Mock), else a model for
              the first provider key set in .env.lite

  Checks: /health; an OpenAI chat completion (x-tokenlean-request-id + x-tokenlean-cost-usd);
  the same streamed (the final ': x-tokenlean-cost' frame and [DONE]); an Anthropic
  /v1/messages call (x-api-key auth, x-tokenlean-* headers); then a usage row for each of the
  three calls, through /admin/usage-export. TokenLean has no /v1/responses endpoint.
  Live mode makes three small real calls (a fraction of a cent). HTTP only, any engine.
#>
. (Join-Path $PSScriptRoot '_lib.ps1')

Import-LiteEnv
if (-not $env:TOKENLEAN_API_KEY) { Stop-Lite 'TOKENLEAN_API_KEY is empty - run up.ps1 first' }
$key = $env:TOKENLEAN_API_KEY

$mode = 'live'
$stackFile = Join-Path $LiteStateDir 'stack.env'
if (Test-Path $stackFile) {
    foreach ($l in [IO.File]::ReadAllLines($stackFile)) {
        if ($l.StartsWith('LITE_MODE=')) { $mode = $l.Substring(10).Trim() }
    }
}
$model = $env:SMOKE_MODEL
if (-not $model) {
    if ($mode -eq 'mock') { $model = 'lite-mock-1' }
    elseif ($env:LLM_KEY_OPENAI) { $model = 'gpt-4o-mini' }
    elseif ($env:LLM_KEY_ANTHROPIC) { $model = 'claude-haiku-4-5' }
    elseif ($env:LLM_KEY_GEMINI -or $env:LLM_KEY_GOOGLE) { $model = 'gemini-2.5-flash' }
    else { Stop-Lite 'no provider key in .env.lite: set one, set SMOKE_MODEL, or use up.ps1 -Mock' }
}

$base = Get-LiteBaseUrl
$nonce = "$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())-$PID"
$script:fails = 0
$ids = New-Object System.Collections.Generic.List[string]

function Write-Pass([string]$Name) { Write-Host "PASS  $Name" }
function Write-Fail([string]$Name) { Write-Host "FAIL  $Name"; $script:fails++ }

function Get-Text($Response) {
    if ($Response.Content -is [byte[]]) { return [Text.Encoding]::UTF8.GetString($Response.Content) }
    return [string]$Response.Content
}
function Get-Header($Response, [string]$Name) {
    $value = $Response.Headers[$Name]
    if ($null -eq $value) { return '' }
    return (@($value) -join ',').Trim()
}
# POST JSON; returns @{ Status; Text; Response } and never throws on an HTTP error.
function Send-Json([string]$Path, [string]$Json, [hashtable]$Headers) {
    try {
        $r = Invoke-WebRequest -UseBasicParsing -Method Post -Uri "$base$Path" -Headers $Headers `
            -ContentType 'application/json' -Body ([Text.Encoding]::UTF8.GetBytes($Json)) -TimeoutSec 120
        return @{ Status = [int]$r.StatusCode; Text = (Get-Text $r); Response = $r }
    } catch {
        $status = 0
        if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode }
        return @{ Status = $status; Text = $_.Exception.Message; Response = $null }
    }
}
function Get-Snippet([string]$Text) {
    if ($Text.Length -gt 300) { return $Text.Substring(0, 300) }
    return $Text
}

Write-Host "lite smoke: $base  model=$model  mode=$mode"
$bearer = @{ Authorization = "Bearer $key" }

# 1. health
try {
    $h = Invoke-WebRequest -UseBasicParsing -Uri "$base/health" -TimeoutSec 10
    if ((Get-Text $h) -match '"status":"ok"') { Write-Pass 'health' } else { Write-Fail 'health' }
} catch { Write-Fail "health ($($_.Exception.Message))" }

# 2. OpenAI chat completion
$body = "{`"model`":`"$model`",`"max_tokens`":16,`"messages`":[{`"role`":`"user`",`"content`":`"Reply with the word pong. ($nonce a)`"}]}"
$r = Send-Json '/v1/chat/completions' $body $bearer
$id1 = ''; $cost1 = ''
if ($r.Response) { $id1 = Get-Header $r.Response 'x-tokenlean-request-id'; $cost1 = Get-Header $r.Response 'x-tokenlean-cost-usd' }
if ($r.Status -eq 200 -and $id1 -and $cost1 -and $r.Text -match '"choices"') {
    Write-Pass "chat completion (cost `$$cost1)"; $ids.Add($id1)
} else {
    Write-Fail "chat completion (status $($r.Status), request-id '$id1', cost '$cost1'): $(Get-Snippet $r.Text)"
}

# 3. OpenAI chat completion, streamed
$body = "{`"model`":`"$model`",`"max_tokens`":16,`"stream`":true,`"messages`":[{`"role`":`"user`",`"content`":`"Reply with the word pong. ($nonce b)`"}]}"
$r = Send-Json '/v1/chat/completions' $body $bearer
$id2 = ''; $cost2 = ''
$m = [regex]::Match($r.Text, '(?m)^: x-tokenlean-cost (\{.*\})')
if ($m.Success) {
    $frame = $m.Groups[1].Value
    $id2 = [regex]::Match($frame, '"request_id":"([^"]*)"').Groups[1].Value
    $cost2 = [regex]::Match($frame, '"cost_usd":"([^"]*)"').Groups[1].Value
}
if ($r.Status -eq 200 -and $id2 -and $r.Text -match '(?m)^data: \[DONE\]') {
    Write-Pass "chat completion stream (final cost frame `$$cost2)"; $ids.Add($id2)
} else {
    Write-Fail "chat completion stream (status $($r.Status), no cost frame or [DONE]): $(Get-Snippet $r.Text)"
}

# 4. Anthropic messages
$body = "{`"model`":`"$model`",`"max_tokens`":16,`"messages`":[{`"role`":`"user`",`"content`":`"Reply with the word pong. ($nonce c)`"}]}"
$r = Send-Json '/v1/messages' $body @{ 'x-api-key' = $key; 'anthropic-version' = '2023-06-01' }
$id3 = ''; $cost3 = ''
if ($r.Response) { $id3 = Get-Header $r.Response 'x-tokenlean-request-id'; $cost3 = Get-Header $r.Response 'x-tokenlean-cost-usd' }
if ($r.Status -eq 200 -and $id3 -and $cost3 -and $r.Text -match '"type":"message"') {
    Write-Pass "messages (cost `$$cost3)"; $ids.Add($id3)
} else {
    Write-Fail "messages (status $($r.Status), request-id '$id3', cost '$cost3'): $(Get-Snippet $r.Text)"
}

# 5. a usage row for each call (rows are written just after each response)
if ($ids.Count -gt 0) {
    $missing = @()
    for ($try = 0; $try -lt 15; $try++) {
        $u = Send-Json '/admin/usage-export' '{}' $bearer
        $missing = @($ids | Where-Object { $u.Text -notmatch [regex]::Escape("`"request_id`":`"$_`"") })
        if ($missing.Count -eq 0) { break }
        Start-Sleep -Seconds 2
    }
    if ($missing.Count -eq 0) { Write-Pass "usage rows ($($ids.Count) of 3 calls)" }
    else { Write-Fail "usage rows missing for: $($missing -join ' ')" }
} else {
    Write-Fail 'usage rows (no call succeeded)'
}

if ($script:fails -gt 0) {
    Write-Host "lite smoke: $($script:fails) check(s) failed"
    exit 1
}
Write-Host 'lite smoke: all checks passed'
exit 0
