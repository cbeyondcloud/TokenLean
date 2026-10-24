<#
.SYNOPSIS
  Smoke-test a running TokenLean LITE stack. Exit 0 when every check passes.

.DESCRIPTION
  smoke.ps1   model: $env:SMOKE_MODEL, else the mock (after up.ps1 -Mock), else a model for
              the first provider key set in .env.lite

  Checks: /health; an OpenAI chat completion (x-tokenlean-request-id + x-tokenlean-cost-usd,
  and no x-tokenlean-model-substituted); the same streamed (the request-id header, and the
  final ': x-tokenlean-cost' frame, cost to 6 decimals, before [DONE]); an Anthropic
  /v1/messages call (x-api-key auth, x-tokenlean-* headers); a usage row for each of the three
  calls, through /admin/usage-export; then a team-bound key issued through /admin/keys: served,
  its usage row carries its team, its team's spend cap (set through /admin/routing, then rolled
  back; skipped when an overlay is already set) answers 429, and once revoked it gets 401.
  Live mode makes about six small real calls (a fraction of a cent). HTTP only, any engine.
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
# Send a request; returns @{ Status; Text; Response } and never throws on an HTTP error (Text
# is then the error body when the server sent one).
function Send-Request([string]$Method, [string]$Path, [string]$Json, [hashtable]$Headers) {
    try {
        $params = @{ UseBasicParsing = $true; Method = $Method; Uri = "$base$Path"
                     Headers = $Headers; TimeoutSec = 120 }
        if ($Json) {
            $params.ContentType = 'application/json'
            $params.Body = [Text.Encoding]::UTF8.GetBytes($Json)
        }
        $r = Invoke-WebRequest @params
        return @{ Status = [int]$r.StatusCode; Text = (Get-Text $r); Response = $r }
    } catch {
        $status = 0
        if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode }
        $text = $_.Exception.Message
        if ($_.ErrorDetails -and $_.ErrorDetails.Message) { $text = $_.ErrorDetails.Message }
        return @{ Status = $status; Text = $text; Response = $null }
    }
}
function Send-Json([string]$Path, [string]$Json, [hashtable]$Headers) {
    return Send-Request 'Post' $Path $Json $Headers
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
$id1 = ''; $cost1 = ''; $sub1 = ''
if ($r.Response) {
    $id1 = Get-Header $r.Response 'x-tokenlean-request-id'
    $cost1 = Get-Header $r.Response 'x-tokenlean-cost-usd'
    $sub1 = Get-Header $r.Response 'x-tokenlean-model-substituted'
}
if ($r.Status -eq 200 -and $id1 -and $cost1 -and -not $sub1 -and $r.Text -match '"choices"') {
    Write-Pass "chat completion (cost `$$cost1, model not substituted)"; $ids.Add($id1)
} else {
    Write-Fail "chat completion (status $($r.Status), request-id '$id1', cost '$cost1', substituted '$sub1'): $(Get-Snippet $r.Text)"
}

# 3. OpenAI chat completion, streamed
$body = "{`"model`":`"$model`",`"max_tokens`":16,`"stream`":true,`"messages`":[{`"role`":`"user`",`"content`":`"Reply with the word pong. ($nonce b)`"}]}"
$r = Send-Json '/v1/chat/completions' $body $bearer
$id2 = ''; $cost2 = ''; $hid2 = ''
$m = [regex]::Match($r.Text, '(?m)^: x-tokenlean-cost (\{.*\})')
if ($m.Success) {
    $frame = $m.Groups[1].Value
    $id2 = [regex]::Match($frame, '"request_id":"([^"]*)"').Groups[1].Value
    $cost2 = [regex]::Match($frame, '"cost_usd":"([^"]*)"').Groups[1].Value
}
if ($r.Response) { $hid2 = Get-Header $r.Response 'x-tokenlean-request-id' }   # before any chunk
# The cost frame comes right before [DONE], with the cost to 6 decimal places.
$done = [regex]::Match($r.Text, '(?m)^data: \[DONE\]')
$ordered = $m.Success -and $done.Success -and ($m.Index -lt $done.Index)
if ($r.Status -eq 200 -and $id2 -and $hid2 -eq $id2 -and $ordered -and $cost2 -match '^\d+\.\d{6}$') {
    Write-Pass "chat completion stream (request-id header, final cost frame `$$cost2 before [DONE])"; $ids.Add($id2)
} else {
    Write-Fail "chat completion stream (status $($r.Status), header id '$hid2', frame id '$id2', cost '$cost2', frame before [DONE] $ordered): $(Get-Snippet $r.Text)"
}

# 4. Anthropic messages
$body = "{`"model`":`"$model`",`"max_tokens`":16,`"messages`":[{`"role`":`"user`",`"content`":`"Reply with the word pong. ($nonce c)`"}]}"
$r = Send-Json '/v1/messages' $body @{ 'x-api-key' = $key; 'anthropic-version' = '2023-06-01' }
$id3 = ''; $cost3 = ''; $sub3 = ''
if ($r.Response) {
    $id3 = Get-Header $r.Response 'x-tokenlean-request-id'
    $cost3 = Get-Header $r.Response 'x-tokenlean-cost-usd'
    $sub3 = Get-Header $r.Response 'x-tokenlean-model-substituted'
}
if ($r.Status -eq 200 -and $id3 -and $cost3 -and -not $sub3 -and $r.Text -match '"type":"message"') {
    Write-Pass "messages (cost `$$cost3, model not substituted)"; $ids.Add($id3)
} else {
    Write-Fail "messages (status $($r.Status), request-id '$id3', cost '$cost3', substituted '$sub3'): $(Get-Snippet $r.Text)"
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

# 6. a team-bound key, issued through /admin/keys (keys live in Postgres): it is served, its
# usage row carries its team, its team's spend cap is enforced, and once revoked it is refused.
function Get-JsonField([string]$Text, [string]$Name) {
    return [regex]::Match($Text, "`"$Name`":`"([^`"]*)`"").Groups[1].Value
}
$tenant = if ($env:LITE_TENANT_ID) { $env:LITE_TENANT_ID } else { 'LITE-LOC-01' }
$team = "lite-smoke-$nonce"
$admin = @{ Authorization = "Bearer $($env:TOKENLEAN_ADMIN_KEY)" }
$r = Send-Json '/admin/keys' "{`"tenant_id`":`"$tenant`",`"team`":`"$team`",`"label`":`"lite smoke`"}" $admin
$teamKey = Get-JsonField $r.Text 'key'
$teamKeyId = Get-JsonField $r.Text 'key_id'
if ($r.Status -eq 201 -and $teamKey -and $teamKeyId) {
    Write-Pass "team key issued (team $team, key_id $teamKeyId)"
    $teamAuth = @{ Authorization = "Bearer $teamKey" }
    function Send-TeamCall([string]$Suffix) {
        $b = "{`"model`":`"$model`",`"max_tokens`":16,`"messages`":[{`"role`":`"user`",`"content`":`"Reply with the word pong. ($nonce team $Suffix)`"}]}"
        return Send-Json '/v1/chat/completions' $b $teamAuth
    }
    $t = Send-TeamCall 'a'
    $tid = ''
    if ($t.Response) { $tid = Get-Header $t.Response 'x-tokenlean-request-id' }
    if ($t.Status -eq 200 -and $tid) {
        $row = ''
        for ($try = 0; $try -lt 10; $try++) {
            $u = Send-Json '/admin/usage-export' '{}' $teamAuth
            $row = @($u.Text -split "`n" | Where-Object { $_ -match [regex]::Escape("`"request_id`":`"$tid`"") }) | Select-Object -First 1
            if ($row) { break }
            Start-Sleep -Seconds 2
        }
        if ($row -and $row -match [regex]::Escape("`"team`":`"$team`"")) {
            Write-Pass "team key served; its usage row carries team $team"
        } else { Write-Fail "team key usage row (request $tid): $row" }
    } else {
        Write-Fail "team key call (status $($t.Status)): $(Get-Snippet $t.Text)"
    }

    # The team's cap, set through /admin/routing only while the routing overlay is empty (a PUT
    # replaces the whole overlay); rolled back to that empty version afterwards.
    $current = Send-Request 'Get' '/admin/routing' '' $admin
    $rver = [regex]::Match($current.Text, '^\{"version":(\d+),').Groups[1].Value
    if ($rver -and $current.Text -match '"fragment":\{\}') {
        $fragment = "{`"tenants`":{`"$tenant`":{`"teams`":{`"$team`":{`"monthly_spend_cap_usd`":0.000001}}}}}"
        $put = Send-Request 'Put' '/admin/routing' "{`"expected_version`":$rver,`"comment`":`"lite smoke`",`"fragment`":$fragment}" $admin
        if ($put.Status -eq 200) {
            $capped = $false; $last = 0
            for ($i = 1; $i -le 5; $i++) {   # the first call's cost reaches the counter just after it
                $c = Send-TeamCall "b$i"
                $last = $c.Status
                if ($c.Status -eq 429 -and $c.Text -match 'team_spend_cap_exceeded') { $capped = $true; break }
                Start-Sleep -Seconds 1
            }
            if ($capped) { Write-Pass 'team spend cap enforced (429 team_spend_cap_exceeded)' }
            else { Write-Fail "team spend cap not enforced (last status $last)" }
            $undo = Send-Json '/admin/routing/rollback' "{`"version`":$rver,`"comment`":`"lite smoke: undo`"}" $admin
            if ($undo.Status -ne 200) {
                Write-Fail "routing overlay not rolled back to version $rver (status $($undo.Status)): $(Get-Snippet $undo.Text)"
            }
        } else {
            Write-Fail "team spend cap (PUT /admin/routing status $($put.Status)): $(Get-Snippet $put.Text)"
        }
    } else {
        Write-Host 'SKIP  team spend cap (a routing overlay is set; smoke does not replace it)'
    }

    $del = Send-Request 'Delete' "/admin/keys/$teamKeyId" '' $admin
    $after = Send-TeamCall 'c'
    if ($del.Status -eq 200 -and $after.Status -eq 401) { Write-Pass 'team key revoked and refused' }
    else { Write-Fail "team key revoke (DELETE status $($del.Status), then call status $($after.Status))" }
} else {
    Write-Fail "team key issue (POST /admin/keys status $($r.Status)): $(Get-Snippet $r.Text)"
}

if ($script:fails -gt 0) {
    Write-Host "lite smoke: $($script:fails) check(s) failed"
    exit 1
}
Write-Host 'lite smoke: all checks passed'
exit 0
