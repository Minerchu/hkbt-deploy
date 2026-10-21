# HKBT Deploy

把「V2bX / sing-box 节点」与「网站」部署在同一端口同一路径的工具。

安装一次之后，你只需要在网页面板里填域名、填端口、上传 HTML 的 zip，剩下的全自动完成。
**面板、节点配置、订阅、客户端配置全程零改动。**

---

## 核心原则：网站端口 = 代理端口

**网站和代理永远共用同一个端口**，就是你在 `--port` 里填的那个。不存在「网站在 443、代理在别的端口」这种情况。

```
listen  <公网IP>:<端口> ssl;          ← 网站
proxy_pass https://127.0.0.1:<端口>;  ← 代理（同一个端口）
```

| 你填的端口 | 网站地址 | 代理地址 |
|---|---|---|
| 443 | `https://域名/` | `域名:443` 路径 `/index` |
| 12233 | `https://域名:12233/` | `域名:12233` 路径 `/index` |

只有 443 才能免端口访问；其他端口浏览器必须显式带上。

**改端口的顺序很重要**（错了会断网）：

1. 先在 XBoard 面板把节点端口改成新端口
2. 确认 V2bX 已拉到新配置（确实在监听新端口）
3. 再用工具填新端口部署

工具会自动比对面板实际端口与你填的值，不一致时给出警告。

## 安装前提

这三件事必须先具备，否则装完只会有面板、没有站点：

1. **VPS 已装好 V2bX**，且 `/etc/V2bX/config.json` 里有节点（工具靠它取域名、证书、端口）
2. **节点证书已签发**，`CertConfig.CertFile` / `KeyFile` 指向真实文件（工具直接复用，不自己签）
3. **域名已解析到这台机器**，且 443（或你用的端口）在防火墙/安全组放行

没有 V2bX 时脚本仍会装好面板，但会跳过站点部署并提示你先装 V2bX。

## 你要做的全部操作

```bash
bash install.sh            # 1. 安装（自动探测你的 V2bX）
```

然后打开它打印的面板地址，填两个字段，点部署：

| 字段 | 说明 |
|---|---|
| **端口** | vless ws tls 的监听端口，通常 443 |
| **代理路径** | 比如 `/index`，必须与面板节点完全一致 |
| **站点模板** | 11 套行业模板任选，默认「通用商贸」 |

**不需要上传任何文件** —— 工具内置了 11 套企业站点模板，会自动生成一个真实网站。

---

## 30 秒上手

```bash
# 在你的 VPS 上（root），一条命令装完
curl -fsSL https://raw.githubusercontent.com/Minerchu/hkbt-deploy/master/install.sh \
  | bash -s -- --path /index --port 443 --password 你的密码
```

也可以先拉下来再装：

```bash
git clone https://github.com/Minerchu/hkbt-deploy.git && cd hkbt-deploy && bash install.sh
```

安装脚本会自动完成：

1. 装 Nginx / Flask
2. 探测 V2bX 配置，并向你的面板 API 查询节点真实端口与路径（自动填进表单）
3. 把 sing-box 收窄到 `127.0.0.1`
4. 生成 Nginx 配置并接管公网端口
5. 启动面板服务、配置证书续期自动重载
6. 打印面板地址

非交互安装：`bash install.sh --password 你的密码`

---

## 自动生成的网站长什么样

内置 **11 套**站点模板，在面板里选一套即可，**不需要准备任何素材**：

| 模板 | 行业 | 视觉风格 | 默认品牌 |
|---|---|---|---|
| `trade` | 通用商贸 / 供应链 | 杂志风 | HKBT |
| `cosmetic` | 化妆品 OEM / ODM | 杂志风 | 妆研生物 |
| `textile` | 功能性面料与服装 | 杂志风 | 锦澜纺织 |
| `medical` | 医用耗材与康复器械 | 杂志风 | 康泽医疗 |
| `food` | 预制菜与复合调味 | 经典企业 | 鲜厨食品 |
| `logistics` | 跨境物流与海外仓 | 经典企业 | 环球跨境物流 |
| `furniture` | 实木家具与工程配套 | 经典企业 | 木言家居 |
| `auto` | 汽车精密零部件 | 现代科技 | 驰盈汽配 |
| `chemical` | 特种化工与新材料 | 现代科技 | 聚川新材 |
| `electronics` | PCBA 与消费电子 | 现代科技 | 锐联电子 |
| `machinery` | 数控装备与非标自动化 | 现代科技 | 恒重机械 |

每套模板都是 **13 个页面**：

```
首页 / 关于我们 / 业务矩阵 / 客户案例 / 市场动态 / 联系我们
物流查询 / 单据下载 / 合作条款 / 404
市场动态下的 3 篇完整文章
```

内容不是占位符——每套都写了该行业的真实业务板块、案例、技术文章与合作条款，
配图 6–20 张本地托管，4 份 PDF 单据可直接下载。

**品牌与域名会自动替换**：模板里的行业默认品牌（如「妆研生物」）和你填的网站名称互换，
`hkbt.example` 换成你的域名，页面里不会留下任何旧品牌。

三种视觉风格共用同一套页面结构，差异在排版与配色：

- **杂志风**：衬线标题、奶油纸感、细边框、留白克制
- **经典企业**：无衬线、白底、胶囊按钮、卡片阴影
- **现代科技**：直角、网格背景、等宽标签、高对比强调色

