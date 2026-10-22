# HKBT Deploy

把 **V2bX / sing-box 节点**伪装成一台正经企业网站的一键部署工具。

装完之后，同一个域名、同一个端口，浏览器打开是一家有 13 个页面的公司官网，
代理客户端连上去是能正常出网的 vless 节点。面板里填几个字段，剩下的全自动。

```
https://你的域名/          →  企业官网（200，13 个页面）
https://你的域名/<路径>    →  代理握手（101）
```

---

## 目录

- [它解决什么问题](#它解决什么问题)
- [三种部署形态](#三种部署形态)
- [架构与原理](#架构与原理)
- [和 nginx + Trojan fallback 方案的区别](#和-nginx--trojan-fallback-方案的区别)
- [安装](#安装)
- [面板](#面板)
- [站点模板](#站点模板)
- [客户端配置](#客户端配置)
- [端口伪装](#端口伪装)
- [证书策略](#证书策略)
- [运维](#运维)
- [文件说明](#文件说明)
- [安全](#安全)
- [已知限制](#已知限制)
- [故障排查](#故障排查)

---

## 它解决什么问题

裸奔的代理节点有两个问题：

1. **端口上什么都没有**。扫描器连上 21311，握手成功，然后呢？没有 HTTP 响应，一眼就知道这不是网站。
2. **证书和协议特征对不上**。正经网站不会把 HTTPS 开在高位端口，也不会对任意路径都返回 404。

这套工具的做法是：**让 Nginx 接管公网端口，按请求特征分流**。

- 普通浏览器访问 → 返回一个真实的企业站点（11 套行业模板，13 个页面，配图、PDF 单据齐全）
- 带 `Upgrade: websocket` 且路径匹配 → 转发给背后的 sing-box

从外面看，这就是一台放着公司官网的普通 Web 服务器。

---

## 三种部署形态

工具会自动判断该用哪种，你不需要选。

### 形态 A：同端口（网站和代理都在 443）

sing-box 被收窄到 `127.0.0.1:443`，Nginx 绑公网 IP 的 443。

```
公网 0.0.0.0:443  →  Nginx
                       ├─ WS + 路径匹配  →  127.0.0.1:443 (sing-box)
                       └─ 其他           →  静态站点
```

适用：节点端口本来就是 443，且没有别的服务占用。
代价：sing-box 只能本机访问，客户端必须走 443。

### 形态 B：分离端口（推荐）

网站和面板在 443，代理留在它自己的端口（如 20000）。

```
公网 0.0.0.0:443   →  Nginx
                        ├─ WS + 路径匹配  →  127.0.0.1:20000 (sing-box)
                        └─ 其他           →  静态站点

公网 0.0.0.0:20000 →  sing-box 直连（同时也可被 NAT 重定向进 443）
```

适用：节点端口不是 443，或者你不想动现有节点配置。
好处：客户端填 20000 和 443 都能用，互不影响。

### 形态 C：NAT 机器

公网 IP 不在网卡上（阿里云、腾讯云的经典网络，或套了端口映射的机器）。

工具会通过外部服务查询公网 IP，绑定地址退回 `0.0.0.0`。
**不能绑公网 IP** —— 网卡上没有这个地址，`bind()` 会直接失败。

---

## 架构与原理

### 分流逻辑

```
                    ┌──────────────────────────────────────┐
   客户端 ──TLS──▶  │  Nginx  :443  (TLS 在这里终结)        │
                    │                                      │
                    │  路径匹配 + 有 Upgrade: websocket ?   │
                    │     ├─ 是 ──▶ proxy_pass 127.0.0.1:20000
                    │     └─ 否 ──▶ root /var/www/hkbt
                    └──────────────────────────────────────┘
                                      │
                                      ▼
                    ┌──────────────────────────────────────┐
                    │  V2bX / sing-box  :20000              │
                    │  vless + ws + tls                     │
                    └──────────────────────────────────────┘
```

### 端口与路径从哪来

V2bX 的 `/etc/V2bX/config.json` 里只有 `NodeID` 和 API 凭据，真正的 `server_port` 和 ws `path`
是它启动时从面板拉取的，**磁盘上没有副本**（`sing_origin.json` 的 `inbounds` 是 `null`，
由 V2bX 在内存里拼装）。所以本工具也从同一个来源读，不需要手工填写：

```
/etc/V2bX/config.json ──ApiHost/ApiKey/NodeID──▶ 面板 API
                                                    │
                          ┌─────────────────────────┘
                          ▼
              server_port + networkSettings.path
                          │
                          ▼
              Nginx location 与 proxy_pass
```

```bash
python3 app.py --probe     # 只探测，输出 JSON
```

```json
{"installed": true, "domain": "jpcdn.example.com", "port": "20000",
 "path": "/sdf=?kfj", "network": "ws", "tls": 1,
 "api_ok": true, "source": "panel"}
```

`source` 说明数据来源：`panel`（面板）、`listen`（进程监听表兜底端口）、
`config`（面板不可达，沿用上次部署记录的路径）。

**读不到 path 就不部署。** 早期版本会默认 `/index`，结果 Nginx 的 location 与节点不符，
代理流量掉进静态站点，表现为「握手能过、随即断开」—— 比直接报错难查得多。
现在改为中止并打印排查项。

字段兼容 V2board 与 Xboard 两套写法：`networkSettings` / `network_settings`、
`server_port` / `serverPort` / `listen_port`、`tls_settings` / `tlsSettings`，
以及 Xboard 部分分支的 `{"data": {...}}` 包装层。

> 在面板里改了 path 或端口之后，要重新部署一次，Nginx 配置才会跟着更新。

### 四条硬约束

这四条是实际装机器时踩出来的，代码里都有对应校验：

| 约束 | 违反后的表现 |
|---|---|
| **对外端口 ≠ 代理端口** | Nginx 绑不上，`Address already in use`，整站带面板一起挂 |
| **分离端口时绑 `0.0.0.0`，同端口时绑公网 IP** | NAT 机器上绑公网 IP 直接 bind 失败 |
| **路径要切掉 query 再匹配** | `location = /sdf=?kfj` 永远命中不了，代理全 404 |
| **`http2 on;` 需要 Nginx ≥ 1.25.1** | 旧版语法报错，工具自动退回 `listen ... ssl http2;` |

### 为什么要切 query

面板里的 path 可能是 `/sdf=?kfj`，同时含 `=` 和 `?`。这带来两个坑：

1. Nginx 的 `location` 只匹配解码后的 URI，`?` 之后属于 query，不参与匹配
2. 不同客户端解析结果不一样，同一个 path 会发出四种请求行

工具的处理：正则让尾部 `=` 可选，再统一 rewrite 回真实 path。

```nginx
location ~ ^/sdf=?$ {
    if ($http_upgrade = "") { rewrite ^ /index.html last; }
    rewrite ^ /sdf=?kfj? break;      # 末尾 ? 丢弃客户端原始 query
    proxy_pass https://127.0.0.1:20000;
}
```

结果：

| 客户端发送 | 修复前 | 修复后 |
|---|---|---|
| `/sdf=?kfj` | 101 | 101 |
| `/sdf?kfj` | 404 | 101 |
| `/sdf` | 404 | 101 |
| `/sdf=` | 404 | 101 |

### 双用途 location

同一条 location 干两件事，靠 `Upgrade` 头区分：

- 带 `Upgrade: websocket` → 转发给代理
- 不带 → `rewrite ^ /index.html last`，返回首页

所以探测者直接打开 `/sdf=?kfj`，看到的也是首页，不是空白页。

---

## 和 nginx + Trojan fallback 方案的区别

网上流传很广的一套做法是 [nginx stream + Trojan 回落](https://xrayrr.gitbook.io/xrayr-doc/za-xiang/nginx+trojan-zan-shi-di-shen)：
Nginx 用 **stream 模块（L4）** 监听 443 并终止 TLS，把明文流量用 `proxy_protocol` 转给本机的 XrayR，
XrayR 配 `CertMode: none` + `EnableFallback: true`，Trojan 收到不像代理的请求就回落给 `fake.website.com:80`。

两者方向一致，实现层次不同：

| | nginx stream + Trojan fallback | 本工具（nginx http + vless ws） |
|---|---|---|
| Nginx 工作层 | stream（L4，TCP） | http（L7，应用层） |
| 谁终止 TLS | Nginx | Nginx 443 + sing-box 20000，两层 |
| 分流依据 | Trojan 协议自己识别，认不出就 fallback | URL 路径 + `Upgrade` 头 |
| 后端证书 | `CertMode: none`，明文回环 | sing-box 自己管证书 |
| 网站位置 | Nginx http 层 80 端口 | Nginx http 层 443 同一个 server |
| 客户端端口 | 443 | 443 或代理端口，都能用 |

**为什么不能照搬 fallback**：`fallbacks` 是 Xray 内核的能力，V2bX 用的是 **sing-box**，
没有这个功能。所以只能在 Nginx 的 L7 层按路径判断。

**代价**：多了一层 TLS（Nginx 解密一次，sing-box 再解一次）。
**收益**：sing-box 的证书、面板、订阅全都不用改，客户端零感知。

如果哪天要优化，正确方向是借鉴 `CertMode: none`，让 sing-box 只听明文、Nginx 转发明文，
省掉一层加解密。这需要同时改 V2bX 配置和 Nginx 的 `proxy_pass` 协议，属于架构变更，不建议在稳定期动。

---

## 安装

### 前提

这三件事必须先具备，否则装完只会有面板、没有站点：

1. **VPS 已装好 V2bX**，`/etc/V2bX/config.json` 里有节点
2. **节点证书已签发**，`CertConfig.CertFile` / `KeyFile` 指向真实文件（工具直接复用，不自己签）
3. **域名已解析到这台机器**，端口在防火墙/安全组放行

没有 V2bX 时脚本仍会装好面板，但会跳过站点部署并提示你先装 V2bX。

### 一条命令

```bash
curl -fsSL https://raw.githubusercontent.com/Minerchu/hkbt-deploy/master/install.sh \
  | bash -s -- --port 443 --password 你的密码
```

`--path` 不用写：代理路径由 V2bX 从面板拉取，工具再向面板查一次，保证与节点一致。

或者先拉下来：

```bash
git clone https://github.com/Minerchu/hkbt-deploy.git
cd hkbt-deploy && bash install.sh
```

### 参数

| 参数 | 含义 | 默认 |
|---|---|---|
| `--path` | **仅调试用**，强制指定代理路径 | 自动向面板 API 查询，不填 |
| `--port` | **对外端口**（网站和面板用），不是代理端口 | `443` |
| `--password` | 面板登录密码 | 自动生成并打印 |
| `--no-camouflage` | 不让代理端口显示网站 | 关闭（即默认开启伪装） |

> 正常情况下只需要 `--port` 和 `--password`。代理路径与代理端口都从面板读，
> 手工填反而容易和节点不一致（面板改过 path 时，填旧值会让代理连上即断）。

> `--port` 填的是 Nginx 的监听端口。代理真实端口由工具从面板 API 读取，
> 两者相同时走形态 A，不同时走形态 B。**填代理端口会被校验直接拒绝**。

### 安装脚本做了什么

1. 装 Nginx / Flask / openssl / unzip
2. 检测本机是否有 IPv6 出口，没有就写 `gai.conf` 让 IPv4 优先
3. 探测 `/etc/V2bX/config.json`，向面板 API 查询节点真实端口与路径
4. 检查端口占用，冲突时直接拒绝并说明原因
5. 生成 Nginx 配置（含双用途 location、面板入口、静态资源缓存）
6. `nginx -t` 校验 → reload
7. 加 NAT 重定向，让代理端口也返回网站（`--no-camouflage` 可跳过）
8. 启动 `hkbt-deploy` 面板服务（只监听 `127.0.0.1:8899`）
9. 配置证书续期自动 reload（每 6 小时检测一次）
10. 打印面板地址与代理参数

---

## 面板

安装完成后会打印类似这样的地址：

```
https://你的域名/__deploy-a2f73116/
```

`__deploy-` 后面是随机生成的，不额外开任何端口，通过 Nginx 隐藏路径反向代理。

### 功能

| 功能 | 说明 |
|---|---|
| 状态总览 | 域名 / 对外端口 / 代理端口 / 路径 / 公网 IP / 核心类型 / 证书到期 / 监听状态 |
| 部署站点 | 填域名、端口、代理路径，选模板或上传 zip，一键部署 |
| 运行自检 | 6 项：首页、路径（浏览器）、路径（WebSocket）、证书、监听收敛、Nginx 接管 |
| 回滚 | 恢复到上一次部署前的状态 |
| 冲突清理 | 自动禁用占用同端口的其他 Nginx 配置 |

### 部署表单

| 字段 | 说明 |
|---|---|
| 域名 | 已解析到本机的域名 |
| **对外端口** | Nginx 监听端口，通常 443。**不能填代理端口** |
| 代理路径 | 与面板节点完全一致，含 `?` 和 `=` 也要原样填 |
| 站点模板 | 11 套任选 |
| 网站名称 | 替换模板里的默认品牌 |
| 上传 zip | 可选，覆盖模板。需含 `index.html`，单层顶层目录会自动提升 |

上传 zip 时，模板自带的 `meta.json` 会被剔除，不会出现在线上目录。

---

## 站点模板

内置 **11 套**，不需要准备任何素材：

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

每套都是 **13 个页面**：

```
首页 / 关于我们 / 业务矩阵 / 客户案例 / 市场动态 / 联系我们
物流查询 / 单据下载 / 合作条款 / 404
市场动态下的 3 篇完整文章
```

内容不是占位符——每套都写了该行业的业务板块、案例、技术文章与合作条款，
配图 6–20 张本地托管，4 份 PDF 单据可直接下载。

**品牌与域名自动替换**：模板里的默认品牌和你填的网站名称互换，
`hkbt.example` 换成你的域名，页面里不留旧品牌。

三种视觉风格共用同一套结构，差异在排版与配色：

- **杂志风**：衬线标题、奶油纸感、细边框、留白克制
- **经典企业**：无衬线、白底、胶囊按钮、卡片阴影
- **现代科技**：直角、网格背景、等宽标签、高对比强调色

---

## 客户端配置

以 vless + ws + tls 为例：

```
地址：你的域名
端口：443 或代理端口（如 20000），两个都能用
UUID：面板里该节点的 UUID
路径：与面板节点完全一致（如 /sdf=?kfj，? 和 = 都要填）
传输：ws
TLS：开启
SNI：你的域名
```

**路径必须原样填**。工具已在服务端兼容四种写法，但如果客户端支持原样填，就原样填，
少一层猜测。

判断是否连上：打开 `https://ip.sb`，看到的是**服务器的公网 IP** 就对了。

---

## 端口伪装

代理端口（如 20000）对外也是开放的。不处理的话，浏览器打开 `https://域名:20000/`
会看到 sing-box 返回的空 404 —— 一眼就知道这个端口不是网站。

**部署时会自动处理。** 代理端口与对外端口不同时，工具加一条 NAT 重定向，
把外部发往代理端口的连接转到 443：

```
外部 :20000 ──NAT──▶ Nginx :443 ──┬─ 无 Upgrade ──▶ 网站
                                  └─ 有 Upgrade ──▶ 127.0.0.1:20000
```

只作用于 `PREROUTING`，本机回环不走这里，所以 Nginx 转发回代理不会形成环路。

规则优先级：`nft` > `iptables`，写成 `hkbt-nat.service`（oneshot + `RemainAfterExit`），
开机自动重放。改端口后重新部署会先清掉旧规则，不会叠加。

关闭：

```bash
python3 app.py --init --no-camouflage --password 你的密码
```

或改 `/opt/hkbt-deploy/config.json` 的 `"camouflage": false` 后重新部署。

手动撤销：

```bash
systemctl disable --now hkbt-nat.service
nft delete table ip hkbt          # nft 系统
# 或
iptables -t nat -D PREROUTING -p tcp --dport 20000 -j REDIRECT --to-ports 443
```

> **副作用**：代理看到的客户端 IP 变成 `127.0.0.1`。
> 流量统计按 UUID 算，不受影响；但面板若开了「同 IP 设备数限制」，多设备可能被算成一个。
> 这是 Nginx 反代本身带来的，和 NAT 规则无关 —— 只要走 443 分流就是如此。

---

## 证书策略

工具**不签证书**，直接复用 V2bX 那份：`/etc/V2bX/fullchain.cer` + `cert.key`。
另外每 6 小时检测一次文件变化，更新后自动 `nginx -s reload`。

V2bX 的 `CertConfig.CertMode` 有三种选择：

| 模式 | 说明 | 适用 |
|---|---|---|
| `dns` | DNS-01 验证，走 Cloudflare/阿里云 API | **推荐**。不占端口，Nginx 占着 80 也不影响 |
| `http` | HTTP-01 验证，V2bX 自己监听 80 | **有坑**。80 被 Nginx 占了，续期必失败 |
| `file` | 手动放置证书 | 自己有签发流程时用 |

> `CertMode: http` 是个定时炸弹：装的时候看不出来，85 天后证书到期，续期绑不上 80，
> 站点和代理一起失效。域名 NS 在 Cloudflare 的话，直接改 `dns`。

改完重启 V2bX 即可，已有证书不受影响，只在到期前 30 天触发续期时走新流程。

---

## 运维

### 常用命令

```bash
systemctl status hkbt-deploy        # 面板服务
systemctl status nginx              # 网站
systemctl status V2bX               # 代理

journalctl -u hkbt-deploy -f        # 面板日志
journalctl -u V2bX -f               # 代理日志
tail -f /var/log/hkbt-deploy.log    # 部署日志

nginx -t                            # 配置校验
ss -lntp | grep -E ':443|:20000'    # 端口归属
```

### 回滚

```bash
# 面板里点「回滚」，或手动恢复备份
ls -lt /opt/hkbt-deploy/backups/
cp /opt/hkbt-deploy/backups/<时间戳>/hkbt-deploy.conf /etc/nginx/conf.d/
nginx -t && systemctl reload nginx
```

### 彻底卸载

```bash
systemctl disable --now hkbt-deploy
rm -f /etc/nginx/conf.d/hkbt-deploy.conf /etc/systemd/system/hkbt-deploy.service
rm -rf /opt/hkbt-deploy /var/www/hkbt
systemctl daemon-reload && nginx -t && systemctl reload nginx
```

### 恢复代理原状

如果之前把 sing-box 收窄过，想恢复直连：

```bash
rm -f /etc/systemd/system/V2bX.service.d/10-listen.conf
sed -i 's/"ListenIP": "127.0.0.1"/"ListenIP": "0.0.0.0"/' /etc/V2bX/config.json
systemctl daemon-reload && systemctl restart V2bX
```

---

## 文件说明

```
app.py                    主程序（CLI + Flask + 部署逻辑）
install.sh                一键安装
templates/panel.html      面板界面
templates/sites/<key>/    11 套站点模板（每套 13 页 + 配图 + 单据 + meta.json）

build/data_a.py           模板数据：化妆品 / 汽车零部件 / 化工
build/data_b.py           模板数据：食品 / 物流 / 电子
build/data_c.py           模板数据：纺织 / 家居 / 机械 / 医疗
build/build_sites.py      生成器（数据 → templates/sites/<key>/）
build/css/base.css        通用布局
build/css/{editorial,classic,tech}.css   三套视觉变体
build/shared/img|docs     共享图库与 PDF 单据
build/test_sites.py       站点自检（13 页 200 / 无死链 / 资源可达）
build/test_deploy.py      部署链路自检（模板选择 / 品牌替换 / 端口校验 / 路径改写）

TROUBLESHOOTING.md        12 个真实故障的现象、根因、验证命令
```

改模板内容只需改 `build/data_*.py`，然后重新生成：

```bash
python3 build/build_sites.py     # 生成全部模板
python3 build/test_sites.py      # 站点自检
python3 build/test_deploy.py     # 部署逻辑自检
```

服务以 systemd 运行，只监听 `127.0.0.1:8899`，通过 Nginx 隐藏路径暴露，不额外开端口。

---

## 安全

- 面板入口是安装时随机生成的路径（`/__deploy-xxxxxxxx`），不额外开端口
- 需要密码登录，密码用 PBKDF2-SHA256 加盐存储
- 所有操作留有备份，支持一键回滚
- 站点目录不暴露目录结构（403 统一转 404）

想换入口路径，改 `/opt/hkbt-deploy/config.json` 里的 `admin_path`，重新部署一次。

**三条建议**：

1. **面板密码用强密码**。这台机器上通常还跑着 1panel、Docker、V2board API，面板被撞库的代价远大于代理端口暴露。
2. **不要同时暴露 1panel 和面板**。1panel 有独立端口，能关就关，或者加 IP 白名单。
3. **高位代理端口本身是弱信号**。正经企业站不会把 HTTPS 放在 21311 上。真要隐蔽，只留 443，用 CDN 挡在前面。

---

## 已知限制

- **只支持 V2bX + sing-box**。未检测到 V2bX 时会明确提示，不盲操作。
- **不支持 Xray 的 fallbacks**。sing-box 没有这个能力，所以只能 L7 路径分流。
- **双层 TLS**。Nginx 解密一次，sing-box 再解一次，有额外 CPU 开销。
- **不自动做端口伪装**。不同机器防火墙不同，需要时按上面的命令手动加。
- **站点内容是示例数据**。案例数字、联系方式请替换成真实信息后再对外使用。

---

## 故障排查

| 现象 | 先看这里 |
|---|---|
| 面板打不开 | `systemctl status hkbt-deploy`；地址结尾的 `/` 不能少 |
| 整站和面板一起挂 | 大概率端口填成了代理端口，`nginx -t` 会报 `Address already in use` |
| 代理连不上 | 路径是否原样填；`curl` 测 WS 握手是否为 101 |
| 能连上但网页打不开 | 出站走了 IPv6，机器没有 IPv6 出口 |
| 证书到期后续期失败 | `CertMode` 是不是 `http`，被 Nginx 抢了 80 |

上面只覆盖最常见的几种。**真机安装踩过的 12 个坑**（端口冲突、代理收窄到回环、IPv6 出站、
NAT 取不到公网 IP、`error_page 418` 在 nginx 1.24 失效、1panel 防火墙、代理端口无伪装、
`meta.json` 上线、装完不打印面板地址、路径带 query、证书抢 80、无 IPv6 出口）
逐条记在 [TROUBLESHOOTING.md](TROUBLESHOOTING.md)，按症状索引，附验证命令。

---

## License

MIT
