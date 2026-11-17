# Changelog

## v2.0.0-r8 · 第三批升级 · 让「更值钱」成立（2026-11-17）

### 长素材理解
- 新增 `fusion/longform.py`：超长转写（≥8,000 字）先按 ~1,800 字分块 map，
  再 reduce 成总摘要；返回 `coverage`（块数 / 覆盖块数 / 覆盖率 / 字数）与
  `provenance`（每块起止秒）。LLM 不可用或单块失败时逐块回退规则要点，
  不虚报覆盖率；短素材仍走原 `enrich_summary`，行为不变。
- 任务快照新增 `summary_coverage`，`meta` 记录 `summary_kind` /
  `summary_coverage` / `summary_provenance`。

### 质量与术语
- `quality.chapter_quality()`：检查章节时间倒序 / 越界 / 过短 / 空标题 /
  重名 / 覆盖空洞，输出 `{ok, score, issues}`；不达标记 `chapter.quality` 降级。
- `quality.normalize_terms()`：归并 ASCII 术语的大小写与空格/点/连字符变体
  （如 `Ai` → `AI`），要求词边界，避免误改 `AID`；写 `term_normalized` 降级项。

### 内容形态
- `knowledge.render_markdown()` 支持 `template`：`interview` / `course` /
  `podcast` / `meeting`，分别产出访谈提纲、课程大纲与思考题、本期亮点与
  资源时间轴、结论与行动项；无模板时输出逐字节不变。
- `PipelineManager.start/start_batch` 与 `/api/pipeline/start|batch` 透传 `template`。

### 素材库评测
- `library.evaluate(cases)`：对检索结果计算 Recall@K、引用正确率与 MRR，
  新增 `tests/fixtures/library_eval.json` 三条正样本做回归。

### 测试与核算
- 新增 `tests/test_r3_upgrade.py`（13 项）与 `tests/test_frontend_smoke.py`；
  全量 **316 项** 单测通过。
- 口径更新为 **31,921 行 / 81 文件**（A 582 + B 21,103 + D 10,236）。

## v2.0.0-r7 · 第十一轮缺陷修复 · 第二批（2026-11-17）

「让可维护成立」：对账、预检、拆分、渲染统一、可观测、前端冒烟。

### 状态与数据
- **D27**：新增 `PipelineManager.reconcile()` 与 `POST /api/fusion/reconcile`。
  以产物目录 `manifest.json` 为唯一真相，恢复被清空的内存/索引记录；
  产物目录已丢失的终态任务标 `state.missing`，不再假装完成。
- **D25 收尾**：manifest 改为先回填 `files` 再落盘，恢复出的任务带完整产物清单。

### 输入与资源
- **D28**：上传前置预检（单文件 4 GiB → 扩展名白名单 → MIME 大类 →
  `.uploads` 配额 → 磁盘余量 512 MiB），不合格在入口即 413/415；
  新增 `AVD_UPLOAD_QUOTA_BYTES`（默认 20 GiB），README 已记录。

### 结构
- **D30**：`handle_post` 从 309 行拆成 6 个域 handler + 薄分派器；
  `_run` 从 497 行拆成采集 / 转写 / 整理 / 渲染 / 收尾 5 个阶段方法。
  新增 4 个直接调用阶段方法的单测，不再只能靠整条线程链验证。
- **D31**：单篇与成册已统一走 `fusion/layout.py` 的 `LayoutDoc`；新增
  3 个 **golden-file 测试**锁定 DOCX `document.xml`、PDF Layout IR 与页码规则。

### 可观测与前端
- **D29**：流水线 / HTTP 路由的 `except Exception: pass` 全部改为
  `avd_log.warning` 或 `_degrade`（Cookie、ASR 装配、取消、续跑、双栏、崩溃现场、
  保留策略）；新增审计测试禁止关键路径再出现静默吞错。
- **D34**：新增前端关键路径冒烟：每个 `/api` 调用都能在后端找到、
  zh/en 词典键一致、SSE/启动/取消/导出/问答均接线；并真实起回环服务
  取首页、`flagship.js`、`/api/fusion/health`。

### 核算
- **D33**：口径更新为 **31,179 行 / 79 文件**、**303 项用例**；
  `verify_docs_units.py --assert-current` 全绿。

