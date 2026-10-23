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
| 部署后代理连不上，配置里 location 带着 `?` | [10](#10-代理路径带-querylocation-匹配不上--客户端写法不一致) |
| 证书到期后续期失败，站点和代理一起失效 | [11](#11-certmodehttp-与-nginx-抢-80-端口续期必失败) |
| 有的网站能开有的打不开，日志里有 network unreachable | [12](#12-没有-ipv6-出口但-dns-返回-aaaa网站打不开证书续不上) |
| 部署全绿但代理连上即断，配置里 location 是个猜的路径 | [13](#13-面板读不到-path-时不能用默认值兜底) |
| 面板改过端口/路径后，网站正常但代理连不上 | [14](#14-面板改了端口或路径nginx-还停在旧值) |
| V2bX 起不来，网站却先把 443 占了，两个问题混在一起 | [15](#15-代理没起来就部署网站问题会搅在一起) |
| Alpine 上 `wget: unrecognized option: N`、装完服务起不来 | [16](#16-alpine-上跑不起来busybox-与-systemd-的差异) |
| `Could not resolve host`，连脚本都拉不下来 | [17](#17-could-not-resolve-host解析不到域名) |

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

`https://域名:21311/` 返回一个空的 404，响应体 0 字节。任何人点开都知道「这不是网站」。

**根因**

sing-box 不像 Xray 有 `fallbacks`，它不做 HTTP 回落。端口上只有代理协议，浏览器请求自然拿不到页面。

**处理（现在已自动化）**

部署时如果代理端口 ≠ 对外端口，工具会自动加一条 NAT 重定向，把外部到代理端口的连接转到 443：

```
外部 :21311 ──NAT──▶ nginx :443 ──┬─ 普通请求       → 网站
                                  └─ WS + 路径匹配  → 127.0.0.1:21311
```

本机回环走 `OUTPUT` 链，不经过 `PREROUTING`，所以 nginx 反代回 `127.0.0.1:21311` 不会形成环路。

优先级 `nft` > `iptables`，规则写成 `hkbt-nat.service`（oneshot，幂等），改端口后重新部署会先清旧规则。
关闭用 `--no-camouflage`，详见 README 的「端口伪装」章节。

**验证**

```bash
curl -sk -o /dev/null -w "%{http_code}\n" https://域名:21311/          # 200 + 首页
curl -sk -o /dev/null -w "%{http_code}\n" https://域名:21311/不存在路径  # 404 + 定制页
curl -sk --http1.1 -o /dev/null -w "%{http_code}\n" \
  -H "Connection: Upgrade" -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Version: 13" -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
  https://域名:21311/<path>                                            # 101
```

**如果没生效**

| 检查 | 命令 |
|---|---|
| 规则是否存在 | `nft list table ip hkbt` 或 `iptables -t nat -S PREROUTING \| grep HKBT` |
| 服务是否启用 | `systemctl is-enabled hkbt-nat` |
| 规则是否被其他防火墙覆盖 | 1panel / ufw / firewalld 的规则可能排在前面 |
| 云厂商安全组 | 代理端口需要在安全组里放行，否则连接根本到不了本机 |

**副作用（重要）**

代理看到的客户端源 IP 会变成 `127.0.0.1`。流量统计不受影响，但如果面板开了「同 IP 设备数限制」，多个设备可能被算成一个。多人共用节点时要留意。

这个副作用来自 Nginx 反代本身：只要代理流量走 443 分流，sing-box 看到的就一定是 `127.0.0.1`，与是否加 NAT 规则无关。

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

## 10. 代理路径带 query，location 匹配不上 / 客户端写法不一致

**现象**

两种表现：

1. 部署日志全部通过，但客户端连不上，浏览器访问代理路径返回 404。
2. 换个客户端就连不上 —— 同一个节点，A 客户端能用，B 客户端 404。

**根因**

面板里的 path 可以是 `/sdf=?kfj` 这种同时含 `=` 和 `?` 的值。这里叠了两层问题：

第一层，nginx 的 `location` 只匹配**解码后的 URI**，`?` 之后属于 query string，不参与匹配。所以 `location = /sdf=?kfj` 永远不可能命中，`$uri` 实际是 `/sdf=`。

第二层，客户端对 `=` 和 `?` 的处理不统一，同一个 path 会发出四种不同的请求行：

| 客户端实际发送 | nginx 看到的 $uri | 结果 |
|---|---|---|
| `/sdf=?kfj`（原样） | `/sdf=` | 101 |
| `/sdf?kfj`（把 ? 当分隔符） | `/sdf` | 404 |
| `/sdf`（只保留前缀） | `/sdf` | 404 |
| `/sdf%3D%3Fkfj`（整串编码） | `/sdf%3D%3Fkfj` | 404 |

只有原样发送才能通。客户端一旦「智能」解析一下，代理就连不上，表现是所有网站都打不开 —— 用户很容易误判成「某个网站挂了」。

**处理**

生成配置时切掉 query，用**正则让尾部 `=` 可选**，再统一 rewrite 回节点真实 path：

```python
path_uri = path.split("?", 1)[0] or "/"      # /sdf=?kfj  →  /sdf=
has_query = "?" in path
loc_pat = re.escape(path_uri)
if loc_pat.endswith("="):
    loc_pat = loc_pat[:-1] + "=?"            # /sdf=  →  /sdf=?
```

```nginx
location ~ ^/sdf=?$ {
    if ($http_upgrade = "") { rewrite ^ /index.html last; }
    rewrite ^ /sdf=?kfj? break;              # 末尾 ? 丢弃客户端原始 query
    proxy_pass https://127.0.0.1:20000;
}
```

`rewrite` 末尾那个 `?` 很关键：它表示丢弃客户端带来的 query，否则会拼成 `/sdf=?kfj?kfj`。

**验证**

四种写法必须全部 101：

```bash
for u in "/sdf=?kfj" "/sdf?kfj" "/sdf" "/sdf="; do
  printf "%-14s " "$u"
  curl -sk --http1.1 -o /dev/null -w "%{http_code}\n" \
    -H "Connection: Upgrade" -H "Upgrade: websocket" \
    -H "Sec-WebSocket-Version: 13" -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
    "https://域名$u"
done
```

`build/test_deploy.py` 里有对应断言：带 query 的 path 必须生成 `location ~ ^/sdf=?$ {` 和 `rewrite ^ /sdf=?kfj? break;`，普通 path 不能生成 rewrite。

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

## 12. 没有 IPv6 出口，但 DNS 返回 AAAA：网站打不开、证书续不上

**现象**

很迷惑，因为服务器上「有的网站能开，有的开不了」：

- `curl https://ip.sb` 通，但浏览器打开卡半天
- `ping` 通，`curl -6` 直接失败
- V2bX 日志里大量 `network is unreachable` 或 `dial tcp 0.0.0.1:xxx: i/o timeout`
- lego 申请证书时报「连不上 Cloudflare」，但 `curl https://api.cloudflare.com` 明明是通的

**根因**

阿里云内网 DNS（`100.100.2.136`）优先返回 AAAA 记录，而机器只有 IPv4 地址、没有 IPv6 默认路由。
程序拿到 IPv6 地址后先尝试连接，内核返回 `ENETUNREACH`，失败后才回退 IPv4。

curl 回退得快（毫秒级），浏览器和 Go 程序回退得慢，表现出来就是「打不开」。
证书申请失败也是同一个原因：lego 连 ACME 服务器时先试 IPv6，超时后整个流程已经失败了。

**判断**

```bash
ip -6 route show default          # 空 = 没有 IPv6 出口
curl -6 -s -m 5 https://ip.sb     # 失败
curl -4 -s -m 5 https://ip.sb     # 成功
getent hosts ip.sb                # 只返回 2606:4700:... 就是命中这个问题
```

**处理**

两个层面，按需选择：

```bash
# 1) 让 glibc 优先 IPv4（安全，不影响双栈服务）
echo "precedence ::ffff:0:0/96  100" >> /etc/gai.conf

# 2) 彻底禁用 IPv6 栈（本机确认没有 IPv6 时）
cat > /etc/sysctl.d/99-disable-ipv6.conf <<'EOF'
net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1
net.ipv6.conf.lo.disable_ipv6 = 1
EOF
sysctl -p /etc/sysctl.d/99-disable-ipv6.conf
```

**注意**：如果做了第 2 步，必须检查 V2bX 的 `ListenIP`。它默认是 `::`（双栈），IPv6 栈禁用后重启会 bind 失败，代理直接起不来：

```bash
# 必须改成 0.0.0.0
python3 - <<'PY'
import json
p = "/etc/V2bX/config.json"
d = json.load(open(p))
for n in d.get("Nodes", []):
    if n.get("ListenIP") in ("::", "[::]"):
        n["ListenIP"] = "0.0.0.0"
json.dump(d, open(p, "w"), indent=2, ensure_ascii=False)
PY
systemctl restart V2bX
```

`install.sh` 已加入检测：无 IPv6 默认路由时自动写 `gai.conf`（只改排序，不动栈）。
`app.py` 的部署流程也会检查 `ListenIP`，IPv6 不可用时自动改成 `0.0.0.0`。

**验证**

```bash
getent ahosts ip.sb | head -3      # IPv4 必须排在第一
for i in 1 2 3; do curl -s -o /dev/null -w "%{time_connect}\n" https://ip.sb; done
# 应该稳定在 5ms 左右；如果出现 500ms，说明还在先试 IPv6
```

**顺带一提**

日志里的 `dial tcp 0.0.0.1:xxx` 是阿里云 DNS 主动拦截某些域名（腾讯统计、广告域名）返回的黑洞地址，属于正常行为，和 IPv6 无关。

---

## 13. 面板读不到 path 时，不能用默认值兜底

**现象**

部署日志全是 OK，Nginx 里 `location` 写的是 `/index`，但：

- 客户端握手拿到 101 后立刻断开
- 或者浏览器访问代理路径，看到的是网站首页而不是进入代理
- 面板上节点的 path 其实是 `/davsvbfb` 之类，和 `/index` 毫无关系

**根因**

早期版本在面板 API 不可达、或节点不是 ws 类型时，会把 path 兜底成 `/index`。
这个值与节点真实 path 无关，后果是：

1. 客户端发的 WebSocket 升级请求路径不匹配 → 掉进静态站点 → 返回 `index.html`
2. 客户端收到的是 HTML 而不是代理响应 → 判定握手失败 → 断开

最迷惑的表现是「能连上、但什么都打不开」，很容易被误判成网络问题。

**处理**

现在改为**读不到 path 就中止**，并打印排查项，不再猜：

```bash
python3 /opt/hkbt-deploy/app.py --probe; echo "exit=$?"
# 拿到 path → 退出码 0；拿不到 → 2，脚本据此停止部署
```

输出里的 `source` 说明数据来源：

| source | 含义 |
|---|---|
| `panel` | 面板 API 返回，最可信 |
| `listen` | 面板不通，端口从进程监听表推断（path 仍为空） |
| `config` | 面板完全不可达，沿用上次部署记录的 path，会明确告警 |

**为什么不能手工填**

V2bX 的 `config.json` 里没有 path，它自己也是运行时从面板拉的。
手工写一个「看起来对」的值，等于在 Nginx 里固化一个可能过期的假设：
面板改了 path，客户端订阅跟着更新，Nginx 还停在旧值，结果整节点失联。

**排查顺序**

```bash
# 1) 凭据与节点 ID
python3 -c "import json;n=json.load(open('/etc/V2bX/config.json'))['Nodes'][0];print(n['ApiHost'],n['NodeID'],n['NodeType'])"

# 2) 面板返回
curl -s "$(python3 -c "import json;print(json.load(open('/etc/V2bX/config.json'))['Nodes'][0]['ApiHost'])")/api/v1/server/UniProxy/config?node_type=vless&node_id=<ID>&token=<KEY>"

# 3) 看 networkSettings.path 是否存在
```

如果面板返回里没有 `networkSettings.path`，说明这个节点不是 ws / grpc 类型（tcp、reality 没有路径），
本工具的分流方式不适用，需要换节点类型。

**Xboard 兼容**

Xboard 与 V2board 用同一个端点，但部分分支会把结果包在 `{"data": {...}}` 里，
字段也可能用 `network_settings`。工具两套都认，不需要改配置。

---

## 14. 面板改了端口或路径，Nginx 还停在旧值

**现象**

在面板上把节点端口从 20000 改成 21000（或者把 ws path 从 `/sdf=?kfj` 改成别的），
客户端全部连不上。但 `https://域名/` 打得开，证书正常，`systemctl status nginx` 也是 active。

**根因**

V2bX 的端口和路径是**运行时从面板拉**的：重启一次就换新值，磁盘上没有副本。
Nginx 的配置却是静态的 —— 里面写死了 `proxy_pass https://127.0.0.1:20000` 和
`location ~ ^/sdf=?$`。面板改了值，V2bX 跟着变，Nginx 不知道。

于是流量按旧规则转发到一个已经没人监听的端口，或者按旧路径匹配不上，掉进静态站点。

**判断**

```bash
# Nginx 认为的端口/路径（第一行指纹）
head -3 /etc/nginx/conf.d/hkbt-deploy.conf

# 面板真实的值
python3 /opt/hkbt-deploy/app.py --probe

# 代理实际在听哪个端口
ss -lntp | grep -E 'V2bX|sing-box'
```

三者对不上就是这个问题。

**处理**

```bash
python3 /opt/hkbt-deploy/app.py --sync
```

指纹一致时会输出「配置一致，无需变更」，不一致时会重写配置、重载 Nginx、重建 NAT 规则。

如果提示「跳过：面板 API 不可达」，说明是面板本身不通，不是同步逻辑的问题，
先修面板连通性 —— 这时保持原配置不动是**故意的**，避免站点下线。

**预防**

安装脚本会自动启用 `hkbt-sync.timer`，V2bX 重启后立刻同步一次，之后每 3 分钟兜底：

```bash
systemctl list-timers hkbt-sync.timer
journalctl -u hkbt-sync -n 30
```

---

## 15. 代理没起来就部署网站，问题会搅在一起

**现象**

一键装完，V2bX 因为配置写错（节点 ID 不对、端口被占、证书路径错）一直起不来。
但网站是好的，`https://域名/` 能打开，看起来"装成功了"。

**根因**

装网站的脚本只检查了 `config.json` 里有没有节点、证书文件在不在，没检查代理**真的跑起来了**。
结果 Nginx 先接管了 443，代理在后台反复重启失败。这时候去排查，会同时看到
「网站正常」和「代理不通」两个信号，很难判断到底是代理挂了，还是 Nginx 分流写错了。

**处理**

现在的 `hkbt.sh` 会在部署前做三道检查，任何一道不过就停下来，不碰 443：

```bash
systemctl is-active V2bX                       # 1. 服务在跑
ss -tlnp | grep -E 'V2bX|sing-box'             # 2. 节点端口在监听（最硬的证据）
journalctl -u V2bX -n 200 | grep -iE \
  'panic|fatal|address already in use|invalid config'   # 3. 日志里没有致命错误
```

端口在监听就直接放行（说明代理确实可用）；没监听才去看日志找原因，
命中致命关键词立刻中止并打印那几行，不用等满 150 秒超时。

**验证**

```bash
# 代理是否真的在跑
ss -lntp | grep -E 'V2bX|sing-box'

# 有没有致命错误
journalctl -u V2bX -n 100 --no-pager | grep -iE 'panic|fatal|error'

# 修好后重新部署网站
bash hkbt.sh
```

> 顺序是「先确认代理能用，再配网站」，不是「一起装完再看」。
> 代理配错时网站不部署，443 空着反而是好事 —— 至少只有一个问题要查。

---

## 16. Alpine 上跑不起来：BusyBox 与 systemd 的差异

**现象**

在 Alpine 机器上执行一键命令，第一步就报错：

```
wget: unrecognized option: N
BusyBox v1.37.0 multi-call binary.
```

或者 `bash install.sh` 直接 `bash: not found`。

**根因**

Alpine 用的是 BusyBox 工具链，跟 GNU 版本差了三处：

| 差异 | 影响 |
|---|---|
| `wget` 没有 `-N` / `--progress=bar` / `--no-check-certificate` | 下载命令直接失败 |
| 默认没有 bash，`/bin/sh` 是 ash | `[[ ]]`、数组全部不可用 |
| 用 OpenRC，没有 systemd | `systemctl` 命令不存在 |

再加上 Alpine 的 nginx 包有两处不同：配置目录是 `/etc/nginx/http.d`（不是 `conf.d`），
运行用户是 `nginx`（不是 `www-data`），而且 `nginx.conf` 里预置了
`ssl_session_cache shared:SSL:2m` —— 配置里再声明 `shared:SSL:10m` 会报
`the size ... conflicts with already declared size`。

**处理**

现在的版本已经全部适配，装的时候不用做任何特殊处理：

```bash
curl -fsSL https://raw.githubusercontent.com/Minerchu/V2bX-script/master/install.sh -o install.sh
sh install.sh          # 用 sh 就行，脚本会自己装 bash 再重新执行
```

脚本内部做的事：

- 下载统一走 `curl`，缺 curl 才退回 `wget -O`
- 检测不到 `BASH_VERSION` 就先 `apk add bash` 再 `exec bash "$0"`
- 服务操作走 `rc-service` / `rc-update`，日志看 `/var/log/V2bX.log`
- Nginx 配置写到 `/etc/nginx/http.d/hkbt-deploy.conf`，属主设成 `nginx`
- SSL 共享内存区改名 `hkbt_ssl`，避开 Alpine 的默认值
- 定时同步改用 `cron`，代理重启钩子挂在 `/etc/init.d/V2bX` 的 `post_start()`

**验证**

```bash
# 判断当前是什么 init
ps -p 1 -o comm=
# systemd → 用 systemctl；init → 用 rc-service

# Alpine 上确认服务
rc-service nginx status
rc-service V2bX status
rc-service hkbt-deploy status

# 同步任务
crontab -l | grep hkbt-sync

# 日志
tail -50 /var/log/V2bX.log
tail -20 /var/log/hkbt-deploy-panel.err
```

> 手动排查时别再敲 `systemctl`。Alpine 上一律换成 `rc-service <名字> <动作>`，
> 开机自启用 `rc-update add <名字> default`。

---

## 17. `Could not resolve host`：解析不到域名

**现象**

```
# curl -fsSL https://raw.githubusercontent.com/... -o install.sh
curl: (6) Could not resolve host: raw.githubusercontent.com
```

第一步就挂，脚本连下载都做不到。

**这不是脚本的问题**，是这台机器解析不了任何域名。常见三种原因：

| 检查 | 命令 | 说明 |
|---|---|---|
| `resolv.conf` 是空的 | `cat /etc/resolv.conf` | Alpine 模板常见，只有注释没有 nameserver |
| 只写了 IPv6 DNS | 同上 | 机器没有 IPv6 出口时，解析全部超时 |
| 网络根本没通 | `ping -c2 1.1.1.1` | 裸 IP 也不通，先查路由和网关 |

**新版脚本会自动修**

`install.sh` 在 `apk add bash` **之前**就会探一遍 `github.com`。解析不了时依次试
`223.5.5.5` / `119.29.29.29` / `1.1.1.1` / `8.8.8.8` / `9.9.9.9`，哪个通就写进
`/etc/resolv.conf`：

```
  DNS 解析失败，尝试自动修复…
  DNS 已修复：nameserver 223.5.5.5（已锁定防 DHCP 覆盖，解锁：chattr -i /etc/resolv.conf）
```

放在这个位置是有原因的：`apk add bash` 本身要解析软件源域名，DNS 不通的话连 bash
都装不上，报错会出现在第 5 行，看不出真正原因。

**手工修**

```bash
printf 'nameserver 223.5.5.5\nnameserver 1.1.1.1\n' > /etc/resolv.conf
nslookup github.com 2>&1 | head -5
```

改完又变回去，说明 `udhcpc` / `dhcpcd` 在覆盖：

```bash
chattr +i /etc/resolv.conf      # 锁住；要改先 chattr -i
```

**解析通了但 GitHub 连不上**

那就是域名被拦，不是 DNS 问题。脚本的下载环节会自动切 `ghproxy.net` / `gh-proxy.com`，
手工装时也可以直接走镜像：

```bash
curl -fsSL https://cdn.jsdelivr.net/gh/Minerchu/V2bX-script@master/install.sh -o install.sh
```

> DNS 是地基。就算用镜像把脚本拉下来，V2bX 申请证书要连 `api.cloudflare.com`，
> 节点要解析 `ApiHost` —— 解析没修好，最后一定卡在证书那一步。

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
