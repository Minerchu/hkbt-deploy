#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""站点生成器：把 data_a/b/c 的模板数据渲染为 templates/sites/<key>/ 下的完整站点。

设计要点
  * 统一 HTML 结构，视觉差异全部交给 build/css/<variant>.css
  * 每个站点自包含：13 个页面 + assets/css + assets/js + assets/img + 4 份 PDF
  * 品牌与域名写行业默认值，部署时由 app.py 按 meta.json 的 default_brand 替换
  * 图片槽位缺失时回落到语义相近的共享图，保证不出现 404
"""
import json
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT_ROOT = os.path.join(ROOT, "templates", "sites")
SHARED_IMG = os.path.join(HERE, "shared", "img")
SHARED_DOC = os.path.join(HERE, "shared", "docs")
CSS_DIR = os.path.join(HERE, "css")
DOMAIN = "hkbt.example"

sys.path.insert(0, HERE)
import data_a  # noqa: E402
import data_b  # noqa: E402
import data_c  # noqa: E402

ALL = data_a.TEMPLATES + data_b.TEMPLATES + data_c.TEMPLATES

# 图片槽位回落：缺失时按顺序取第一个存在的
FALLBACK = {
    "beauty.jpg": ["beauty.jpg", "office-team.jpg"],
    "planning.jpg": ["planning.jpg", "meeting-room.jpg"],
    "customs.jpg": ["customs.jpg", "signing.jpg"],
    "handshake.jpg": ["handshake.jpg", "signing.jpg"],
    "office-team.jpg": ["office-team.jpg", "team-desk.jpg"],
    "warehouse-aisle.jpg": ["warehouse-aisle.jpg", "warehouse-large.jpg"],
    "warehouse-large.jpg": ["warehouse-large.jpg", "warehouse-pallets.jpg"],
    "truck.jpg": ["truck.jpg", "port.jpg"],
    "port.jpg": ["port.jpg", "port-aerial.jpg"],
    "dashboard.jpg": ["dashboard.jpg", "planning.jpg"],
}
DEFAULT_IMG = "office-team.jpg"


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def img(name):
    """解析图片槽位到真实存在的文件"""
    for cand in FALLBACK.get(name, [name]):
        if os.path.exists(os.path.join(SHARED_IMG, cand)):
            return cand
    return DEFAULT_IMG


# ------------------------------------------------------------------ 公共组件
def nav(tpl, active):
    links = [("index.html", "首页"), ("about.html", "关于我们"), ("business.html", "业务矩阵"),
             ("cases.html", "客户案例"), ("insights.html", "市场动态"), ("contact.html", "联系我们")]
    items = "".join(
        '<a href="/%s"%s>%s</a>' % (h, ' class="on"' if h == active else "", t)
        for h, t in links)
    b = esc(tpl["brand"])
    return """<nav class="nav" id="nav">
  <div class="wrap nav-in">
    <a href="/" class="logo"><span class="logo-mark">%s</span><span>%s<small>%s</small></span></a>
    <div class="nav-links" id="navlinks">%s</div>
    <a href="/contact.html" class="nav-cta">获取方案</a>
    <button class="nav-toggle" id="navtoggle" aria-label="菜单">☰</button>
  </div>
</nav>""" % (b[0], b, esc(tpl["brand_en"]), items)


def footer(tpl):
    b = esc(tpl["brand"])
    cols = ""
    for title, items in tpl["footer"]["cols"]:
        lis = "".join("<li>%s</li>" % esc(x) for x in items)
        cols += '<div class="f-col"><h4>%s</h4><ul>%s</ul></div>' % (esc(title), lis)
    return """<footer class="foot">
  <div class="wrap">
    <div class="foot-top">
      <div class="f-brand">
        <div class="logo"><span class="logo-mark">%s</span><span>%s<small>%s</small></span></div>
        <p>%s</p>
        <div class="f-contact"><span>%s</span><span>%s</span></div>
      </div>
      %s
    </div>
    <div class="foot-bot">
      <span>© 2026 %s. All rights reserved.</span>
      <span>%s · %s</span>
    </div>
  </div>
