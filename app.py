#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HKBT Deploy — V2bX(sing-box) 与网站共存的一键部署面板

核心逻辑（已在 Debian 13 + V2bX v0.4.0 + sing-box 上验证）：
  1. sing-box 没有 Xray 的 fallbacks，无法做路径回落
  2. 因此必须让 Nginx 终结 TLS，按 Upgrade 头分流
  3. sing-box 收窄到 127.0.0.1:<port>，Nginx 接管公网 <ip>:<port>
  4. Nginx 绝不能绑 0.0.0.0:<port>，否则与 127.0.0.1:<port> 冲突 (EADDRINUSE)
  5. 证书直接复用 V2bX 通过 Cloudflare DNS 签发的那一份
"""
import argparse
import hashlib
import ipaddress
import json
import os
import random
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import datetime

BASE = "/opt/hkbt-deploy"
CONF_PATH = os.path.join(BASE, "config.json")
BACKUP_DIR = os.path.join(BASE, "backups")
V2BX_CONF = "/etc/V2bX/config.json"
V2BX_SYSTEMD_DROPIN = "/etc/systemd/system/V2bX.service.d/10-listen.conf"
CERT_RELOAD = "/usr/local/bin/hkbt-cert-reload.sh"
LOG_FILE = "/var/log/hkbt-deploy.log"
PANEL_PORT = 8899
DEFAULT_SITE = "/var/www/hkbt"

STATIC_EXTS = r"\.(css|js|png|jpg|jpeg|gif|svg|webp|ico|woff2?|ttf|pdf|txt|json)$"


# ------------------------------------------------------------------ 基础工具
def log(msg):
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line, flush=True)


def run(cmd, timeout=90, shell=False):
    try:
        p = subprocess.run(
            cmd if shell else (cmd if isinstance(cmd, list) else cmd.split()),
            shell=shell, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except Exception as e:
        return 1, "", str(e)


# ------------------------------------------------------------------ 服务管理
# 整套部署要启停 nginx、重启 V2bX、装定时同步。Debian/Ubuntu 用 systemd，
# Alpine 用 OpenRC，命令完全不同。这里统一成一层，上层不再直接写 systemctl。
def init_system():
    if shutil.which("systemctl") and os.path.isdir("/run/systemd/system"):
        return "systemd"
    if shutil.which("rc-service") or os.path.isfile("/sbin/openrc-run"):
        return "openrc"
    return "none"


INIT = init_system()


def svc(action, name):
    """统一服务操作：start / stop / restart / reload / enable / disable"""
    if INIT == "systemd":
        return run(["systemctl", action, name])
    if INIT == "openrc":
        if action in ("start", "stop", "restart", "reload", "status"):
            return run(["rc-service", name, action])
        if action == "enable":
            return run(["rc-update", "add", name, "default"])
        if action == "disable":
            return run(["rc-update", "del", name, "default"])
        return 1, "", "OpenRC 不支持的操作：%s" % action
    # 容器 / 无 init 的环境：退化成直接调二进制，方便在镜像里跑自检
    if name == "nginx":
        if action == "reload":
            return run(["nginx", "-s", "reload"])
        if action in ("start", "restart"):
            return run(["nginx"])
        if action == "stop":
            return run(["nginx", "-s", "quit"])
    return 1, "", "本机没有 systemd / OpenRC，无法管理服务 %s" % name


def svc_active(name):
    if INIT == "systemd":
        rc, _, _ = run(["systemctl", "is-active", "--quiet", name])
        return rc == 0
    if INIT == "openrc":
        # rc-service status：running 返回 0，stopped 返回 3
        rc, out, _ = run(["rc-service", name, "status"])
        return rc == 0 and "started" in (out or "").lower()
    if name == "nginx":
        rc, out, _ = run(["ss", "-tlnp"])
        return "nginx" in out
    return False


def svc_exists(name):
    if INIT == "systemd":
        _, out, _ = run(["systemctl", "list-unit-files", name + ".service"])
        return (name + ".service") in out
    if INIT == "openrc":
        return os.path.exists("/etc/init.d/" + name)
    return False


def svc_logs(name, n=60):
    """取服务最近日志：systemd 走 journal，OpenRC 走 syslog / 日志文件。"""
    if INIT == "systemd":
        rc, out, _ = run(["journalctl", "-u", name, "-n", str(n), "--no-pager"])
        return out if rc == 0 else ""
    parts = []
    for p in ("/var/log/%s.log" % name.lower(), "/var/log/messages"):
        if os.path.exists(p):
            _, out, _ = run(["tail", "-n", str(n), p])
            if out:
                parts.append(out)
    return "\n".join(parts)


def install_service(name, systemd_unit, openrc_script, enable=True):
    """按当前 init 系统落一份服务定义，返回 (ok, 路径或原因)。"""
    try:
        if INIT == "systemd":
            path = "/etc/systemd/system/%s.service" % name
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(systemd_unit)
            run(["systemctl", "daemon-reload"])
            if enable:
                run(["systemctl", "enable", name])
            return True, path
        if INIT == "openrc":
            path = "/etc/init.d/" + name
            with open(path, "w", encoding="utf-8") as f:
                f.write(openrc_script)
            os.chmod(path, 0o755)
            if enable:
                run(["rc-update", "add", name, "default"])
            return True, path
    except Exception as e:
        return False, str(e)
    return False, "本机没有 systemd / OpenRC，跳过服务注册"


def nginx_conf_dir():
    """Nginx 的站点配置目录。Alpine 的 nginx 包用 http.d，别的发行版用 conf.d。"""
    for d in ("/etc/nginx/conf.d", "/etc/nginx/http.d"):
        if os.path.isdir(d):
            return d
    return "/etc/nginx/conf.d"


def nginx_user():
    """Nginx 运行用户：Debian 系是 www-data，Alpine/RHEL 是 nginx。"""
    for u in ("www-data", "nginx"):
        rc, _, _ = run(["id", "-u", u])
        if rc == 0:
            return u
    return ""


# 站点配置路径要在 nginx_conf_dir() 之后才能确定，所以放在这里而不是常量区
NGINX_CONF = os.path.join(nginx_conf_dir(), "hkbt-deploy.conf")


def load_conf():
    if not os.path.exists(CONF_PATH):
        return {}
    try:
        with open(CONF_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_conf(cfg):
    os.makedirs(BASE, exist_ok=True)
    with open(CONF_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.chmod(CONF_PATH, 0o600)


def hash_pw(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 120000).hex()


def verify_pw(pw, cfg):
    return hash_pw(pw, cfg.get("salt", "")) == cfg.get("password_hash", "")


def is_private_ip(ip):
    return bool(ip.startswith(("10.", "127.", "169.254.", "192.168."))
                or re.match(r"^172\.(1[6-9]|2\d|3[01])\.", ip))


def local_ips():
    """本机网卡上真实存在的 IPv4 地址"""
    rc, out, _ = run(["ip", "-4", "-o", "addr", "show", "scope", "global"])
    return set(re.findall(r"inet (\d{1,3}(?:\.\d{1,3}){3})", out or ""))


def public_ip():
    """优先取真实公网 IP；NAT 机器上路由 src 只是内网地址"""
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"):
        rc, out, _ = run(["curl", "-4", "-s", "-m", "6", url])
        ip = (out or "").strip().splitlines()[0].strip() if out else ""
        if rc == 0 and ip:
            try:
                ipaddress.IPv4Address(ip)
                if not is_private_ip(ip):
                    return ip
            except Exception:
                pass
    for c in (["ip", "-4", "-o", "route", "get", "1.1.1.1"],
              ["ip", "-4", "-o", "addr", "show", "scope", "global"]):
        rc, out, _ = run(c)
        if rc == 0 and out:
            m = re.search(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", out.split("src")[-1] if "src" in out else out)
            if m:
                try:
                    ipaddress.IPv4Address(m.group(1))
                    if not m.group(1).startswith("127."):
                        return m.group(1)
                except Exception:
                    pass
    return ""


def valid_domain(d):
    if not d or len(d) > 253:
        return False
    return bool(re.match(r"^(?!-)[A-Za-z0-9-_.]{1,63}(\.[A-Za-z0-9-_]{1,63})+$", d))


def valid_port(p):
    try:
        return 1 <= int(p) <= 65535
    except Exception:
        return False


def valid_path(p):
    return bool(p) and p.startswith("/")and "//" not in p and len(p) < 128


def port_owner(port):
    """返回监听该端口的进程名，无人监听返回 None。
    Nginx 自己占着端口时返回 'nginx'，用于区分「重复部署」和「被别人占用」。"""
    rc, out, _ = run(["ss", "-lntp"])
    for line in out.splitlines():
        if re.search(r":%s\s" % re.escape(str(port)), line):
            m = re.search(r'users:\(\("([^"]+)"', line)
            return (m.group(1) if m else "unknown").split(":")[0]
    return None


def proxy_listen_port():
    """面板 API 取不到端口时，直接从进程监听表里找代理端口。

    排除面板自己的端口，否则重复部署时会把 8899 当成代理端口。
    """
    rc, out, _ = run(["ss", "-lntp"])
    for line in out.splitlines():
        if not re.search(r"V2bX|sing-box|xray", line):
            continue
        m = re.search(r":(\d+)\s", line)
        if m and m.group(1) != str(PANEL_PORT):
            return m.group(1)
    return ""


def port_conflict(port, proxy_port):
    """判断 Nginx 对外端口能否绑定，返回 (是否冲突, 原因)

    代理与网站用同一个端口是允许的，靠地址分离共存：
    sing-box 收窄到 127.0.0.1:443，Nginx 绑公网 IP:443 —— 这是两个不同的
    socket，Linux 不会冲突。只有「代理不在这个端口上、却被别的程序占着」
    才是真冲突。
    """
    owner = port_owner(port)
    if not owner or owner == "nginx":
        return False, ""
    if proxy_port and str(proxy_port) == str(port):
        # 代理自己占着：走同端口模式，由 do_deploy 把它收窄到回环
        return False, ""
    if owner in ("V2bX", "sing-box"):
        return True, ("%s 端口被 %s 占着，但面板给出的节点端口是 %s，两者对不上。"
                      "请在面板把节点端口改到空闲端口（如 20000），把 %s 让给网站"
                      % (port, owner, proxy_port, port))
    return True, ("%s 端口已被 %s 占用，Nginx 无法绑定。请先停掉该服务，或换一个空闲端口"
                  % (port, owner))


# ------------------------------------------------------------------ V2bX 探测
def _pick(d, *keys, **kw):
    """按优先级取第一个非空字段。Xboard 与 V2board 的命名有 camelCase / snake_case 两套。"""
    default = kw.get("default")
    if not isinstance(d, dict):
        return default
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return default


def node_api_config(api, key, ntype, node_id, timeout=15):
    """向面板拉取节点运行时配置，返回 (data, err)。

    V2board 与 Xboard 共用 /api/v1/server/UniProxy/config 这个端点，
    但 Xboard 的部分分支会把结果包在 data 里、字段也可能换成 snake_case，
    这里统一兼容，避免因为一个字段名差异就退回「猜端口」。
    """
    base = (api or "").strip().rstrip("/")
    if base.endswith("/api"):
        base = base[:-4]
    if not base:
        return None, "config.json 里没有 ApiHost"
    if not key:
        return None, "config.json 里没有 ApiKey"
    if not node_id:
        return None, "config.json 里没有 NodeID"

    url = "%s/api/v1/server/UniProxy/config?node_type=%s&node_id=%s&token=%s" % (
        base, ntype, node_id, key)

    err = ""
    for _ in range(2):  # 面板偶发 5xx / 网络抖动，重试一次
        rc, out, e = run(["curl", "-s", "-m", str(timeout), url])
        if not out:
            err = (e or "面板无响应")[:120]
            continue
        try:
            data = json.loads(out)
        except Exception:
            err = "面板返回非 JSON：%s" % out[:100]
            continue
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            data = data["data"]
        if not isinstance(data, dict):
            err = "面板返回结构异常"
            continue
        if _pick(data, "server_port", "serverPort", "port", "listen_port"):
            return data, ""
        err = str(_pick(data, "message", "msg", default=data))[:120]
        if err.strip() in ("{}", "[]", "null", ""):
            err = "面板返回空响应（可能正在重启或维护）"
    return None, err


def detect_v2bx():
    """读取 V2bX 配置 + 面板 API，拿到节点真实端口/路径/域名。

    端口和路径只能问面板：V2bX 的 config.json 里只有 NodeID 和 API 凭据，
    真正的 server_port 与 ws path 是运行时从面板拉的，磁盘上没有副本
    （sing_origin.json 的 inbounds 是 null，由 V2bX 在内存里拼装）。
    """
    info = {
        "installed": os.path.exists(V2BX_CONF),
        "core": "", "domain": "", "port": "", "path": "",
        "sni": "", "network": "", "tls": 0,
        "listen_ip": "", "cert": "", "key": "", "node_id": "",
        "api_ok": False, "api_msg": "", "source": "",
        "api_host": "", "ntype": ""
    }
    if not info["installed"]:
        info["api_msg"] = "未检测到 /etc/V2bX/config.json"
        return info

    try:
        with open(V2BX_CONF, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        info["api_msg"] = "配置文件解析失败: %s" % e
        return info

    nodes = cfg.get("Nodes", [])
    if not nodes:
        info["api_msg"] = "配置中没有节点"
        return info

    node = nodes[0]
    info["core"] = node.get("Core", "sing")
    info["listen_ip"] = node.get("ListenIP", "")
    info["node_id"] = str(node.get("NodeID", ""))
    info["api_host"] = node.get("ApiHost", "")
    info["ntype"] = node.get("NodeType", "vless")
    cc = node.get("CertConfig", {})
    info["domain"] = cc.get("CertDomain", "")
    info["cert"] = cc.get("CertFile", "")
    info["key"] = cc.get("KeyFile", "")

    data, err = node_api_config(node.get("ApiHost"), node.get("ApiKey"),
                                node.get("NodeType", "vless"), info["node_id"])
    if data:
        info["port"] = str(_pick(data, "server_port", "serverPort", "port",
                                 "listen_port", default=""))
        # path 可能在 networkSettings / network_settings，ws 节点也可能嵌一层 wsSettings
        ns = _pick(data, "networkSettings", "network_settings", default={})
        if isinstance(ns, str):
            try:
                ns = json.loads(ns)
            except Exception:
                ns = {}
        ws = _pick(ns, "wsSettings", "ws_settings", default={}) or {}
        info["path"] = str(_pick(ns, "path", "Path")
                           or _pick(ws, "path", "Path") or "")
        ts = _pick(data, "tls_settings", "tlsSettings", default={}) or {}
        info["sni"] = str(_pick(ts, "server_name", "serverName", "sni", default="") or "")
        info["network"] = str(_pick(data, "network", "type", default="") or "")
        info["tls"] = _pick(data, "tls", default=0) or 0
        info["api_ok"] = True
        info["source"] = "panel"
    else:
        info["api_msg"] = err

    # 端口兜底：进程监听表。面板临时不通时仍能拿到代理端口，
    # 但路径没有等价兜底 —— 猜错路径会让代理静默失联，所以宁可不部署。
    if not info["port"]:
        info["port"] = proxy_listen_port()
        if info["port"]:
            info["source"] = "listen"

    # 路径兜底仅限「本机上次部署成功时记录的值」，且面板必须完全不可达
    if not info["path"] and not info["api_ok"]:
        prev = load_conf().get("path") or ""
        if prev:
            info["path"] = prev
            info["source"] = info["source"] or "config"

    if not info["sni"]:
        info["sni"] = info["domain"]
    return info


def cert_expiry(cert):
    if not cert or not os.path.exists(cert):
        return ""
    rc, out, _ = run(["openssl", "x509", "-in", cert, "-noout", "-enddate"])
    if rc == 0 and "=" in out:
        return out.split("=", 1)[1].strip()
    return ""


# ------------------------------------------------------------------ Nginx 配置生成
def nginx_version():
    """返回 (主, 次, 修订)。nginx -v 输出到 stderr。"""
    rc, out, err = run(["nginx", "-v"])
    m = re.search(r"nginx/([\d.]+)", out + err)
    if not m:
        return (0, 0, 0)
    try:
        parts = [int(x) for x in m.group(1).split(".")]
        while len(parts) < 3:
            parts.append(0)
        return tuple(parts[:3])
    except Exception:
        return (0, 0, 0)


CONF_VERSION = "2"   # 改 gen_nginx 模板时 +1，让已有部署自动重写一次


def _fp(domain, port, path, proxy_port):
    """当前生效参数的指纹，写在 Nginx 配置第一行。

    同步时拿它和面板最新值比：一致就什么都不做，不一致才重写。
    放在配置文件里而不是 config.json，是因为配置文件才是真正生效的东西 ——
    有人手改过、或文件被别的面板覆盖过，指纹就消失了，sync 会自动重建。
    """
    return "v%s|%s|%s|%s|%s" % (CONF_VERSION, domain, port, path, proxy_port)


def gen_nginx(domain, port, path, site_dir, public_ip, cert, key, admin_path, proxy_port=None):
    """生成共存配置。注意：必须绑公网 IP，不能绑 0.0.0.0

    port       = Nginx 对外监听端口（通常 443）
    proxy_port = V2bX 实际监听的端口（默认与 port 相同；分离部署时如 21311）
    """
    proxy_port = str(proxy_port or port)

    # 面板里的 path 可能带 query（例：/sdf=?kfj）。Nginx 的 location 只匹配 URI 部分，
    # 带 ? 的整串永远匹配不上，必须切掉 query 再匹配。
    path_uri = path.split("?", 1)[0] or "/"
    has_query = "?" in path

    # 客户端对带 ? = 的 path 解析五花八门：可能把 ? 当分隔符（/sdf?kfj）、
    # 可能只保留前缀（/sdf）、也可能原样发送（/sdf=?kfj）。
    # 用正则让尾部 = 可选，再统一 rewrite 回节点真实 path，三种写法都能通。
    loc_pat = re.escape(path_uri)
    if loc_pat.endswith("="):
        loc_pat = loc_pat[:-1] + "=?"
    loc_line = "    location ~ ^%s$ {" % loc_pat
    # replacement 末尾的 ? 表示丢弃客户端原始 query，避免拼接出 /sdf=?kfj?kfj
    rewrite_line = ("        rewrite ^ %s? break;\n" % path) if has_query else ""

    # 绑定地址：代理在独立端口时绑 0.0.0.0 最稳（NAT 机器上公网 IP 不在网卡上，
    # 绑它会直接 bind 失败）；同端口场景必须绑本机真实持有的地址，否则会和代理抢 127.0.0.1
    if proxy_port != str(port):
        bind = "0.0.0.0"
    elif public_ip and public_ip in local_ips():
        bind = public_ip
    else:
        bind = "0.0.0.0"
    # http2 on; 指令需要 nginx >= 1.25.1，旧版需写在 listen 上
    ver = nginx_version()
    new_h2 = ver >= (1, 25, 1)
    h2_listen = "" if new_h2 else " http2"
    h2_directive = "    http2 on;\n" if new_h2 else ""

    mapv = "map $http_upgrade $hkbt_connection_upgrade {\n    default upgrade;\n    ''      close;\n}\n"
    admin_block = ""
    if admin_path:
        admin_block = """
    # ---- 部署面板入口（隐藏路径）----
    location = %s { return 301 %s/; }
    location %s/ {
        proxy_pass http://127.0.0.1:%d/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        client_max_body_size 200m;
    }
