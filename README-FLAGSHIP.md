# AVD Flagship v2.0.0 · 素材工作台旗舰版

**一个发布包、一个进程、一个版本。** AVD 自研工作台 + 原样内嵌的 yt-dlp 2026.10.26，
通过融合层接成「一次解析、全链复用」的产线。第三方边界见 `NOTICE.md` 与 `LICENSE`；
能力边界见 `CAPABILITIES.md`。

---

## 1. 启动与验收

**运行环境：Python 3.11+**（yt-dlp 2026.10.26 已放弃 3.10；系统自带 3.9 会直接拒绝启动）。
本地语音识别另需随包的 3.13 独立运行时（`.venv-asr`），不影响主服务启动。

```bash
# macOS：双击「启动旗舰版.command」（start.command / 一键启动.command 同指向）
# Windows：双击「启动旗舰版.bat」；若 ASR 未就绪先跑 setup-windows-runtime.bat
python3 boot.py                             # 浏览器开 http://127.0.0.1:8760

python3 doctor.py --check                   # 引擎/插件/ffmpeg/模型/ASR/便携性体检
python3 doctor.py --repair                  # macOS 换机器后自动重接 Python
python3 -m unittest discover -s tests -v   # 回归，零联网
python3 verify_smoke.py                    # 轻量冒烟（无需 ASR runtime）
python3 verify_e2e_synth.py                # 合成字幕轻量 E2E（无 ASR；成册+工单）
python3 verify_resume_synth.py             # R4 合成续跑实证（检查点 / 崩溃 / 工单 local_only）
python3 verify_windows_paths.py            # Windows 交付路径冒烟（不要求在 Win 上跑）
python3 verify_e2e.py                      # 离线全链：视频→声音→文字→知识稿（需 fixture + runtime）
python3 demo_offline.py                    # 离线样例（虚构数据，仅看形态）
```

启动后界面右下角「旗舰版 · 一键知识稿」：粘链接、点一次，一条进度条走完
「解析 → 下载 → 字幕/转写 → 整理 → 结构化知识稿」，可导出 MD / SRT / TXT / JSON / DOCX / PDF。

---

## 2. 目录要点

| 路径 | 说明 |
|---|---|
| `fusion/` | 旗舰融合层：流水线、导出、素材库、质量、守卫 |
| `downloader/` | 原版工作台逻辑（下载 / ASR / 整理） |
| `static/` | 前端（`style.css` + `flagship.css` / `flagship.js`） |
| `vendor/yt_dlp` | 内嵌锁定引擎 |
| `models/` | 离线 ASR 权重（turbo / tiny） |
| `cookies/` | Cookie 模板与导出指引（不含真实凭据） |
| `examples/` | 样例知识稿形态 |
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

---

## 4. 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `AVD_HOST` / `AVD_PORT` | `127.0.0.1` / `8760` | 监听地址（不建议改 0.0.0.0） |
| `AVD_TOKEN` | 空 | 设置后所有 API 需带 `X-AVD-Token` |
| `AVD_ALLOWED_HOSTS` | 内置合规站点列表 | 逗号分隔域名后缀 |
| `AVD_COOKIE_FILE` | `cookies/cookies.txt` | 包内 Netscape cookie；包外路径会被忽略 |
| `AVD_LLM_ALLOW_HOSTS` / `AVD_LLM_ALLOW_PORTS` | 空 / 常见本地端口 | 显式放行内网自建模型与端口 |
| `AVD_NO_BROWSER` | 空 | 不开浏览器 |
| `VD_ASR_FASTER_MODEL` | 包内最大档 | 可切到 tiny 等 |
| `AVD_ASR_ENGINE` | 离线模式自动 `faster` | `auto / faster` |
| `AVD_UPLOAD_QUOTA_BYTES` | `21474836480`（20 GiB） | `.uploads` 目录配额；超限返回 413 并提示清理 |

### 上传预检（D28）

拖入本地文件在落盘前依次检查：单文件大小（4 GiB）→ 扩展名白名单（常见音视频）→
`Content-Type` 大类 → `.uploads` 配额 → 目标磁盘余量（保留 512 MiB）。任一不满足直接
返回 413 / 415，不会跑到转写阶段才失败。流式落盘，不把整个文件读进内存。

### 威胁模型（请按此理解 `AVD_TOKEN`）

本产品是**单机本地工具**，不是多用户 Web 服务：

- 服务只监听回环地址，拒绝非 `127.0.0.1 / localhost / ::1` 的 Host；
- 默认无 Token —— 同机进程本来就能访问 `127.0.0.1:8760`，Token 不是权限边界；
- 浏览器跨站请求必须携带 `Origin`，会被同源校验拦截；命令行/脚本不带 `Origin`，因此放行；
- 设置 `AVD_TOKEN` 后，所有 API 需带 `X-AVD-Token`，比较使用 `hmac.compare_digest`；
- 若确需开放局域网（不建议），必须同时启用 Token、Origin 校验与前置限流。

---

## 5. 边界（不粉饰）

1. 需登录站点（如部分抖音链路）依赖本机可用 Cookie；无凭据时 diagnose 会明确提示。
2. Windows 真机需按 `setup-windows-runtime.bat` 自验。
3. 说话人分离：未装 pyannote 时仅为停顿启发式，且证据不足不标 A/B。
4. 可称「自主研发的产品 / 自研交互与转写文案工作流」；不可称「全栈自研 / 自研语音识别模型 / 无第三方依赖」。
5. 上游 yt-dlp `.NO_AI` 政策：本包只读内嵌，不产生上游贡献。
