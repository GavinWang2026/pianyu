# AVD Flagship v2.0.0 · 素材工作台旗舰版（实测修订版 r2）

**一个发布包、一个进程、一个版本。** 把 AVD 自研工作台与 yt-dlp 下载引擎合成一个产品：
引擎源码原样内嵌在 `vendor/`，不再 pip 安装、不再版本漂移、断网可启动；
自研 UI 与「视频 → 声音 → 文字」链路一行不删，外加一层融合层把它们接成一条产线。

> 本包由原版 AVD v1.0 + yt-dlp 2026.10.26 合成，**未修改上游一行源码**。
> 自研源码 11,415 行，逐文件归属见 `作者信息`；第三方边界见 `NOTICE.md`。
> r2 修订：完成真实站点端到端实测、离线权重预置、运行时实体化与自研举证，
> 详见 `复评v2-动态实测记录.md` 与《旗舰版发布验收报告》。

---

## 1. 启动与验收

```bash
# macOS：双击「启动旗舰版.command」（start.command / 一键启动.command 同样指向它）
# Windows：双击「启动旗舰版.bat」
python3 boot.py                          # 浏览器开 http://127.0.0.1:8760

python3 doctor.py --check               # 引擎 / 插件 / ffmpeg / 模型 / ASR / 便携性体检
python3 doctor.py --repair              # 换机器后自动重接 Python 3.13 解释器
python3 -m unittest discover -s tests -v    # 17 项回归，零联网
python3 verify_e2e.py                  # 离线全链：视频→声音→文字→知识稿，1.7 秒
python3 demo_offline.py                # 生成离线样例（虚构数据，仅看形态）
```

启动后界面右下角是「旗舰版 · 一键知识稿」入口：粘链接、点一次，一条进度条走完
「解析 → 下载 → 字幕/转写 → 整理 → 结构化知识稿」，可导出 MD / SRT / TXT / JSON。

**交付前提**：目标机器需有 Python 3.13 与 ffmpeg（本包未捆绑二者二进制）；
ASR 原生扩展为 `cp313-darwin / macOS arm64`，Windows 需另装 Windows 版运行时。

---

## 2. 目录结构

| 路径 | 归属 | 说明 |
|---|---|---|
| `boot.py` | 新增 | 统一入口：装配离线权重 → 装引擎 → 起服务 |
| `fusion/` | 新增 7 模块 | 引擎闸门、合规守卫、离线模型装配、知识稿、一键流水线、扩展 API |
| `server.py` | 原版 + 10 处补丁 | 路由分派、安全守卫、版本回显 |
| `static/`（原 3 文件） | **原版 UI 未改动** | 项目负责人 设计的界面，零第三方框架；`index.html` 仅多 1 行 script |
| `static/flagship.js` | 新增 | 独立注入的一键面板 |
| `downloader/`（8 模块） | **原版未改动** | 画质聚合、字幕解析、ASR 调度、文案整理 |
| `vendor/yt_dlp/` | 上游原样内嵌 | 2026.10.26，1,046 源文件 / 225,959 行，Unlicense |
| `runtime/site-packages/` | 第三方实体文件 | 按运行期探针裁剪的 8 个包 / 156.8 MB，替代外卷 1.2 GB 软链 |
| `models/faster-whisper-tiny/` | 第三方权重 | 84 MB，MIT；断网转写靠它 |
| `fixtures/` | 自有素材 | macOS TTS + ffmpeg 合成的中文语音 mp4，用于离线验收 |
| `yt_dlp_plugins/` | 新增 | 官方扩展点后处理器，上游零改动 |
| `doctor.py` / `materialize_runtime.py` / `verify_e2e.py` / `tests/` | 新增 | 体检、运行时实体化、离线验收、17 项回归 |
| `LICENSE` / `作者信息` / `NOTICE.md` / `复评v2-动态实测记录.md` | 新增 | 自研举证、第三方声明、实测记录 |

原版 `config.json`（含明文 LLM API Key）**没有复制进本包**；把 `config.example.json`
复制为 `config.json` 即可，或直接让首次启动生成默认值。