## v2.0.0-r6 · 第十一轮缺陷修复 · 第一批（2026-11-17）

按第十一轮全面剖析（D21–D35）先做「让可交付成立」的一批，全部有回归测试。

### 产物与测试
- **D21**：修正 `test_one_probe_for_whole_pipeline` 的素材过短问题（35 字被判 C 级却断言
  docx/txt）。新增 **A/B/C 产物矩阵测试**：A 级 6 件套、B 级 6 件套、C 级仅
  md/srt/json，和生产裁剪契约逐项对齐。

### 可运维性
- **D25**：`fusion/persist.py` 不再静默 `except: pass`。落库/索引写失败会：
  ① 在任务快照上标 `persist.failed` 降级项；② 写结构化日志；③ 在
  `/api/fusion/health` 的 `persist.health` 暴露失败次数与最后错误。

### 运行环境承诺
- **D24**：README 与 `start.command` / `启动旗舰版.command` / `一键启动.command`
  显式要求 **Python 3.11+**，删除 3.10 候选，缺失时给出安装指引。
- **D23**：`.app` 启动器按 `runtime/python → 用户安装路径 → PATH` 搜索 3.11+，
  找不到时弹原生对话框；构建脚本明确提示 `.venv-asr` 未随包（模型随包），
  收敛「开箱即转写」的过度承诺。

### 卫生与核算
- **D32**：清理 40 个 4 字节 junk 文件；打包从黑名单改为**白名单**
  （报告、验证脚本、测试、状态目录不再可能误入包）；单测清理改用
  `shutil.rmtree`，不再在 `outputs/` 留空壳。
- **D33**：`作者信息` / `NOTICE.md` 口径更新为 **30,586 行 / 78 文件**、
  **285 项用例**；`verify_docs_units.py --assert-current` 自动核对文档与工作树。
- **D35**：`AVD_TOKEN` 改用 `hmac.compare_digest`，README 增加威胁模型说明：
  单机本地防护，Token 不是多用户权限边界。

## v2.0.0-r5 · 交付阻断修复 + 四项落地（2026-11-14）

本轮两个批次：**R1** 修发布阻断项，**R2** 按 项目负责人 定的顺序做四件事
（补测 → 降级模型化 → 接口接 UI → 真机续跑）。

### R1 · 交付阻断修复
- Cookie 只读源 + 出包凭据自检：不再把凭据复制进包内可写目录。
- 保留策略作用域收窄到 `outputs/`，不再误删兄弟目录媒体。
- 下载媒体复用：下载后处理异常时先认盘，媒体已落盘就不重下（省 26s 空档）。
- 低内容素材告警：转写结果过短不再当成功出稿。
- CI 门禁转绿；关键词词级化；DOCX 样式与目录域修正；安全响应头。

### R2 · ① 覆盖率补测
- `fusion/pipeline.py` 60% → **82%**；`fusion/task_store.py` 59% → **97%**；
  `fusion/degradation.py` 97%。用例 159 → **269** 项。
- 新增 `tests/test_r2_coverage.py`（77 项）、`EventBus` / `Registry` 分支测试。
- **单测污染修复**：`fusion/library.py` 路径写死，599 行素材库里 598 行是测试垃圾。
  新增 `AVD_LIBRARY_DB` 隔离 + 备份 + 清理（`outputs/_backup/`）。

### R2 · ② 降级模型化
- 新增 `fusion/degradation.py`：`note/warn/major/block` 四档 +
  24 条 code 目录表 + `retriable` / `action`，frozen dataclass。
- `task.degraded: list[str]` 保持向后兼容，另增 `degraded_items` / `degraded_summary`；
  前端四档 pill、按严重度排序、头部汇总「共 N 条 · M 条可重试」。

### R2 · ③ 接口接 UI
- 接通旗舰版 5 个「后端有、前端零调用」的路由：合集 / 素材 / 任务历史 /
  章节切片 / 存储统计。
- **修真实缺陷**：`to_dict()` 从不返回 `chapters`，`/api/pipeline/export-chapters`
  100% 回「任务没有章节」；索引不落 `meta` 导致重启后标题变空。

