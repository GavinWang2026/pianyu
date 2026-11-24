# 平台支持矩阵与运行环境（P1 修复说明）

针对「Windows ASR 装配」与「目标机 Python 版本分层」：**版本分层**、**Windows 装配脚本**
与指引已补齐；本机为 macOS，**Windows 脚本未做真机验证**，见文末诚实声明。

## 1. 支持矩阵

| 能力 | Python 要求 | macOS (Apple Silicon) | Windows 10/11 x64 | Linux |
|---|---|---|---|---|
| 主服务 + Web UI + 解析下载 | **3.11+** | ✅ 实测 | ⚠️ 脚本齐备、未真机验证 | ⚠️ 未验证 |
| 平台字幕转文字稿 | 3.11+ | ✅ 实测 | ⚠️ 未验证 | ⚠️ 未验证 |
| 本地语音识别（ASR） | **独立 3.13 运行时** | ✅ 实测（faster + 随包 turbo 权重） | ⚠️ 需自备 Win 版 ASR 依赖 | ⚠️ 需自行装配 |
| 离线断网转写 | 同上 | ✅ 实测 6.9s 全链 | ⚠️ 未验证 | ⚠️ 未验证 |

**为什么分两层**：主服务是纯 Python + 标准库 + 内嵌 yt-dlp（也是纯 Python），
3.11 即可；ASR 依赖 `ctranslate2 / av / onnxruntime` 的 **cp313 编译扩展**，
由 `.venv-asr` 单独提供。启动器 `boot.py` 会做版本闸门检查并给出分层提示。

## 2. macOS（已实测）

```bash
python3 boot.py
```

换机器 / 解释器软链失效时：

```bash
python3 doctor.py --check       # 体检（含便携性、模型档位、Cookie 通道）
python3 doctor.py --repair      # 自动重接本机 Python 3.13 解释器
```

`runtime/site-packages/`（161 MB 实体文件）已随包，不依赖外卷；`.venv-asr` 用相对软链。

## 3. Windows（需手动装配，待真机验收）

1. 安装 **Python 3.13 x64**（python.org，勾选 Add to PATH）。
2. 用 `py -3.13` 创建 `.venv-asr`（`Scripts\python.exe` + `Lib\site-packages`），
   并 pip 安装 faster-whisper / ctranslate2 / av / onnxruntime 等 Win 版依赖（首次需联网，仅此一次）；
   `models/` 里的 CT2 权重是**跨平台**的，直接复用，不用重下 1.5 GB。
3. 运行 `python boot.py`。

> **关键限制**：`runtime/site-packages/` 内是 `cp313-darwin` 原生扩展，
> **不能在 Windows 复用**。`doctor.py --repair` 在 Windows 会明确拒绝软链修复并指向上面的装配步骤，
> 不会静默做错事。版本对齐口径见 `runtime/MANIFEST.json` 与 `NOTICE.md` §2.2。

## 4. 平台站点 Cookie（抖音 / TikTok 实测）

抖音真实短链实测返回 `COOKIE_REQUIRED`（Web detail API 403，需要 `s_v_web_id`）。
两条恢复路径见 `cookies/README.md`：

- 界面「设置 → Cookie 来源」选浏览器；
- 或导出 Netscape 格式 `cookies.txt` 放进 `cookies/`。

macOS 选 Safari 还需给终端「完全磁盘访问权限」（实测本机返回
`Operation not permitted`，已做成 `COOKIE_PERMISSION` 诊断）。
r8.2 起读不到时会**自动摘除该 Cookie 来源继续解析**（主页「解析」与融合层
共用同一判断），公开素材不再报错，
并在任务降级项里提示授权路径；`POST /api/platform/diagnose` 会直接告诉你
缺什么、去哪儿补。

## 5. 诚实声明（不要把推断当实测）

- ✅ 实测：macOS + Python 3.13.12、离线全链、turbo 权重、抖音短链解析失败原因与诊断、
  cookiefile 参数贯通、Safari 权限失败；
- ⚠️ **未实测**：任何 Windows 机器上的启动、ASR 转写、ffmpeg 路径；
  Windows 脚本是静态编写 + 语法检查，需在一台 Win 机器上按第 3 节验收后再对外承诺；
- ⚠️ 未实测：3.11 / 3.12 主服务（本机只有 3.13），版本要求来自上游 yt-dlp 声明；
- ⚠️ 未实测：B站 / YouTube 的平台专属链路（需授权链接与相应 Cookie / PO Token）。

## 6. 交付话术建议

- ✅ 可说：「macOS 版已完成离线端到端验收；Windows 版提供装配脚本，需现场验收」；
- ❌ 不要说：「全平台已验证」「Windows 开箱即用」「断网全平台可用」。
