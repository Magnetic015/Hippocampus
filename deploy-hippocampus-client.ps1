# deploy-hippocampus-client.ps1 —— Hippocampus MCP 客户端一键部署（Windows / Codex 侧）
# 规范托管：/home/kkp/hippocampus/deploy-hippocampus-client.ps1（Pi5 192.168.2.41）。
# 拉取即用（需已有到 kkp@192.168.2.41 的 SSH）：
#   scp kkp@192.168.2.41:/home/kkp/hippocampus/deploy-hippocampus-client.ps1 . ; ./deploy-hippocampus-client.ps1
# 部署即自动注册：client 已存在则轮换 token；不存在则按服务模式派生项目、签发默认 14 天 token 并绑定本机 IP。
# 安全：token 经 SSH stdout 取回、不入 argv；config.toml 只放 bearer_token_env_var 引用；
#       生成的 launcher 仅向 Codex 子进程注入 token，绝不写 User 级持久环境。
param(
    [string]$Source = "auto",
    [string]$ClientId = "windows-codex",
    [string]$Project = "auto",
    [int]$IssueExpiresDays = 14,
    [string]$Pi5Ssh = "kkp@192.168.2.41",
    [string]$McpUrl = "http://192.168.2.41:8888/mcp/",
    [string]$TokenEnv = "HIPPOCAMPUS_CODEX_TOKEN",
    [string]$TokenFile = "$HOME\.codex\hippocampus\codex.token",
    [string]$LauncherFile = "$HOME\.codex\hippocampus\launch-codex-hippocampus.ps1",
    [string]$CodexConfig = "$HOME\.codex\config.toml",
    [string]$McpContainer = "hippocampus-hippocampus-mcp-1",
    [string]$StateDb = "/data/state/outbox.db"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Log([string]$Message) {
    Write-Host "[deploy] $Message"
}

function Die([string]$Message) {
    throw "[deploy] $Message"
}

function Clear-LegacyTokenEnvironment {
    # Older installers persisted the secret under HKCU\Environment. Remove that
    # copy before doing any work; the generated launcher uses Process scope only.
    [Environment]::SetEnvironmentVariable($TokenEnv, $null, "User")
    [Environment]::SetEnvironmentVariable($TokenEnv, $null, "Process")
}

if ($IssueExpiresDays -lt 1) {
    Die "IssueExpiresDays must be a positive integer."
}
if ($TokenEnv -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') {
    Die "TokenEnv must be a valid environment variable name."
}

function Write-Utf8NoBom([string]$Path, [string]$Content) {
    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $utf8NoBom)
}

function Backup-File([string]$Path) {
    if (Test-Path -LiteralPath $Path) {
        $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
        $backup = "$Path.bak.$stamp"
        Copy-Item -LiteralPath $Path -Destination $backup -Force
        Log "Backed up $(Split-Path -Leaf $Path) -> $(Split-Path -Leaf $backup)"
    }
}

function Get-PeerIp {
    $targetHost = ([Uri]$McpUrl).Host
    $result = Test-NetConnection $targetHost -InformationLevel Detailed
    if (-not $result.PingSucceeded) {
        Die "Cannot reach $targetHost from this machine."
    }
    if (-not $result.SourceAddress) {
        Die "Cannot determine local source IP for $targetHost."
    }
    if ($result.SourceAddress.PSObject.Properties["IPAddress"]) {
        return "$($result.SourceAddress.IPAddress)"
    }
    if ($result.SourceAddress.PSObject.Properties["IPv4Address"]) {
        return "$($result.SourceAddress.IPv4Address)"
    }
    return "$($result.SourceAddress)"
}

