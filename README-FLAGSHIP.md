# AVD Flagship v2.0.0 · 素材工作台旗舰版（r3 · P0–P2 闭环版）

**一个发布包、一个进程、一个版本。** AVD 自研工作台 + 原样内嵌的 yt-dlp 2026.10.26，
通过一层融合层接成「一次解析、全链复用」的产线。上游一行未改，项目负责人 设计的 UI
与「视频 → 声音 → 文字」编排原封不动。

> 当前准确口径见 **`AVD-Flagship-v2_缺陷闭环复评v3.html`** 与
> `内部闭环记录（未入库）`；上一版验收报告与初版说明均已标注为「由 r3 修订取代」。
> 自研源码 **12,482 行**（A 5,500 / B 3,643 / D 3,339），逐文件归属见 `作者信息`；
> 第三方边界见 `NOTICE.md` 与 `SCA-供应链清单.md`。

---

## 1. 启动与验收

```bash
# macOS：双击「启动旗舰版.command」（start.command / 一键启动.command 同指向）
# Windows：双击「启动旗舰版.bat」；若 ASR 未就绪先跑 setup-windows-runtime.bat
python3 boot.py                             # 浏览器开 http://127.0.0.1:8760

python3 doctor.py --check                   # 引擎/插件/ffmpeg/模型/ASR/便携性体检
python3 doctor.py --repair                  # macOS 换机器后自动重接 Python 3.13
python3 -m unittest discover -s tests -v   # 36 项回归，零联网
python3 verify_e2e.py                      # 离线全链：视频→声音→文字→知识稿，6.9 秒
python3 sca_report.py                      # 重新生成离线 SBOM
python3 demo_offline.py                    # 离线样例（虚构数据，仅看形态）
```

启动后界面右下角「旗舰版 · 一键知识稿」：粘链接、点一次，一条进度条走完
「解析 → 下载 → 字幕/转写 → 整理 → 结构化知识稿」，可导出 MD / SRT / TXT / JSON。

**运行环境（版本分层）**：主服务 Python ≥3.11；本地 ASR 需独立 Python 3.13 运行时
（随包 `cp313` 扩展）；ffmpeg 由系统 PATH 提供，本包未捆绑。详见 `PLATFORM_SUPPORT.md`。

---

## 2. 目录结构（r3）

| 路径 | 归属 | 说明 |
|---|---|---|
| `boot.py` | 新增 | 统一入口：版本闸门 → 装配离线权重 → 装引擎 → 起服务 |
| `fusion/` 10 模块 | 新增 | 引擎闸门、合规守卫、**加固层**、**引擎参数契约**、**平台诊断**、离线模型装配、知识稿、一键流水线、扩展 API |
| `server.py` | 原版 + 19 处补丁 | 路由分派、安全守卫、HEAD 语义、请求体/缓存/并发上限、配置校验 |
| `downloader/` 8 模块 | 5 个未改 + 3 个精确补丁 | `asr.py`（子进程回收）、`polish.py`（LLM 出口）、`tasks.py`（锁内快照） |
| `static/` 原 3 文件 | UI 未改动 | 项目负责人 设计的纯原生界面；`index.html` 仅多 1 行 script |
| `vendor/yt_dlp/` | 上游原样内嵌 | 1,043 个 .py / 225,959 行，Unlicense |
| `runtime/site-packages/` | 第三方实体文件 | 8 包 / 156.8 MB，替代外卷 1.2 GB 软链 |
| `models/` | 第三方权重 2 档 | **large-v3-turbo 1.5 GB（默认，MIT）** + tiny 84 MB（轻量备用） |
| `LICENSES/` | 许可全文 | MIT / Apache-2.0 原文 + 来源与变更说明 |
| `cookies/` | 说明文档 | Netscape cookie 放置方法；`*.txt` 已排除出 git |
| `.github/workflows/ci.yml` / `ruff.toml` / `mypy.ini` | 新增 | CI 静态门禁（源码仓库不含二进制资产，故只跑编译/lint/单测/SBOM/HTML） |
| `tests/` | 新增 | `test_fusion.py` 17 项 + `test_hardening.py` 19 项 |
| `PLATFORM_SUPPORT.md` / `SCA-供应链清单.md` / `set_git_identity.sh` | 新增 | 平台矩阵、SBOM、仓库主体设置 |

