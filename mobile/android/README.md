# PGF Connector — Android APK

**Purpose**: side-loadable Android app that opens a persistent SSH
tunnel to your workstation, forwards `remote:8000 → phone:8000`, and
presents a single-screen dashboard for connecting to and jumping into
the PowerGrandFather mobile SPA at `http://localhost:8000/m/`.

The visual language is intentionally adapted from the
`powergrandfather-responsive/android-connector` reference: outlined
cards, a deep-blue business palette, Chinese labels, and a
"connect + open" primary button that stays above the fold.

## Requirements

- Android **8.0** (API 26) or newer
- SSH access to the workstation from your phone
- SSH **private key** (OpenSSH format) whose public key is already in
  the target user's `~/.ssh/authorized_keys`
- CSM / PGF running on the workstation:
  ```bash
  ./mobile/scripts/start_with_mobile.sh
  ```

## UI overview (single screen)

```
┌──────────────────────────────────────────┐
│ PowerGrandFather                          │  ← 26sp bold
│ 安全连接器 · 移动端                        │  ← muted subtitle
│                                          │
│ ┌────────────────────────────────────┐  │
│ │ ● PGF 已就绪                       │  │  ← tinted pill (green/amber/red/blue)
│ └────────────────────────────────────┘  │
│  user@host:22 → :8000  · 运行 5m│
│                                          │
│ ┌────────────────────────────────────┐  │
│ │        连接并打开 PGF               │  │  ← primary filled button, 56dp
│ └────────────────────────────────────┘  │
│ ┌────────────┐  ┌────────────┐          │
│ │  打开网页   │  │  断开连接   │          │  ← outlined buttons
│ └────────────┘  └────────────┘          │
│                                          │
│ 快捷入口                                  │
│ ┌────┐┌────┐┌────┐                       │
│ │📋  ││💬  ││🖥  │                       │  ← outlined mini cards (36 grid)
│ │任务 ││对话 ││会话 │                       │
│ └────┘└────┘└────┘                       │
│ ┌────┐┌────┐┌────┐                       │
│ │🔔3│││📊  ││⚙   │                       │  ← Notifications with red badge
│ │通知 ││用量 ││更多 │                       │
│ └────┘└────┘└────┘                       │
│                                          │
│ ╭───── SSH 密钥 ─────────────────────╮  │  ← outlined card, 14dp radius
│ │ [ 粘贴 OpenSSH 格式私钥 ...      ] │  │
│ │ [ 私钥口令（可选）             ⌘ ] │  │
│ │ 仅在本机以 AES-GCM 加密保存         │  │
│ ╰────────────────────────────────────╯  │
│                                          │
│ ╭───── 服务器 ──────────────────────╮  │
│ │ [ SSH 服务器地址 ]                 │  │
│ │ [ SSH 用户名 ]                     │  │
│ │ [ SSH 端口 ][ 服务端 PGF 端口 ]     │  │
│ │ [ 手机本地端口 ]                    │  │
│ │ [ PGF Access Token（可选） ⌘ ]     │  │
│ │ ┌────────────────────────────┐    │  │
│ │ │       保存并连接             │    │  │
│ │ └────────────────────────────┘    │  │
│ ╰────────────────────────────────────╯  │
│                                          │
│ 首次使用：粘贴私钥 → 填服务器 → 点顶部的  │
│ "连接并打开 PGF"。以后打开 App 直接点一次  │
│ 连接按钮即可。                             │
└──────────────────────────────────────────┘
```

**Palette** — mirrors the reference project:

| Token | Light | Dark |
|---|---|---|
| Primary | `#2952CC` | `#8FA9FF` |
| Surface | `#F5F7FB` | `#0F1420` |
| Card fill | `#FFFFFF` | `#161C2A` |
| Card stroke | `#D8DEEA` | `#2A3247` |
| Text | `#172033` | `#E4E9F5` |
| Muted text | `#667085` | `#8A93A8` |
| Status ok bg | `#DDF3E6` | `#173627` |
| Status warn bg | `#FDF1D6` | `#3A2C10` |
| Status err bg | `#FBE0E0` | `#3A1717` |

**Design language**:
- **Outlined cards** (`stroke=1dp`, `elevation=0dp`, `radius=12-14dp`) —
  clean business look, no elevated tinted cards.
- **Rounded pill** for status label — colored fill changes with state.
- **Big primary button** (56dp, 12dp corners) always above the fold.
- **Chinese labels** throughout.
- Follows system light/dark mode via a full night palette.

## Install

