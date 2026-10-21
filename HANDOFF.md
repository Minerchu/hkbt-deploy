# 交接文档

> 本文件面向接手维护的人（或 agent）。当前状态：**功能完成，已推送并做过真实 Nginx 端到端验证**。

## 1. 项目是什么

HKBT Deploy 把「V2bX / sing-box 节点」和「网站」部署在同一个端口：
Nginx 终结 TLS，按 `Upgrade` 头把 WebSocket 流量转发给 sing-box，普通浏览器请求返回站点。
面板（Flask，127.0.0.1:8899）负责部署、自检与回滚。

核心约束（踩过的坑，已固化在 `app.py`）：

- sing-box 没有 Xray 的 `fallbacks`，不能做路径回落，必须由 Nginx 分流
- **对外端口必须 ≠ 代理端口**。同一 `IP:端口` 只能一个进程监听，`0.0.0.0:x` 与 `127.0.0.1:x` 也互斥。填成一样会让 Nginx 绑不上，旧配置继续跑但面板地址已改，表现为整站和面板一起打不开
- 代理在独立端口时，Nginx 绑 `0.0.0.0`（NAT 机器上公网 IP 不在网卡，绑它会直接失败）；同端口场景才需要绑具体地址
- 证书复用 V2bX 那份（`/etc/V2bX/fullchain.cer` + `cert.key`）
- `http2 on;` 需要 Nginx ≥ 1.25.1，旧版退回 `listen ... ssl http2;`
- 远端默认分支是 **master**，不是 main

更细的实战排查（9 个真实故障，含验证命令）见 [TROUBLESHOOTING.md](TROUBLESHOOTING.md)。

## 2. 当前状态

| 项 | 状态 |
|---|---|
| 11 套站点模板（13 页/套，共 143 页） | ✅ 已生成，自检全过 |
| 三套视觉变体（editorial / classic / tech） | ✅ |
| `app.py` 模板注册与选择 | ✅ |
| `panel.html` 模板下拉 | ✅ |
| `install.sh` 拷贝 `templates/sites` | ✅ |
| README 模板清单 | ✅ |
| 推送 GitHub | ✅ `master` |
| 真实 Nginx 安装验证 | ✅ 见第 9 节 |

## 3. 架构

```
build/data_a|b|c.py      行业数据（唯一内容源）
        ↓
build/build_sites.py     渲染器：统一 HTML 结构 + 按 variant 合并 CSS
        ↓
templates/sites/<key>/   自包含站点：13 页 + assets/{style.css,app.js,img,docs} + meta.json
        ↓
app.py generate_default_site()   按 meta.default_brand 替换品牌、hkbt.example 替换域名
```

**关键设计**：三套视觉变体共用一份 HTML 结构，差异全部在 CSS。
`build/css/base.css` 负责布局，`<variant>.css` 只写变量与覆盖规则，生成时拼接成单文件。

## 4. 数据 schema

每套模板是一个 dict，字段如下（`build/data_a.py` 的 `cosmetic` 是完整参考）：

| 字段 | 说明 |
|---|---|
| `key` / `name` / `industry` | 目录名 / 中文名 / 行业描述 |
| `brand` / `brand_en` | 行业默认品牌（部署时被替换） |
| `variant` | `editorial` / `classic` / `tech` |
| `theme` | accent / bg / ink / line 等颜色，注入 `:root` |
| `hero` | eyebrow、标题两行、lead、两个 CTA、4 项指标、主图 |
| `marquee` | 滚动关键词 |
| `intro` | 首页第二屏的标题与导语 |
| `business` | 6 项：no / anchor / title / desc / points(4) / image |
| `process` | 4 步流程 |
| `stats` | 4 项数据（可 2 或 3 元组，渲染器容错） |
| `about` | story(3 段) / values(4) / timeline(4) / certs |
| `cases` | 3 个案例：client / sector / scope / result / quote / person |
| `insights` | 3 篇文章：slug / title / date / tag / excerpt / body[(h2, [para])] |
| `contact` / `tracking` / `downloads` / `terms` / `footer` | 对应页面内容 |

## 5. 图片策略

没有外部图床依赖。`build/shared/img/` 放 20 张实拍图，数据里按语义引用；
缺失的槽位由 `build_sites.py` 的 `FALLBACK` 表回落到相近图，保证不出现 404。

