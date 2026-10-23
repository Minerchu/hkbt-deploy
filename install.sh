#!/usr/bin/env bash
# Alpine 默认没有 bash，而本脚本用了 [[ ]]、数组等 bash 语法。
# 用 sh install.sh 启动时先自动补装 bash 再重新执行，否则会一路报错。
if [ -z "${BASH_VERSION:-}" ]; then
    if command -v apk >/dev/null 2>&1; then
        echo "检测到 Alpine，正在安装 bash..."
        apk add --no-cache bash >/dev/null 2>&1
        command -v bash >/dev/null 2>&1 && exec bash "$0" "$@"
    fi
    echo "本脚本需要 bash，请先安装：apk add bash  或  apt install bash"
    exit 1
fi

# ============================================================
#  HKBT Deploy — 一键安装脚本
#  V2bX(sing-box) 与网站共存部署面板
#
#  一键安装（推荐，参数全写在命令里）：
#    curl -fsSL https://raw.githubusercontent.com/Minerchu/hkbt-deploy/master/install.sh \
#      | bash -s -- --path /index --port 443 --password 你的密码
#
#  本地安装：
#    bash install.sh --path /index --port 443 --password 你的密码
#
#  参数：
#    --path      代理路径，必须与面板节点一致，如 /index
#    --port      网站与面板的对外端口，默认 443
#                （不能填代理端口：同一端口只能一个进程监听，填了会整站带面板一起挂）
#    --password  面板登录密码；省略则自动生成并打印
# ============================================================
set -e

REPO="Minerchu/hkbt-deploy"
TARBALL="https://github.com/${REPO}/archive/refs/heads/master.tar.gz"

RED='\033[0;31m'; GRN='\033[0;32m'; YEL='\033[0;33m'
BLU='\033[0;34m'; BLD='\033[1m'; NC='\033[0m'

info(){ echo -e "${BLU}[信息]${NC} $1"; }
ok(){   echo -e "${GRN}[完成]${NC} $1"; }
warn(){ echo -e "${YEL}[注意]${NC} $1"; }
err(){  echo -e "${RED}[错误]${NC} $1"; exit 1; }

# ---------------- 参数 ----------------
PASSWORD=""; PORT="443"; PATH_ARG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --password|-p) PASSWORD="$2"; shift 2;;
    --port)        PORT="$2"; shift 2;;
    --path)        PATH_ARG="$2"; shift 2;;
    -h|--help)     sed -n '2,18p' "$0"; exit 0;;
    *)             warn "忽略未知参数: $1"; shift;;
  esac
done

# ---------------- 前置检查 ----------------
[[ $EUID -eq 0 ]] || err "请用 root 运行"

echo -e "${BLD}"
echo "  ┌──────────────────────────────────────────────┐"
echo "  │   HKBT Deploy  ·  代理与网站共存部署面板      │"
echo "  └──────────────────────────────────────────────┘"
echo -e "${NC}"

# ---------------- 1. 获取源码（本地 or 远程） ----------------
LOCAL_DIR=""
if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
  LOCAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

if [[ -n "$LOCAL_DIR" && -f "$LOCAL_DIR/app.py" ]]; then
  SRC="$LOCAL_DIR"
  info "使用本地源码：$SRC"
else
  info "从 GitHub 下载源码…"
  if ! command -v curl >/dev/null 2>&1; then
    if command -v apk >/dev/null 2>&1; then apk add --no-cache curl >/dev/null 2>&1
    elif command -v apt-get >/dev/null 2>&1; then apt-get update -qq >/dev/null 2>&1; apt-get install -y -qq curl >/dev/null 2>&1
    elif command -v yum >/dev/null 2>&1; then yum install -y curl >/dev/null 2>&1
    fi
  fi
  TMP="$(mktemp -d)"
  curl -fsSL "$TARBALL" -o "$TMP/hkbt.tar.gz" || err "下载失败：${TARBALL}"
  tar xzf "$TMP/hkbt.tar.gz" -C "$TMP"
  SRC="$(find "$TMP" -maxdepth 1 -type d -name 'hkbt-deploy*' | head -1)"
  [[ -n "$SRC" && -f "$SRC/app.py" ]] || err "源码包内容异常"
  ok "已下载源码"
fi

# ---------------- 2. 安装依赖 ----------------
info "检查并安装依赖…"
export DEBIAN_FRONTEND=noninteractive