```bash
# Build
cd mobile/android
/opt/gradle/gradle-7.5/bin/gradle assembleDebug --no-daemon
# → app-debug.apk at app/build/outputs/apk/debug/  (~14 MB)

# Deploy
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

Package id: `com.csm.mobile.debug` · Label: **PGF Connector** · minSdk 26 · targetSdk 34.

## Usage

1. Launch **PGF Connector** from the app drawer.
2. Grant the notification permission when asked (needed for the
   foreground service pill).
3. Fill the config cards near the bottom:
   - **SSH 密钥**: paste the OpenSSH private key (optionally passphrase)
   - **服务器**: host / user / port / remote port / local port /
     optional Access Token
   - Tap **保存并连接** — status pill flips to "PGF 已就绪" once the
     tunnel is up
4. Tap **连接并打开 PGF** at the top → full-screen WebView loads `/m/`.
5. Or tap any shortcut mini-card → WebView opens at
   `/m/missions`, `/m/chat`, `/m/sessions`, `/m/notifications`,
   `/m/tokens`, `/m/more`.
6. Back gesture from the WebView returns to the dashboard.
7. Any time: **断开连接** stops the foreground tunnel service.

The dashboard also polls `/api/notifications/unread-summary` every 60s
through the tunnel and paints a red badge on the **通知** card.

If your CSM backend has `settings.access_token` set, the token is
appended to the first WebView load (`?token=...`) so the SPA's client
can persist it as a cookie; subsequent navigation keeps the URL clean.

## Architecture

```
┌──────────────────────────────────────────────────┐
│ MainActivity  (permission + route)               │
│         │                                        │
│         └── DashboardActivity  (single screen)   │
│                     ├── config form (inline)     │
│                     ├── ConfigStore save+load    │
│                     ├── shortcuts → WebView      │
│                     ├── owns SshTunnelService    │
│                     └── observes TunnelStateBus  │
│                                                  │
│ SshTunnelService  (foreground)                   │
│         ├── sshj SSHClient (private-key auth)    │
│         ├── LocalPortForwarder :8000 → ws:8000   │
│         ├── keepAlive 30s + WakeLock 12h cap     │
│         └── reconnect exp backoff (2s → 30s)     │
│                                                  │
│ WebViewActivity                                  │
│         ├── EXTRA_PATH = deep link (/missions …) │
│         ├── first load appends ?token=xxx        │
│         │   (SPA client persists to cookie)      │
│         ├── swipe-to-refresh                     │
│         └── observes TunnelStateBus for pill     │
└──────────────────────────────────────────────────┘
```

## Files

```
mobile/android/app/src/main/
├── kotlin/com/csm/mobile/
│   ├── App.kt                     (Application + notification channel)
│   ├── ConfigStore.kt             (EncryptedSharedPreferences, now with accessToken)
│   ├── TunnelState.kt             (enum + LiveData bus)
│   ├── SshTunnelService.kt        (sshj foreground service)
│   ├── MainActivity.kt            (permission + route to Dashboard)
│   ├── DashboardActivity.kt       (single-screen UI, inline config, shortcuts)
│   └── WebViewActivity.kt         (SPA host with deep link + token bootstrap)
├── res/
│   ├── values/{colors,themes,strings}.xml       (PGF palette + M3 outlined styles + Chinese)
│   ├── values-night/colors.xml                  (dark palette)
│   ├── layout/activity_dashboard.xml            (ScrollView + outlined cards)
│   ├── layout/activity_web_view.xml             (WebView + status pill + swipe refresh)
│   ├── layout/item_shortcut.xml                 (outlined mini card)
│   ├── drawable/ic_{missions,chat,sessions,
│   │              notifications,tokens,more,
│   │              open_in_new,settings,link,
│   │              refresh,stop}.xml             (vector icons)
│   ├── drawable/status_background.xml           (pill bg, tinted at runtime)
│   ├── drawable/ic_launcher_foreground.xml      (PGF-style "P" logo)
│   └── ...
```

The old `SshConfigActivity` / `activity_ssh_config.xml` were removed in
this pass — configuration now lives inline on the dashboard.

## What's out of scope (MVP)

- **Known-hosts verification**: still uses `PromiscuousVerifier`. The
  reference project pins a SHA256 host key; adding that here is a
  natural follow-up.
- **On-device key generation**: user still pastes an existing OpenSSH
  private key. Generating an RSA/Ed25519 keypair on the phone is a
  reference-project feature that would be a nice add.
- **Multiple hosts / profiles**: one profile total, editing overwrites.
- **Password auth**: private key only.
- **Extra port forwards**: only the CSM `local → remote` mapping.
- **APK signing for release**: `release` build uses the debug key.
- **Real push notifications**: badge polls the tunnel every 60s.
- **Doze whitelisting UX**: users on MIUI/ColorOS should manually
  whitelist PGF Connector in system battery settings.
- **Automated UI tests**: relies on manual verification.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Status stuck on "正在连接" | SSH auth failed, host unreachable, wrong port | Check `adb logcat --pid=$(adb shell pidof com.csm.mobile.debug)` — sshj logs the exception |
| Snackbar "隧道未就绪" when tapping a shortcut | Tunnel not connected yet | Wait for pill to turn green |
| WebView blank / `ERR_CONNECTION_REFUSED` | Tunnel dropped between navigation | Pull-to-refresh; pill will reflect state |
| Badge on 通知 always 0 | 60s poll not yet fired, or CSM has no unread items | Wait a minute, or verify CSM has unread notifications |
| Service dies after minutes | Battery optimizer killed the process | Whitelist PGF Connector in system battery settings |
| WebView shows "invalid or missing CSM access token" | Backend has token set, form was empty | Fill the "PGF Access Token" field and 保存并连接 again |

## Building without wrapper

The wrapper generation hung during initial scaffolding, so this project
ships without `gradlew`. Use the system gradle:

```bash
/opt/gradle/gradle-7.5/bin/gradle assembleDebug --no-daemon
```

Requirements: JDK 17, Gradle 7.5, Android SDK 34 + build-tools 34.0.0,
Kotlin 1.8.22, AGP 7.4.2.

## Security notes

- Private key + Access Token never leave the phone in plaintext
  (`EncryptedSharedPreferences`).
- App backup is disabled to prevent key exfiltration via `adb backup`.
- `usesCleartextTraffic="true"` is required for the loopback WebView
  target. Only `localhost` is allowed; the tunnel itself is SSH-encrypted.
- Debug APK is debug-signed — the signature is a shared, well-known key.
  Fine for personal side-load, unacceptable for public distribution.