### R2 · ④ 真机长素材续跑（发现 3 个真实缺陷）
- **D1 / P0**：worker 里从来 import 不到 `fusion`（`.venv-asr` + `tools/` 路径 +
  池把 `PYTHONPATH` 清空），`from fusion import checkpoint` 每次 ImportError，
  而所有调用点都 `except Exception: pass` —— 写检查点、取消哨兵、崩溃现场
  **三件事一起静默失效**。修：补主包根 bootstrap，`_ckpt_probe()` 失败必须 `log()`。
- **D2 / P1**：取消是「乐观置终态」，worker 还在写检查点，用户点重试从 0 重来。
  修：`_wait_checkpoint_flush()`（默认 30s，`AVD_ASR_CANCEL_FLUSH_SECS` 可调）。
- **D3 / P1**：上一单的尾巴毒害下一单（`job_end{cancelled:true}` 留在共享队列，
  下一个任务刚排队就被判已取消）。修：`AsrPool.drain_pending()`，EOF 哨兵保留。
- 顺带修：模型加载 78.8s 无事件（加 `_LoadHeartbeat`）、`info` 分支把 `duration`
  冲成 0、纯文案进度传不上去、`safe_stem` 不清洗控制字符。
- `tools/asr_worker.py` 续跑改为**按秒**（`resume_at`）而非 chunk 下标，
  块内每 15s 落盘、取消前落盘。
- 新增 `verify_long_resume.py`：预热 → 中途取消 → 同键续跑，12 条判据；
  `--source` 支持真实网络素材。
- 新增 `verify_cross_platform.py`：AST 扫无 `encoding` 的文本 I/O、
  `safe_stem` 11 用例、真实落盘（中文/emoji/长名/Windows 保留名）、
  SRT/MD 无 `\r`/BOM、`guards.within` 路径围栏。

### R2 · ⑤ 合规证据按 r5 重做（不恢复旧文件）
- `SCA-供应链清单.md` 重新生成：**8 个包 → 23 个包**。
- **NOTICE.md §2.2 补齐 23 个包**（此前 15 个组件零声明，Apache-2.0 §4 /
  MIT 署名义务未履行）；§1 自研行数按 r5 实测重算：**29,903 行 / 78 文件**
  （A 815 / B 19,245 / D 9,843）。
- CVE 双源重跑：OSV **24 组件 0 命中**、历史漏洞 156 条逐条核验修复点；
  PyPI Advisory **24 组件 0 条**（23 个 runtime 包 + vendored yt-dlp），附 `requests 2.31.0` 阳性对照（6 条）。
- `内部核查清单-待确认.md` 更新到 r5：组件矩阵 9 → 27 项，
  新增 Q13（历史漏声明追溯）/ Q14（PSF-2.0、CNRI-Python）/ Q15（git 主体占位）。
- `作者信息` 重写为 r5 口径，附方法论坦白（含「上游原始包不在仓库内、
  无法二次 diff」这条局限）。
- 新增 `verify_supply_chain.py`：把「runtime 实物 / NOTICE / SBOM」三方对齐，
  漂移即红，已接入 CI —— 这次的问题正是没有任何门禁能发现它。

### R2 · ⑥ 可续跑入口打通
- **修真实缺陷**：`_persist_index` 只落终态任务，崩在转写中途的任务从未入库；
  重启后「可续跑」列表能列出 task_id，但 `retry` 直接抛「任务不存在」——
  列表是活的，入口是死的。
- 检查点新增 `source` 字段（来源 URL）；`Manager._rehydrate()` 从检查点重建任务；
  `retry` 回落重建；前端列表项可点即续跑，旧检查点如实标灰并说明原因。

### R2 · ⑦ 门禁挪到真正能挡住的位置（自查补修）
- **发现自己的疏漏**：`verify_supply_chain.py` 挂进了源码仓 CI，但 `runtime/` 被
  `.gitignore` 排除，CI 上不存在 → 脚本走 `SKIP` 返回 0，**看着绿，其实什么都没查**。
  D4 能活这么久，就是因为门禁永远在查不到事实的地方。
- `verify_supply_chain.py` 新增 `--root`（校验基准）、`--strict`（缺 runtime 即失败）、
  `--docs-only`（不依赖 runtime，只校 NOTICE 与 SCA 互不矛盾；表格解析为空也判失败）。