# Debian/Ubuntu 用 apt，Alpine 用 apk，RHEL 系用 yum
PKG=""
if command -v apt-get >/dev/null 2>&1; then PKG="apt"
elif command -v apk >/dev/null 2>&1; then PKG="apk"
elif command -v yum >/dev/null 2>&1; then PKG="yum"
fi

pkg_install() {
  case "$PKG" in
    apt) DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@" >/dev/null 2>&1 ;;
    apk) apk add --no-cache "$@" >/dev/null 2>&1 ;;
    yum) yum install -y "$@" >/dev/null 2>&1 ;;
  esac
}

flask_pkg() {
  case "$PKG" in
    apk) echo "py3-flask" ;;
    yum) echo "python3-flask" ;;
    *)   echo "python3-flask" ;;
  esac
}

if ! command -v nginx >/dev/null 2>&1; then
  [[ "$PKG" == "apt" ]] && apt-get update -qq >/dev/null 2>&1
  pkg_install nginx || err "Nginx 安装失败"
  command -v nginx >/dev/null 2>&1 && ok "Nginx 已安装"
else
  ok "Nginx 已存在 ($(nginx -v 2>&1 | cut -d/ -f2))"
fi

if ! python3 -c "import flask" >/dev/null 2>&1; then
  pkg_install "$(flask_pkg)" || pip3 install --quiet flask >/dev/null 2>&1 \
    || err "Flask 安装失败，请手动安装 python3-flask / py3-flask"
  ok "Flask 已安装"
else
  ok "Flask 已存在"
fi

command -v openssl >/dev/null 2>&1 || pkg_install openssl
command -v unzip   >/dev/null 2>&1 || pkg_install unzip
command -v ss      >/dev/null 2>&1 || pkg_install iproute2

# 本机没有 IPv6 出口时，DNS 仍会返回 AAAA，程序先连 IPv6 会卡住（表现为「网站打不开」）。
# 只调整 glibc 排序让 IPv4 优先，不动 IPv6 栈，避免影响依赖双栈的服务。
if ! ip -6 route show default 2>/dev/null | grep -q default; then
  if ! grep -q "^precedence ::ffff:0:0/96" /etc/gai.conf 2>/dev/null; then
    echo "precedence ::ffff:0:0/96  100" >> /etc/gai.conf
    ok "无 IPv6 出口，已设置 IPv4 优先"
  fi
fi

# ---------------- 3. 部署程序文件 ----------------
info "部署程序到 /opt/hkbt-deploy …"
mkdir -p /opt/hkbt-deploy/templates /opt/hkbt-deploy/backups
cp "$SRC/app.py" /opt/hkbt-deploy/app.py
cp "$SRC/templates/panel.html" /opt/hkbt-deploy/templates/panel.html
if [[ -d "$SRC/templates/site" ]]; then
  rm -rf /opt/hkbt-deploy/templates/site
  cp -r "$SRC/templates/site" /opt/hkbt-deploy/templates/site
fi
if [[ -d "$SRC/templates/sites" ]]; then
  rm -rf /opt/hkbt-deploy/templates/sites
  cp -r "$SRC/templates/sites" /opt/hkbt-deploy/templates/sites
  n_tpl=$(find /opt/hkbt-deploy/templates/sites -maxdepth 2 -name meta.json | wc -l)
  ok "站点模板已就位（${n_tpl} 套）"
fi
chmod 700 /opt/hkbt-deploy/app.py
[[ -f /opt/hkbt-deploy/config.json ]] || echo '{}' > /opt/hkbt-deploy/config.json
chmod 600 /opt/hkbt-deploy/config.json
ok "程序文件已就位"

