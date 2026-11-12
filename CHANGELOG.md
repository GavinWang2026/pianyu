# Changelog

## v2.0.0 · 旗舰版（2026-11-12）

从「两个独立程序」合成为「一个发布包、一个进程、一个版本」。

### 合成与装配
- 新增 `boot.py` 统一入口；`start.command` / `一键启动.command` / `start.bat` / `一键启动.bat`
  全部统一指向旗舰版，双击任何一个都是同一个产品。
- yt-dlp 2026.10.26 源码包**原样内嵌** `vendor/yt_dlp/`（1,048 个干净源文件 / 13 MB，Unlicense），
  未 fork、未裁剪、未改动一行；附上游 LICENSE / README / Changelog 作溯源。
- 新增 `fusion/boot_check.py`：vendor 优先注入 sys.path、清除系统 pip 版、
  硬校验版本与文件来源；启动不再联网、不再 pip install、不再重建 venv。
- 新增 `yt_dlp_plugins/` 官方命名空间包与 `AVDProvenancePP` 后处理器，
  证明「上游零改动定制」，nightly 升级可自动继承。
- 新增 `VERSION` / `requirements.lock` / `NOTICE.md`；原版含明文密钥的 config.json 未复制，
  改附 `config.example.json`。

### 1+1>2 融合层
- 新增 `fusion/pipeline.py`：**一次 extract_info，全链复用**。下载走官方
  `process_info(info)`、字幕 URL 直接取 `info`、无字幕才转 ASR；四步接口并成一次点击、
  一条进度；产物落盘到 `outputs/pipeline/<id>/`。
- 新增 `fusion/knowledge.py`：保留全文简介 / 章节 / 标签 / 分类 / 字幕轨 / 出处，
  产出带 YAML front matter、章节标题、摘要关键词、来源可信度说明的 Markdown，
  另有 SRT / TXT / JSON。可选按 `info` 内离线段数据剔除口播（不联网）。
- 新增 `fusion/api.py` 与 6 个扩展接口；`server.py` 只加 10 处精确补丁，
  原版 700+ 行路由逻辑一行未删。
- 新增 `static/flagship.js` 一键面板；`static/index.html` 仅多 1 行 script，
  您设计的 3,723 行 UI 完全不动。

### 安全与工程质量
- 新增 `fusion/guards.py`：GET/POST 同源校验 + 仅本机 Host + 可选令牌；
  图片代理 CDN 白名单；禁内网 / 元数据地址 / 非常规端口 / DNS rebinding；
  配置脱敏（POST 忽略 `***` 哨兵防覆盖）；`reveal` / `open-file` 路径越界拦截。
- 新增 `doctor.py`：环境体检与**离线修复** ASR 解释器断链 —— 用影子虚拟环境
  重接 Python 3.13 解释器 + 软链已装依赖，零联网、零重装、零拷贝 1.2 GB。
- 新增 `tests/test_fusion.py`：**14 项回归测试**（原版零测试），含
  「全链路只解析一次」的硬断言。

### 验证
14/14 测试通过；服务实起后 `/api/health` = 2.0.0、引擎锁定 2026.10.26、插件已装配、
ASR 双引擎就绪、ffmpeg 在位；5 条攻击请求均被拒。发布包 18 MB。

### 保持不动（刻意）
上游 `vendor/yt_dlp/` 全部源码；AVD 自研 `static/` 三文件与 `downloader/` 八模块；
单条下载定位（`noplaylist: True`）；原项目目录。

---

## v2.0.0-r2 · 动态实测修订（2026-11-12）

版本号不变，内容按「1 → 2 → 3」验收序列补齐。全部改动有实测依据，
详见 `复评v2-动态实测记录.md`。

### 真实链路实测暴露并修复的 4 个缺陷
- **F1 (P0)**：模型权重未随包、首次转写抛 `HfHubHTTPError`，方案书「断网当场验收」
  不成立 → 预置 `models/faster-whisper-tiny/`（84 MB, MIT），新增
  `fusion/asr_offline.py` 通过官方 `VD_ASR_FASTER_MODEL` 装配，上游零改动。
- **F2 (P1)**：无音轨素材仍跑 ASR 并误报「环境未就绪」→ `pipeline.has_audio_stream()`
  ffprobe 判音轨，四类降级原因分开上报。
- **F3 (P1)**：已下载媒体但 ASR 仍拿 URL 再解析一次 → 改为 `is_url=False` 复用本地文件，
  补上「一次解析全链复用」最后一环。
- **F4 (P2)**：默认 auto 引擎先试需联网的 MLX 大模型 → 离线模式固定 faster。
- 效果：公网一键流水线 13.1s → **6.2s**；离线全链从不可用 → **1.7s 出中文稿**；
  回归测试 14 → **17 项**。

### 便携包（解除外卷依赖）
- 新增 `materialize_runtime.py`：按**运行期导入探针**把 1.2 GB 依赖精确裁成
  8 个包 / 156.8 MB 实体文件；`torch`(568M) / `mlx`(211M) / `llvmlite`(125M) 等未进包。
- `.venv-asr/.../site-packages` 改为**相对软链** `../../../runtime/site-packages`，
  整包可移动；模块加载根探针确认零 `/Volumes/` 依赖。
- `doctor.py` 改造：包内运行时优先、外卷仅作一次性迁移兜底、检测到解释器断链
  自动重接；`--check` 新增 `portable` / `models` 报告。
- 模拟换机器断链 → `--repair` 重接 → 全链 1.7s 恢复，已实测通过。
- 发布包 18 MB → **267 MB**（换来真正的断网、拔卷、换机器可用）。

### 自研举证
- 新增 `LICENSE`（专有，明确排除第三方）、`作者信息`（A/B/C/D 四类、
  自研 11,415 行逐类可复核）、`.gitignore`（git 仓库只收自研源码）。
- B 类 17 个新增文件统一加 `Copyright (c) 2026 Hyht-Team` 版权头；
  A 类原版文件与 `downloader/` 保持不动，以 `diff` 一致性举证。
- `NOTICE.md` 补齐：yt-dlp Unlicense、8 个运行包各自许可（从 dist-info 实测）、
  模型权重 MIT / 上游基座 Apache-2.0、ffmpeg 与 CPython 未捆绑说明。
- 建立私有 Git 基线，从今日起累积原创提交历史。

### 新增可复现验收
- `verify_e2e.py`：不联网、不启服务，用自有中文语音 fixture 跑完整产线，
  断言 `source_kind=asr` 与正文非空。
- `fixtures/avd_voice_sample.mp4`：macOS TTS + ffmpeg 合成的自有素材。

### 更正（评分通胀修正）
- 章节化知识稿**不是普适收益**：真实 Generic 直链实测 `chapters / tags /
  description / duration` 全为 `None`，该增值依赖平台 extractor。
- 「双引擎可用」更正为「离线包只提供 faster 引擎」；MLX 随裁剪移除。
- tiny 模型精度不足生产档位，已将「断网可用」与「生产精度」拆成两件事如实说明。