若要增加行业专属配图，把图片放进 `build/shared/img/`，在数据里写文件名即可。

## 6. 测试

```bash
python3 build/build_sites.py      # 重新生成 11 套
python3 build/test_sites.py       # 每套独立起服务：13 页 200、内链 200、无渲染残留
python3 build/test_deploy.py      # 模板注册、选择、品牌/域名替换无残留、非法 key 回落
```

`test_sites.py --serve <key>` 可单独起预览服务（http://127.0.0.1:8801/）。

注意：站点用绝对路径 `/assets/...`，这是部署到域名根目录的正确写法。
本地预览多套时不能放在同一服务的子目录下，必须按套单独起服务（测试脚本已处理）。

## 7. 凭证与推送

- 仓库：`minerchu/hkbt-deploy`，默认分支 **master**
- Token **不写进任何文件**。GitHub 机密扫描会 422 拦截含 token 的推送（曾因此失败一次）
- 本地可放在 `/workspace/.gh_token`（600 权限），并确保 `.gitignore` 覆盖
- 推送后验证：
  ```bash
  curl -4 -sI https://raw.githubusercontent.com/minerchu/hkbt-deploy/master/build/data_c.py
  ```

## 8. 已知限制

- 只支持 V2bX + sing-box；未检测到会跳过站点部署并提示先装 V2bX
- 面板表单为演示，不接后端（contact 页）
- 模板中的公司名、案例、数据均为示例内容，正式对外前应替换
- `templates/site/`（原始单模板）保留兼容，`app.py` 优先使用 `templates/sites/`
- `narrow_v2bx()` 写的 systemd dropin 只处理 `"ListenIP": "::"` 这一种写法；
  若 V2bX 重启后改回 `0.0.0.0`，dropin 兜不住，需要手工检查
- 收窄监听会 `systemctl restart V2bX`，代理会短暂中断（配置已备份，可用面板回滚）

## 9. 安装链路验证记录

在沙箱里用**真实 Nginx 1.24 + 模拟 V2bX 配置**跑通了完整 `install.sh`（systemd/crontab 用桩替代）：

| 项 | 结果 |
|---|---|
| 配置语法 `nginx -t` | ✅ 通过 |
| 端口绑定 | ✅ 绑 `公网IP:443`，未绑 `0.0.0.0` |
| 站点首页 | ✅ 200 |
| 代理路径 `/index`（浏览器） | ✅ 200，返回站点首页 |
| 静态资源 css/img | ✅ 200 |
| 面板反向代理 | ✅ 200，可登录、读状态 |
| HTTP → HTTPS | ✅ 301 |
| 404 页 | ✅ 404 |
| 面板切换模板重新部署 | ✅ 14 项自检全 OK |
| 上传 zip 覆盖站点 | ✅ |
| 非 443 端口（8443/2053） | ✅ 语法通过，301 带端口 |

本次修掉的三个安装级 bug（都有回归断言）：

1. `cmd_init` 收尾用旧 `cfg` 覆盖磁盘配置 → 装完不打印面板地址、状态缺域名
2. `admin_path` 未先落盘就调 `do_deploy` → Nginx 配置缺面板段，面板必然 404
3. 模板 `meta.json` 被拷进站点根目录 → 线上可公开访问到模板元信息

### 复现验证的命令

```bash
# 造一个最小 V2bX 环境（域名 + 自签证书即可）
mkdir -p /etc/V2bX
cat > /etc/V2bX/config.json <<'EOF'
{"Nodes":[{"Core":"sing","NodeID":7,"NodeType":"vless","ListenIP":"0.0.0.0",
"CertConfig":{"CertDomain":"demo.example.com","CertFile":"/etc/V2bX/fullchain.cer","KeyFile":"/etc/V2bX/privkey.key"}}]}
EOF
openssl req -x509 -newkey rsa:2048 -nodes -days 90 -subj "/CN=demo.example.com" \
  -keyout /etc/V2bX/privkey.key -out /etc/V2bX/fullchain.cer

bash install.sh --path /index --port 443 --password test1234
curl -sk --resolve demo.example.com:443:<本机IP> https://demo.example.com/ | head
```