""" % (admin_path, admin_path, admin_path, PANEL_PORT)

    return """# ===== 由 hkbt-deploy 自动生成，请勿手改 =====
# 生成时间: %s
# hkbt-fingerprint: %s
# 代理: V2bX / sing-box  %s (ws/tls)  |  公网 %s:%s  |  路径 %s

%s
server {
    listen 80;
    listen [::]:80;
    server_name %s;
    location ^~ /.well-known/acme-challenge/ { root %s; default_type "text/plain"; }
    location / { return 301 https://$host%s$request_uri; }
}

server {
    listen %s:%s ssl%s;
%s    server_name %s;

    ssl_certificate     %s;
    ssl_certificate_key %s;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_cache shared:hkbt_ssl:10m;
    ssl_session_timeout 1d;
    ssl_prefer_server_ciphers off;

    add_header Strict-Transport-Security "max-age=31536000" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;

    gzip on;
    gzip_vary on;
    gzip_min_length 1024;
    gzip_types text/plain text/css application/json application/javascript text/xml application/xml image/svg+xml;

    error_page 404 /404.html;
    # 目录无 index 时 Nginx 默认回 403，统一成 404，避免暴露目录结构
    error_page 403 =404 /404.html;

    # ---- 代理路径：%s 双用途 ----
    # 带 Upgrade: websocket -> 代理
    # 普通浏览器访问      -> 网站首页（伪装成普通页面）
%s
        if ($http_upgrade = "") { rewrite ^ /index.html last; }
%s        proxy_pass https://127.0.0.1:%s;
        proxy_ssl_verify off;
        proxy_ssl_server_name on;
        proxy_ssl_name %s;
        proxy_ssl_session_reuse on;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $hkbt_connection_upgrade;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
        proxy_buffering off;
    }
%s
    # ---- 网站 ----
    location / {
        root %s;
        index index.html;
        try_files $uri $uri/ =404;
    }

    location ~* %s {
        root %s;
        expires 7d;
        add_header Cache-Control "public";
        access_log off;
    }
}
""" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), _fp(domain, port, path, proxy_port),
       domain, public_ip, port, path,
       mapv, domain, site_dir, ("" if str(port) == "443" else ":%s" % port),
       bind, port, h2_listen, h2_directive, domain,
       cert, key,
       path, loc_line, rewrite_line, proxy_port, domain,
       admin_block,
       site_dir, STATIC_EXTS, site_dir)


# ------------------------------------------------------------------ 部署流程
def backup(tag):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    d = os.path.join(BACKUP_DIR, "%s-%s" % (ts, tag))
    os.makedirs(d, exist_ok=True)
    for src in (V2BX_CONF, NGINX_CONF):
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(d, os.path.basename(src)))
    log("已备份到 %s" % d)
    return d


SITE_TPL = os.path.join(BASE, "templates", "site")
SITES_TPL = os.path.join(BASE, "templates", "sites")
DEFAULT_TEMPLATE = "trade"

# 全自动部署时随机挑一个中性企业名。刻意选与行业无关的词，配任何模板都不违和；
# 中英文成对，避免页面里出现「中文名 + 不相干英文」的破绽。
BRAND_POOL = [
    ("远洋实业", "YUANYANG"), ("恒通国际", "HENGTONG"), ("锦程集团", "JINCHENG"),
    ("嘉禾实业", "JIAHE"), ("鼎盛国际", "DINGSHENG"), ("天工科技", "TIANGONG"),
    ("弘毅实业", "HONGYI"), ("博远国际", "BOYUAN"), ("瑞恒集团", "RUIHENG"),
    ("中科实业", "ZHONGKE"), ("东辰国际", "DONGCHEN"), ("万邦实业", "WANBANG"),
    ("华宸集团", "HUACHEN"), ("明远实业", "MINGYUAN"), ("同创国际", "TONGCHUANG"),
    ("正泰实业", "ZHENGTAI"), ("联创集团", "LIANCHUANG"), ("金桥国际", "JINQIAO"),
    ("海纳实业", "HAINA"), ("卓越集团", "ZHUOYUE"), ("世纪实业", "SHIJI"),
    ("长城国际", "CHANGCHENG"), ("东方实业", "DONGFANG"), ("新元集团", "XINYUAN"),
    ("天成实业", "TIANCHENG"), ("立信国际", "LIXIN"), ("恒信实业", "HENGXIN"),
    ("汇通集团", "HUITONG"), ("泰和实业", "TAIHE"), ("荣泰国际", "RONGTAI"),
]


def random_brand():
    return random.choice(BRAND_POOL)


def random_template():
    keys = [t["key"] for t in list_templates()]
    return random.choice(keys) if keys else DEFAULT_TEMPLATE



def list_templates():
    """扫描 templates/sites/<key>/meta.json，返回可选模板列表（按名称排序）"""
    out = []
    if not os.path.isdir(SITES_TPL):
        return out
    for key in sorted(os.listdir(SITES_TPL)):
        d = os.path.join(SITES_TPL, key)
        mp = os.path.join(d, "meta.json")
        if not (os.path.isdir(d) and os.path.exists(mp)):
            continue
        try:
            with open(mp, encoding="utf-8") as f:
                m = json.load(f)
        except Exception:
            continue
        out.append({
            "key": key,
            "name": m.get("name", key),
            "industry": m.get("industry", ""),
            "variant": m.get("variant", ""),
            "default_brand": m.get("default_brand", ""),
            "pages": m.get("pages", 0),
        })
    return out


def template_dir(key):
    """解析模板目录；key 非法或不存在时回落到默认模板"""
    key = re.sub(r"[^a-z0-9_-]", "", (key or "").lower())
    if key:
        d = os.path.join(SITES_TPL, key)
        if os.path.isdir(d):
            return d, key
    if os.path.isdir(SITES_TPL):
        keys = sorted(os.listdir(SITES_TPL))
        if DEFAULT_TEMPLATE in keys:
            return os.path.join(SITES_TPL, DEFAULT_TEMPLATE), DEFAULT_TEMPLATE
        if keys:
            return os.path.join(SITES_TPL, keys[0]), keys[0]
    # 兼容旧布局
    return SITE_TPL, "trade"


def default_brand(domain):
    """从域名推导品牌名：hkbt.netflixcn.de -> HKBT"""
    seg = re.split(r"[.\-_]", domain)[0]
    seg = re.sub(r"[^A-Za-z0-9]", "", seg)
    if not seg:
        return "HKBT"
    # 短名称全大写（hkbt -> HKBT），较长名称首字母大写（sunrise -> Sunrise）
    return seg.upper() if len(seg) <= 4 else (seg[:1].upper() + seg[1:])


def generate_default_site(brand, domain, site_dir, template=None):
    """用选定模板生成站点，替换品牌与域名占位符"""
    src, key = template_dir(template)
    if not os.path.isdir(src):
        return False, "站点模板缺失：%s" % src
    if os.path.exists(site_dir):
        shutil.rmtree(site_dir)
    shutil.copytree(src, site_dir)
    # meta.json 只是模板元数据，不该出现在线上站点里（会被直接访问到）
    if os.path.exists(os.path.join(site_dir, "meta.json")):
        os.remove(os.path.join(site_dir, "meta.json"))

    # 模板自带的行业默认品牌（用于替换），域名统一占位
    old_brand = "HKBT"
    mp = os.path.join(src, "meta.json")
    if os.path.exists(mp):
        try:
            with open(mp, encoding="utf-8") as f:
                old_brand = json.load(f).get("default_brand") or old_brand
        except Exception:
            pass

    pages = imgs = 0
    for root, _, files in os.walk(site_dir):
        for f in files:
            p = os.path.join(root, f)
            low = f.lower()
            if low.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
                imgs += 1
                continue
            if not low.endswith((".html", ".css", ".js", ".svg", ".txt", ".json")):
                continue
            if f == "meta.json":
                continue
            try:
                with open(p, encoding="utf-8", errors="ignore") as fh:
                    s = fh.read()
                s2 = s.replace(old_brand, brand).replace("hkbt.example", domain)
                if s2 != s:
                    with open(p, "w", encoding="utf-8") as fh:
                        fh.write(s2)
                if low.endswith(".html"):
                    pages += 1
            except Exception:
                pass

    nu = nginx_user()
    if nu:
        run(["chown", "-R", "%s:%s" % (nu, nu), site_dir])
    return True, "已生成「%s」模板站点：%d 个页面 / %d 张配图，品牌名「%s」" % (
        key, pages, imgs, brand)


def disable_conflicts(port):
    """禁用同样监听目标端口的其他 Nginx 配置，避免 bind 冲突。
    常见冲突源：旧的站点配置、sites-enabled/default。"""
    moved = []
    pat = re.compile(r"listen\s+(?:\[::\]:)?(?:[\d.]+:)?%s\b" % re.escape(str(port)))
    for d in ("/etc/nginx/conf.d", "/etc/nginx/sites-enabled"):
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if not (name.endswith(".conf") or os.path.islink(p)):
                continue
            if name == "hkbt-deploy.conf":
                continue
            try:
                txt = open(p, encoding="utf-8", errors="ignore").read()
            except Exception:
                continue
            if not pat.search(txt):
                continue
            try:
                if os.path.islink(p):
                    os.remove(p)
                    moved.append("%s (软链已移除)" % p)
                else:
                    bak = p + ".disabled-hkbt"
                    shutil.move(p, bak)
                    moved.append(bak)
            except Exception as e:
                log("禁用 %s 失败: %s" % (p, e))
    return moved


def narrow_v2bx(target="127.0.0.1", port=None):
    """把 sing-box 收窄到回环地址，把公网地址让给 Nginx

    只在「代理端口 == 网站端口」时调用。两个进程绑不同的具体地址（127.0.0.1
    与公网 IP）可以共存，但都绑 0.0.0.0 / :: 就会抢。
    """
    if not os.path.exists(V2BX_CONF):
        return True, "未检测到 V2bX，跳过"
    if not svc_exists("V2bX"):
        return True, "未检测到 V2bX 服务，跳过收窄"
    with open(V2BX_CONF, encoding="utf-8") as f:
        txt = f.read()
    cur = re.search(r'"ListenIP"\s*:\s*"([^"]*)"', txt)
    if cur and cur.group(1) == target:
        return True, "监听地址已是 %s" % target
    new = re.sub(r'"ListenIP"\s*:\s*"[^"]*"', '"ListenIP": "%s"' % target, txt)
    with open(V2BX_CONF, "w", encoding="utf-8") as f:
        f.write(new)

    # 固化：防止 V2bX 重启后改回。systemd 用 drop-in，OpenRC 改不了别人的
    # init 脚本，只在当前进程生效，重启后由配置本身保证（已写进 config.json）
    if INIT == "systemd":
        os.makedirs(os.path.dirname(V2BX_SYSTEMD_DROPIN), exist_ok=True)
        with open(V2BX_SYSTEMD_DROPIN, "w", encoding="utf-8") as f:
            f.write("[Service]\nExecStartPre=/bin/sed -i 's/\"ListenIP\": \"::\"/\"ListenIP\": \"%s\"/' %s\n"
                    % (target, V2BX_CONF))
        run(["systemctl", "daemon-reload"])
    svc("restart", "V2bX")
    time.sleep(6)
    _, out, _ = run(["ss", "-tlnp"])
    if port:
        ok = re.search(r"%s:%s\b" % (re.escape(target), str(port)), out or "") is not None
    else:
        ok = re.search(r"%s:\d+" % re.escape(target), out or "") is not None
    return ok, ("V2bX 监听已收窄到 %s:%s" % (target, port or "*")) if ok \
        else "收窄后未观察到 %s:%s 的监听，请检查 V2bX 日志" % (target, port or "*")


def ipv6_usable():
    """本机是否真的能走 IPv6：有默认路由且能连外网"""
    rc, out, _ = run(["ip", "-6", "route", "show", "default"])
    if rc != 0 or not out.strip():
        return False
    rc2, _, _ = run(["curl", "-6", "-s", "-m", "5", "-o", "/dev/null", "https://ip.sb"])
    return rc2 == 0


def normalize_listen_ip():
    """ListenIP 为 :: 时依赖 IPv6 栈。本机没有 IPv6 出口时必须改回 0.0.0.0，
    否则 V2bX 重启后 bind 失败，代理直接起不来。"""
    if not os.path.exists(V2BX_CONF):
        return True, "未检测到 V2bX，跳过"
    try:
        with open(V2BX_CONF, encoding="utf-8") as f:
            d = json.load(f)
    except Exception as e:
        return True, "V2bX 配置解析失败，跳过：%s" % e
    nodes = d.get("Nodes") or []
    targets = [n for n in nodes if str(n.get("ListenIP", "")) in ("::", "[::]")]
    if not targets:
        return True, "监听地址已是 IPv4 通配，无需调整"
    if ipv6_usable():
        return True, "本机 IPv6 可用，保留 :: 双栈监听"
    for n in targets:
        n["ListenIP"] = "0.0.0.0"
    with open(V2BX_CONF, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)
    svc("restart", "V2bX")
    time.sleep(4)
    return True, "本机无 IPv6 出口，监听地址已改为 0.0.0.0"


def unzip_site(zip_path, site_dir):
    """解压站点；若 zip 只有一层顶层目录，自动提升为根"""
    if os.path.exists(site_dir):
        shutil.rmtree(site_dir)
    os.makedirs(site_dir, exist_ok=True)
    tmp = tempfile.mkdtemp()
    try:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(tmp)
        # 找 index.html 所在层
        entries = [e for e in os.listdir(tmp) if not e.startswith("__MACOSX") and not e.startswith(".")]
        root = tmp
        if len(entries) == 1 and os.path.isdir(os.path.join(tmp, entries[0])):
            inner = os.path.join(tmp, entries[0])
            if os.path.exists(os.path.join(inner, "index.html")) or len(os.listdir(inner)) > 0:
                root = inner
        for item in os.listdir(root):
            s = os.path.join(root, item)
            if item.startswith("__MACOSX") or item.startswith("."):
                continue
            d = os.path.join(site_dir, item)
            if os.path.isdir(s):
                shutil.copytree(s, d)
            else:
                shutil.copy2(s, d)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if not os.path.exists(os.path.join(site_dir, "index.html")):
        return False, "压缩包内未找到 index.html（若站点在子目录，工具已自动提升，但仍未找到）"
    nu = nginx_user()
    rc = 0 if not nu else run(["chown", "-R", "%s:%s" % (nu, nu), site_dir])[0]
    n = sum(len(fs) for _, _, fs in os.walk(site_dir))
    return True, "已解压 %d 个文件到 %s" % (n, site_dir)


def verify(domain, port, path, public_ip, proxy_port=None):
    """部署后自检。统一走 127.0.0.1，避免 NAT 机器公网回环不通造成误报。"""
    proxy_port = str(proxy_port or port)
    # 同端口时 127.0.0.1:port 上是 sing-box，Nginx 绑的是公网 IP，只能从公网地址访问。
    # 分离部署时走回环，避免 NAT 机器公网回环不通造成误报。
    probe = (public_ip or "127.0.0.1") if proxy_port == str(port) else "127.0.0.1"
    checks = []
    r = "--resolve %s:%s:%s" % (domain, port, probe)

    _, code, _ = run("curl -4 -sk -o /dev/null -w '%%{http_code}' -m 10 %s https://%s:%s/" % (r, domain, port), shell=True)
    checks.append(("网站首页 /", code == "200", code or "连接失败"))

    _, n, _ = run("curl -4 -sk -m 10 %s 'https://%s:%s%s' | grep -c '<html'" % (r, domain, port, path), shell=True)
    ok_page = n.strip() not in ("", "0")
    checks.append(("代理路径 %s（浏览器）" % path, ok_page, "返回站点页面" if ok_page else (n.strip() or "未返回页面")))

    # 必须强制 HTTP/1.1：HTTP/2 会丢弃 Connection/Upgrade 头，nginx 拿不到
    # $http_upgrade 就会走「浏览器访问」分支返回网站首页，于是 200 被当成通过 ——
    # 代理明明不通，自检却全绿。只有 h1 下的 101 才是真的握手成功。
    _, wsc, _ = run(
        "curl -4 -sk --http1.1 -o /dev/null -w '%%{http_code}' -m 10 %s -H 'Connection: Upgrade' "
        "-H 'Upgrade: websocket' -H 'Sec-WebSocket-Version: 13' "
        "-H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' 'https://%s:%s%s'" % (r, domain, port, path), shell=True)
    checks.append(("代理路径 %s（WebSocket）" % path, wsc == "101",
                   wsc or "连接失败"))

    _, subj, _ = run("echo | openssl s_client -connect %s:%s -servername %s 2>/dev/null | openssl x509 -noout -subject" % (probe, port, domain), shell=True)
    ok_cert = (("CN=%s" % domain) in subj) or (("CN = %s" % domain) in subj)
    checks.append(("证书匹配域名", ok_cert, subj.replace("subject=", "") or "未取到（若自签证书可忽略）"))

    _, ls, _ = run(["ss", "-tlnp"])
    has_v2bx = svc_exists("V2bX")
    if has_v2bx and proxy_port == str(port):
        checks.append(("sing-box 仅本机监听", ("127.0.0.1:%s" % proxy_port) in ls,
                       "127.0.0.1:%s" % proxy_port if ("127.0.0.1:%s" % proxy_port) in ls else "未收窄，请检查"))
    elif has_v2bx:
        checks.append(("sing-box 独立端口", (":%s" % proxy_port) in ls,
                       "监听 %s，由 Nginx 按路径转发" % proxy_port if (":%s" % proxy_port) in ls else "未观察到 %s" % proxy_port))
    else:
        checks.append(("sing-box 仅本机监听", True, "本机未安装 V2bX 服务，跳过"))

    listening = any(x in ls for x in ("0.0.0.0:%s" % port, "*:%s" % port, "[::]:%s" % port,
                                      "%s:%s" % (public_ip, port)))
    checks.append(("Nginx 监听端口", listening,
                   "已监听 %s" % port if listening else "未观察到 %s 监听" % port))
    return checks


# ------------------------------------------------------------------ 端口伪装
CAMOUFLAGE_RULES = "/etc/hkbt-nat.nft"
CAMOUFLAGE_UNIT = "hkbt-nat"


def setup_camouflage(proxy_port, port, enable=True):
    """让代理端口也能打开网站：外部连 proxy_port 的流量转到 port（Nginx）。

    只作用于 PREROUTING，本机回环不受影响 —— Nginx 转发到 127.0.0.1:proxy_port
    不会绕回自己形成环路。规则同时写成 systemd oneshot，重启后自动重放。
    """
    proxy_port, port = str(proxy_port), str(port)
    if proxy_port == port:
        return True, "同端口部署，无需伪装"
    if not enable:
        return True, "已关闭（camouflage=false）"

    nft = shutil.which("nft")
    ipt = shutil.which("iptables")
    if not nft and not ipt:
        return False, "系统里没有 nft / iptables，跳过端口伪装"

    if nft:
        body = ("table ip hkbt {\n"
                "  chain prerouting {\n"
                "    type nat hook prerouting priority dstnat; policy accept;\n"
                "    tcp dport %s redirect to :%s\n"
                "  }\n"
                "}\n" % (proxy_port, port))
        try:
            with open(CAMOUFLAGE_RULES, "w", encoding="utf-8") as f:
                f.write(body)
        except Exception as e:
            return False, "写入规则文件失败：%s" % e
        run([nft, "delete", "table", "ip", "hkbt"])
        rc, out, err = run([nft, "-f", CAMOUFLAGE_RULES])
        if rc != 0:
            return False, "加载 nft 规则失败：%s" % (err or out)[:200]
        start = "%s delete table ip hkbt 2>/dev/null; %s -f %s" % (nft, nft, CAMOUFLAGE_RULES)
        stop = "%s delete table ip hkbt 2>/dev/null || true" % nft
    else:
        # 老系统走 iptables：先清掉本工具以前加的规则，避免改端口后旧规则残留
        _, cur, _ = run([ipt, "-t", "nat", "-S", "PREROUTING"])
        for line in (cur or "").splitlines():
            if "HKBT-CAMOUFLAGE" in line and line.startswith("-A"):
                run([ipt, "-t", "nat", "-D"] + line.split()[1:])
        rc, out, err = run([ipt, "-t", "nat", "-I", "PREROUTING", "1",
                            "-p", "tcp", "--dport", proxy_port,
                            "-j", "REDIRECT", "--to-ports", port,
                            "-m", "comment", "--comment", "HKBT-CAMOUFLAGE"])
        if rc != 0:
            return False, "加载 iptables 规则失败：%s" % (err or out)[:200]
        start = ("%s -t nat -C PREROUTING -p tcp --dport %s -j REDIRECT --to-ports %s 2>/dev/null || "
                 "%s -t nat -I PREROUTING 1 -p tcp --dport %s -j REDIRECT --to-ports %s"
                 % (ipt, proxy_port, port, ipt, proxy_port, port))
        stop = ("%s -t nat -D PREROUTING -p tcp --dport %s -j REDIRECT --to-ports %s 2>/dev/null || true"
                % (ipt, proxy_port, port))

    unit = """[Unit]
