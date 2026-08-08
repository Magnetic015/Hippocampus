#!/usr/bin/env bash
# deploy-hippocampus-client.sh —— Hippocampus MCP 客户端一键部署（Mac 侧）
#
# 作用：从 Pi5 取得客户端 token → 写入本机 0600 文件 → 合并 ~/.claude.json 的
#       hippocampus http MCP 条目 → 安装启动注入 wrapper（标准 Claude.app）→
#       校验 token 可认证。完成后「完全退出 Claude → 经 wrapper/分身 重启」即开箱可用。
#
# 安全约定（与 plan 04 §4 一致，勿破坏）：
#   * token 只经 SSH 通道落到 0600 文件；绝不进 argv / stdout / 日志 / shell 历史。
#   * ~/.claude.json 里只放 `Bearer ${HIPPOCAMPUS_CLAUDE_TOKEN}` 变量引用，不落明文。
#   * 注入靠 wrapper/分身 launcher 的进程内 env；绝不用 `launchctl setenv`（全局泄漏）。
#
# ⚠️ 轮换副作用：当没有可用本机 token（或 --source provision）时，脚本会在 Pi5 上
#   为该 client 轮换出新 token —— 这会使旧 token 立即失效，所有用同一 client 的
#   Mac 客户端（标准版 + 分身，共享同一 0600 文件）都需用新 token，重启后恢复。
#
# 用法：
#   ./deploy-hippocampus-client.sh                     # auto：有可用 token 则沿用，否则 Pi5 轮换
#   ./deploy-hippocampus-client.sh --source provision  # 强制在 Pi5 轮换新 token
#   ./deploy-hippocampus-client.sh --source copy:/home/kkp/.config/hippocampus/mac-claude.token
#   变量可覆盖：PI5_SSH / MCP_URL / CLIENT_ID / TOKEN_FILE / MCP_CONTAINER / STATE_DB ...
set -euo pipefail

# ---------------- 配置（按需覆盖为环境变量） ----------------
PI5_SSH="${PI5_SSH:-kkp@192.168.2.41}"
MCP_URL="${MCP_URL:-http://192.168.2.41:8888/mcp/}"
CLIENT_ID="${CLIENT_ID:-mac-claude}"
TOKEN_ENV="${TOKEN_ENV:-HIPPOCAMPUS_CLAUDE_TOKEN}"
TOKEN_FILE="${TOKEN_FILE:-$HOME/.config/hippocampus/claude.token}"
CLAUDE_JSON="${CLAUDE_JSON:-$HOME/.claude.json}"
WRAPPER="${WRAPPER:-$HOME/.config/hippocampus/launch-claude.command}"
STD_APP="${STD_APP:-/Applications/Claude.app}"
MCP_CONTAINER="${MCP_CONTAINER:-hippocampus-hippocampus-mcp-1}"
STATE_DB="${STATE_DB:-/data/state/outbox.db}"
TOKEN_SOURCE="${TOKEN_SOURCE:-auto}"     # auto | provision | copy:<pi5-path>
# -----------------------------------------------------------

while [ $# -gt 0 ]; do
  case "$1" in
    --source) TOKEN_SOURCE="${2:?}"; shift 2;;
    --client) CLIENT_ID="${2:?}"; shift 2;;
    --pi5) PI5_SSH="${2:?}"; shift 2;;
    -h|--help) sed -n '2,30p' "$0"; exit 0;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

umask 077
log(){ printf '[deploy] %s\n' "$*" >&2; }
die(){ printf '[deploy] ERROR: %s\n' "$*" >&2; exit 1; }
SSH(){ ssh -T -o BatchMode=yes -o ConnectTimeout=8 "$PI5_SSH" "$@"; }

command -v curl >/dev/null || die "curl 缺失"
command -v jq   >/dev/null || die "jq 缺失（用于安全合并 ~/.claude.json）"
mkdir -p "$(dirname "$TOKEN_FILE")" "$(dirname "$WRAPPER")"

# 用一次 initialize 探测 token 是否可认证（200=通过），不回显 token
auth_ok(){ # $1=token 文件
  [ -r "$1" ] || return 1
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 -X POST \
    -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
    -H "Authorization: Bearer $(cat "$1")" \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"deploy-probe","version":"0"}}}' \
    "$MCP_URL" 2>/dev/null) || return 1
  [ "$code" = "200" ]
}

# 本机到达 Pi5 所用网卡的 LAN IPv4（用于 peer 绑定）
mac_peer_ip(){
  local host iface
  host="$(printf '%s' "$MCP_URL" | sed -E 's#^[a-z]+://([^:/]+).*#\1#')"
  iface="$(route -n get "$host" 2>/dev/null | awk '/interface:/{print $2}')"
  [ -n "${iface:-}" ] && ipconfig getifaddr "$iface" 2>/dev/null && return 0
  ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true
}

