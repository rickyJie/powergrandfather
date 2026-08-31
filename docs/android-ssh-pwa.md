# Android：通过 SSH 隧道安装和使用 PowerGrandFather PWA

这份指南适用于以下场景：

- PowerGrandFather 运行在企业云服务器上；
- 服务端不开放 PowerGrandFather 的公网端口；
- 用户可以从 Android 手机 SSH 到服务器；
- 希望像普通 App 一样从安卓桌面打开 PowerGrandFather。

推荐使用开源 Android SSH 客户端 [ConnectBot](https://connectbot.org/)。它支持本地端口转发、后台保持连接和 ProxyJump，不需要 root。

## 使用效果

```text
Android Chrome / PGF 桌面图标
http://127.0.0.1:18000
            │
            ▼
ConnectBot 本地端口转发
            │ SSH 加密连接
            ▼
企业云服务器 127.0.0.1:8000
            │
            ▼
PowerGrandFather
```

手机不需要和服务器处于同一个网络。只要手机能够 SSH 到企业云服务器，或者能够通过公司 VPN/跳板机 SSH 到服务器，就能使用。

> 浏览器/PWA 本身不能建立 SSH 隧道。每次使用 PGF 前，需要先在 ConnectBot 中连接服务器。

## 需要准备的信息

开始前准备好：

| 配置 | 示例 |
|---|---|
| SSH 服务器地址 | `dev.example.com` |
| SSH 端口 | `22` |
| SSH 用户名 | `alice` |
| SSH 认证方式 | 独立的手机 SSH Key，推荐 |
| PowerGrandFather 服务端口 | `8000` |
| Android 本地转发端口 | `18000` |
| 跳板机 | 没有则留空 |

以下命令中的地址和用户名需要替换成自己的实际值。

## 一、服务端准备

### 1. 构建前端

在企业云服务器的仓库根目录执行：

```bash
conda activate csm
cd frontend
npm ci
npm run build
cd ..
```

确认 PWA 文件已经生成：

```bash
test -f frontend/dist/index.html && echo "OK: frontend"
test -f frontend/dist/manifest.webmanifest && echo "OK: manifest"
test -f frontend/dist/sw.js && echo "OK: service worker"
```

### 2. 只在服务器本机启动 PowerGrandFather

```bash
./scripts/start.sh 127.0.0.1 8000
```

确认服务可用：

```bash
curl -sf http://127.0.0.1:8000/ | grep -q 'id="app"' && echo "OK: PGF"
```

SSH 隧道模式不需要局域网 HTTPS 证书。SSH 已经负责手机到服务器之间的加密，而手机浏览器访问的是自己的 `127.0.0.1`。

如果上面的 `curl http://...` 失败，而日志显示服务使用了 HTTPS，说明之前运行过 `scripts/gen-cert.sh`。可以把证书暂时移到不会被 `start.sh` 自动发现的位置，再重启服务：

```bash
mkdir -p secrets/disabled
mv secrets/csm-cert.pem secrets/disabled/
mv secrets/csm-key.pem secrets/disabled/
./scripts/stop.sh
./scripts/start.sh 127.0.0.1 8000
```

需要恢复原来的 HTTPS 部署时，把两个文件移回 `secrets/` 即可。

### 3. 确认服务器没有对公网暴露 8000

```bash
ss -tlnp | grep ':8000'
```

预期监听地址是：

```text
127.0.0.1:8000
```

不应该是：

```text
0.0.0.0:8000
```

## 二、安装 ConnectBot

推荐从以下任一官方渠道安装：

- Google Play 搜索 `ConnectBot`；
- [ConnectBot 官方网站](https://connectbot.org/)；
- [ConnectBot GitHub Releases](https://github.com/connectbot/connectbot/releases)。

ConnectBot 是 Apache-2.0 开源软件。其官方说明表明，SSH 数据直接发送到用户指定的服务器，不经过 ConnectBot 开发者的服务器；应用也提供后台连接保持能力。

## 三、配置 SSH 认证

如果手机上的 ConnectBot 已经可以 SSH 登录企业云服务器，可以跳过本节。

推荐为手机单独创建一把 SSH Key，不要把电脑上的主私钥复制到手机。

### 1. 在 ConnectBot 创建手机专用密钥

1. 打开 ConnectBot。
2. 打开右上角菜单。
3. 进入 `Manage pubkeys`。
4. 点击新增或生成密钥。
5. 名称填写 `pgf-android`。
6. 优先选择 Ed25519；如果当前版本没有该选项，使用 RSA 3072 或 4096。
7. 为私钥设置密码，并启用生物识别解锁（如果设备支持）。
8. 复制生成的公钥。只复制 `.pub` 公钥，不要导出私钥。

### 2. 把公钥加入企业云服务器

通过已经可信的电脑 SSH 会话，把手机公钥追加到目标用户：

```bash
mkdir -p ~/.ssh
chmod 700 ~/.ssh
printf '%s\n' '这里替换成手机生成的 ssh-ed25519 公钥' >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

确认服务器允许公钥认证后，再在手机上测试登录。

## 四、在 ConnectBot 添加服务器

1. 打开 ConnectBot 主界面。
2. 新建 Host。
3. 在 `Quick connect` 中填写：

```text
用户名@服务器地址:SSH端口
```

例如：

```text
alice@dev.example.com:22
```

4. 展开 `Show advanced options`。
5. 在 `Use pubkey authentication` 中选择 `pgf-android`。
6. 打开 `Stay connected`。
7. 如果只把该连接用于端口转发，可以关闭 `Start shell session`。
8. 保存 Host。

第一次连接时，ConnectBot 会显示服务器 Host Key 指纹。应与管理员或电脑上记录的指纹核对；不要在指纹不一致时直接确认。

## 五、添加本地端口转发

在 ConnectBot 的 Host 列表中：

1. 长按目标 Host，或者点击它右侧的 Host options。
2. 选择 `Edit port forwards`。
3. 点击新增按钮。
4. 按下面填写：

| ConnectBot 字段 | 填写内容 |
|---|---|
| `Nickname` | `PowerGrandFather` |
| `Type` | `Local` |
| `Source port` | `18000` |
| `Destination` | `127.0.0.1:8000` |

5. 点击 `Create port forward`。
6. 返回 Host 列表并连接服务器。

这里的含义是：

```text
手机 127.0.0.1:18000
  → SSH 隧道
  → 企业云服务器 127.0.0.1:8000
```

不要选择 `Remote` 或 `Dynamic (SOCKS)`。

## 六、使用跳板机（可选）

如果企业云服务器只能通过堡垒机/跳板机访问：

1. 先在 ConnectBot 中保存跳板机 Host，并确认它能成功登录。
2. 再编辑 PowerGrandFather 所在服务器的 Host。
3. 展开高级设置。
4. 在 `Jump host (ProxyJump)` 中选择已保存的跳板机。
5. 端口转发仍然填写：

```text
Source port: 18000
Destination: 127.0.0.1:8000
Type: Local
```

如果公司堡垒机要求专用客户端、动态口令或不允许 TCP Forwarding，需要由公司管理员开放对应能力，不能通过 PWA 绕过。

## 七、首次在 Android 浏览器打开

1. 在 ConnectBot 中连接服务器。
2. 确认通知栏出现 ConnectBot 的活动连接通知。
3. 打开 Android Chrome。
4. 访问：

```text
http://127.0.0.1:18000/sessions
```

5. 确认页面正常显示。
6. 点击任意会话，确认终端状态变为 `connected`。

如果启用了 `CSM_ACCESS_TOKEN`，首次访问使用：

```text
http://127.0.0.1:18000/?token=你的访问令牌
```

成功后地址栏会自动移除令牌，以后的请求使用安全 Cookie。

## 八、安装 PGF 到安卓桌面

保持 ConnectBot 隧道处于连接状态，然后：

1. 在 Chrome 打开 `http://127.0.0.1:18000/sessions`。
2. 等待页面完全加载。
3. 如果页面出现“安装到手机主屏幕”，点击“安装”。
4. 没有出现提示时，打开 Chrome 右上角菜单。
5. 选择“安装应用”或“添加到主屏幕”。
6. 确认名称为 `PGF` 或 `PowerGrandFather`。

`127.0.0.1` 属于浏览器认可的本地可信来源，因此可以注册 PWA Service Worker，不需要给手机安装自签名 HTTPS 证书。

## 九、避免 Android 杀掉 SSH 隧道

### ConnectBot 内部设置

1. 打开 ConnectBot `Settings`。
2. 启用 `Stay connected in the background`。
3. 保持 Android 通知权限开启。
4. 如果经常使用 Wi-Fi，可以启用 `Keep Wi-Fi active`。

### Android 系统设置

不同厂商菜单名称略有区别，通常是：

```text
设置 → 应用 → ConnectBot → 电池 → 不受限制 / Unrestricted
```

还应确认：

- 允许 ConnectBot 后台活动；
- 允许后台网络数据；
- 不要在系统管家中“一键清理”ConnectBot；
- 锁屏后连接经常断开时，把 ConnectBot 加入后台白名单；
- 省电模式可能限制网络连接，使用 PGF 时应关闭省电模式。

设置为“不受限制”会增加少量耗电，只建议对 ConnectBot 使用。

## 十、每天怎么使用

日常流程只有三步：

1. 打开 ConnectBot，连接 PowerGrandFather Host。
2. 确认通知栏显示 SSH 连接正在运行。
3. 点击安卓桌面的 PGF 图标。

使用结束后：

1. 退出或切走 PGF。
2. 回到 ConnectBot。
3. 主动断开 SSH，减少电量和移动数据消耗。

## 十一、故障排查

### Chrome 显示“拒绝连接”

依次检查：

1. ConnectBot 是否已经连接服务器；
2. `Edit port forwards` 中的转发是否存在；
3. `Type` 是否为 `Local`；
4. `Source port` 是否为 `18000`；
5. `Destination` 是否为 `127.0.0.1:8000`；
6. 服务端是否监听 `127.0.0.1:8000`。

服务端检查：

```bash
curl -sf http://127.0.0.1:8000/ | head
ss -tlnp | grep ':8000'
tail -n 100 csm.log
```

### ConnectBot 报 Port forwarding failed

- 手机端口 `18000` 可能被另一个连接占用；断开旧连接后重试。
- 不要同时运行两个使用相同 Source port 的 Host。
- 如果服务器 SSH 配置禁止转发，需要管理员检查 `AllowTcpForwarding` 策略。

### 页面能打开，但终端显示 disconnected

- 确认 ConnectBot 隧道没有断开；
- 回到 ConnectBot 后重新连接；
- 确认浏览器地址一直是 `127.0.0.1:18000`；
- 查看服务器 `csm.log` 中是否有 WebSocket 错误。

SSH 是 TCP 级转发，正常情况下无需单独配置 WebSocket 代理。

### 切换 Wi-Fi/移动网络后断开

网络 IP 变化会让现有 SSH TCP 连接失效。回到 ConnectBot，等待自动重连；未自动恢复时手动断开并重新连接，然后返回 PGF。

### PGF 图标能打开，但只有离线界面

这是 PWA 静态缓存仍然有效，但 SSH 隧道没有连接：

1. 打开 ConnectBot；
2. 重新连接服务器；
3. 返回 PGF；
4. 必要时点击终端中的重新连接按钮。

### Chrome 没有“安装应用”菜单

- 确认使用的是 Chrome，而不是 ConnectBot 内置终端；
- 确认地址是 `http://127.0.0.1:18000`；
- 确认 `manifest.webmanifest` 和 `sw.js` 已构建；
- 先正常使用一次页面，再重新打开 Chrome 菜单；
- 即使浏览器只显示“添加到主屏幕”，仍可先用这个入口启动。

## 十二、Termux 备用方案

如果不想使用图形化 SSH 客户端，可以使用 [Termux](https://termux.dev/) 提供的 OpenSSH。

安装 SSH：

```bash
pkg update
pkg install openssh
```

建立隧道：

```bash
termux-wake-lock
ssh -NT \
  -L 127.0.0.1:18000:127.0.0.1:8000 \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  用户名@服务器地址
```

然后在 Chrome 打开：

```text
http://127.0.0.1:18000
```

Termux 方案更透明，也更方便排查错误，但日常使用步骤比 ConnectBot 多。因此推荐普通用户使用 ConnectBot，熟悉命令行的用户使用 Termux。

## 安全检查清单

- [ ] PowerGrandFather 只监听服务器 `127.0.0.1:8000`
- [ ] 云安全组没有向公网开放 8000
- [ ] 手机使用独立 SSH Key
- [ ] 已核对服务器 Host Key 指纹
- [ ] 手机设置了锁屏密码或生物识别
- [ ] ConnectBot 没有备份私钥到云端
- [ ] 本地转发固定使用 `127.0.0.1:18000`
- [ ] 不使用时主动断开 SSH 隧道

## 参考资料

- [ConnectBot 官方网站](https://connectbot.org/)
- [ConnectBot 源代码](https://github.com/connectbot/connectbot)
- [ConnectBot 隐私与后台连接说明](https://connectbot.org/privacy/)
- [Termux 官方网站](https://termux.dev/)
- [W3C Secure Contexts](https://www.w3.org/TR/secure-contexts/)
- [Android 后台电池优化](https://developer.android.com/topic/performance/background-optimization)