function Test-TokenFile([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) {
        return $false
    }
    $token = (Get-Content -LiteralPath $Path -Raw).Trim()
    if ([string]::IsNullOrWhiteSpace($token)) {
        return $false
    }

    $body = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"codex-deploy-probe","version":"0"}}}'
    $client = [System.Net.Http.HttpClient]::new()
    try {
        $request = [System.Net.Http.HttpRequestMessage]::new([System.Net.Http.HttpMethod]::Post, $McpUrl)
        $request.Headers.Accept.ParseAdd("application/json")
        $request.Headers.Accept.ParseAdd("text/event-stream")
        $request.Headers.Authorization = [System.Net.Http.Headers.AuthenticationHeaderValue]::new("Bearer", $token)
        $request.Content = [System.Net.Http.StringContent]::new($body, [System.Text.Encoding]::UTF8, "application/json")
        $response = $client.SendAsync($request).GetAwaiter().GetResult()
        return ([int]$response.StatusCode) -eq 200
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Write-TokenFile([string]$Token) {
    $dir = Split-Path -Parent $TokenFile
    if (-not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    Write-Utf8NoBom -Path $TokenFile -Content $Token
}

function Provision-Token {
    $peerIp = Get-PeerIp
    Log "Provisioning a fresh token on Pi5 for $ClientId bound to $peerIp ..."

    $remoteScript = @'
set -euo pipefail
CID="$1"; CTN="$2"; DB="$3"; PEER="$4"; PROJ="$5"; EXPIRES_DAYS="$6"
MODE="$(docker exec "$CTN" sh -c 'printf %s "${HIPPOCAMPUS_MODE:-commissioning}"')"
case "$MODE" in
  commissioning)
    [ "$PROJ" = auto ] && PROJ=commissioning
    [ "$PROJ" = commissioning ] || {
      echo "commissioning mode requires project=commissioning" >&2; exit 2;
    }
    ;;
  production)
    [ "$PROJ" = auto ] && PROJ=soul
    ;;
  *) echo "unsupported HIPPOCAMPUS_MODE" >&2; exit 2;;
esac
[[ "$PROJ" =~ ^[a-z0-9][a-z0-9_-]{0,63}$ ]] || {
  echo "invalid project" >&2; exit 2;
}
[[ "$EXPIRES_DAYS" =~ ^[1-9][0-9]*$ ]] || {
  echo "invalid issue expiry" >&2; exit 2;
}
tok="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
# 已注册则轮换；未注册则 issue 自动注册（部署即自动注册 client_id，授予 $PROJ 读写）
if ! printf '%s' "$tok" | docker exec -i "$CTN" python -m hippocampus.registry_cli --db "$DB" rotate "$CID" >/dev/null 2>&1; then
  printf '%s' "$tok" | docker exec -i "$CTN" python -m hippocampus.registry_cli --db "$DB" issue "$CID" --source-tag "$CID" --readable "$PROJ" --writable "$PROJ" --expires-days "$EXPIRES_DAYS" >/dev/null
fi
docker exec "$CTN" python -m hippocampus.registry_cli --db "$DB" bind-peer "$CID" "$PEER" >/dev/null 2>&1 || true
printf '%s' "$tok"
'@

    $token = $remoteScript | & ssh -T -o BatchMode=yes -o ConnectTimeout=8 $Pi5Ssh bash -s -- $ClientId $McpContainer $StateDb $peerIp $Project $IssueExpiresDays
    if ($LASTEXITCODE -ne 0) {
        Die "Pi5 token provisioning failed."
    }
    $token = "$token".Trim()
    if ([string]::IsNullOrWhiteSpace($token)) {
        Die "Pi5 returned an empty token."
    }
    Write-TokenFile -Token $token
}

function Copy-TokenFromPi5([string]$RemotePath) {
    Log "Copying token from Pi5 path: $RemotePath"
    $token = & ssh -T -o BatchMode=yes -o ConnectTimeout=8 $Pi5Ssh cat -- $RemotePath
    if ($LASTEXITCODE -ne 0) {
        Die "Failed to copy token from Pi5."
    }
    $token = "$token".Trim()
    if ([string]::IsNullOrWhiteSpace($token)) {
        Die "Pi5 returned an empty token."
    }
    Write-TokenFile -Token $token
}