</footer>
<script src="/assets/app.js"></script>""" % (
        b[0], b, esc(tpl["brand_en"]), esc(tpl["footer"]["desc"]),
        esc(tpl["contact"]["phone"]), esc(tpl["contact"]["email"]),
        cols, b, esc(tpl["industry"]), DOMAIN)


THEME_KEYS = ("accent", "accent_deep", "accent_soft", "bg", "bg2", "ink", "ink2", "muted", "line")


def theme_css(tpl):
    t = tpl["theme"]
    parts = []
    for k in THEME_KEYS:
        if k in t:
            parts.append("--%s:%s" % (k.replace("_", "-"), t[k]))
    return ":root{%s}" % ";".join(parts)


def page(tpl, title, desc, body, active=""):
    accent = tpl["theme"]["accent"]
    return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%s</title>
<meta name="description" content="%s">
<meta property="og:title" content="%s">
<meta property="og:description" content="%s">
<meta property="og:type" content="website">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="stylesheet" href="/assets/style.css">
<style>%s</style>
</head>
<body class="v-%s">
%s
%s
%s
</body>
</html>
""" % (esc(title), esc(desc), esc(title), esc(desc), theme_css(tpl), tpl["variant"],
       nav(tpl, active), body, footer(tpl))


def page_hero(tpl, tag, title, lead):
    return """<header class="phero">
  <div class="wrap">
    <div class="eyebrow">%s</div>
    <h1>%s</h1>
    <p class="phero-lead">%s</p>
  </div>
</header>""" % (esc(tag), title, esc(lead))


def cta_band(tpl):
    return """<section class="cta-band">
  <div class="wrap cta-in">
    <div>
      <h2>准备好把想法做成产品？</h2>
      <p>把需求发给我们，1 个工作日内给出可行性评估与报价区间。</p>
    </div>
    <a href="/contact.html" class="btn btn-primary">预约沟通 <span>→</span></a>
  </div>
</section>"""