Description=HKBT camouflage: redirect external %s to %s
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c "%s"
ExecStop=/bin/sh -c "%s"

[Install]
WantedBy=multi-user.target
""" % (proxy_port, port, start, stop)

    openrc = """#!/sbin/openrc-run
description="HKBT camouflage: redirect external %s to %s"

depend() {
    need net
    after firewall
}

start() {
    ebegin "Redirecting %s -> %s"
    /bin/sh -c '%s'
    eend $?
}

stop() {
    ebegin "Removing HKBT camouflage rules"
    /bin/sh -c '%s'
    eend $?
}
""" % (proxy_port, port, proxy_port, port, start, stop)

    okc, msgc = install_service(CAMOUFLAGE_UNIT, unit, openrc, enable=True)
    if not okc:
        # 规则本身已经加载生效了，只是没法注册开机自启（容器/无 init 环境）。
        # 这不算失败，但要让用户知道重启后得手动重放。
        return True, ("外部访问 %s 会转到 %s；但未注册开机自启（%s），"
                      "重启后需重新执行 --sync" % (proxy_port, port, msgc))

    return True, "外部访问 %s 会转到 %s，浏览器打开代理端口看到的是网站" % (proxy_port, port)


def do_deploy(domain, port, path, zip_path=None, brand=None, template=None):
    """完整部署流程，返回 steps 列表。
    不传 zip 时，用内置模板自动生成一个完整站点。"""
    steps = []

    def add(name, ok, msg):
        steps.append({"name": name, "ok": bool(ok), "msg": str(msg)})
        log("%s | %s | %s" % ("OK  " if ok else "FAIL", name, msg))
        return ok

    if not valid_domain(domain):
        return add("参数校验", False, "域名格式不合法"), steps
    if not valid_port(port):
        return add("参数校验", False, "端口不合法"), steps
    if not valid_path(path):
        return add("参数校验", False, "路径必须以 / 开头且不含 //"), steps

    v = detect_v2bx()
    if not v["installed"]:
        return add("环境检测", False, "未检测到 V2bX，本工具目前只支持 V2bX + sing-box"), steps

    # 代理真实端口以面板 API 为准；取不到时从进程监听表兜底
    proxy_port = str(v["port"]) if v["api_ok"] and v["port"] else proxy_listen_port()
    if not proxy_port:
        return add("环境检测", False,
                   "读不到代理监听端口（面板 API 无响应，进程表里也没有 V2bX/sing-box），"
                   "请确认代理正在运行"), steps
    if v["api_ok"] and v["path"] and v["path"] != path:
        return add("路径一致性", False,
                   "面板节点实际路径为 %s，你填的是 %s。路径必须完全一致，否则客户端连不上"
                   % (v["path"], path))

    # Nginx 与代理不能共用同一个 TCP 端口：0.0.0.0:x 与 127.0.0.1:x 在 Linux 上互相排斥，
    # 后启动的一方直接 bind 失败。整站和面板都挂在同一个 server 上，一旦失败就全挂。
    conflict, why = port_conflict(port, proxy_port)
    if conflict:
        return add("端口冲突", False, why), steps

    add("端口映射", True, "代理实际监听 127.0.0.1:%s，Nginx 对外 %s，按路径转发"
        % (proxy_port, port))

    ip = public_ip()
    if not ip:
        return add("网络检测", False, "无法获取公网 IP"), steps
    add("环境检测", True, "V2bX / %s 核心，节点 %s，公网 IP %s" % (v["core"], v["node_id"], ip))

    cert, key = v["cert"], v["key"]
    if not (cert and key and os.path.exists(cert) and os.path.exists(key)):
        return add("证书检查", False, "未找到 V2bX 证书文件，无法复用"), steps
    add("证书检查", True, "复用 %s（到期 %s）" % (cert, cert_expiry(cert)))

    backup("deploy")

    moved = disable_conflicts(port)
    if moved:
        add("清理冲突配置", True, "已禁用：%s" % "、".join(os.path.basename(m) for m in moved))
    else:
        add("清理冲突配置", True, "无冲突配置")

    if str(proxy_port) == str(port):
        # 同端口共存：sing-box 让出公网地址，Nginx 绑公网 IP 接客
        if not (ip and ip in local_ips()):
            return add("同端口共存", False,
                       "公网 IP %s 不在本机网卡上，无法与代理共用 %s 端口。"
                       "请在面板把节点端口改成 20000 之类的空闲端口，再重新部署"
                       % (ip or "未取到", port)), steps
        ok, msg = narrow_v2bx("127.0.0.1", port)
        if not add("收窄 sing-box 监听", ok, msg):
            return False, steps
    else:
        # ListenIP=:: 依赖 IPv6 栈；本机没有 IPv6 出口时重启后会 bind 失败
        ok, msg = normalize_listen_ip()
        if not add("规范 sing-box 监听地址", ok, msg):
            return False, steps
        # 代理在独立端口时不抢端口，收窄反而会让客户端断连，跳过
        add("收窄 sing-box 监听", True, "代理在独立端口 %s，无需收窄" % proxy_port)

    site_dir = DEFAULT_SITE
    if zip_path:
        ok, msg = unzip_site(zip_path, site_dir)
        if not add("部署站点文件", ok, msg):
            return False, steps
    else:
        used_brand = (brand or "").strip() or default_brand(domain)
        ok, msg = generate_default_site(used_brand, domain, site_dir, template)
        if not add("生成站点", ok, msg):
            return False, steps
        cfg0 = load_conf()
        cfg0["brand"] = used_brand
        cfg0["template"] = template_dir(template)[1]
        save_conf(cfg0)

    cfg = load_conf()
    admin_path = cfg.get("admin_path", "")
    conf = gen_nginx(domain, port, path, site_dir, ip, cert, key, admin_path, proxy_port)
    os.makedirs(os.path.dirname(NGINX_CONF), exist_ok=True)
    with open(NGINX_CONF, "w", encoding="utf-8") as f:
        f.write(conf)
    add("生成 Nginx 配置", True, "%s（对外 %s 端口，转发到 127.0.0.1:%s）"
        % (NGINX_CONF, port, proxy_port))

    rc, out, err = run(["nginx", "-t"])
    if rc != 0:
        return add("Nginx 语法检查", False, (err or out)[:400]), steps
    add("Nginx 语法检查", True, "通过")

    rc, out, err = svc("reload", "nginx")
    if rc != 0:
        svc("restart", "nginx")
    add("重载 Nginx", True, "已生效")

    # 端口伪装：外部访问代理端口时也返回网站。属于增强项，失败不阻断部署。
    okc, msgc = setup_camouflage(proxy_port, port, cfg.get("camouflage", True))
    add("端口伪装", okc, msgc)

    time.sleep(2)
    checks = verify(domain, port, path, ip, proxy_port)
    for name, okk, detail in checks:
        add("验证 · " + name, okk, detail)

    cfg.update({"brand": cfg.get("brand") or default_brand(domain),
                "domain": domain, "port": port, "path": path,
                "proxy_port": proxy_port,
                "site_dir": site_dir, "public_ip": ip,
                "last_deploy": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
    save_conf(cfg)

    # 配置已落盘，但站点本机自检打不开时不能报成功，否则用户装完找不到问题
    site_ok = next((o for n, o, _ in checks if n == "网站首页 /"), False)
    if not site_ok:
        add("部署结果", False,
            "配置已写入，但本机访问站点失败。常见原因：域名未解析到本机、%s 端口被防火墙/安全组拦截、证书与域名不匹配" % port)
        return False, steps
    return True, steps


def do_rollback():
    steps = []

    def add(name, ok, msg):
        steps.append({"name": name, "ok": bool(ok), "msg": str(msg)})
        log("%s | %s | %s" % ("OK  " if ok else "FAIL", name, msg))
        return ok

    cands = sorted([d for d in os.listdir(BACKUP_DIR) if os.path.isdir(os.path.join(BACKUP_DIR, d))],
                   reverse=True) if os.path.exists(BACKUP_DIR) else []
    if not cands:
        return add("回滚", False, "没有可用备份"), steps

    d = os.path.join(BACKUP_DIR, cands[0])
    for name, target in (("config.json", V2BX_CONF), ("hkbt-deploy.conf", NGINX_CONF)):
        src = os.path.join(d, name)
        if os.path.exists(src):
            shutil.copy2(src, target)
            add("恢复 " + name, True, target)
    if os.path.exists(V2BX_SYSTEMD_DROPIN):
        os.remove(V2BX_SYSTEMD_DROPIN)
        if INIT == "systemd":
            run(["systemctl", "daemon-reload"])
    svc("restart", "V2bX")
    time.sleep(5)
    add("重启 V2bX", True, "已恢复原始监听")
    rc, out, err = run(["nginx", "-t"])
    if rc == 0:
        svc("reload", "nginx")
        add("重载 Nginx", True, "已生效")
    else:
        if os.path.exists(NGINX_CONF):
            os.remove(NGINX_CONF)
        svc("reload", "nginx")
        add("重载 Nginx", True, "配置有误，已移除部署配置")
    return True, steps


# ------------------------------------------------------------------ 配置同步
SYNC_SERVICE = "hkbt-sync"          # 不带后缀，OpenRC 直接用这个名字
SYNC_UNIT = SYNC_SERVICE + ".service"   # systemd 的文件名和 unit 名
SYNC_TIMER = "hkbt-sync.timer"
V2BX_SYNC_DROPIN = "/etc/systemd/system/V2bX.service.d/20-hkbt-sync.conf"


def current_fingerprint():
    """读已生效配置里的指纹。文件不存在、被改过、被覆盖都返回空串。"""
    try:
        with open(NGINX_CONF, encoding="utf-8") as f:
            for line in f:
                if line.startswith("# hkbt-fingerprint:"):
                    return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return ""


def do_sync(quiet=False, retries=3):
    """把面板上的最新节点参数同步到本机 Nginx 配置。

    与 --init 的关键区别：这里所有「取不到」都只告警、不动现有配置。
    面板临时不可达、节点临时改成 tcp、证书还没续上 —— 这些都不该让正在
    服务的 443 裸奔。真正需要变更时才会重写配置，写之前先备份、写之后先
    过 nginx -t，不通过就原样退回去。

    返回 (ok, msg, steps)。ok=True 表示「没有造成破坏」，包含「跳过」。
    """
    steps = []

    def add(name, ok, msg):
        steps.append({"name": name, "ok": bool(ok), "msg": str(msg)})
        if not quiet or not ok:
            log("%s | %s | %s" % ("OK  " if ok else "FAIL", name, msg))
        return ok

    cfg = load_conf()
    if not cfg.get("domain"):
        return True, "跳过：尚未初始化（config.json 里没有 domain）", steps

    # 面板可能刚随 V2bX 一起重启，给几次重试，别因为一次抖动就放弃
    v = None
    for i in range(max(1, retries)):
        v = detect_v2bx()
        if v["api_ok"]:
            break
        if i < retries - 1:
            time.sleep(5)

    if not v["installed"]:
        return True, "跳过：未检测到 V2bX", steps
    if not v["api_ok"]:
        return True, "跳过：面板 API 不可达（%s），保持现有配置不变" % (v["api_msg"] or "无响应"), steps

    domain = v["domain"] or cfg.get("domain") or ""
    port = str(cfg.get("port") or "443")
    proxy_port = str(v["port"] or "")
    path = v["path"] or ""

    if not proxy_port:
        return True, "跳过：面板没有返回节点端口，保持现有配置不变", steps
    if not path:
        return True, ("跳过：面板节点没有 ws path（可能已改成 tcp / reality），"
                      "无法做路径分流，保持现有配置不变"), steps
    if not domain:
        return True, "跳过：取不到节点域名，保持现有配置不变", steps

    # 面板上把节点端口改成和网站同端口（如 443）时，Nginx 和代理会抢同一个
    # 监听地址，谁后起谁 bind 失败。这种改法本身就不成立，不能照抄。
    conflict, why = port_conflict(port, proxy_port)
    if conflict:
        if str(proxy_port) == str(port):
            return True, ("跳过：面板把节点端口改成了 %s，与网站对外端口相同，"
                          "Nginx 和代理会抢同一个监听地址。请在面板把节点端口改成其他值"
                          % proxy_port), steps
        return True, "跳过：%s" % why, steps

    want = _fp(domain, port, path, proxy_port)
    if current_fingerprint() == want:
        return True, "配置一致，无需变更", steps

    old = ""
    try:
        with open(NGINX_CONF, encoding="utf-8") as f:
            old = f.read()
    except Exception:
        pass

    cert, key = v["cert"], v["key"]
    if not (cert and key and os.path.exists(cert) and os.path.exists(key)):
        return True, "跳过：证书文件还不存在（%s），保持现有配置不变" % (cert or "未配置"), steps

    prev = current_fingerprint() or "（无）"
    add("发现变更", True, "%s  →  %s" % (prev, want))

    backup("sync")
    ip = public_ip() or cfg.get("public_ip") or ""
    conf = gen_nginx(domain, port, path, DEFAULT_SITE, ip, cert, key,
                     cfg.get("admin_path", ""), proxy_port)
    os.makedirs(os.path.dirname(NGINX_CONF), exist_ok=True)
    with open(NGINX_CONF, "w", encoding="utf-8") as f:
        f.write(conf)

    rc, out, err = run(["nginx", "-t"])
    if rc != 0:
        if old:
            with open(NGINX_CONF, "w", encoding="utf-8") as f:
                f.write(old)
        add("Nginx 语法检查", False, "新配置未通过，已回滚到原配置：%s" % (err or out)[:300])
        return False, "Nginx 语法检查未通过，已回滚到原配置", steps
    add("Nginx 语法检查", True, "通过")

    rc, out, err = svc("reload", "nginx")
    if rc != 0:
        svc("restart", "nginx")
    add("重载 Nginx", True, "已生效")

    # 端口变了，NAT 规则也要跟着换，否则「代理端口打开是网站」会失效
    okc, msgc = setup_camouflage(proxy_port, port, cfg.get("camouflage", True))
    add("端口伪装", okc, msgc)

    cfg.update({"domain": domain, "port": port, "path": path,
                "proxy_port": proxy_port, "public_ip": ip or cfg.get("public_ip", ""),
                "last_sync": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
    save_conf(cfg)

    _, code, _ = run(
        "curl -4 -sk -o /dev/null -w '%%{http_code}' -m 10 --resolve %s:%s:127.0.0.1 "
        "https://%s:%s/" % (domain, port, domain, port), shell=True)
    add("验证 · 网站首页", code == "200", code or "连接失败")

    if code != "200":
        return False, "已按面板重写配置并重载，但本机访问站点失败，请检查 nginx 错误日志", steps
    return True, "已同步：代理 %s，路径 %s" % (proxy_port, path), steps


def install_sync_units(interval_min=3):
    """装自动同步：面板改了端口/路径，Nginx 要自己跟上。

    两个触发点，两种 init 系统各用各的机制：

    | | systemd | OpenRC |
    |---|---|---|
    | 代理重启后立刻同步 | `V2bX.service.d` drop-in | init 脚本的 `post_start()` |
    | 定时兜底 | `hkbt-sync.timer` | cron（crond） |

    用 drop-in / post_start 而不是改主单元，是为了让 `V2bX update`
    覆盖服务定义时这层钩子仍然在。
    """
    if INIT == "none":
        return False, "本机没有 systemd / OpenRC，跳过（可手动执行 app.py --sync）"

    py = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else (shutil.which("python3") or "python3")

    if INIT == "openrc":
        ok_cron, msg_cron = _install_sync_cron(interval_min, py)
        ok_hook, msg_hook = _hook_openrc_v2bx(py)
        if not ok_cron:
            return False, "配置定时同步失败：%s" % msg_cron
        note = "已启用：每 %d 分钟同步一次（cron）" % interval_min
        note += "；V2bX 重启钩子%s" % ("已挂上" if ok_hook else "未挂上（%s）" % msg_hook)
        return True, note

    try:
        unit_body = """[Unit]
