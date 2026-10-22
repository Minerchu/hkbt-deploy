# 实战踩坑记录

> 来源：2026-10-21 在 `usa-cdn-cn2.netflixcn.de` 上的真实安装。那台机器跑着 1panel 和 33 个容器，属于"不能弄坏"的生产环境，所以下面每条都是真踩出来的，不是推演。
>
> 写法统一为「现象 → 根因 → 处理 → 验证」，遇到问题按症状查。

## 快速索引

| 症状 | 对应章节 |
|---|---|
| 面板突然打不开，但 nginx 显示 active | [1](#1-把代理端口填成对外端口整站和面板一起挂) |
| 客户端连代理端口超时，服务端日志里没有入站记录 | [2](#2-sing-box-被收窄到-127001客户端连不上) |
| 节点连上了，但所有网页都打不开 | [3](#3-能连上但网页打不开出站走了-ipv6) |
| 生成的配置绑了个 10.x 内网地址 | [4](#4-nat-机器上取公网-ip-拿到的是内网地址) |
| 浏览器访问代理路径，被转给代理返回 400 | [5](#5-error_page-418--if-在-nginx-124-上不生效) |
| 本机 curl 通，外网连不上 | [6](#6-1panel-防火墙没放行端口) |
| 访问代理端口是一张空白的 404 | [7](#7-代理端口没有网页伪装一眼就露馅) |
| 线上站点目录里出现 meta.json | [8](#8-模板的-metajson-被拷到了线上) |
| 装完不知道面板地址是什么 | [9](#9-安装完不打印面板地址) |
| 部署后代理连不上，配置里 location 带着 `?` | [10](#10-代理路径带-querylocation-永远匹配不上) |
| 证书到期后续期失败，站点和代理一起失效 | [11](#11-certmodehttp-与-nginx-抢-80-端口续期必失败) |

---

## 1. 把代理端口填成对外端口，整站和面板一起挂

**现象**

在面板里把端口改成代理端口（例：21311），提交后网站和面板同时打不开。
但 `systemctl is-active nginx` 仍然返回 `active`，`ss -lntp` 里也找不到 21311 上的 nginx。

**根因**

同一个 `IP:端口` 只能被一个进程监听。sing-box 已经占了 `127.0.0.1:21311`，nginx 再去绑 `0.0.0.0:21311` 会直接失败 —— 因为 `0.0.0.0` 包含 `127.0.0.1`，两者互斥。

reload 失败后，nginx 的**旧 worker 继续用旧配置服务**（这就是 `active` 还成立的原因），但面板页面上显示的访问地址已经变成了 `:21311`。那个端口上没有 nginx，所以打不开。

日志里的证据：

```
[emerg] bind() to 0.0.0.0:21311 failed (98: Address already in use)
```

**处理**

对外端口只能是 443，代理端口由 V2bX 自己占着，两者必须不同。

代码里已加拦截：`port_conflict()` 会在部署前检查「对外端口 == 代理端口」和「端口被其他进程占用」，命中就直接终止并给出原因，不会写出坏配置。

**验证**

```bash
nginx -t                          # 必须 successful
ss -lntp | grep -E ':443|:21311'  # 443 归 nginx，21311 归 V2bX
```

**教训**

面板显示 `active` 不代表新配置生效。判断依据应该是 `ss` 里的监听状态和 `nginx -t` 的结果，不是 systemd 的状态。

---

## 2. sing-box 被收窄到 127.0.0.1，客户端连不上

**现象**

客户端连 21311 一直超时。服务端 `ss` 显示 `127.0.0.1:21311`，V2bX 日志里没有任何入站连接记录。

**根因**

`ListenIP` 被改成了 `127.0.0.1`，只监听回环，公网流量根本到不了。
而 V2board 面板里这个节点的 `server_port` 仍然是 21311，客户端按订阅配置连 21311，必然失败。

两者错位：**面板告诉客户端连 21311，但 21311 只对本地开放**。

**处理**

```bash
# 1. 改回公网监听
sed -i 's/"ListenIP": "127.0.0.1"/"ListenIP": "0.0.0.0"/' /etc/V2bX/config.json

# 2. 删掉会把它改回去的 systemd dropin
rm -f /etc/systemd/system/V2bX.service.d/10-listen.conf
systemctl daemon-reload

# 3. 重启
systemctl restart V2bX
```

**验证**

```bash
ss -lntp | grep 21311        # 必须是 0.0.0.0:21311，不是 127.0.0.1
```

**注意**

`narrow_v2bx()` 只适用于「nginx 要和代理抢同一个端口」的场景。分离端口部署时**不要**收窄，否则就是这条坑。

---

## 3. 能连上但网页打不开：出站走了 IPv6

**现象**

客户端显示已连接、有握手，但任何网站都打不开。服务端日志：

```
ERROR connection: open connection to xxx.fbcdn.net:443 using outbound/direct[direct]:
dial tcp [2a03:2880:f33e:4:face:b00c:0:79f4]:443: connect: network is unreachable
```

**根因**

sing-box 的 DNS 解析返回了 AAAA 记录，但服务器没有公网 IPv6 出口（`ip -6 addr` 只有 docker 的 `fd00::` 内网地址）。
表现就是"节点是通的，但流量出不去"，很容易被误判成代理没配好。

**处理**

sing-box 1.13 里 `domain_strategy` 已移除，必须用 `domain_resolver` + `strategy`：

```json
{
  "dns": {
    "servers": [{ "tag": "cf", "address": "1.1.1.1" }],
    "strategy": "ipv4_only"
  },
  "outbounds": [
    {
      "tag": "direct",
      "type": "direct",
      "domain_resolver": { "server": "cf", "strategy": "ipv4_only" }
    }
  ]
}
```

改 `/etc/V2bX/sing_origin.json`（这是 V2bX 的内核模板），然后 `systemctl restart V2bX`。

**验证**

```bash
ip -6 route show default        # 无输出 = 没有 IPv6 出口
journalctl -u V2bX --since "2 min ago" | grep -c "network is unreachable"   # 应为 0
```

**教训**

"能连上"和"能用"是两件事。握手成功只说明认证过了，不代表出站链路正常。排查时先看服务端日志里有没有出站错误，再怀疑客户端。

---

## 4. NAT 机器上取公网 IP 拿到的是内网地址

**现象**

生成的 nginx 配置里 `listen 10.0.0.117:443`，外网访问不了。

**根因**

`ip route get 1.1.1.1` 返回的 `src` 是内网地址。NAT 机器上网卡上没有公网 IP，出站经过 SNAT，所以本机视角看到的是内网。

**处理**

`public_ip()` 改为优先走外部服务查询，只有外部查询全失败时才退回本地探测。

**验证**

```bash
python3 -c "import app; print(app.public_ip())"   # 必须是公网地址
```

---

## 5. `error_page 418 + if` 在 nginx 1.24 上不生效

**现象**

浏览器访问代理路径，没有被送回首页，而是被转给 sing-box，返回 400。

**根因**

早期实现用 `error_page 418 = @site; if ($http_upgrade = "") { return 418; }` 做分流。
在 nginx 1.24 上，`if` 属于 rewrite 阶段，`return 418` 触发的内部跳转与 `error_page` 的交互不可靠，实际没有落到 `@site`。

**处理**

改成直接在 rewrite 阶段改写 URI，让它重新匹配静态 location：

```nginx
location = /davsvbfb {
    if ($http_upgrade = "") { rewrite ^ /index.html last; }
    proxy_pass https://127.0.0.1:21311;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $hkbt_connection_upgrade;
}
```

**验证**

```bash
# 不带 Upgrade 头 → 应返回首页 200
curl -sk -o /dev/null -w "%{http_code}\n" https://域名/路径

# 带 Upgrade 头 → 应返回 101
curl -sk --http1.1 -o /dev/null -w "%{http_code}\n" \
  -H "Connection: Upgrade" -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Version: 13" -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
  https://域名/路径
```

---

## 6. 1panel 防火墙没放行端口

**现象**

服务器上 `curl 127.0.0.1:端口` 正常，外网连不上。

**根因**

1panel 接管了 iptables，`INPUT` 链跳到 `1PANEL_BASIC`，未放行的端口直接丢包。云厂商的安全组是第二道，两层都要放行。

**处理**

在 1panel 的防火墙里放行，或者干脆用 443（默认已放行）。

**验证**

```bash
iptables -S | grep 1PANEL_BASIC | head
# 从外部机器测，不要只在本机 curl
```

---

## 7. 代理端口没有网页伪装，一眼就露馅

**现象**

`https://域名:21311/` 返回一个空的 404，响应体 0 字节。任何人点开都知道"这不是网站"。

**根因**

sing-box 不像 Xray 有 `fallbacks`，它不做 HTTP 回落。端口上只有代理协议，浏览器请求自然拿不到页面。

**处理**

用 NAT 把外部到代理端口的连接重定向到 443，交给 nginx 统一处理：

```bash
iptables -t nat -I PREROUTING 1 -p tcp --dport 21311 -j REDIRECT --to-ports 443
```

链路变成：

```
外部 → 21311（被 REDIRECT）→ 443 nginx
                              ├─ 普通请求        → 网站
                              └─ WS + 路径匹配   → 127.0.0.1:21311 sing-box
```

本机回环走 `OUTPUT` 链，不经过 `PREROUTING`，所以 nginx 反代回 `127.0.0.1:21311` 不会形成环路。

持久化写成 systemd oneshot（幂等，先 `-C` 检查再插入）：

```ini
[Unit]
Description=redirect external 21311 to 443
After=network-online.target netfilter-persistent.service docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c "/sbin/iptables -t nat -C PREROUTING -p tcp --dport 21311 -j REDIRECT --to-ports 443 2>/dev/null || /sbin/iptables -t nat -I PREROUTING 1 -p tcp --dport 21311 -j REDIRECT --to-ports 443"
ExecStop=/bin/sh -c "/sbin/iptables -t nat -D PREROUTING -p tcp --dport 21311 -j REDIRECT --to-ports 443 2>/dev/null || true"

[Install]
WantedBy=multi-user.target
```

**验证**

```bash
curl -sk -o /dev/null -w "%{http_code}\n" https://域名:21311/          # 200 + 首页
curl -sk -o /dev/null -w "%{http_code}\n" https://域名:21311/不存在路径  # 404 + 定制页
```

**副作用（重要）**

代理看到的客户端源 IP 会变成 `127.0.0.1`。流量统计不受影响，但如果面板开了「同 IP 设备数限制」，多个设备可能被算成一个。多人共用节点时要留意。

---

## 8. 模板的 meta.json 被拷到了线上

**现象**

站点根目录出现 `meta.json`，里面是模板的 `default_brand` 等元数据。

**根因**

`generate_default_site()` 直接整目录拷贝模板，没有排除元数据文件。

**处理**

`do_deploy()` 里部署完成后剔除 `meta.json`。`test_deploy.py` 加了回归断言：线上目录出现 `meta.json` 即判定失败。

---

## 9. 安装完不打印面板地址

**现象**

`install.sh` 跑完，屏幕上没有面板登录地址，用户不知道该去哪登录。

**根因**

`cmd_init()` 在收尾时用内存里的旧 `cfg` 覆盖了 `do_deploy()` 刚写入磁盘的 `domain` / `brand` / `template`，面板地址拼不出来。

**处理**

改为以磁盘上的配置为准做合并（`merge_init_conf()`），内存值只用于补缺，不覆盖已有字段。

---

## 10. 代理路径带 query，location 永远匹配不上

**现象**

部署日志全部通过，但客户端连不上。nginx 配置里写着 `location = /sdf=?kfj`，浏览器访问这个地址返回 404，代理握手拿不到 101。

**根因**

面板里的 path 可以是 `/sdf=?kfj` 这种带 `?` 的值。nginx 的 `location` 只匹配**解码后的 URI**，`?` 之后属于 query string，不参与匹配。
所以 `location = /sdf=?kfj` 永远不可能命中 —— 客户端实际发的是 `GET /sdf=?kfj`，nginx 眼里 `$uri` 是 `/sdf=`。

**处理**

生成配置时切掉 query，只拿 URI 部分做 location；`proxy_pass` 后面不带 URI，原始请求行（含 query）会原样透传，客户端无感。

```python
path_uri = path.split("?", 1)[0] or "/"      # /sdf=?kfj  →  /sdf=
```

```nginx
location = /sdf= {
    if ($http_upgrade = "") { rewrite ^ /index.html last; }
    proxy_pass https://127.0.0.1:20000;      # 不带 URI，透传原始请求
}
```

**验证**

```bash
# 注意 URL 必须加引号，否则 shell 会把 ? 当通配符
curl -sk --http1.1 -o /dev/null -w "%{http_code}\n" \
  -H "Connection: Upgrade" -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Version: 13" -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
  "https://域名/sdf=?kfj"        # 必须是 101
```

`build/test_deploy.py` 里有对应断言：带 query 的 path 必须生成 `location = /sdf= {`。

---

## 11. CertMode=http 与 nginx 抢 80 端口，续期必失败

**现象**

安装当天一切正常。两三个月后证书过期，站点和代理同时失效，`journalctl -u V2bX` 里能看到 ACME 相关报错。

**根因**

V2bX 的 `CertMode: "http"` 走 HTTP-01 验证，需要**自己监听 80 端口**放 challenge 文件。
而本工具让 nginx 接管了 80 和 443，V2bX 续期时绑不上 80，验证失败。
这是一个定时炸弹：装的时候看不出来，到期那天才炸。

**处理**

域名 DNS 托管在 Cloudflare 时，直接改成 DNS-01 验证，彻底绕开 80 端口：

```json
"CertConfig": {
  "CertMode": "dns",
  "Provider": "cloudflare",
  "DNSEnv": {
    "CLOUDFLARE_EMAIL": "...",
    "CLOUDFLARE_API_KEY": "..."
  }
}
```

改完 `systemctl restart V2bX`。已有证书不受影响，只在到期前 30 天触发续期时才会走新流程。

**验证**

```bash
openssl x509 -in /etc/V2bX/fullchain.cer -noout -enddate   # 确认证书仍在
journalctl -u V2bX --since "2 min ago" | grep -iE "cert|acme"   # 重启后不应有报错
```

**如果 DNS 不在 Cloudflare**

两个选择：一是把域名 NS 迁到 CF；二是在续期窗口前手动 `systemctl stop nginx`，续完再起。后者不可持续，只适合应急。

---

## 通用排查顺序

遇到"打不开"先按这个顺序走，能覆盖上面 90% 的情况：

```bash
# 1. 端口到底在谁手里
ss -lntp | grep -E ':443|:21311'

# 2. 配置是否合法（reload 失败时旧配置还在跑，active 会骗人）
nginx -t

# 3. 本机能不能通（排除防火墙）
curl -sk -o /dev/null -w "%{http_code}\n" https://127.0.0.1/ -H "Host: 你的域名"

# 4. 代理入站有没有记录
journalctl -u V2bX --since "5 min ago" | tail -30

# 5. 出站有没有报错（IPv6 / DNS / 路由）
journalctl -u V2bX --since "5 min ago" | grep -E "unreachable|refused|timeout"
```

第 2 步最容易被跳过。`systemctl reload nginx` 失败时返回码可能仍是 0，真正要看的是 `nginx -t` 和 error.log 里的 `[emerg]`。

## 回滚

所有部署都会在 `/opt/hkbt-deploy/backups/<时间戳>/` 留一份 `config.json` 和 `hkbt-deploy.conf`。
面板上有「回滚」按钮，等价于：

```bash
systemctl stop nginx
cp /opt/hkbt-deploy/backups/<最新>/hkbt-deploy.conf /etc/nginx/conf.d/
cp /opt/hkbt-deploy/backups/<最新>/config.json /etc/V2bX/config.json
systemctl restart V2bX
systemctl start nginx
```

如果连 nginx 都起不来，先 `systemctl stop nginx`，把 1panel 的 openresty 容器拉起来顶上：

```bash
docker update --restart=always 1Panel-openresty-iXLb
docker start 1Panel-openresty-iXLb
```