# ------------------------------------------------------------------ 页面
def render_index(tpl):
    h = tpl["hero"]
    metrics = "".join("<div><b>%s</b><span>%s</span></div>" % (esc(m[0]), esc(m[1])) for m in h["metrics"])
    marquee = "".join("<span>%s</span>" % esc(x) if x != "·" else "<span>·</span>" for x in tpl["marquee"])
    marquee = marquee + marquee

    biz = ""
    for b in tpl["business"]:
        pts = "".join("<li>%s</li>" % esc(p) for p in b["points"])
        biz += """<article class="biz rv">
  <div class="biz-img"><img src="/assets/img/%s" alt="%s" loading="lazy"></div>
  <div class="biz-body">
    <div class="biz-no">%s</div>
    <h3>%s</h3>
    <p>%s</p>
    <ul>%s</ul>
    <a href="/business.html#%s" class="biz-link">了解详情 →</a>
  </div>
</article>""" % (img(b["image"]), esc(b["image_alt"]), esc(b["no"]), esc(b["title"]),
                 esc(b["desc"]), pts, esc(b["anchor"]))

    steps = ""
    for s in tpl["process"]:
        steps += """<div class="step rv"><div class="step-no">%s</div><h4>%s</h4><p>%s</p></div>""" % (
            esc(s["no"]), esc(s["title"]), esc(s["desc"]))

    stats = "".join("""<div class="stat rv"><b>%s</b><span>%s</span><small>%s</small></div>""" % (
        esc(x[0]), esc(x[1]), esc(x[2] if len(x) > 2 else "")) for x in tpl["stats"])

    cases = ""
    for c in tpl["cases"][:3]:
        cases += """<article class="case-card rv">
  <div class="case-img"><img src="/assets/img/%s" alt="%s" loading="lazy"></div>
  <div class="case-body">
    <div class="case-meta">%s · %s</div>
    <h3>%s</h3>
    <p class="case-result">%s</p>
    <blockquote>%s</blockquote>
    <div class="case-person">%s</div>
  </div>
</article>""" % (img(c["image"]), esc(c["client"]), esc(c["sector"]), esc(c["scope"]),
                 esc(c["client"]), esc(c["result"]), esc(c["quote"]), esc(c["person"]))

    arts = ""
    for a in tpl["insights"]:
        arts += """<a class="art-card rv" href="/insights/%s.html">
  <div class="art-img"><img src="/assets/img/%s" alt="%s" loading="lazy"></div>
  <div class="art-body">
    <div class="art-meta"><span>%s</span><span>%s · %s</span></div>
    <h3>%s</h3>
    <p>%s</p>
    <span class="art-more">阅读全文 →</span>
  </div>
</a>""" % (esc(a["slug"]), img(a["image"]), esc(a["title"]), esc(a["tag"]),
           esc(a["date"]), esc(a["read"]), esc(a["title"]), esc(a["excerpt"]))

    body = """<header class="hero">
  <div class="wrap">
    <div class="hero-grid">
      <div class="hero-copy">
        <div class="eyebrow">%s</div>
        <h1>%s<br><em>%s</em></h1>
        <p class="hero-lead">%s</p>
        <div class="hero-actions">
          <a href="/business.html" class="btn btn-primary">%s <span>→</span></a>
          <a href="/contact.html" class="btn btn-ghost">%s</a>
        </div>
        <div class="hero-meta">%s</div>
      </div>
      <div class="hero-figure rv">
        <img src="/assets/img/%s" alt="%s">
        <div class="hero-badge"><b>%s</b><span>%s</span></div>
      </div>
    </div>
  </div>
</header>
<div class="marquee"><div>%s</div></div>
<section id="business">
  <div class="wrap">
    <div class="sec-head rv">
      <div class="sec-tag">%s</div>
      <h2>%s</h2>
      <p>%s</p>
    </div>
    <div class="biz-grid">%s</div>
  </div>
</section>
<section class="process-sec">
  <div class="wrap">
    <div class="sec-head rv"><div class="sec-tag">Process</div><h2>从需求到出货<br>四个节点</h2></div>
    <div class="process">%s</div>
  </div>
</section>
<section class="stats-sec"><div class="wrap"><div class="stats">%s</div></div></section>
<section class="cases-sec">
  <div class="wrap">
    <div class="sec-head rv"><div class="sec-tag">Cases</div><h2>他们在做的生意</h2>
      <p>不同品类、不同阶段的客户，共用同一套开发与品控流程。</p></div>
    <div class="case-grid">%s</div>
    <div class="more-row"><a href="/cases.html" class="btn btn-ghost">查看全部案例</a></div>
  </div>
</section>
<section class="insights-sec">
  <div class="wrap">
    <div class="sec-head rv"><div class="sec-tag">Insights</div><h2>行业观察</h2></div>
    <div class="art-grid">%s</div>
  </div>
</section>
%s""" % (esc(h["eyebrow"]), esc(h["title_l1"]), esc(h["title_em"]), esc(h["lead"]),
           esc(h["cta1"]), esc(h["cta2"]), metrics, img(h["image"]), esc(h["image_alt"]),
           esc(h["badge_t"]), esc(h["badge_s"]), marquee,
           esc(tpl["intro"]["tag"]), tpl["intro"]["title"], esc(tpl["intro"]["lead"]),
           biz, steps, stats, cases, arts, cta_band(tpl))
    return page(tpl, tpl["seo"]["title"], tpl["seo"]["desc"], body, "index.html")


def render_about(tpl):
    a = tpl["about"]
    story = "".join("<p>%s</p>" % esc(p) for p in a["story"])
    values = "".join("""<div class="val rv"><h4>%s</h4><p>%s</p></div>""" % (
        esc(v["title"]), esc(v["desc"])) for v in a["values"])
    tl = "".join("""<div class="tl-item rv"><div class="tl-year">%s</div>
<div class="tl-body"><h4>%s</h4><p>%s</p></div></div>""" % (
        esc(t["year"]), esc(t["title"]), esc(t["desc"])) for t in a["timeline"])
    certs = "".join("<span>%s</span>" % esc(c) for c in a["certs"])
    body = """%s
<section class="sec">
  <div class="wrap narrow">
    <div class="story">%s</div>
  </div>
</section>
<section class="sec alt">
  <div class="wrap">
    <div class="sec-head rv"><div class="sec-tag">Values</div><h2>我们坚持的四件事</h2></div>
    <div class="val-grid">%s</div>
  </div>
</section>
<section class="sec">
  <div class="wrap">
    <div class="sec-head rv"><div class="sec-tag">Milestones</div><h2>发展历程</h2></div>
    <div class="timeline">%s</div>
  </div>
</section>
<section class="sec alt">
  <div class="wrap narrow center">
    <div class="sec-tag">Certification</div>
    <h2 class="h2s">资质与认证</h2>
    <div class="certs">%s</div>
  </div>
</section>
%s""" % (page_hero(tpl, "About Us", a["title"], a["lead"]), story, values, tl, certs, cta_band(tpl))
    return page(tpl, "%s — 关于我们" % tpl["brand"], a["lead"], body, "about.html")