- 门禁分两层钉死：`packaging/macos/build_app.sh` 出包时跑 `--root <产物> --strict`
  （runtime 与 NOTICE 同时在场，这才是缺陷能造成伤害的地方）；CI 跑 `--docs-only`
  （守住两份文档不自相矛盾）。
- 新增 `tests/test_supply_chain.py` 7 项：切根、严格模式失败、非严格 SKIP、
  文档互校通过/缺行失败/空表拒绝、真实仓库一致。全量 **269 → 276 项**。
- **文档数字重新对账**：本轮新增 2 个文件后，作者信息/NOTICE/LEGAL/CHANGELOG 里
  的「29,443 行 / 76 文件」立即过期 → 实测改为 **29,903 行 / 78 文件**
  （A 815 / B 19,245 / D 9,843，`asr_worker.py` 也由 1,063 更正为 1,064）。

## v2.0.0-r4 · 发布前核查（2026-11-12）

项目负责人 拍板「先走 1 + 2」：① 补 Cookie 打通抖音全链；② 发布前核查包。

### ① 抖音全链：工程侧收口，凭据侧仍阻塞
- 只读实测本机凭据源：Safari Cookie 库存在但被 macOS TCC 拒绝
  (`Operation not permitted`)；Chrome / Edge / Firefox / Brave profile 全部不存在；
  包内 `cookies/` 无凭据、无 `AVD_COOKIE_FILE`。**不绕系统权限、不绕平台风控**。
- 新增 `cookies/导出指引.md`：三条正当获取路径、TCC 授权步骤、Netscape 格式、
  交付前清理；明确拒绝伪造 `s_v_web_id` / 模拟验证挑战等做法。
- 新增 `cookies/cookie_doctor.py`：体检文件位置 / 格式 / 域 / `s_v_web_id` /
  过期 / gitignore，**绝不回显 Cookie 值**；`--selftest` 用内存合成数据验证解析逻辑，
  `--probe` 调真实诊断接口。实测合成自检通过、真实体检准确报缺。
- 新增 `run_douyin_e2e.py`：服务自检 → `ready` 判定 → 启动流水线 → 轮询 →
  取 MD/SRT/TXT/JSON → 写 `manifest.json` 证据。`ready=false` 时**不伪造成功**，
  写 `evidence/douyin-e2e/BLOCKED.md` 并以退出码 2 结束。
- 实测 `--dry-run` 返回 `COOKIE_REQUIRED` + 5 条动作，阻塞证据已落盘。
  **抖音完整出稿仍未跑通，等 项目负责人 提供新鲜 Cookie。**

### ② 在线 CVE 核查（两个独立源 + 修复点核验）
- `cve_scan.py`：从包内 `METADATA` / `version.py` 读 9 个组件，向 OSV API 做
  精确版本查询 + 包级历史查询。结果 9/9 精确 0 命中，历史漏洞合计 42 条
  （yt-dlp 22 / numpy 16 / tqdm 4）。
- `cve_crosscheck.py`：逐条拉取历史漏洞详情，收集 `fixed` 版本比对。
  **yt-dlp 2026.10.26 晚于全部已知修复点（最新 2026.7.4）**；numpy / tqdm 同理。
  6 个包 OSV 无收录（数据空白，非「零漏洞」）。
  过程中修正自身两个 bug：把 GIT commit hash 当版本号比较、
  把「无历史记录」误标为「有漏洞但无修复点」。
- `pypi_advisory_check.py`：第二独立源 PyPI Advisory（PyPI JSON API），
  带 `requests 2.31.0` **阳性对照**（命中 6 条，证明查询链路有效）；
  随包 9 个组件 0 条。
- `pip-audit` 安装成功但 `-r` 模式需建 venv，`ensurepip` 被沙箱中断
  （SIGABRT），**未产出结果、不记作已执行**，改用等价只读接口。
- Trivy / Grype：本机无二进制、无 brew、GitHub Releases 不可达（HTTP 000），
  **仍未执行**，放行清单保持未闭环。

### ③ 许可与权属
- 新增 `内部核查清单-待确认.md`：12 项待确认问题（Q1–Q12）、
  组件许可事实矩阵、证据清单、**空白签署栏**。明确「未签署、不构成法律意见」。