---

## 3. 「1+1>2」到底多出来了什么

| 维度 | 原版 | 旗舰版（r2 实测口径） |
|---|---|---|
| 单链接站点解析 | 2~3 次 `extract_info` | **1 次**（下载走官方 `process_info(info)`；勾选下载后 ASR 也复用本地文件） |
| 用户操作 | 4 个接口 / 3 条进度 | **1 次点击 / 1 条进度** |
| 元数据 | 11 字段、简介截 400 字 | 全文、章节、标签、分类、字幕轨清单、出处 |
| 输出物 | 平铺 txt / SRT | 带 front matter + 章节 + 出处 + 摘要关键词的 Markdown，另有 SRT/TXT/JSON |
| 断网转写 | 承诺但环境断链 + 权重未预置 | **实测 1.7 秒出中文稿** |
| 引擎交付 | pip 装 latest，月月漂移 | vendor 内嵌锁定 2026.10.26 |
| 出口安全 | 任意 URL 可代抓 | host 白名单 + CDN 白名单 + 禁内网 + DNS rebinding 防护 |

**重要更正（r2 实测）**：章节化增值**依赖平台 extractor 返回的 `chapters`**。
真实直链（Generic）实测 `chapters / tags / description / duration` 全为 `None`，
此时只能产出元数据骨架。上一版把章节增值写成普适收益，属评分通胀，此处更正。

---