def render_business(tpl):
    blocks = ""
    for i, b in enumerate(tpl["business"]):
        pts = "".join("<li>%s</li>" % esc(p) for p in b["points"])
        rev = " rev" if i % 2 else ""
        blocks += """<article class="biz-block%s" id="%s">
  <div class="bb-media rv"><img src="/assets/img/%s" alt="%s" loading="lazy"></div>
  <div class="bb-text rv">
    <div class="biz-no">%s</div>
    <h2>%s</h2>
    <p>%s</p>
    <ul class="tick">%s</ul>
    <a href="/contact.html" class="btn btn-ghost btn-sm">咨询该项服务</a>
  </div>
</article>""" % (rev, esc(b["anchor"]), img(b["image"]), esc(b["image_alt"]),
                 esc(b["no"]), esc(b["title"]), esc(b["desc"]), pts)
    steps = "".join("""<div class="step rv"><div class="step-no">%s</div><h4>%s</h4><p>%s</p></div>""" % (
        esc(s["no"]), esc(s["title"]), esc(s["desc"])) for s in tpl["process"])
    body = """%s
<section class="sec">
  <div class="wrap">%s</div>
</section>
<section class="sec alt">
  <div class="wrap">
    <div class="sec-head rv"><div class="sec-tag">Process</div><h2>服务流程</h2></div>
    <div class="process">%s</div>
  </div>
</section>
%s""" % (page_hero(tpl, "Business", "六大业务板块<br>覆盖从开发到交付的全链路",
                    "每一项都可以单独合作，也可以打包成完整方案。"), blocks, steps, cta_band(tpl))
    return page(tpl, "%s — 业务矩阵" % tpl["brand"], "业务矩阵与服务范围", body, "business.html")


def render_cases(tpl):
    cards = ""
    for c in tpl["cases"]:
        cards += """<article class="case-card rv">
  <div class="case-img"><img src="/assets/img/%s" alt="%s" loading="lazy"></div>
  <div class="case-body">
    <div class="case-meta">%s · %s</div>
    <h3>%s</h3>
    <p class="case-result">%s</p>
    <blockquote>%s</blockquote>
    <div class="case-person">%s</div>
  </div>
</article>""" % (img(c["image"]), esc(c["client"]), esc(c["sector"]), esc(c["scope"]),
                 esc(c["client"]), esc(c["result"]), esc(c["quote"]), esc(c["person"]))
    stats = "".join("""<div class="stat rv"><b>%s</b><span>%s</span><small>%s</small></div>""" % (
        esc(x[0]), esc(x[1]), esc(x[2] if len(x) > 2 else "")) for x in tpl["stats"])
    body = """%s
<section class="sec"><div class="wrap"><div class="case-grid">%s</div></div></section>
<section class="stats-sec"><div class="wrap"><div class="stats">%s</div></div></section>
%s""" % (page_hero(tpl, "Cases", "合作案例", "以下是部分可公开的客户项目，细节已做脱敏处理。"),
         cards, stats, cta_band(tpl))
    return page(tpl, "%s — 客户案例" % tpl["brand"], "客户合作案例", body, "cases.html")


def render_insights(tpl):
    arts = ""
    for a in tpl["insights"]:
        arts += """<a class="art-card rv" href="/insights/%s.html">
  <div class="art-img"><img src="/assets/img/%s" alt="%s" loading="lazy"></div>
  <div class="art-body">
    <div class="art-meta"><span>%s</span><span>%s · %s</span></div>
    <h3>%s</h3>
    <p>%s</p>
    <span class="art-more">阅读全文 →</span>
  </div>
</a>""" % (esc(a["slug"]), img(a["image"]), esc(a["title"]), esc(a["tag"]),
           esc(a["date"]), esc(a["read"]), esc(a["title"]), esc(a["excerpt"]))
    body = """%s
<section class="sec"><div class="wrap"><div class="art-grid">%s</div></div></section>
%s""" % (page_hero(tpl, "Insights", "市场动态与行业观察",
                  "来自研发、生产与合规一线的经验，写给正在做产品决策的人。"), arts, cta_band(tpl))
    return page(tpl, "%s — 市场动态" % tpl["brand"], "行业观察与经验分享", body, "insights.html")