- `set_git_identity.sh` 加固：默认 dry-run，需 `--apply`；拒绝占位主体 /
  示例邮箱 / 非法邮箱；支持 `--name` / `--email`。实测 4 种输入均按预期拦截，
  `git config` 未被改动。**真实仓库主体仍需 项目负责人 提供。**

### ④ 同步
- `SCA-供应链清单.md` §5 更正「未联网调用 CVE 数据库」；
  CI 增加离线证据可重生成校验；`VERSION` / `README-FLAGSHIP.md` /
  `内部条目清单` 同步到 r4。
- 新增 `AVD-Flagship-v2_发布前核查复评v4.html`（本轮正式报告）。

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
  不成立 → 预置 `models/faster-whisper-tiny/`（~75 MiB, MIT），新增
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

---

## v2.0.0-r3 · P0–P2 闭环修订（2026-11-12）

触发：项目负责人 给出抖音真实短链并要求修复两份报告中的 P0–P2。
详见 `AVD-Flagship-v2_缺陷闭环复评v3.html` 与 `内部闭环记录（未入库）`。

### 平台链路（实测 + 产品化恢复路径）
- 实测抖音短链 302 → Web detail API 403 → 定位 `tiktok.py:1493-1506` 要求 `s_v_web_id`；
  Safari Cookie 被 macOS TCC 拒绝；cookiefile 通道实证贯通。
- 新增 `fusion/engine_opts.py`：probe / download 统一继承 Cookie、代理、重试、
  extractor 裁剪参数，并支持**限定包内**的 `cookies.txt`。
- 新增 `fusion/diagnostics.py` 与 `POST /api/platform/diagnose`：8 类平台错误中文诊断
  （含 `COOKIE_PERMISSION` / `COOKIE_STALE`）；流水线失败时任务带 `diagnosis` 字段。
- `_probe_once` 增加短链 / 开放重定向落地地址二次校验。
- 新增 `cookies/README.md`、`.gitignore` 排除 `cookies/*.txt`。

### 工程缺陷闭环（16 处精确补丁，每处断言命中 1 次）
- `fusion/hardening.py`：P0-5 配置写入校验、P1-1 缓存三重淘汰、P1-2 48 并发上限、
  P1-4 1 MiB JSON 上限、P1-5 LLM 出口校验。
- `server.py`：HEAD 语义、413、上传完整性、并发服务器替换、配置校验接线。
- `downloader/asr.py`：取消时异步 reap 子进程 + `wait(timeout)` 兜底（P1-3）。
- `downloader/polish.py`：`list_models` / `chat_stream` 出口校验（P1-5）。
- `downloader/tasks.py`：进度回退值锁内快照（P2-4）。

### 精度、许可与供应链
- 新增 `models/faster-whisper-large-v3-turbo/`（1.5 GB，MIT），默认档由 tiny 升级；
  同一段自有语音实测「期见版」→「旗舰版」，离线转写 6.4s。
- 新增 `LICENSES/`（MIT / Apache-2.0 全文 + 来源与变更说明）；`NOTICE.md` 补齐
  5 个模型卡的许可证据链，按最保守口径履行双重义务，法律结论仍留待确认。
- 新增 `sca_report.py` + `SCA-供应链清单.md`（离线 SBOM）、
  `.github/workflows/ci.yml`、`ruff.toml`、`mypy.ini`、`set_git_identity.sh`。
- 新增 `PLATFORM_SUPPORT.md` 与 `setup-windows-runtime.bat`；`doctor.py` / `boot.py`
  增加跨平台分支与版本分层闸门（主服务 ≥3.11 / ASR 3.13）。

### 验证与口径更正
- **36 / 36** 单测（新增 `test_hardening.py` 19 项）、离线全链 **6.9s**、
  HTTP 端到端 413 / 403 / HEAD / 诊断全绿。
- 新增单测当场抓出加固代码自身的大小写 bug（`/System/Library` 漏放）并修复。
- `作者信息` 更正：`asr.py / polish.py / tasks.py` 从 A 类改归 D 类；
  自研源码 11,415 → **12,482 行**（A 5,500 / B 3,643 / D 3,339）。
- 发布包 267 MB → **1.8 GB**（turbo 权重 1.5 GB），3,240 文件。
- 仍未闭环：抖音完整出稿需新鲜 Cookie、Windows 真机验收、在线 CVE 扫描、
  许可确认、真实 git 主体、批量下放决策。