Description=HKBT: sync nginx config with V2bX panel
After=network-online.target nginx.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=%s %s/app.py --sync --quiet
Nice=10
TimeoutStartSec=180
""" % (py, BASE)

        timer = """[Unit]
Description=HKBT: periodic config sync

[Timer]
OnBootSec=90
OnUnitActiveSec=%dm
AccuracySec=30s
Unit=%s

[Install]
WantedBy=timers.target
""" % (interval_min, SYNC_UNIT)

        dropin = """[Service]
# 由 hkbt-deploy 写入：V2bX 重启后立刻把面板最新参数同步到 Nginx
ExecStartPost=/bin/sh -c 'systemctl start --no-block %s || true'
""" % SYNC_UNIT

        for path, body in (("/etc/systemd/system/" + SYNC_UNIT, unit_body),
                           ("/etc/systemd/system/" + SYNC_TIMER, timer),
                           (V2BX_SYNC_DROPIN, dropin)):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(body)

        run(["systemctl", "daemon-reload"])
        run(["systemctl", "enable", "--now", SYNC_TIMER])
        return True, "已启用：V2bX 重启后立即同步，之后每 %d 分钟检查一次" % interval_min
    except Exception as e:
        return False, "安装同步服务失败：%s" % e


def _install_sync_cron(interval_min, py):
    """OpenRC 没有 timer，用 cron 做周期触发。"""
    marker = "hkbt-sync"
    line = "*/%d * * * * %s %s/app.py --sync --quiet  # %s" % (interval_min, py, BASE, marker)
    try:
        if shutil.which("crontab"):
            _, cur, _ = run(["crontab", "-l"])
            kept = [l for l in (cur or "").splitlines() if marker not in l]
            kept.append(line)
            p = subprocess.run(["crontab", "-"], input="\n".join(kept) + "\n",
                               text=True, capture_output=True, timeout=30)
            if p.returncode != 0:
                return False, (p.stderr or "crontab 写入失败").strip()[:200]
        else:
            # busybox crond 直接读 spool 文件
            d = "/var/spool/cron/crontabs"
            os.makedirs(d, exist_ok=True)
            f = os.path.join(d, "root")
            cur = ""
            if os.path.exists(f):
                cur = open(f, encoding="utf-8", errors="ignore").read()
            kept = [l for l in cur.splitlines() if marker not in l]
            kept.append(line)
            with open(f, "w", encoding="utf-8") as fh:
                fh.write("\n".join(kept) + "\n")
            os.chmod(f, 0o600)

        # crond 得在跑，否则规则写了也不会执行
        if not svc_active("crond"):
            svc("start", "crond")
        svc("enable", "crond")
        return True, line
    except Exception as e:
        return False, str(e)


def _hook_openrc_v2bx(py):
    """给 V2bX 的 OpenRC 脚本挂 post_start，重启后立刻同步一次。"""
    init = "/etc/init.d/V2bX"
    if not os.path.exists(init):
        return False, "未找到 /etc/init.d/V2bX"
    try:
        txt = open(init, encoding="utf-8", errors="ignore").read()
        if "hkbt-sync-hook" in txt:
            return True, "已存在"
        hook = ("\n# hkbt-sync-hook\n"
                "post_start() {\n"
                "    [ -f %s/app.py ] && %s %s/app.py --sync --quiet >/dev/null 2>&1 &\n"
                "}\n" % (BASE, py, BASE))
        with open(init, "a", encoding="utf-8") as f:
            f.write(hook)
        return True, init
    except Exception as e:
        return False, str(e)


# ------------------------------------------------------------------ CLI 初始化
def merge_init_conf(cfg):
    """初始化收尾用：以磁盘为准，只补本次生成的字段。
    do_deploy 会写入 domain/brand/template，不能用开头加载的旧 cfg 覆盖掉。"""
    disk = load_conf()
    for k in ("admin_path", "salt", "secret_key", "password_hash"):
        if cfg.get(k):
            disk[k] = cfg[k]
    return disk


def cmd_init(password, port=None, path=None, template=None, brand=None, auto=False,
             camouflage=None):
    os.makedirs(BASE, exist_ok=True)
    os.makedirs(BACKUP_DIR, exist_ok=True)
    cfg = load_conf()

    if not cfg.get("admin_path"):
        cfg["admin_path"] = "/__deploy-" + secrets.token_hex(4)
    if not cfg.get("salt"):
        cfg["salt"] = secrets.token_hex(16)
    if not cfg.get("secret_key"):
        cfg["secret_key"] = secrets.token_hex(32)
    if password:
        cfg["password_hash"] = hash_pw(password, cfg["salt"])

    # 全自动：模板和品牌都没指定时随机，让每台机器长得都不一样
    if auto:
        template = template or random_template()
        brand = brand or random_brand()[0]
    if template:
        cfg["template"] = template
    if brand:
        cfg["brand"] = brand
    if camouflage is not None:
        cfg["camouflage"] = bool(camouflage)

    # 必须先落盘：do_deploy 会重新 load_conf() 读取 admin_path 来生成面板反代段，
    # 否则首次安装时 admin_path 还在内存里，Nginx 配置会缺面板入口（访问 404）
    save_conf(cfg)

    v = detect_v2bx()
    domain = v["domain"] or ""
    # 对外端口固定 443：节点端口可能是 20000/21311，Nginx 不能去抢它。
    # 两者不同时走分离端口（代理留在原端口），相同时由 do_deploy 收窄 sing-box。
    port = str(port or "443")
    path = path or v["path"] or ""

    # 路径不猜。猜 /index 之类的默认值时，Nginx 的 location 与节点真实 path 不符，
    # 代理流量会掉进静态站点（握手能过、随即断开），比直接报错难排查得多。
    if not path:
        print("=" * 62)
        print("  无法从面板读取节点路径（ws path），已停止部署")
        print("=" * 62)
        print("  V2bX 的端口和路径都是运行时从面板拉取的，本机没有副本，")
        print("  因此必须能连通面板 API 才能拿到，不能靠手工填写或默认值。")
        print()
        print("  请依次检查：")
        print("    1) /etc/V2bX/config.json 的 ApiHost / ApiKey / NodeID 是否正确")
        print("    2) 面板节点是否为 ws / grpc 类型（tcp、reality 节点没有 path）")
        print("    3) 本机能否访问面板：%s" % (v["api_host"] or "<ApiHost 为空>"))
        print("       请求：%s/api/v1/server/UniProxy/config?node_type=%s&node_id=%s&token=***"
              % ((v["api_host"] or "<ApiHost>").rstrip("/"), v["ntype"], v["node_id"] or "<NodeID>"))
        print("    4) 面板返回内容：%s" % (v["api_msg"] or "API 未返回数据"))
        print()
        print("  面板连通后重新执行即可，无需改动任何配置。")
        print("=" * 62)
        log("部署中止：面板未返回 path")
        return False

    ip = public_ip()

    print("=" * 62)
    print("  HKBT Deploy 初始化")
    print("=" * 62)
    print("  代理程序   : %s" % ("V2bX / %s 核心" % v["core"] if v["installed"] else "未检测到"))
    print("  节点域名   : %s" % (domain or "未取到"))
    print("  对外端口   : %s（网站与面板）" % port)
    print("  代理端口   : %s  ← 面板读取" % (v["port"] or "未取到"))
    print("  代理路径   : %s  ← 面板读取" % path)
    print("  节点类型   : %s%s" % (v["network"] or "未知",
                                " / TLS" if v["tls"] else ""))
    print("  公网 IP    : %s" % (ip or "未取到"))
    print("  站点模板   : %s" % (cfg.get("template") or DEFAULT_TEMPLATE))
    print("  网站名称   : %s" % (cfg.get("brand") or "模板默认"))
    print("  面板入口   : https://%s%s/" % (domain or "<域名>", cfg["admin_path"]))
    if v["source"] == "config":
        print("  " + "-" * 58)
        print("  注意：面板 API 不可达（%s）" % (v["api_msg"] or "无响应"))
        print("        当前沿用上次部署记录的路径 %s。" % path)
        print("        如果面板上改过 path，请先恢复面板连通再重新部署，")
        print("        否则 Nginx 分流会与节点不一致，客户端连上即断。")
    print("=" * 62)

    if v["installed"] and domain and ip:
        ok, _steps = do_deploy(domain, port, path, None, cfg.get("brand"), cfg.get("template"))
        # 首次部署还没有站点文件，放一个占位首页
        site = DEFAULT_SITE
        os.makedirs(site, exist_ok=True)
        if not os.path.exists(os.path.join(site, "index.html")):
            with open(os.path.join(site, "index.html"), "w", encoding="utf-8") as f:
                f.write("<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
                        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
                        "<title>HKBT Deploy</title></head><body style=\"font-family:system-ui,"
                        "-apple-system,sans-serif;display:grid;place-items:center;height:100vh;"
                        "margin:0;background:#0F1115;color:#E6E8EB\"><div style=\"text-align:center\">"
                        "<h1 style=\"color:#E53E3E;margin:0 0 12px\">HKBT Deploy</h1>"
                        "<p style=\"color:#8B93A1\">站点尚未部署，请在面板上传 HTML 压缩包。</p>"
                        "</div></body></html>")
            nu = nginx_user()
            if nu:
                run(["chown", "-R", "%s:%s" % (nu, nu), site])
            svc("reload", "nginx")
        if not ok:
            # 不能只打印一行「未通过」就往下走：调用方（hkbt.sh）会当成部署成功，
            # 继续打印面板地址和「站点已就绪」，用户看到的全是空值。
            print("  初始部署   : 失败，原因见上方 FAIL 行")
            print("  修好后重新执行即可，不需要手工清理。")
            return None
        print("  初始部署   : 成功")
    else:
        print("  初始部署   : 跳过（未检测到 V2bX 或缺少域名/IP）")
        if not v["installed"]:
            print("              本工具用于与 V2bX 共存部署，需要先装好 V2bX 并配置节点。")
            print("              装好后重新执行：python3 %s/app.py --init --password 你的密码" % BASE)
        elif not domain:
            print("              未从 V2bX 配置取到域名，请检查 /etc/V2bX/config.json 的 CertConfig.CertDomain")

    # 以磁盘为准再合并：do_deploy 已写入 domain/brand/template，不能用开头加载的旧 cfg 覆盖
    cfg = merge_init_conf(cfg)
    save_conf(cfg)
    print("\n完成。启动服务后访问上面的面板入口。\n")
    return cfg


# ------------------------------------------------------------------ Flask 应用
def create_app():
    from flask import Flask, jsonify, render_template_string, request, session

    app = Flask(__name__)
    cfg = load_conf()
    app.secret_key = cfg.get("secret_key", secrets.token_hex(32))

    HTML = open(os.path.join(BASE, "templates", "panel.html"), encoding="utf-8").read()

    def need_login():
        return not session.get("ok")

    @app.route("/")
    def index():
        return render_template_string(HTML)

    @app.route("/api/login", methods=["POST"])
    def api_login():
        d = request.get_json(silent=True) or {}
        c = load_conf()
        if verify_pw(d.get("password", ""), c):
            session["ok"] = True
            session.permanent = True
            log("登录成功")
            return jsonify({"ok": True})
        log("登录失败")
        return jsonify({"ok": False, "msg": "密码错误"}), 401

    @app.route("/api/logout", methods=["POST"])
    def api_logout():
        session.clear()
        return jsonify({"ok": True})

    @app.route("/api/state")
    def api_state():
        if need_login():
            return jsonify({"ok": False, "login": False}), 401
        c = load_conf()
        v = detect_v2bx()
        _, ls, _ = run(["ss", "-tlnp"])
        port = c.get("port") or v["port"] or "443"
        site = c.get("site_dir", DEFAULT_SITE)
        idx = os.path.join(site, "index.html")
        deployed = False
        if os.path.exists(idx):
            try:
                with open(idx, encoding="utf-8", errors="ignore") as fh:
                    deployed = "尚未部署" not in fh.read()[:2000]
            except Exception:
                deployed = True
        return jsonify({
            "ok": True, "login": True,
            "panel": {
                "domain": c.get("domain") or v["domain"],
                "port": c.get("port") or v["port"],
                "path": c.get("path") or v["path"],
                "public_ip": c.get("public_ip") or public_ip(),
                "last_deploy": c.get("last_deploy", ""),
                "brand": c.get("brand") or default_brand(c.get("domain") or v["domain"] or ""),
                "admin_path": c.get("admin_path", ""),
                "site_deployed": deployed,
                "template": c.get("template") or DEFAULT_TEMPLATE,
            },
            "templates": list_templates(),
            "v2bx": {
                "installed": v["installed"], "core": v["core"],
                "listen_ip": v["listen_ip"], "node_id": v["node_id"],
                "panel_port": v["port"], "panel_path": v["path"],
                "api_ok": v["api_ok"], "api_msg": v["api_msg"],
                "cert_expiry": cert_expiry(v["cert"]),
                "loopback_only": ("127.0.0.1:%s" % port) in ls,
                "nginx_public": ("%s:%s" % (c.get("public_ip") or public_ip(), port)) in ls,
            }
        })

    @app.route("/api/deploy", methods=["POST"])
    def api_deploy():
        if need_login():
            return jsonify({"ok": False, "login": False}), 401
        domain = (request.form.get("domain") or "").strip()
        port = (request.form.get("port") or "").strip()
        path = (request.form.get("path") or "").strip()
        brand = (request.form.get("brand") or "").strip()
        f = request.files.get("site")
        tmp = None
        if f and f.filename:
            tmp = os.path.join(tempfile.gettempdir(), "hkbt-upload-%s.zip" % secrets.token_hex(4))
            f.save(tmp)
        template = (request.form.get("template") or "").strip()
        ok, steps = do_deploy(domain, port, path, tmp, brand, template)
        if tmp and os.path.exists(tmp):
            os.remove(tmp)
        return jsonify({"ok": ok, "steps": steps})

    @app.route("/api/rollback", methods=["POST"])
    def api_rollback():
        if need_login():
            return jsonify({"ok": False, "login": False}), 401
        ok, steps = do_rollback()
        return jsonify({"ok": ok, "steps": steps})

    @app.route("/api/verify", methods=["POST"])
    def api_verify():
        if need_login():
            return jsonify({"ok": False, "login": False}), 401
        c = load_conf()
        res = verify(c.get("domain", ""), c.get("port", "443"), c.get("path", "/index"),
                     c.get("public_ip") or public_ip())
        return jsonify({"ok": True, "checks": [{"name": n, "ok": o, "msg": m} for n, o, m in res]})

    return app


def main():
    ap = argparse.ArgumentParser(description="HKBT Deploy")
    ap.add_argument("--init", action="store_true", help="初始化：探测环境并生成配置")
    ap.add_argument("--password", help="设置面板登录密码")
    ap.add_argument("--port", help="对外端口（网站/面板），默认 443；代理端口从面板自动读取")
    ap.add_argument("--path", help="仅调试用：强制指定代理路径。正常情况不用填，自动从面板读取")
    ap.add_argument("--template", help="站点模板 key，默认沿用配置或 trade")
    ap.add_argument("--brand", help="网站名称，默认沿用配置或模板默认品牌")
    ap.add_argument("--auto", action="store_true",
                    help="全自动：随机模板 + 随机品牌，适合批量装机")
    ap.add_argument("--probe", action="store_true",
                    help="只探测节点配置并以 JSON 输出（端口/路径来自面板，不部署）")
    ap.add_argument("--sync", action="store_true",
                    help="把面板最新端口/路径同步到 Nginx（幂等，无变化不动任何文件）")
    ap.add_argument("--quiet", action="store_true", help="安静模式：只在有变更或出错时输出")
    ap.add_argument("--install-sync", action="store_true",
                    help="安装 systemd timer：面板改动后自动同步（V2bX 重启即触发 + 定时兜底）")
    ap.add_argument("--sync-interval", type=int, default=3,
                    help="定时同步间隔，单位分钟，默认 3")
    ap.add_argument("--no-camouflage", action="store_true",
                    help="不把代理端口的入站流量转到 Nginx（默认开启：代理端口也显示网站）")
    ap.add_argument("--serve", action="store_true", help="启动面板服务")
    a = ap.parse_args()

    if a.probe:
        v = detect_v2bx()
        keys = ("installed", "core", "domain", "port", "path", "sni",
                "network", "tls", "node_id", "api_ok", "api_msg", "source")
        print(json.dumps({k: v[k] for k in keys}, ensure_ascii=False))
        # 退出码 2 表示「面板没给出 path」，调用方据此中止，不要继续部署
        sys.exit(0 if v["path"] else 2)

    if a.install_sync:
        ok, msg = install_sync_units(a.sync_interval)
        print(("OK   " if ok else "FAIL ") + msg)
        sys.exit(0 if ok else 1)

    if a.sync:
        ok, msg, _steps = do_sync(quiet=a.quiet)
        if not a.quiet or not ok or not msg.startswith("配置一致"):
            print(("OK    " if ok else "FAIL  ") + msg)
        sys.exit(0 if ok else 1)

    if a.init:
        r = cmd_init(a.password, a.port, a.path, a.template, a.brand, a.auto,
                     camouflage=False if a.no_camouflage else None)
        sys.exit(0 if r else 1)
    if a.serve:
        app = create_app()
        app.run(host="127.0.0.1", port=PANEL_PORT, debug=False, threaded=True)
        return
    ap.print_help()


if __name__ == "__main__":
    main()