def render_article(tpl, a, idx):
    secs = ""
    for h2, paras in a["body"]:
        ps = "".join("<p>%s</p>" % esc(p) for p in paras)
        secs += "<h2>%s</h2>%s" % (esc(h2), ps)
    others = [x for x in tpl["insights"] if x["slug"] != a["slug"]][:2]
    rel = "".join("""<a class="rel-item" href="/insights/%s.html">
  <span class="rel-tag">%s</span><h4>%s</h4><span class="rel-date">%s</span>
</a>""" % (esc(o["slug"]), esc(o["tag"]), esc(o["title"]), esc(o["date"])) for o in others)
    body = """<header class="article-head">
  <div class="wrap narrow">
    <div class="crumb"><a href="/insights.html">市场动态</a> / <span>%s</span></div>
    <h1>%s</h1>
    <div class="art-meta"><span>%s</span><span>%s</span><span>约 %s</span></div>
  </div>
</header>
<article class="article-body">
  <div class="wrap narrow">
    <img class="article-cover" src="/assets/img/%s" alt="%s">
    <p class="article-lead">%s</p>
    %s
    <div class="article-foot">
      <span>本文由 %s 技术团队撰写</span>
      <a href="/contact.html" class="btn btn-ghost btn-sm">就这个话题聊聊</a>
    </div>
  </div>
</article>
<section class="sec alt">
  <div class="wrap narrow">
    <div class="sec-tag">More</div><h2 class="h2s">继续阅读</h2>
    <div class="rel-grid">%s</div>
  </div>
</section>""" % (esc(a["tag"]), esc(a["title"]), esc(a["date"]), esc(a["tag"]), esc(a["read"]),
                 img(a["image"]), esc(a["title"]), esc(a["excerpt"]), secs,
                 esc(tpl["brand"]), rel)
    return page(tpl, "%s — %s" % (a["title"], tpl["brand"]), a["excerpt"], body, "insights.html")


def render_contact(tpl):
    c = tpl["contact"]
    body = """%s
<section class="sec">
  <div class="wrap contact-grid">
    <div class="contact-info">
      <div class="ci-item"><h4>办公地址</h4><p>%s</p></div>
      <div class="ci-item"><h4>生产基地</h4><p>%s</p></div>
      <div class="ci-item"><h4>电话</h4><p>%s</p></div>
      <div class="ci-item"><h4>邮箱</h4><p>%s</p></div>
      <div class="ci-item"><h4>工作时间</h4><p>%s</p></div>
      <div class="ci-note">%s</div>
    </div>
    <form class="contact-form" onsubmit="return hkbtForm(event)">
      <h3>留下需求，我们回电</h3>
      <div class="row2">
        <label>称呼<input type="text" name="name" placeholder="您的姓名" required></label>
        <label>联系方式<input type="text" name="phone" placeholder="手机 / 微信" required></label>
      </div>
      <label>公司名称<input type="text" name="company" placeholder="公司或品牌名称"></label>
      <label>需求方向
        <select name="topic">%s</select>
      </label>
      <label>需求描述<textarea name="msg" rows="5" placeholder="产品品类、预计数量、期望交期"></textarea></label>
      <button type="submit" class="btn btn-primary">提交需求</button>
      <p class="form-hint">演示站点：表单不会真实提交，正式部署后请接入你的表单服务。</p>
    </form>
  </div>
</section>""" % (page_hero(tpl, "Contact", "聊聊你的下一个产品",
                         "无论是打样、报价还是合规问题，都可以直接找到对应的人。"),
                 esc(c["address"]), esc(c["factory"]), esc(c["phone"]), esc(c["email"]),
                 esc(c["hours"]), esc(c["note"]),
                 "".join("<option>%s</option>" % esc(b["title"]) for b in tpl["business"]))
    return page(tpl, "%s — 联系我们" % tpl["brand"], "联系方式与需求提交", body, "contact.html")