function Acquire-Token {
    switch -Regex ($Source) {
        '^auto$' {
            if (Test-TokenFile -Path $TokenFile) {
                Log "Reusing existing working token."
            } else {
                Log "No working local token found. Provisioning from Pi5."
                Provision-Token
            }
            break
        }
        '^provision$' {
            Provision-Token
            break
        }
        '^copy:(.+)$' {
            Copy-TokenFromPi5 -RemotePath $Matches[1]
            break
        }
        default {
            Die "Unknown source mode: $Source"
        }
    }
}

function Quote-PowerShellSingle([string]$Value) {
    return "'" + $Value.Replace("'", "''") + "'"
}

function Write-CodexLauncher {
    $tokenFileLiteral = Quote-PowerShellSingle -Value $TokenFile
    $tokenEnvLiteral = Quote-PowerShellSingle -Value $TokenEnv
    $launcher = @"
param(
    [string]`$CodexExecutable = "codex"
)
Set-StrictMode -Version Latest
`$ErrorActionPreference = "Stop"
`$tokenFile = $tokenFileLiteral
`$tokenEnv = $tokenEnvLiteral
`$token = (Get-Content -LiteralPath `$tokenFile -Raw).Trim()
if ([string]::IsNullOrWhiteSpace(`$token)) {
    throw "Hippocampus token file is empty."
}
`$previous = [Environment]::GetEnvironmentVariable(`$tokenEnv, "Process")
try {
    [Environment]::SetEnvironmentVariable(`$tokenEnv, `$token, "Process")
    Start-Process -FilePath `$CodexExecutable | Out-Null
} finally {
    [Environment]::SetEnvironmentVariable(`$tokenEnv, `$previous, "Process")
    `$token = `$null
}
"@
    Write-Utf8NoBom -Path $LauncherFile -Content $launcher
    Log "Wrote process-scoped Codex launcher at $LauncherFile"
}

function Update-CodexConfig {
    $managedStart = "# >>> hippocampus-codex:start >>>"
    $managedEnd = "# <<< hippocampus-codex:end <<<"
    $block = @"
$managedStart
[mcp_servers.hippocampus]
url = "$McpUrl"
bearer_token_env_var = "$TokenEnv"
enabled = true
required = false
enabled_tools = ["memory_search", "memory_read", "memory_commit", "memory_status"]
$managedEnd
"@

    if (-not (Test-Path -LiteralPath $CodexConfig)) {
        Write-Utf8NoBom -Path $CodexConfig -Content ""
    } else {
        Backup-File -Path $CodexConfig
    }

    $content = Get-Content -LiteralPath $CodexConfig -Raw
    $managedPattern = '(?ms)^# >>> hippocampus-codex:start >>>\r?\n.*?^# <<< hippocampus-codex:end <<<\r?\n?'
    $plainPattern = '(?ms)^\[mcp_servers\.hippocampus\]\r?\n.*?(?=^\[|\z)'

    if ($content -match $managedPattern) {
        $content = [regex]::Replace($content, $managedPattern, $block + [Environment]::NewLine)
    } elseif ($content -match $plainPattern) {
        $content = [regex]::Replace($content, $plainPattern, $block + [Environment]::NewLine)
    } else {
        $content = $content.TrimEnd()
        if ($content.Length -gt 0) {
            $content += [Environment]::NewLine + [Environment]::NewLine + $block + [Environment]::NewLine
        } else {
            $content = $block + [Environment]::NewLine
        }
    }

    Write-Utf8NoBom -Path $CodexConfig -Content $content
    Log "Updated Codex MCP config at $CodexConfig"
}

Log "Target MCP endpoint: $McpUrl"
Log "Token source mode: $Source"

Clear-LegacyTokenEnvironment
Acquire-Token
Update-CodexConfig
Write-CodexLauncher

if (Test-TokenFile -Path $TokenFile) {
    Log "Token authentication check passed."
} else {
    Die "Token authentication check failed."
}

Write-Host ""
Write-Host "[deploy] Done."
Write-Host "[deploy] Fully exit Codex, then launch it through: $LauncherFile"
Write-Host "[deploy] If Codex is not on PATH, pass -CodexExecutable <path-to-Codex.exe>."
Write-Host "[deploy] After restart, the server should appear as: hippocampus"
