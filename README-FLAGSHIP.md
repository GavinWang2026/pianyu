# 片语 v2.0.0 · 完整工作台

> [README](README.md) 讲的是「怎么用」；这一份讲「完整工作台跑起来是什么样、有哪些开关、边界在哪」。

**一个进程、一个版本。** 片语自研工作台 + 原样内嵌的 yt-dlp 2026.10.26，通过融合层接成
「一次解析、全链复用」的产线。第三方边界见 `NOTICE.md` 与 `LICENSE`；能力边界见 `CAPABILITIES.md`。

---

## 1. 启动与自检

**运行环境：Python 3.11+**（yt-dlp 2026.10.26 已放弃 3.10；系统自带 3.9 会直接拒绝启动）。
本地语音识别另需独立的 3.13 运行时（`.venv-asr`），不影响主服务启动。

```bash
# macOS / Linux：python3 boot.py
# Windows：python boot.py；若 ASR 未就绪需自备 Win 版 .venv-asr 运行时
python3 boot.py                            # 浏览器开 http://127.0.0.1:8760

python3 doctor.py --check                  # 引擎/插件/ffmpeg/模型/ASR/可移植性体检
python3 doctor.py --repair                 # macOS 换机器后自动重接 Python
python3 -m unittest discover -s tests -v   # 回归，零联网
```

启动后界面右下角有个闪电按钮「旗舰版 · 一键知识稿」：粘链接、点一次，一条进度条走完
「解析 → 下载 → 字幕/转写 → 整理 → 结构化知识稿」，可导出 MD / SRT / TXT / JSON / DOCX / PDF。

---

## 2. 目录要点

| 路径 | 说明 |
|---|---|
| `fusion/` | 融合层：流水线、导出、素材库、质量、守卫 |
| `downloader/` | 下载 / ASR / 整理的主逻辑 |
| `static/` | 前端（`style.css` + `flagship.css` / `flagship.js`） |
| `vendor/yt_dlp` | 内嵌锁定引擎（不进仓库，由 `boot.py` 准备） |
| `models/` | 离线 ASR 权重（不进仓库，需自行放置） |
| `cookies/` | Cookie 模板与导出指引（不含真实凭据） |
| `tests/` | 单元测试 |

平台支持矩阵见 `PLATFORM_SUPPORT.md`。

---

## 3. Cookie（需登录的站点）

见 `cookies/README.md` 与 `cookies/导出指引.md`。体检：

```bash
python3 cookies/cookie_doctor.py --probe
```

诊断接口：

```bash
curl -s -X POST http://127.0.0.1:8760/api/platform/diagnose \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://v.douyin.com/<你的链接>/"}'
```

> 只读取浏览器已有的登录状态，不绕过任何付费限制。

---

## 4. 环境变量

> 变量名沿用历史 `AVD_` 前缀。产品名已经是「片语」，但这些名字没改——改名会让老用户的
> 启动脚本全部失效，收益不值得。

| 变量 | 默认 | 说明 |
|---|---|---|
| `AVD_HOST` / `AVD_PORT` | `127.0.0.1` / `8760` | 监听地址（不建议改 `0.0.0.0`） |
| `AVD_TOKEN` | 空 | 设置后所有 API 需带 `X-AVD-Token` |
| `AVD_ALLOWED_HOSTS` | 内置合规站点列表 | 逗号分隔域名后缀 |
| `AVD_COOKIE_FILE` | `cookies/cookies.txt` | 包内 Netscape cookie；包外路径会被忽略 |
| `AVD_LLM_ALLOW_HOSTS` / `AVD_LLM_ALLOW_PORTS` | 空 / 常见本地端口 | 显式放行内网自建模型与端口 |
| `AVD_NO_BROWSER` | 空 | 不开浏览器 |
| `VD_ASR_FASTER_MODEL` | 包内最大档 | 可切到 tiny 等 |
| `AVD_ASR_ENGINE` | 离线模式自动 `faster` | `auto` / `faster` |
| `AVD_UPLOAD_QUOTA_BYTES` | `21474836480`（20 GiB） | `.uploads` 目录配额；超限返回 413 并提示清理 |

### 上传预检

拖入本地文件在落盘前依次检查：单文件大小（4 GiB）→ 扩展名白名单（常见音视频）→
`Content-Type` 大类 → `.uploads` 配额 → 目标磁盘余量（保留 512 MiB）。任一不满足直接
返回 413 / 415，不会跑到转写阶段才失败。流式落盘，不把整个文件读进内存。

### 威胁模型（请按此理解 `AVD_TOKEN`）

片语是**单机本地工具**，不是多用户 Web 服务：

- 服务只监听回环地址，拒绝非 `127.0.0.1 / localhost / ::1` 的 Host；
- 默认无 Token —— 同机进程本来就能访问 `127.0.0.1:8760`，Token 不是权限边界；
- 浏览器跨站请求必须携带 `Origin`，会被同源校验拦截；命令行 / 脚本不带 `Origin`，因此放行；
- 设置 `AVD_TOKEN` 后，所有 API 需带 `X-AVD-Token`，比较使用 `hmac.compare_digest`；
- 若确需开放到局域网（不建议），必须同时启用 Token、Origin 校验与前置限流。

---

## 5. 边界（不粉饰）

1. 需登录的站点（如部分抖音链路）依赖本机可用的 Cookie；没有凭据时 `diagnose` 会明确提示。
2. Windows 真机需自备 Win 版 ASR 依赖后自行验证；默认以 macOS arm64 为准。
3. 说话人分离：未装 pyannote 时只是停顿启发式，证据不足时不标 A / B。
4. 可以说「自研的产品、自研的交互与转写文案工作流」；不宜说「全栈自研」「自研语音识别模型」
   「零第三方依赖」。
5. 内嵌的 yt-dlp 只读引用，不向上游提交改动（上游有 `.NO_AI` 政策）。