def render_tracking(tpl):
    t = tpl["tracking"]
    steps = "".join('<div class="tr-step"><span>%s</span><i>%d</i></div>' % (esc(s), i + 1)
                    for i, s in enumerate(t["steps"]))
    carriers = "".join("<tr><td>%s</td><td>全程可查</td></tr>" % esc(c) for c in t["carriers"])
    body = """%s
<section class="sec">
  <div class="wrap narrow">
    <div class="track-box">
      <label>单号查询</label>
      <div class="track-row">
        <input type="text" id="trInput" placeholder="例如 %s" value="%s">
        <button class="btn btn-primary" onclick="hkbtTrack()">查询</button>
      </div>
      <div class="track-result" id="trResult"></div>
      <div class="tr-steps">%s</div>
    </div>
    <h2 class="h2s" style="margin-top:56px">合作承运商</h2>
    <table class="tbl"><thead><tr><th>承运商</th><th>查询支持</th></tr></thead>
    <tbody>%s</tbody></table>
    <p class="note">演示站点：查询结果为模拟数据，正式部署后请接入你的物流接口。</p>
  </div>
</section>""" % (page_hero(tpl, "Tracking", "订单与物流查询",
                         t["lead"]), esc(t["sample"]), esc(t["sample"]), steps, carriers)
    return page(tpl, "%s — 物流查询" % tpl["brand"], "订单进度与物流状态查询", body, "")


def render_downloads(tpl):
    cards = ""
    for d in tpl["downloads"]:
        cards += """<a class="dl-card rv" href="/assets/docs/%s" download>
  <div class="dl-ico">PDF</div>
  <div class="dl-body"><h4>%s</h4><p>%s</p><span>%s</span></div>
  <div class="dl-arrow">↓</div>
</a>""" % (esc(d["file"]), esc(d["name"]), esc(d["desc"]), esc(d["size"]))
    body = """%s
<section class="sec"><div class="wrap"><div class="dl-grid">%s</div>
<p class="note">以上文件为示例单据，正式使用时请替换为贵司版本。</p></div></section>""" % (
        page_hero(tpl, "Downloads", "单据与资料下载", "常用文件集中在这里，点击即可下载。"), cards)
    return page(tpl, "%s — 资料下载" % tpl["brand"], "服务单据与资料下载", body, "")


def render_terms(tpl):
    blocks = ""
    for t in tpl["terms"]:
        lis = "".join("<li>%s</li>" % esc(x) for x in t["items"])
        blocks += """<section class="term-block"><h3>%s</h3><ul>%s</ul></section>""" % (
            esc(t["title"]), lis)
    body = """%s
<section class="sec"><div class="wrap narrow"><div class="terms">%s</div>
<p class="note">本文档为通用条款示例，具体合作以双方签署的合同为准。</p></div></section>""" % (
        page_hero(tpl, "Terms", "合作条款", "以下为常规合作条款，可按项目实际情况调整。"), blocks)
    return page(tpl, "%s — 合作条款" % tpl["brand"], "合作条款与交付约定", body, "")


def render_404(tpl):
    body = """<section class="err">
  <div class="wrap center">
    <div class="err-code">404</div>
    <h1>这个页面不存在</h1>
    <p>链接可能已失效，或者地址输错了。</p>
    <div class="hero-actions" style="justify-content:center">
      <a href="/" class="btn btn-primary">回到首页</a>
      <a href="/contact.html" class="btn btn-ghost">联系我们</a>
    </div>
  </div>
</section>"""
    return page(tpl, "页面不存在 — %s" % tpl["brand"], "404", body, "")