想换成自己的站点？在面板上传 zip 即可覆盖（需包含 `index.html`，若有一层顶层目录会自动提升）。

## 面板能做什么

| 功能 | 说明 |
|---|---|
| 状态总览 | 域名 / 端口 / 路径 / 公网 IP / 核心类型 / 证书到期 / 监听状态，实时读取 |
| 部署站点 | 填域名、端口、代理路径，选模板或上传 zip，一键部署 |
| 运行自检 | 6 项检查：首页、路径（浏览器）、路径（WebSocket）、证书、监听收敛、Nginx 接管 |
| 回滚 | 一键恢复到上一次部署前的状态 |
| 冲突清理 | 自动禁用占用同端口的其他 Nginx 配置 |

上传的 zip 如果只有一层顶层目录（GitHub 下载的包常见），会自动提升为站点根目录。

---

## 原理（这次踩的坑，全部固化在代码里）

sing-box **没有 Xray 的 `fallbacks`**，所以网上那套「加回落规则」的做法无效。工具改用：

```
公网 <IP>:<端口>  →  Nginx 终结 TLS
                     ├─ 带 Upgrade: websocket  →  转发 sing-box(127.0.0.1:<端口>)
                     └─ 普通浏览器访问          →  返回网站
```

四个关键点：

1. **sing-box 收窄到 `127.0.0.1`**，公网端口交给 Nginx
2. **Nginx 必须绑公网 IP，不能绑 `0.0.0.0`** —— 否则与 `127.0.0.1:<端口>` 冲突，报 `EADDRINUSE`
3. **证书直接复用 V2bX 的那份**（`/etc/V2bX/fullchain.cer` + `cert.key`），工具加了每 6 小时的续期检测，证书更新后自动 reload Nginx
4. **`http2 on;` 需要 Nginx ≥ 1.25.1**，工具会检测版本，旧版自动退回 `listen ... ssl http2;`

---

## 文件说明

```
app.py                    主程序（CLI 初始化 + Flask 服务 + 部署逻辑）
install.sh                一键安装脚本
templates/panel.html      面板界面
templates/sites/<key>/    11 套站点模板（每套 13 页 + 配图 + 单据 + meta.json）
templates/site/           原始单模板（保留兼容，部署时优先用 templates/sites）

build/data_a.py           模板数据：化妆品 / 汽车零部件 / 化工
build/data_b.py           模板数据：食品 / 物流 / 电子
build/data_c.py           模板数据：纺织 / 家居 / 机械 / 医疗
build/build_sites.py      站点生成器（数据 → templates/sites/<key>/）
build/css/base.css        通用布局
build/css/{editorial,classic,tech}.css   三套视觉变体
build/shared/img|docs     共享图库与 PDF 单据
build/test_sites.py       站点自检（13 页 200 / 无死链 / 资源可达）
build/test_deploy.py      部署链路自检（模板选择 + 品牌域名替换）
```

改模板内容只需要改 `build/data_*.py`，然后重新生成：

```bash
python3 build/build_sites.py     # 生成全部模板
python3 build/test_sites.py      # 自检
python3 build/test_deploy.py     # 验证替换逻辑
```

`templates/sites/<key>/meta.json` 记录该模板的 `default_brand`，
部署时 `app.py` 按它把行业默认品牌替换成你填的网站名称。

服务以 systemd 运行，只监听 `127.0.0.1:8899`，通过 Nginx 的隐藏路径反向代理暴露，
**不额外开放任何端口**。

```
systemctl status hkbt-deploy      # 服务状态
journalctl -u hkbt-deploy -f      # 服务日志
tail -f /var/log/hkbt-deploy.log  # 部署日志
```

---

## 安全

- 面板入口是安装时随机生成的路径（`/__deploy-xxxxxxxx`），不额外开端口
- 需要密码登录，密码用 PBKDF2-SHA256 加盐存储，不明文
- 所有操作留有备份（`/opt/hkbt-deploy/backups/`），支持一键回滚

想换入口路径，改 `/opt/hkbt-deploy/config.json` 里的 `admin_path` 后重新部署一次即可。

---

## 注意事项

- **目前只支持 V2bX + sing-box**。未检测到 V2bX 时会给出明确提示而不是盲操作。
- **端口和路径必须与面板节点完全一致**，工具会向面板 API 查询并比对，不一致时给出警告。
- 部署后**必须用客户端实测能否联网**。这是唯一无法自动验证的环节 —— 服务端握手能过，不代表实际代理流量正常。
- 站点数据（案例数字、联系方式等）请替换成真实信息后再对外使用。

## 故障排查

| 现象 | 处理 |
|---|---|
| 面板打不开 | `systemctl status hkbt-deploy`；确认访问地址带结尾的 `/` |
| 部署报「证书检查」失败 | 检查 `/etc/V2bX/fullchain.cer` 是否存在 |
| 部署报「Nginx 语法检查」失败 | 看失败详情，通常是端口被占；用「回滚」恢复 |
| 代理不通 | 确认填写的端口/路径与面板节点一致；用面板「运行自检」看 WebSocket 那一项 |

上面这张表只覆盖最常见的几种。**真机安装时踩过的 9 个坑**（端口冲突导致整站带面板一起挂、代理被收窄到回环、出站走 IPv6 导致"能连上但打不开"、NAT 机器取不到公网 IP 等）逐条记在 [TROUBLESHOOTING.md](TROUBLESHOOTING.md) 里，按症状索引，附验证命令。