原版 `config.json`（含明文 Key）**没有复制进本包**；用 `config.example.json` 或让首次启动生成默认值。

---

## 3. 「1+1>2」的产物

| 维度 | 原版 | 旗舰版 r3 |
|---|---|---|
| 单链接站点解析 | 2~3 次 `extract_info` | **1 次**（下载走官方 `process_info(info)`，ASR 复用本地文件） |
| 用户操作 | 4 个接口 / 3 条进度 | **1 次点击 / 1 条进度** |
| 元数据 | 11 字段、简介截 400 字 | 全文、章节、标签、分类、字幕轨清单、出处 |
| 输出物 | 平铺 txt / SRT | front matter + 章节 + 出处 + 摘要关键词的 Markdown，另有 SRT/TXT/JSON |
| 断网转写 | 承诺但环境断链 + 权重未预置 | **6.9 秒出中文稿（turbo 权重随包）** |
| 引擎交付 | pip 装 latest，月月漂移 | vendor 内嵌锁定 2026.10.26 |
| 出口安全 | 任意 URL 可代抓 | host/CDN/LLM 三处白名单 + 禁内网 + DNS rebinding 防护 |
| 平台失败反馈 | 英文原文 | **结构化中文诊断 + 恢复动作** |

**更正（不在报告里粉饰）**：章节化增值依赖平台 extractor 返回 `chapters`；
真实 Generic 直链实测 `chapters / tags / description / duration` 全为 `None`，
此时只能产出元数据骨架。

---

