#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""站点自检：每套独立起服务，验证 13 页 200、资源可达、无渲染残留、无死链。

用法：
  python3 build/test_sites.py            # 全部
  python3 build/test_sites.py auto food  # 指定几套
  python3 build/test_sites.py --serve auto   # 只起服务（预览用，Ctrl+C 结束）
"""
import functools
import http.server
import json
import os
import re
import socketserver
import sys
import threading
import urllib.error
import urllib.request
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SITES = os.path.join(ROOT, "templates", "sites")

PAGES = ["index.html", "about.html", "business.html", "cases.html", "insights.html",
         "contact.html", "tracking.html", "downloads.html", "terms.html", "404.html"]
BAD_TOKENS = ["%s", "%d", "None", "{{", "}}", "undefined"]


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        for k in ("href", "src"):
            v = d.get(k)
            if v and not v.startswith(("http", "#", "mailto:", "tel:", "data:", "javascript:")):
                self.links.append(v)


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def serve(directory, port):
    handler = functools.partial(Quiet, directory=directory)
    httpd = socketserver.TCPServer(("127.0.0.1", port), handler)
    httpd.allow_reuse_address = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:
        return 0, ""


def check(key, port):
    d = os.path.join(SITES, key)
    base = "http://127.0.0.1:%d" % port
    problems = []
    pages = list(PAGES)
    ins = os.path.join(d, "insights")
    if os.path.isdir(ins):
        pages += ["insights/" + f for f in sorted(os.listdir(ins)) if f.endswith(".html")]

    if len(pages) != 13:
        problems.append("页面数为 %d，应为 13" % len(pages))

    seen = set()
    for p in pages:
        code, html = get("%s/%s" % (base, p))
        if code != 200:
            problems.append("%s -> HTTP %s" % (p, code))
            continue
        for tok in BAD_TOKENS:
            if tok in html:
                problems.append("%s 含渲染残留 %r" % (p, tok))
                break
        lp = LinkParser()
        lp.feed(html)
        for link in lp.links:
            if link.startswith("/"):
                if link in seen:
                    continue
                seen.add(link)
                c2, _ = get(base + link)
                if c2 != 200:
                    problems.append("%s 内链 %s -> %s" % (p, link, c2))

    # meta.json
    mp = os.path.join(d, "meta.json")
    if not os.path.exists(mp):
        problems.append("缺少 meta.json")
    else:
        m = json.load(open(mp, encoding="utf-8"))
        if not m.get("default_brand"):
            problems.append("meta.json 缺少 default_brand")

    return problems, len(pages), len(seen)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    keys = args or sorted([k for k in os.listdir(SITES)
                           if os.path.isdir(os.path.join(SITES, k))])

    if "--serve" in sys.argv:
        key = keys[0]
        print("预览 http://127.0.0.1:8801/  (目录 %s)" % key)
        handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                    directory=os.path.join(SITES, key))
        socketserver.TCPServer(("0.0.0.0", 8801), handler).serve_forever()
        return

    total_bad = 0
    for i, k in enumerate(keys):
        port = 8820 + i
        httpd = serve(os.path.join(SITES, k), port)
        try:
            probs, npages, nres = check(k, port)
        finally:
            httpd.shutdown()
            httpd.server_close()
        if probs:
            total_bad += 1
            print("  FAIL  %-12s %d 页 / %d 资源" % (k, npages, nres))
            for p in probs[:12]:
                print("        - %s" % p)
            if len(probs) > 12:
                print("        ... 另有 %d 项" % (len(probs) - 12))
        else:
            print("  OK    %-12s %d 页 / %d 资源全部 200" % (k, npages, nres))

    print("\n%d 套中 %d 套通过" % (len(keys), len(keys) - total_bad))
    return 1 if total_bad else 0


if __name__ == "__main__":
    sys.exit(main())