# ---------------- 4. 面板密码 ----------------
if [[ -z "$PASSWORD" ]]; then
  if [[ -t 0 ]]; then
    while true; do
      read -r -s -p "  请设置面板密码: " PASSWORD; echo
      [[ ${#PASSWORD} -ge 4 ]] || { warn "密码至少 4 位"; continue; }
      read -r -s -p "  再输入一次: " PW2; echo
      [[ "$PASSWORD" == "$PW2" ]] && break || warn "两次不一致，请重来"
    done
  else
    PASSWORD="$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 12)"
    warn "未提供密码，已自动生成：$PASSWORD"
  fi
fi

# ---------------- 5. 禁用默认站点 ----------------
if [[ -e /etc/nginx/sites-enabled/default ]]; then
  rm -f /etc/nginx/sites-enabled/default
  info "已禁用 sites-enabled/default（避免 80 端口冲突）"
fi

# ---------------- 6. 等代理就绪 ----------------
# 代理没跑起来就先配网站，Nginx 会先把 443 抢在手里。之后排查时同时看到
# 「网站正常」和「代理不通」，分不清是代理挂了还是分流写错了。
v2bx_present=0
if systemctl list-unit-files 2>/dev/null | grep -q '^V2bX\.service'; then v2bx_present=1
elif [[ -x /etc/init.d/V2bX ]]; then v2bx_present=1
fi
if [[ $v2bx_present -eq 1 ]]; then
  info "等待 V2bX 就绪…"
  ready=0
  for _ in $(seq 1 40); do
    running=0
    if systemctl is-active --quiet V2bX 2>/dev/null; then running=1
    elif rc-service V2bX status >/dev/null 2>&1; then running=1
    fi
    if [[ $running -eq 1 ]] && ss -tlnp 2>/dev/null | grep -qE 'users:\(\("(V2bX|sing-box)"'; then
      ready=1; break
    fi
    sleep 3
  done
  if [[ $ready -eq 1 ]]; then
    ok "V2bX 已在监听，继续部署"
  else
    warn "120 秒内没观察到 V2bX 的监听端口"
    warn "代理可能配置有误：journalctl -u V2bX -n 50 --no-pager"
    warn "继续部署网站（若代理稍后起来，Nginx 分流仍然有效）"
  fi
fi

# ---------------- 7. 初始化并部署 ----------------
info "探测代理环境并部署站点…"
echo
INIT_ARGS="--init --password $PASSWORD --port $PORT"
[[ -n "$PATH_ARG" ]] && INIT_ARGS="$INIT_ARGS --path $PATH_ARG"
python3 /opt/hkbt-deploy/app.py $INIT_ARGS || warn "初始化有告警，请查看上面的输出"
echo

# ---------------- 8. 面板服务 ----------------
info "写入面板服务…"
if command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]]; then
  cat > /etc/systemd/system/hkbt-deploy.service <<'EOF'
[Unit]
Description=HKBT Deploy Panel
After=network.target nginx.service

[Service]
Type=simple
WorkingDirectory=/opt/hkbt-deploy
ExecStart=/usr/bin/python3 /opt/hkbt-deploy/app.py --serve
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable hkbt-deploy >/dev/null 2>&1
  systemctl restart hkbt-deploy
  sleep 2
  if systemctl is-active --quiet hkbt-deploy; then
    ok "面板服务已启动 (127.0.0.1:8899)"
  else
    err "面板服务启动失败，请查看: journalctl -u hkbt-deploy -n 50"
  fi
elif command -v rc-service >/dev/null 2>&1; then
  cat > /etc/init.d/hkbt-deploy <<'EOF'
#!/sbin/openrc-run
name="HKBT Deploy Panel"
command="/usr/bin/python3"
command_args="/opt/hkbt-deploy/app.py --serve"
command_user="root"
command_background="yes"
pidfile="/run/hkbt-deploy.pid"
output_log="/var/log/hkbt-deploy-panel.log"
error_log="/var/log/hkbt-deploy-panel.err"

depend() {
    need net
    after nginx
}
EOF
  chmod +x /etc/init.d/hkbt-deploy
  rc-update add hkbt-deploy default >/dev/null 2>&1
  rc-service hkbt-deploy restart >/dev/null 2>&1
  sleep 2
  if rc-service hkbt-deploy status >/dev/null 2>&1; then
    ok "面板服务已启动 (127.0.0.1:8899)"
  else
    err "面板服务启动失败，请查看: tail -20 /var/log/hkbt-deploy-panel.err"
  fi
else
  warn "未检测到 systemd / OpenRC，跳过面板服务（可手动运行 app.py --serve）"
fi

# ---------------- 9. 证书续期自动重载 ----------------
info "配置证书续期自动重载…"
cat > /usr/local/bin/hkbt-cert-reload.sh <<'EOF'
#!/bin/sh
# V2bX 通过 Cloudflare DNS 续签证书后，Nginx 需要 reload 才能加载新证书
CERT=/etc/V2bX/fullchain.cer
STAMP=/var/run/hkbt-cert.stamp
[ -f "$CERT" ] || exit 0
CUR=$(stat -c %Y "$CERT")
OLD=$(cat "$STAMP" 2>/dev/null || echo 0)
if [ "$CUR" != "$OLD" ]; then
  echo "$CUR" > "$STAMP"
  nginx -t >/dev/null 2>&1 && { systemctl reload nginx 2>/dev/null || rc-service nginx reload 2>/dev/null || nginx -s reload; }
  echo "$(date -Is) cert changed -> nginx reloaded" >> /var/log/hkbt-cert.log
fi
EOF
chmod +x /usr/local/bin/hkbt-cert-reload.sh
add_cron() {
  local line="$1" marker="$2" f="/var/spool/cron/crontabs/root"
  if command -v crontab >/dev/null 2>&1; then
    ( crontab -l 2>/dev/null | grep -v "$marker"; echo "$line" ) | crontab - 2>/dev/null && return 0
  fi
  mkdir -p "$(dirname "$f")"
  { [ -f "$f" ] && grep -v "$marker" "$f" 2>/dev/null; echo "$line"; } > "$f.tmp"
  mv "$f.tmp" "$f"; chmod 600 "$f"
}
add_cron "17 */6 * * * /usr/local/bin/hkbt-cert-reload.sh" "hkbt-cert-reload"
if command -v rc-service >/dev/null 2>&1; then
  rc-service crond status >/dev/null 2>&1 || rc-service crond start >/dev/null 2>&1
  rc-update add crond default >/dev/null 2>&1
fi
ok "已加入定时任务（每 6 小时检查）"

# ---------------- 10. 配置同步 ----------------
# 面板改端口/路径后 Nginx 不会自己变，靠这个定时比对跟上
if python3 /opt/hkbt-deploy/app.py --install-sync >/dev/null 2>&1; then
  ok "已启用配置同步（V2bX 重启即触发 + 每 3 分钟兜底）"
else
  warn "配置同步未启用，可手动执行: python3 /opt/hkbt-deploy/app.py --install-sync"
fi

# ---------------- 11. 输出 ----------------
ADMIN=$(python3 -c "
import json;print(json.load(open('/opt/hkbt-deploy/config.json')).get('admin_path',''))" 2>/dev/null)
DOMAIN=$(python3 -c "
import json;print(json.load(open('/opt/hkbt-deploy/config.json')).get('domain',''))" 2>/dev/null)
DPORT=$(python3 -c "
import json;print(json.load(open('/opt/hkbt-deploy/config.json')).get('port','443'))" 2>/dev/null)
PPORT=$(python3 -c "
import json;print(json.load(open('/opt/hkbt-deploy/config.json')).get('proxy_port',''))" 2>/dev/null)
[[ -z "$PPORT" ]] && PPORT="$DPORT"
BRAND=$(python3 -c "
import json;print(json.load(open('/opt/hkbt-deploy/config.json')).get('brand',''))" 2>/dev/null)

echo
echo -e "${BLD}════════════════════════════════════════════════${NC}"
echo -e "${BLD}  安装完成${NC}"
echo -e "${BLD}════════════════════════════════════════════════${NC}"
echo
# 443 时省略端口，其他端口显式带上
if [[ "$DPORT" == "443" ]]; then PSUFFIX=""; else PSUFFIX=":${DPORT}"; fi
if [[ "$PPORT" == "443" ]]; then PPSUFFIX=""; else PPSUFFIX=":${PPORT}"; fi
[[ -n "$DOMAIN" ]] && echo -e "  网站地址   ${GRN}https://${DOMAIN}${PSUFFIX}/${NC}"
echo -e "  代理地址   ${DOMAIN}${PPSUFFIX}  路径 ${PATH_ARG:-（自动探测）}"
if [[ "$PPORT" == "$DPORT" ]]; then
  echo -e "  共用端口   网站与代理都在 ${DPORT}"
else
  echo -e "  端口分工   网站与面板在 ${DPORT}，代理在 ${PPORT}（客户端连 ${PPORT}）"
fi
[[ -n "$BRAND" ]] && echo -e "  网站品牌   ${BRAND}"
if [[ -n "$DOMAIN" && -n "$ADMIN" ]]; then
  echo -e "  面板地址   ${GRN}https://${DOMAIN}${PSUFFIX}${ADMIN}/${NC}"
fi
echo -e "  面板密码   ${YEL}${PASSWORD}${NC}"
echo
if command -v rc-service >/dev/null 2>&1; then
  echo -e "  服务状态   rc-service hkbt-deploy status"
else
  echo -e "  服务状态   systemctl status hkbt-deploy"
fi
echo -e "  部署日志   tail -f /var/log/hkbt-deploy.log"
echo
echo -e "${BLD}════════════════════════════════════════════════${NC}"
echo
warn "请立即用客户端实测代理能否正常联网 —— 这是唯一无法自动验证的环节"
echo