## 4. HTTP 接口

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/fusion/health` | 版本、插件、ffmpeg、ASR、模型档位、**Cookie 通道状态** |
| GET | `/api/pipeline/tasks` | 任务列表 |
| GET | `/api/pipeline/status?id=` | 单任务进度（含 `diagnosis` 结构化诊断） |
| GET | `/api/pipeline/result?id=&fmt=md\|srt\|txt\|json` | 取回结构化稿 |
| POST | `/api/pipeline/start` | 一键启动：`url / language / want_download / want_polish` |
| POST | `/api/pipeline/cancel` | 取消 |
| **POST** | **`/api/platform/diagnose`** | **只解析不下载，返回平台需要什么（Cookie / PO Token / 白名单拒绝）** |

原有接口全部保留。示例：

```bash
curl -s -X POST http://127.0.0.1:8760/api/platform/diagnose \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://v.douyin.com/<你的链接>/"}'
```

---

## 5. P0–P2 闭环（r3 做了什么）

**验收报告 §09**：识别精度（turbo 1.5 GB 随包）、Python 版本分层、CI/类型/SBOM 已闭环；
模型许可证据链闭环但许可确认待办；平台链路、Windows、git 主体为部分闭环 / 需输入。

**初版报告 §09**：P1-1 缓存上限、P1-2 并发上限、P1-3 子进程回收、P1-4 请求体上限、
P1-5 LLM 出口 SSRF、P2-1 HEAD 语义、P2-2 上传完整性、P2-4 锁外竞态、P0-5 配置写入校验
**全部修复并实测**；P0-1～P0-4 / P1-6 / P2-3 为 r2 已修，r3 复验未退化。

**r3 实测新发现 7 项**（N1 P0 起）：融合层未继承 Cookie 配置、短链落地地址未二次校验、
平台错误不可操作、Safari 权限错误未分类、包外 cookie 文件外带风险、加固代码自身大小写 bug、
Cookie 失效与缺失混淆。逐条明细见 `内部闭环记录（未入库）`。

---

## 6. 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `AVD_HOST` / `AVD_PORT` | `127.0.0.1` / `8760` | 监听地址（不建议改 0.0.0.0） |
| `AVD_TOKEN` | 空 | 设置后所有 API 需带 `X-AVD-Token` |
| `AVD_ALLOWED_HOSTS` | 内置合规站点列表 | 逗号分隔域名后缀 |
| `AVD_ALLOWED_EXTRACTORS` | 空 | yt-dlp 官方 `allowed_extractors` 裁剪开关 |
| **`AVD_COOKIE_FILE`** | `cookies/cookies.txt` | 包内 Netscape cookie 文件；**包外路径会被忽略** |
| **`AVD_LLM_ALLOW_HOSTS` / `AVD_LLM_ALLOW_PORTS`** | 空 / 常见本地端口 | 显式放行内网自建模型与端口 |
| `AVD_NO_BROWSER` / `AVD_WRITE_SIDECAR` | 空 | 不开浏览器 / 插件写来源留痕 |
| `AVD_ASR_SITE` | 空 | ASR 依赖来源（一次性迁移用） |
| `VD_ASR_FASTER_MODEL` | `boot.py` 自动设为**包内最大档** | 显式设置可切到 tiny 等 |
| `AVD_ASR_ENGINE` | 离线模式自动 `faster` | `auto / faster / mlx` |

---

## 7. 实测结果（真跑过，不是推断）

- **36 / 36** 单测通过；全部自有代码 `py_compile` 通过。
- **抖音真实短链**：短链 302 落地、Web detail API 403、定位到 `s_v_web_id` 缺失；
  Safari Cookie 通道被 macOS TCC 拒绝；cookiefile 通道实证贯通；
  `/api/platform/diagnose` 1.5s 返回 `COOKIE_REQUIRED` + 4 条恢复动作。
- **turbo 权重**：1.5 GB MIT 随包，离线转写 6.4s；tiny「期见版」→ turbo「旗舰版」。
- **离线全链** `verify_e2e.py`：6.9 秒、零网络请求，产出带章节与出处的中文知识稿。
- **HTTP 加固层**：HEAD 三条路由 body=0B；JSON 超限 413；配置/LLM 出口 403；
  本地 Ollama 放行；原 4 条 P0 守卫全 403。
- **可移植性**：运行时 161 MB 包内实体文件、零 `/Volumes/` 依赖、相对软链；
  换机器断链 → `--repair` 自动重接 → 全链恢复。
- 发布包 **1.8 GB / 3,240 文件**（models 1.6G 是大模型权重）。

---

## 8. 边界、未完成项与需拍板（不粉饰）

1. **抖音完整出稿仍缺新鲜 Cookie**：本机无有效浏览器 Cookie，
   拿到后先用 `/api/platform/diagnose` 复验，再跑一键流水线。B站 / YouTube 完全未测。
2. **Windows 真机未验收**：脚本与跨平台分支齐备，但本机 macOS，不能写成「已交付」。
3. **在线 CVE 扫描未做**：只有离线 SBOM 与许可证据链；大客户核查前需跑 Trivy / Grype。
4. **许可书面签字待办**：模型许可按最保守口径同时履行 MIT + Apache-2.0 义务，结论不由本包代作。
5. **git 提交主体仍是占位**：项目负责人 提供真实仓库主体后执行 `set_git_identity.sh`。
6. **turbo 仍有错字**：一段语音的对比观察，不是字准率测量，不承诺正式零错。
7. **MLX 引擎已移除**：为压缩体积刻意裁掉 torch/mlx，离线包只提供 faster。
8. **批量 / 播放列表未放开**：`noplaylist: True` 单条定位不变，是否下放需 项目负责人 拍板。
9. **口径**：可称「自主研发的产品 / 自研交互与转写文案工作流」；不可称
   「全栈自研 / 自研语音识别模型 / 无第三方依赖」。
10. **上游 `.NO_AI` 政策**：禁止 AI 生成的改动回灌 issue/PR；本包只读内嵌，不产生上游贡献。

---

## 9. 与原版并存

原项目 `/Volumes/Adu5T/01 Software/AduVedioDawnload(AVD)/` **完全未被修改**。
本包独立目录、运行时实体化在包内，可直接整体复制交付
（macOS 换机器跑一次 `doctor.py --repair` 重接解释器即可）。