# ------------------------------------------------------------------ 组装
FAVICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<rect width="64" height="64" rx="6" fill="%s"/>
<text x="32" y="44" font-family="Helvetica,Arial,sans-serif" font-size="36" font-weight="700"
 fill="#fff" text-anchor="middle">%s</text></svg>"""


def build_one(tpl):
    key = tpl["key"]
    out = os.path.join(OUT_ROOT, key)
    if os.path.exists(out):
        shutil.rmtree(out)
    os.makedirs(os.path.join(out, "assets", "img"), exist_ok=True)
    os.makedirs(os.path.join(out, "assets", "docs"), exist_ok=True)
    os.makedirs(os.path.join(out, "insights"), exist_ok=True)

    pages = {
        "index.html": render_index(tpl),
        "about.html": render_about(tpl),
        "business.html": render_business(tpl),
        "cases.html": render_cases(tpl),
        "insights.html": render_insights(tpl),
        "contact.html": render_contact(tpl),
        "tracking.html": render_tracking(tpl),
        "downloads.html": render_downloads(tpl),
        "terms.html": render_terms(tpl),
        "404.html": render_404(tpl),
    }
    for i, a in enumerate(tpl["insights"]):
        pages["insights/%s.html" % a["slug"]] = render_article(tpl, a, i)

    for name, html in pages.items():
        p = os.path.join(out, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(html)

    # CSS / JS：base + 变体合并为单文件
    with open(os.path.join(CSS_DIR, "base.css"), encoding="utf-8") as f:
        css = f.read()
    with open(os.path.join(CSS_DIR, "%s.css" % tpl["variant"]), encoding="utf-8") as f:
        css += "\n" + f.read()
    with open(os.path.join(out, "assets", "style.css"), "w", encoding="utf-8") as f:
        f.write(css)
    shutil.copy2(os.path.join(CSS_DIR, "app.js"), os.path.join(out, "assets", "app.js"))

    # 图片：只拷贝引用到的
    used = set()
    for b in tpl["business"]:
        used.add(img(b["image"]))
    for c in tpl["cases"]:
        used.add(img(c["image"]))
    for a in tpl["insights"]:
        used.add(img(a["image"]))
    used.add(img(tpl["hero"]["image"]))
    for name in used:
        src = os.path.join(SHARED_IMG, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(out, "assets", "img", name))

    # PDF
    for d in tpl["downloads"]:
        src = os.path.join(SHARED_DOC, d["file"])
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(out, "assets", "docs", d["file"]))

    # favicon / robots
    b = tpl["brand"]
    with open(os.path.join(out, "favicon.svg"), "w", encoding="utf-8") as f:
        f.write(FAVICON % (tpl["theme"]["accent"], esc(b[0])))
    with open(os.path.join(out, "robots.txt"), "w", encoding="utf-8") as f:
        f.write("User-agent: *\nAllow: /\nSitemap: https://%s/sitemap.xml\n" % DOMAIN)

    meta = {
        "key": key, "name": tpl["name"], "industry": tpl["industry"],
        "variant": tpl["variant"], "brand": tpl["brand"], "brand_en": tpl["brand_en"],
        "default_brand": tpl["brand"], "default_domain": DOMAIN,
        "pages": len(pages), "title": tpl["seo"]["title"],
    }
    with open(os.path.join(out, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    return key, len(pages), len(used)


def make_trade():
    """把原始 templates/site 注册为通用商贸模板 trade（默认选项，保持向后兼容）"""
    src = os.path.join(ROOT, "templates", "site")
    out = os.path.join(OUT_ROOT, "trade")
    if not os.path.isdir(src):
        return None
    if os.path.isdir(out):
        shutil.rmtree(out)
    shutil.copytree(src, out)
    meta = {
        "key": "trade", "name": "通用商贸", "industry": "国际贸易与供应链服务",
        "variant": "editorial", "brand": "HKBT", "brand_en": "International Trading",
        "default_brand": "HKBT", "default_domain": DOMAIN,
        "pages": 13, "title": "HKBT — 国际商贸与全球供应链服务商",
    }
    with open(os.path.join(out, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    return "trade"


def main():
    os.makedirs(OUT_ROOT, exist_ok=True)
    total = 0
    for tpl in ALL:
        key, npages, nimg = build_one(tpl)
        total += 1
        print("  %-14s %2d 页  %d 图  [%s]" % (key, npages, nimg, tpl["variant"]))
    if make_trade():
        total += 1
        print("  %-14s 13 页  %d 图  [editorial]  (原始模板)" % ("trade", 20))
    print("\n共生成 %d 套站点 → %s" % (total, OUT_ROOT))


if __name__ == "__main__":
    main()
