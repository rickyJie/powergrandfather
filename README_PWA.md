# PowerGrandFather 移动端 PWA 安装指南

这份指南用于把 PowerGrandFather 安装在一台电脑或服务器上，然后从手机访问并安装到主屏幕。

> PWA 安装在手机上的是 Web 应用入口，不会把 Python 后端、SQLite 或 Claude/Codex 进程搬到手机里。手机使用实时终端、Sessions、Automation 等功能时，仍需能连接运行 PowerGrandFather 的电脑。

如果 PowerGrandFather 运行在企业云服务器上，并且日常通过 SSH 端口转发访问，请直接阅读：[Android SSH 隧道安装指南](docs/android-ssh-pwa.md)。

## 1. 推荐部署方式

最简单的拓扑是：

```text
手机上的 PWA  ── Wi-Fi / VPN ──>  电脑上的 PowerGrandFather
                                      ├─ FastAPI
                                      ├─ Vue PWA
                                      ├─ SQLite
                                      └─ Claude / Codex 会话
```

推荐条件：

- 手机和电脑在同一个可信 Wi-Fi，或者通过同一个 VPN/Tailscale 网络互通。
- PowerGrandFather 只运行在你控制的电脑上。
- 手机使用 HTTPS 地址访问。
- 局域网部署时设置 `CSM_ACCESS_TOKEN`。

## 2. 环境要求

- Linux 或 macOS
- Python 3.11 以上
- Node.js 20.19 以上，或 Node.js 22.12 以上；推荐 Node.js 22 LTS
- Conda/Miniconda
- OpenSSL
- Claude Code 或 Codex CLI，取决于需要管理的 Agent

检查环境：

```bash
python3 --version
node --version
conda --version
openssl version
```

## 3. 首次安装

以下命令都在仓库根目录执行。

### 3.1 切换到移动端分支

如果分支已经在本机：

```bash
git switch feat/mobile-pwa-mvp
```

如果该分支已推送到远端，但本机还没有：

```bash
git fetch origin
git switch --track origin/feat/mobile-pwa-mvp
```

### 3.2 创建 Python 环境

```bash
conda create -n csm python=3.11 -y
source "$(conda info --base)/etc/profile.d/conda.sh"
conda activate csm
```

已经存在 `csm` 环境时，只需要：

```bash
conda activate csm
```

### 3.3 安装后端并初始化数据库

```bash
pip install -e ".[dev]"
alembic upgrade head
```

如果默认 Python 源不可用：

```bash
pip install -e ".[dev]" -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### 3.4 安装并构建 PWA 前端

```bash
cd frontend
npm ci
npm run build
cd ..
```

构建成功后应存在以下文件：

```text
frontend/dist/index.html
frontend/dist/manifest.webmanifest
frontend/dist/sw.js
```

可以这样检查：

```bash
test -f frontend/dist/index.html && echo "OK: frontend"
test -f frontend/dist/manifest.webmanifest && echo "OK: manifest"
test -f frontend/dist/sw.js && echo "OK: service worker"
```

## 4. 配置手机可用的 HTTPS

PWA 安装、Service Worker 和安全剪贴板依赖 HTTPS。仓库已经提供局域网证书生成脚本。

先查看电脑的局域网 IP：

```bash
# Linux
hostname -I

# macOS，Wi-Fi 通常是 en0
ipconfig getifaddr en0
```

假设电脑 IP 是 `192.168.1.20`，生成包含该 IP 的证书：

```bash
./scripts/gen-cert.sh 192.168.1.20
```

脚本会生成：

```text
secrets/csm-cert.pem    # 可以导入手机的公开证书
secrets/csm-key.pem     # 私钥，只能留在服务器上
```

> 不要把 `csm-key.pem` 发送到手机、提交到 Git 或分享给其他人。

如果电脑 IP 发生变化，需要使用新 IP 重新运行 `gen-cert.sh`，然后重新信任证书。

## 5. 设置局域网访问令牌

PowerGrandFather 能读取文件、创建终端进程并操作 Claude/Codex，会比普通网页拥有更高权限。只要服务监听局域网，就建议设置访问令牌。

生成并导出令牌：

```bash
export CSM_ACCESS_TOKEN="$(openssl rand -hex 24)"
echo "请保存这个令牌：$CSM_ACCESS_TOKEN"
```

令牌必须在启动 PowerGrandFather 的同一个终端中保持有效。不要把真实令牌写进 README 或提交到 Git。

## 6. 启动服务

```bash
./scripts/start.sh
```

检测到 `secrets/csm-cert.pem` 和 `secrets/csm-key.pem` 后，脚本会自动使用 HTTPS，并监听：

```text
https://0.0.0.0:8000
```

`0.0.0.0` 是监听地址，不是手机应输入的地址。手机应打开电脑的真实局域网 IP，例如：

```text
https://192.168.1.20:8000/?token=你的访问令牌
```

第一次使用带 `?token=` 的地址后，服务端会写入安全 Cookie，页面会自动从地址栏移除令牌。以后直接访问：

```text
https://192.168.1.20:8000/
```

停止服务：

```bash
./scripts/stop.sh
```

查看启动错误：

```bash
tail -n 100 csm.log
```

## 7. 在手机上信任局域网证书

仅在浏览器警告页点击“继续访问”不一定能开放所有 PWA 能力。更可靠的方式是把公开证书安装为受信任证书。

只需要把下面这个文件传到手机：

```text
secrets/csm-cert.pem
```

绝对不要传 `secrets/csm-key.pem`。

### iPhone / iPad

1. 通过 AirDrop、iCloud Drive 或其他可信方式把 `csm-cert.pem` 发送到设备。
2. 打开证书文件，允许下载描述文件。
3. 打开“设置 → 通用 → VPN 与设备管理”。
4. 选择刚下载的证书并完成安装。
5. 打开“设置 → 通用 → 关于本机 → 证书信任设置”。
6. 为该证书启用“完全信任”。
7. 使用 Safari 重新打开 PowerGrandFather 的 HTTPS 地址。

### Android

不同厂商的菜单名称略有不同，通常是：

1. 把 `csm-cert.pem` 传到手机。
2. 打开“设置 → 安全 → 加密与凭据”。
3. 选择“安装证书 → CA 证书”。
4. 选择 `csm-cert.pem` 并确认安装。
5. 使用 Chrome 重新打开 PowerGrandFather 的 HTTPS 地址。

如果公司设备禁止安装用户 CA，请让管理员提供受信任证书，或通过已有的可信 HTTPS 反向代理/VPN 域名访问。

## 8. 安装到手机主屏幕

### Android / Chrome

1. 用 Chrome 打开 PowerGrandFather。
2. 等页面完成加载。
3. 点击页面出现的“安装”提示；或者打开 Chrome 菜单。
4. 选择“安装应用”或“添加到主屏幕”。
5. 从桌面打开 `PGF`。

### iPhone / iPad / Safari

1. 必须使用 Safari 打开 PowerGrandFather。
2. 点击底部或顶部的“分享”按钮。
3. 选择“添加到主屏幕”。
4. 确认名称，然后点击“添加”。
5. 从桌面打开 `PGF`。

## 9. 验证安装

### 在服务器上验证

把示例 IP 换成电脑真实 IP；使用自签名证书时，命令行验证可加 `-k`：

```bash
curl -sk https://192.168.1.20:8000/ | grep -q 'id="app"' && echo "OK: SPA"
curl -sk https://192.168.1.20:8000/manifest.webmanifest | grep -q 'PowerGrandFather' && echo "OK: manifest"
curl -skI https://192.168.1.20:8000/sw.js | head
```

启用了访问令牌时，验证 API：

```bash
curl -sk "https://192.168.1.20:8000/api/backends?token=$CSM_ACCESS_TOKEN" \
  -H 'X-CSM-Client: 1'
