#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""部署链路自检：验证模板注册、选择、品牌与域名替换（不触碰 Nginx / systemd）。"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import app  # noqa: E402

app.SITES_TPL = os.path.join(ROOT, "templates", "sites")
app.SITE_TPL = os.path.join(ROOT, "templates", "site")


def main():
    tpls = app.list_templates()
    print("注册模板：%d 套" % len(tpls))
    for t in tpls:
        print("  %-12s %-10s %-8s %2d 页  默认品牌 %s" % (
            t["key"], t["name"], t["variant"], t["pages"], t["default_brand"]))

    if len(tpls) < 11:
        print("FAIL 模板数量不足")
        return 1

    tmp = tempfile.mkdtemp()
    bad = 0
    try:
        for t in tpls:
            out = os.path.join(tmp, t["key"])
            ok, msg = app.generate_default_site("海联实业", "hai-lian.com", out, t["key"])
            if not ok:
                print("FAIL %-12s %s" % (t["key"], msg))
                bad += 1
                continue

            residual = []
            pages = 0
            # meta.json 属于模板元数据，不应被部署到站点目录
            if os.path.exists(os.path.join(out, "meta.json")):
                residual.append("meta.json 未剔除（会暴露模板信息）")
            for root, _, files in os.walk(out):
                for f in files:
                    if not f.endswith((".html", ".css", ".js", ".svg", ".txt")):
                        continue
                    p = os.path.join(root, f)
                    s = open(p, encoding="utf-8", errors="ignore").read()
                    if f.endswith(".html"):
                        pages += 1
                    if "hkbt.example" in s:
                        residual.append("%s: hkbt.example" % f)
                    if t["default_brand"] != "海联实业" and t["default_brand"] in s:
                        residual.append("%s: 旧品牌 %s" % (f, t["default_brand"]))
                    if "海联实业" not in s and f.endswith(".html"):
                        residual.append("%s: 未替换品牌" % f)

            if pages != 13:
                residual.append("页面数 %d != 13" % pages)
            if residual:
                bad += 1
                print("FAIL %-12s %d 处残留：%s" % (t["key"], len(residual), "; ".join(residual[:3])))
            else:
                print("  OK %-12s 13 页，品牌与域名全部替换，无残留" % t["key"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 非法模板名应回落到默认
    d, k = app.template_dir("../../etc")
    print("\n非法 key 回落：%s" % k)
    if k != app.DEFAULT_TEMPLATE:
        print("FAIL 未回落到默认模板")
        bad += 1

    # 初始化收尾不能覆盖 do_deploy 写入的 domain/brand/template
    disk = {"domain": "hai-lian.com", "brand": "海联实业", "template": "trade",
            "port": "443", "path": "/index"}
    orig = app.load_conf
    app.load_conf = lambda: dict(disk)
    try:
        merged = app.merge_init_conf({"admin_path": "/__x", "salt": "s"})
    finally:
        app.load_conf = orig
    lost = [x for x in ("domain", "brand", "template") if not merged.get(x)]
    if lost:
        print("FAIL 初始化收尾丢失字段：%s（装完不会打印面板地址）" % "、".join(lost))
        bad += 1
    else:
        print("初始化收尾保留 domain/brand/template")

    # 端口冲突：对外端口 = 代理端口时必须拒绝，否则 Nginx 绑不上、整站带面板一起挂
    ok, why = app.port_conflict(21311, 21311)
    if not ok or "21311" not in why:
        print("FAIL 未拦截「对外端口 = 代理端口」")
        bad += 1
    else:
        print("端口冲突拦截：对外端口 = 代理端口")

    orig_owner = app.port_owner
    app.port_owner = lambda p: "V2bX"
    try:
        ok, why = app.port_conflict(21311, 9999)
    finally:
        app.port_owner = orig_owner
    if not ok or "V2bX" not in why:
        print("FAIL 未拦截「端口被其他进程占用」")
        bad += 1
    else:
        print("端口冲突拦截：端口已被 V2bX 占用")

    app.port_owner = lambda p: "nginx"
    try:
        ok, _ = app.port_conflict(443, 21311)
    finally:
        app.port_owner = orig_owner
    if ok:
        print("FAIL 重复部署被误判为端口冲突")
        bad += 1
    else:
        print("重复部署放行：端口由 nginx 自己持有")

    # 分离端口时 Nginx 绑 0.0.0.0（NAT 机上公网 IP 不在网卡，绑死会失败）
    conf = app.gen_nginx("hai-lian.com", "443", "/davsvbfb", "/var/www/hkbt",
                         "132.226.145.154", "/tmp/c.cer", "/tmp/c.key", "/__x", "21311")
    for want, desc in (("listen 0.0.0.0:443", "对外 443 绑 0.0.0.0"),
                       ("proxy_pass https://127.0.0.1:21311", "按路径转发到代理"),
                       ("location /__x/", "面板入口")):
        if want not in conf:
            print("FAIL 配置缺少：%s" % desc)
            bad += 1
        else:
            print("配置校验：%s" % desc)

    # path 带 query（面板里可能是 /sdf=?kfj）：location 只用 ? 前的部分匹配，
    # 并且尾部 = 可选 + rewrite 回真实 path，兼容客户端把 ? 当分隔符的各种写法
    conf_q = app.gen_nginx("hai-lian.com", "443", "/sdf=?kfj", "/var/www/hkbt",
                           "132.226.145.154", "/tmp/c.cer", "/tmp/c.key", "/__x", "20000")
    if "location ~ ^/sdf=?$ {" not in conf_q:
        print("FAIL 带 query 的 path 未正确切分 location")
        bad += 1
    else:
        print("配置校验：path 带 query 时 location 取 ? 前部分")
    if "rewrite ^ /sdf=?kfj? break;" not in conf_q:
        print("FAIL 带 query 的 path 未统一改写回真实路径")
        bad += 1
    else:
        print("配置校验：path 带 query 时统一改写")
    if "proxy_pass https://127.0.0.1:20000;" not in conf_q:
        print("FAIL 带 query 的 path 未正确指向代理端口")
        bad += 1

    # 无 query 的普通 path 不应产生 rewrite，保持原样透传
    conf_plain = app.gen_nginx("hai-lian.com", "443", "/davsvbfb", "/var/www/hkbt",
                               "132.226.145.154", "/tmp/c.cer", "/tmp/c.key", "/__x", "21311")
    if "location ~ ^/davsvbfb$ {" not in conf_plain or "rewrite ^ /davsvbfb" in conf_plain:
        print("FAIL 普通 path 不应改写")
        bad += 1
    else:
        print("配置校验：普通 path 精确匹配不改写")

    print("\n%s" % ("全部通过" if bad == 0 else "%d 项失败" % bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
