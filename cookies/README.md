# Cookie 放置说明（平台站点解析用）

部分平台（实测：抖音 / TikTok 系）要求浏览器验证挑战生成的 `s_v_web_id`，
服务端直接请求其 Web detail API 会返回 403。旗舰版已接通两条官方 Cookie 通道，
**都不需要修改 yt-dlp 源码**：

| 通道 | 怎么配 | 适合场景 |
|---|---|---|
| 浏览器 Cookie | 界面「设置 → Cookie 来源」选 Chrome / Edge / Firefox / Safari | 开发机、有桌面浏览器 |
| `cookies.txt` | 把 Netscape 格式文件放到本目录，命名为 `cookies.txt` | 交付机、无浏览器、断网验收 |

也可以用环境变量 `AVD_COOKIE_FILE` 指向**发布包内**的另一份 cookie 文件：

```bash
AVD_COOKIE_FILE="/path/to/AVD-Flagship-v2/cookies/custom.txt" python3 boot.py
```

> 安全约束：cookie 文件必须位于发布包目录内（由 `fusion/guards.within()` 强制校验）。
> 指向包外文件会被忽略——防止把本机任意文件当作 Cookie 发到远端造成数据外带。

## cookies.txt 格式（Netscape / curl jar）

```
# Netscape HTTP Cookie File
.douyin.com	TRUE	/	TRUE	1799999999	s_v_web_id	<实际值>
```

字段依次为：`域名  include_subdomains  路径  secure  过期时间(unix秒)  名称  值`，**用 Tab 分隔**。
可用浏览器插件（如 Get cookies.txt LOCALLY）导出，或用 `curl -c cookies.txt` 生成后补齐。

## macOS 特别注意

Safari 的 Cookie 库受系统 TCC 保护。若诊断返回 `COOKIE_PERMISSION`，需在
「系统设置 → 隐私与安全性 → 完全磁盘访问权限」里授权终端 / 启动器，然后重启服务；
或改用 Chrome / Edge / Firefox，或直接用 `cookies.txt`（推荐，最省事）。

## 验证是否生效

```bash
curl -s -X POST http://127.0.0.1:8760/api/platform/diagnose \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://v.douyin.com/<你的链接>/"}'
```

返回 `ready: true` 即可用 `/api/pipeline/start` 一键出稿。
`GET /api/fusion/health` 的 `auth` 段会显示当前 Cookie 来源（**不回显任何 Cookie 值**）。

## 隐私与合规

- 本目录**不要提交到 Git**（`.gitignore` 已排除 `cookies/*.txt`）；
- 只放置处理**自有或已获授权素材**所需的 Cookie；
- 不要把含登录态的 Cookie 附在对外发布包里；交付前清空本目录下所有 `.txt`。