```

### 在手机上验证

- 可以从主屏幕独立窗口启动 PGF。
- Sessions 列表能够加载。
- 点击会话后可以进入终端详情并返回列表。
- 终端显示“connected”。
- Esc、Ctrl-C、Ctrl-D、方向键和 Paste 能正常使用。
- 手机断开服务器网络后，静态应用外壳可能仍可打开，但实时数据和终端应显示断线。

## 10. 日常启动和升级

日常启动：

```bash
source "$(conda info --base)/etc/profile.d/conda.sh"
conda activate csm
export CSM_ACCESS_TOKEN='你保存的令牌'
./scripts/start.sh
```

更新代码后：

```bash
git pull
conda activate csm
pip install -e ".[dev]"
alembic upgrade head
cd frontend
npm ci
npm run build
cd ..
./scripts/stop.sh
./scripts/start.sh
```

前端发现新 Service Worker 后会显示“有新版本”。建议先结束或暂停正在操作的终端，再点击“刷新更新”，避免刷新页面影响当前输入。

## 11. 常见问题

### 手机打不开服务器

- 确认手机和电脑在同一个网络，或者 VPN 路由互通。
- 地址中使用电脑真实 IP，不要使用 `localhost` 或 `0.0.0.0`。
- 确认服务正在监听 `0.0.0.0:8000`。
- 检查电脑防火墙是否允许 TCP 8000 端口。
- 运行 `tail -n 100 csm.log` 查看服务是否启动失败。

### 页面能打开，但没有“安装应用”

- 确认使用 `https://`，而不是局域网 `http://`。
- 确认证书已经在手机上受信任。
- 确认运行过 `npm run build`，且 `frontend/dist/sw.js` 存在。
- iPhone/iPad 不依赖网页安装弹窗，直接使用 Safari 的“分享 → 添加到主屏幕”。
- 已经安装过时，浏览器通常不会再次显示安装提示。

### 显示 401

访问令牌已启用但 Cookie 尚未建立。重新打开一次：

```text
https://服务器IP:8000/?token=你的访问令牌
```

### HTTPS 提示证书与地址不匹配

当前 IP 没有写进证书。重新生成：

```bash
./scripts/gen-cert.sh 当前服务器IP
./scripts/stop.sh
./scripts/start.sh
```

然后删除手机上的旧证书，安装并信任新证书。

### 终端一直显示 disconnected

- 确认手机到服务器的网络没有断开。
- 如果前面还有反向代理，确认它支持 WebSocket 升级。
- HTTPS 页面必须连接 `wss://`，不能被代理改回 `ws://`。
- 查看 `csm.log` 中是否有会话或 WebSocket 错误。

### 手机仍显示旧界面

- 等待页面出现“有新版本”，然后点击“刷新更新”。
- 完全关闭桌面上的 PGF 后重新打开。
- 仍未更新时，在浏览器网站设置中清理该站点缓存，再重新访问和安装。

## 12. 安全提醒

- 这是单用户控制台，不是多租户系统。
- 不要直接暴露到公网。
- 不要在酒店、机场等不可信 Wi-Fi 上以 `0.0.0.0` 无令牌运行。
- 建议设置 `CSM_ACCESS_TOKEN`。
- `secrets/csm-key.pem` 永远只能保留在服务器。
- 如需跨网络访问，优先使用受控 VPN；若使用公网 HTTPS 反向代理，必须同时配置访问令牌和访问控制。
- PWA 的离线缓存只包含静态界面，不包含 API 数据、终端输出或认证信息；实时功能始终需要连接服务器。
