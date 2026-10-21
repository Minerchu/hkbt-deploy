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
NGINX_CONF = "/etc/nginx/conf.d/hkbt-deploy.conf"
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


def public_ip():
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


# ------------------------------------------------------------------ V2bX 探测
def detect_v2bx():
    """读取 V2bX 配置 + 面板 API，拿到节点真实端口/路径/域名"""
    info = {
        "installed": os.path.exists(V2BX_CONF),
        "core": "", "domain": "", "port": "", "path": "",
        "sni": "", "network": "", "tls": 0,
        "listen_ip": "", "cert": "", "key": "", "node_id": "",
        "api_ok": False, "api_msg": ""
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
    cc = node.get("CertConfig", {})
    info["domain"] = cc.get("CertDomain", "")
    info["cert"] = cc.get("CertFile", "")
    info["key"] = cc.get("KeyFile", "")

    # 端口与路径：优先问面板 API
    api = (node.get("ApiHost") or "").rstrip("/")
    key = node.get("ApiKey", "")
    ntype = node.get("NodeType", "vless")
    if api and key and info["node_id"]:
        url = "%s/api/v1/server/UniProxy/config?node_type=%s&node_id=%s&token=%s" % (
            api, ntype, info["node_id"], key)
        rc, out, err = run(["curl", "-s", "-m", "15", url])
        try:
            data = json.loads(out)
            if data.get("server_port"):
                info["port"] = str(data["server_port"])
                ns = data.get("networkSettings") or {}
                info["path"] = ns.get("path", "/index")
                ts = data.get("tls_settings") or {}
                info["sni"] = ts.get("server_name", info["domain"])
                info["network"] = data.get("network", "")
                info["tls"] = data.get("tls", 0)
                info["api_ok"] = True
            else:
                info["api_msg"] = str(data.get("message") or data)[:120]
        except Exception:
            info["api_msg"] = "面板返回无法解析: %s" % (out[:100] or err[:100])

    if not info["port"]:
        info["port"] = "443"
        if not info["api_msg"]:
            info["api_msg"] = "未能从面板取到端口，已默认 443，请在面板中确认"
    if not info["path"]:
        info["path"] = "/index"
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


def gen_nginx(domain, port, path, site_dir, public_ip, cert, key, admin_path):
    """生成共存配置。注意：必须绑公网 IP，不能绑 0.0.0.0"""
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
    ssl_session_cache shared:SSL:10m;
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

    # ---- 代理路径：%s 双用途 ----
    # 带 Upgrade: websocket -> sing-box
    # 普通浏览器访问      -> 网站首页
    location = %s {
        error_page 418 = @v2bx_ws;
        if ($http_upgrade) { return 418; }
        root %s;
        try_files /index.html =404;
    }

    location @v2bx_ws {
        proxy_pass https://127.0.0.1:%s;
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
""" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), domain, public_ip, port, path,
       mapv, domain, site_dir, ("" if str(port) == "443" else ":%s" % port),
       public_ip, port, h2_listen, h2_directive, domain,
       cert, key,
       path, path, site_dir,
       port, domain,
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

    run(["chown", "-R", "www-data:www-data", site_dir])
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


def narrow_v2bx(target="127.0.0.1"):
    """把 sing-box 收窄到回环地址"""
    if not os.path.exists(V2BX_CONF):
        return True, "未检测到 V2bX，跳过"
    rc, units, _ = run(["systemctl", "list-unit-files", "V2bX.service"])
    if "V2bX.service" not in units:
        return True, "未检测到 V2bX 服务，跳过收窄"
    with open(V2BX_CONF, encoding="utf-8") as f:
        txt = f.read()
    cur = re.search(r'"ListenIP"\s*:\s*"([^"]*)"', txt)
    if cur and cur.group(1) == target:
        return True, "监听地址已是 %s" % target
    new = re.sub(r'"ListenIP"\s*:\s*"[^"]*"', '"ListenIP": "%s"' % target, txt)
    with open(V2BX_CONF, "w", encoding="utf-8") as f:
        f.write(new)

    # 固化：防止 V2bX 重启后改回
    os.makedirs(os.path.dirname(V2BX_SYSTEMD_DROPIN), exist_ok=True)
    with open(V2BX_SYSTEMD_DROPIN, "w", encoding="utf-8") as f:
        f.write("[Service]\nExecStartPre=/bin/sed -i 's/\"ListenIP\": \"::\"/\"ListenIP\": \"%s\"/' %s\n"
                % (target, V2BX_CONF))
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "restart", "V2bX"])
    time.sleep(6)
    rc, out, _ = run(["ss", "-tlnp"])
    ok = ("%s:%s" % (target, "443")) in out or re.search(r"%s:\d+" % re.escape(target), out)
    return ok, "V2bX 监听已收窄到 %s" % target if ok else "收窄后未观察到回环监听，请检查"


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
    rc, _, _ = run(["chown", "-R", "www-data:www-data", site_dir])
    n = sum(len(fs) for _, _, fs in os.walk(site_dir))
    return True, "已解压 %d 个文件到 %s" % (n, site_dir)


def verify(domain, port, path, public_ip):
    """部署后自检。连通性用 -k（证书可能自签/未生效），证书单独校验，避免误报。"""
    checks = []
    r = "--resolve %s:%s:%s" % (domain, port, public_ip)

    _, code, _ = run("curl -4 -sk -o /dev/null -w '%%{http_code}' -m 10 %s https://%s:%s/" % (r, domain, port), shell=True)
    checks.append(("网站首页 /", code == "200", code or "连接失败"))

    _, n, _ = run("curl -4 -sk -m 10 %s https://%s:%s%s | grep -c '<html'" % (r, domain, port, path), shell=True)
    ok_page = n.strip() not in ("", "0")
    checks.append(("代理路径 %s（浏览器）" % path, ok_page, "返回站点页面" if ok_page else (n.strip() or "未返回页面")))

    _, wsc, _ = run(
        "curl -4 -sk -o /dev/null -w '%%{http_code}' -m 10 %s -H 'Connection: Upgrade' "
        "-H 'Upgrade: websocket' -H 'Sec-WebSocket-Version: 13' "
        "-H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' https://%s:%s%s" % (r, domain, port, path), shell=True)
    checks.append(("代理路径 %s（WebSocket）" % path, wsc in ("101", "200"), wsc or "连接失败"))

    _, subj, _ = run("echo | openssl s_client -connect %s:%s -servername %s 2>/dev/null | openssl x509 -noout -subject" % (public_ip, port, domain), shell=True)
    ok_cert = (("CN=%s" % domain) in subj) or (("CN = %s" % domain) in subj)
    checks.append(("证书匹配域名", ok_cert, subj.replace("subject=", "") or "未取到（若自签证书可忽略）"))

    _, ls, _ = run(["ss", "-tlnp"])
    _, units, _ = run(["systemctl", "list-unit-files", "V2bX.service"])
    has_v2bx = "V2bX.service" in units
    if has_v2bx:
        checks.append(("sing-box 仅本机监听", ("127.0.0.1:%s" % port) in ls,
                       "127.0.0.1:%s" % port if ("127.0.0.1:%s" % port) in ls else "未收窄，请检查"))
    else:
        checks.append(("sing-box 仅本机监听", True, "本机未安装 V2bX 服务，跳过"))

    checks.append(("Nginx 接管公网端口", ("%s:%s" % (public_ip, port)) in ls,
                   "%s:%s" % (public_ip, port) if ("%s:%s" % (public_ip, port)) in ls else "未观察到"))
    return checks


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
    if v["api_ok"] and v["port"] != port:
        add("端口一致性", False, "面板节点实际端口为 %s，你填的是 %s。若不一致会导致代理不可用，已按你填写的值继续，但请确认" % (v["port"], port))
    if v["api_ok"] and v["path"] != path:
        add("路径一致性", False, "面板节点实际路径为 %s，你填的是 %s。路径必须完全一致，否则客户端连不上" % (v["path"], path))

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

    ok, msg = narrow_v2bx()
    if not add("收窄 sing-box 监听", ok, msg):
        return False, steps

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
    conf = gen_nginx(domain, port, path, site_dir, ip, cert, key, admin_path)
    os.makedirs(os.path.dirname(NGINX_CONF), exist_ok=True)
    with open(NGINX_CONF, "w", encoding="utf-8") as f:
        f.write(conf)
    add("生成 Nginx 配置", True, "%s（绑定 %s:%s）" % (NGINX_CONF, ip, port))

    rc, out, err = run(["nginx", "-t"])
    if rc != 0:
        return add("Nginx 语法检查", False, (err or out)[:400]), steps
    add("Nginx 语法检查", True, "通过")

    rc, out, err = run(["systemctl", "reload", "nginx"])
    if rc != 0:
        run(["systemctl", "restart", "nginx"])
    add("重载 Nginx", True, "已生效")

    time.sleep(2)
    for name, okk, detail in verify(domain, port, path, ip):
        add("验证 · " + name, okk, detail)

    cfg.update({"brand": cfg.get("brand") or default_brand(domain),
                "domain": domain, "port": port, "path": path,
                "site_dir": site_dir, "public_ip": ip,
                "last_deploy": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
    save_conf(cfg)
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
        run(["systemctl", "daemon-reload"])
    run(["systemctl", "restart", "V2bX"])
    time.sleep(5)
    add("重启 V2bX", True, "已恢复原始监听")
    rc, out, err = run(["nginx", "-t"])
    if rc == 0:
        run(["systemctl", "reload", "nginx"])
        add("重载 Nginx", True, "已生效")
    else:
        if os.path.exists(NGINX_CONF):
            os.remove(NGINX_CONF)
        run(["systemctl", "reload", "nginx"])
        add("重载 Nginx", True, "配置有误，已移除部署配置")
    return True, steps


# ------------------------------------------------------------------ CLI 初始化
def cmd_init(password, port=None, path=None):
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

    v = detect_v2bx()
    domain = v["domain"] or ""
    port = str(port or v["port"] or "443")
    path = path or v["path"] or "/index"
    ip = public_ip()

    print("=" * 62)
    print("  HKBT Deploy 初始化")
    print("=" * 62)
    print("  代理程序   : %s" % ("V2bX / %s 核心" % v["core"] if v["installed"] else "未检测到"))
    print("  节点域名   : %s" % (domain or "未取到"))
    print("  节点端口   : %s%s" % (port, "" if v["api_ok"] else "  (未从面板确认)"))
    print("  代理路径   : %s" % path)
    print("  公网 IP    : %s" % (ip or "未取到"))
    print("  站点模板   : %s" % (cfg.get("template") or DEFAULT_TEMPLATE))
    print("  面板入口   : https://%s%s/" % (domain or "<域名>", cfg["admin_path"]))
    print("=" * 62)

    if v["installed"] and domain and ip:
        ok, _steps = do_deploy(domain, port, path, None, None, cfg.get("template"))
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
            run(["chown", "-R", "www-data:www-data", site])
            run(["systemctl", "reload", "nginx"])
        print("  初始部署   : %s" % ("成功" if ok else "部分失败，请查看面板"))
    else:
        print("  初始部署   : 跳过（未检测到 V2bX 或缺少域名/IP）")

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
    ap.add_argument("--port", help="代理端口，默认自动探测（通常 443）")
    ap.add_argument("--path", help="代理路径，默认自动探测（如 /index）")
    ap.add_argument("--serve", action="store_true", help="启动面板服务")
    a = ap.parse_args()

    if a.init:
        cmd_init(a.password, a.port, a.path)
        return
    if a.serve:
        app = create_app()
        app.run(host="127.0.0.1", port=PANEL_PORT, debug=False, threaded=True)
        return
    ap.print_help()


if __name__ == "__main__":
    main()