# 在 Pi5 上生成→轮换→绑定，明文 token 只经 SSH stdout 灌进本机 0600 文件
provision_token(){
  local ip; ip="$(mac_peer_ip)"; [ -n "${ip:-}" ] || die "无法确定本机 LAN IP（peer 绑定需要）"
  log "在 Pi5 为 $CLIENT_ID 轮换新 token 并绑定 peer $ip ..."
  local tmp; tmp="$(mktemp "${TMPDIR:-/tmp}/hcqtok.XXXXXX")"
  # 远端：secrets 生成 token → stdin 喂给 registry rotate（不入 argv）→ 幂等 bind-peer →
  #       仅把 token 打到 stdout（本地捕获进文件）；其余输出全部丢弃到 stderr/null
  if ! SSH 'bash -s' "$CLIENT_ID" "$MCP_CONTAINER" "$STATE_DB" "$ip" >"$tmp" <<'REMOTE'
set -euo pipefail
CID="$1"; CTN="$2"; DB="$3"; PEER="$4"
tok="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
printf '%s' "$tok" | docker exec -i "$CTN" python -m hippocampus.registry_cli --db "$DB" rotate "$CID" >/dev/null
docker exec "$CTN" python -m hippocampus.registry_cli --db "$DB" bind-peer "$CID" "$PEER" >/dev/null 2>&1 || true
printf '%s' "$tok"
REMOTE
  then rm -f "$tmp"; die "Pi5 端轮换失败（检查 SSH / docker / registry）"; fi
  [ -s "$tmp" ] || { rm -f "$tmp"; die "轮换得到空 token"; }
  install -m 600 /dev/null "$TOKEN_FILE"
  cat "$tmp" >"$TOKEN_FILE"; rm -f "$tmp"
  chmod 600 "$TOKEN_FILE"
}

acquire_token(){
  case "$TOKEN_SOURCE" in
    auto)
      if auth_ok "$TOKEN_FILE"; then
        log "本机已有可用 token，沿用（不改动 Pi5）。"
      else
        log "本机无可用 token → 从 Pi5 轮换获取。"
        provision_token
      fi;;
    provision) provision_token;;
    copy:*)
      local src="${TOKEN_SOURCE#copy:}"
      [ -n "$src" ] || die "copy 模式需指定 Pi5 路径：--source copy:/path"
      log "从 Pi5 拷贝明文 token：$src"
      install -m 600 /dev/null "$TOKEN_FILE"
      SSH "cat -- '$src'" >"$TOKEN_FILE"
      chmod 600 "$TOKEN_FILE"
      [ -s "$TOKEN_FILE" ] || die "拷贝得到空 token（路径是否存在/可读？）";;
    *) die "未知 TOKEN_SOURCE：$TOKEN_SOURCE";;
  esac
}

# 合并 ~/.claude.json 的 hippocampus 条目（备份 + 只放变量引用，不落明文）
write_claude_json(){
  [ -f "$CLAUDE_JSON" ] || echo '{}' >"$CLAUDE_JSON"
  jq -e . "$CLAUDE_JSON" >/dev/null 2>&1 || die "$CLAUDE_JSON 非合法 JSON，已中止（请先修复）"
  cp -p "$CLAUDE_JSON" "$CLAUDE_JSON.bak.$(date +%Y%m%d-%H%M%S)"
  local tmp; tmp="$(mktemp)"
  jq --arg url "$MCP_URL" --arg auth "Bearer \${$TOKEN_ENV}" '
      .mcpServers = (.mcpServers // {})
      | .mcpServers.hippocampus = {type:"http", url:$url, headers:{Authorization:$auth}}
    ' "$CLAUDE_JSON" >"$tmp" && mv "$tmp" "$CLAUDE_JSON"
  log "已写入 ~/.claude.json → mcpServers.hippocampus（Bearer \${$TOKEN_ENV} 引用）"
}

# 安装标准 Claude.app 的注入 wrapper（open 不透传 env，故 wrapper 直接注入后启动）
install_wrapper(){
  cat >"$WRAPPER" <<WRAP
#!/bin/sh
# 由 deploy-hippocampus-client.sh 生成：注入 token 后启动标准 Claude.app
set -eu
TOKEN_FILE="$TOKEN_FILE"
[ -r "\$TOKEN_FILE" ] || { echo "missing \$TOKEN_FILE" >&2; exit 1; }
export $TOKEN_ENV="\$(cat "\$TOKEN_FILE")"
APP_BIN="\${CLAUDE_APP_BIN:-$STD_APP/Contents/MacOS/Claude}"
[ -x "\$APP_BIN" ] || { echo "app binary not executable: \$APP_BIN" >&2; exit 1; }
nohup "\$APP_BIN" >/dev/null 2>&1 &
echo "launched: \$APP_BIN (token injected). You can close this window."
WRAP
  chmod 700 "$WRAPPER"
  log "已安装 wrapper：$WRAPPER"
}

# 检测（不改写）分身 launcher 的 token 注入状态；分身共享同一 0600 文件
check_fenshen(){
  local app launcher found=0
  for app in "$HOME/Applications"/*[Cc]laude*.app; do
    [ -d "$app" ] || continue
    launcher="$app/Contents/MacOS/launcher"; [ -f "$launcher" ] || continue
    found=1
    if grep -q "$TOKEN_ENV" "$launcher"; then
      log "分身 OK（已自注入 token）：$app"
    else
      log "分身 需手工加注入行（在其 exec 前的子壳内）：$launcher"
      log "    [ -r \"$TOKEN_FILE\" ] && export $TOKEN_ENV=\"\$(cat \"$TOKEN_FILE\")\""
    fi
  done
  [ "$found" = 1 ] || log "未发现 ~/Applications 下的分身应用（仅配置标准版）。"
}

# ---------------- 执行 ----------------
log "目标：client=$CLIENT_ID  endpoint=$MCP_URL  source=$TOKEN_SOURCE"
acquire_token
write_claude_json
install_wrapper
check_fenshen

log "校验 token ..."
if auth_ok "$TOKEN_FILE"; then log "OK：token 认证通过（HTTP 200）。"; else
  die "token 未通过认证 —— 检查 peer 绑定 / registry / endpoint。"; fi

cat >&2 <<DONE
[deploy] 完成。重启即用：
  1) 完全退出所有 Claude 窗口（⌘Q）。
  2) 重新启动：
       · 标准版： open "$WRAPPER"
       · 分身：   直接打开分身应用（其 launcher 会自注入 token）。
  3) 新会话中确认出现 hippocampus 的 4 个工具（memory_commit/read/search/status）。
DONE