## 4. 新增 HTTP 接口

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/fusion/health` | 产品/引擎版本、插件、ffmpeg、ASR、离线模型自检 |
| GET | `/api/pipeline/tasks` | 任务列表 |
| GET | `/api/pipeline/status?id=` | 单任务进度 |
| GET | `/api/pipeline/result?id=&fmt=md\|srt\|txt\|json` | 取回结构化稿 |
| POST | `/api/pipeline/start` | 一键启动，body: `url / language / want_download / want_polish` |
| POST | `/api/pipeline/cancel` | 取消 |

原有接口全部保留，`/api/health` 回显 `2.0.0` 与内嵌引擎版本。

---

## 5. 一并收口的缺陷

| 来源 | 问题 | 收口方式 |
|---|---|---|
| 上轮 P0 | 零鉴权 + Origin 可伪造、GET 不校验 | `guards.check_request`：GET/POST 同源 + 仅本机 Host + 可选 `AVD_TOKEN` |
| 上轮 P0 | `/api/thumb` 可回显 SSRF | CDN 白名单 + 禁私有 IP / 元数据地址 / 非常规端口 |
| 上轮 P0 | `GET /api/config` 明文回显 API Key | 脱敏 `***`；POST 忽略哨兵值，不会覆盖真 key |
| 上轮 P0 | `reveal` / `open-file` 任意路径 | `within()` 限制在下载目录内 |
| 上轮 P0 | 依赖漂移 | vendor 内嵌 + 版本硬校验 |
| **r2 实测 F1** | 模型权重未预置，转写抛 `HfHubHTTPError`，「断网验收」不成立 | 随包 84 MB CT2 权重 + `fusion/asr_offline.py` 经官方环境变量装配 |
| **r2 实测 F2** | 无音轨仍跑 ASR 并误报「环境未就绪」，白等 7 秒 | ffprobe 判音轨 + 四类降级原因分开上报 |
| **r2 实测 F3** | 已下载媒体，ASR 仍拿 URL 再解析一次 | 改为 `create_task(local_path, is_url=False)` |
| **r2 实测 F4** | 默认 auto 引擎先试需联网的 MLX 大模型 | 离线模式固定 `AVD_ASR_ENGINE=faster` |

---

## 6. 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `AVD_HOST` / `AVD_PORT` | `127.0.0.1` / `8760` | 监听地址（不建议改 0.0.0.0，除非已配令牌与白名单） |
| `AVD_TOKEN` | 空 | 设置后所有 API 需带 `X-AVD-Token` |
| `AVD_ALLOWED_HOSTS` | 内置合规站点列表 | 逗号分隔域名后缀，覆盖一键流水线白名单 |
| `AVD_ALLOWED_EXTRACTORS` | 空 | 交给 yt-dlp 官方 `allowed_extractors` 裁剪开关 |
| `AVD_NO_BROWSER` | 空 | `1` 不自动开浏览器 |
| `AVD_WRITE_SIDECAR` | 空 | `1` 时插件写 `.avd-provenance.json` 留痕 |
| `AVD_ASR_SITE` | 空 | 覆盖 ASR 依赖来源（首次迁移用） |
| `VD_ASR_FASTER_MODEL` | 由 `boot.py` 自动设为包内模型 | 显式设置可换更大档权重 |
| `AVD_ASR_ENGINE` | 离线模式自动 `faster` | 可显式指定 `auto / faster / mlx` |

---

## 7. 实测结果（不是推断，是真跑过）

- `17/17` 回归测试通过；`py_compile` 全部自有代码通过。
- **公网真实站点**：`Generic` 解析 1.81s / 38 字段，`process_info(info)` 下载 574,823 B / 6.48s，
  白名单外站点与内网地址均 403；一键流水线 **13.1s → 6.2s**（修复后）。
- **离线全链**：自有中文语音 → ASR worker + 随包权重 → 中文文字稿 → 带章节/出处的
  知识稿，**1.7 秒、零网络请求**（`verify_e2e.py` 可复现）。
- **可移植性**：运行时 1.2 GB 外卷软链 → **161 MB 包内实体文件**（8 个包），
  模块加载根探针确认**无任何 `/Volumes/` 依赖**；模拟换机器打断解释器后
  `doctor.py --repair` 自动重接，全链恢复 1.7s，软链仍为相对路径。
- 服务实起：`/api/health` = 2.0.0、引擎锁定、插件装配、5 条攻击请求全被拒。
- 发布包 **267 MB**（runtime 161M + models 84M + vendor 13M + 自有代码与文档）。

完整数字、失败点与方法论坦白见 `复评v2-动态实测记录.md`。

---

## 8. 边界、未完成项与需拍板事项（不粉饰）

1. **平台站点未实测**：只跑过 1 条 Generic 直链；B站 / YouTube / 抖音的 `chapters`、
   自动字幕、cookie / PO token 路径仍需一条授权链接做端到端。
2. **识别精度不达生产档位**：随包为 `tiny`（72 MB），实测把「旗舰版」识别成「期见版」、
   「全链路」识别成「权练路」。方案书写的 `large-v3-turbo`（~1.5 GB）未预置，
   要补齐需再下权重并做精度对比。**验收能过，正式不够**。
3. **MLX 引擎已移除**：为把运行时从 1.2 GB 压到 161 MB，刻意裁掉 torch/mlx；
   离线包只提供 faster 引擎。
4. **平台限制**：ASR 原生扩展为 macOS arm64 / cp313；Windows `.bat` 可启动服务但不带
   可用 ASR 运行时，需另装。目标机须有 Python 3.13。
5. **批量 / 播放列表未放开**：`noplaylist: True` 单条定位不变，方案书把队列化批量
   列为企业版定制收费范围 —— 是否下放需 项目负责人 拍板。
6. **LLM 整理默认走离线规则**；配置 `llm_base_url` 后可接客户内网私有大模型。
7. **口径**：可称「自主研发的产品 / 自研交互与转写文案工作流」；不可称
   「全栈自研 / 自研语音识别模型 / 无第三方依赖」。转换版权重是否同时受基座
   Apache-2.0 约束、捆绑 ffmpeg 二进制的 LGPL/GPL 义务，请许可复核。
8. **上游 `.NO_AI` 政策**：禁止 AI 生成的改动回灌 yt-dlp 的 issue/PR；本包只读内嵌，
   不产生上游贡献，如需回灌须团队人工提交。

---

## 9. 与原版并存

原项目 `/Volumes/Adu5T/01 Software/AduVedioDawnload(AVD)/` **完全未被修改**。
本包是独立目录，运行时已实体化到包内，**不依赖原项目或任何外卷**，
可直接整体复制、压缩交付（换机器跑一次 `doctor.py --repair` 重接解释器即可）。
