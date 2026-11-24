# 第三方组件声明 · AVD Flagship v2.0.0

本产品由 Hyht-Team 自主研发的部分，与随包分发的第三方组件，边界如下。
本项目以 **MIT 许可**开源，全文见 `LICENSE`；第三方组件各自按其原始许可执行。
**本文件随发布包一起提供，请勿删除。**

---

## 1. 自主研发部分（Copyright © 2026 Hyht-Team）

| 类别 | 内容 | 行数（r8.2 实测 2026-11-18） |
|---|---|---|
| A 原版自研未改动 | `downloader/`（`__init__` / `subtitles`）、`requirements.txt` | 582 |
| B Hyht-Team 新增 | `fusion/` 29 模块、`tests/` 回归测试、`static/flagship.js` + `flagship.css` + `i18n.js`、`yt_dlp_plugins/` 3 个、`boot.py` / `doctor.py` / `cookies/cookie_doctor.py` 等 | 21,182 |
| D 原版 + 精确补丁 | `server.py`、`static/index.html`、`static/app.js`、`static/style.css`、`downloader/`（`asr` / `polish` / `tasks` / `config` / `engine` / `runtime_paths`）、`tools/`（`asr_worker` / `asr_setup`） | 10,267 |

**合计 32,031 行自研源码（4 + 61 + 16 = 81 个文件）。** 判定规则：与首个提交
`957b362` 逐文件 `git diff` 为空 → A 类；有改动 → D 类；不在该提交内 → B 类。

> **口径说明**：行数与文件数为 r8.2（2026-11-18）快照。开源瘦身删除了 CVE 扫描脚本、
> `verify_*` 验收脚本、`setup-*` 安装脚本与样例文件，仅减少开发期文件，不改变上表的归属判定。

r5 相对 r3 的变化：`static/app.js`（+292/−5）、`style.css`（+31/−2）、
`downloader/config.py`（+107/−3）、`engine.py`（+138/−7）、`tools/asr_worker.py`（+491/−50）
均已有补丁，按 r3 自己的规则从 A 类改归 D 类 —— 这与 r3 把
`asr.py / polish.py / tasks.py` 改归 D 类是同一套口径。
UI、交互、业务编排、合规话术、文字稿结构化均为自主研发；
使用第三方库不影响本产品作为「自主研发产品」对外表述，但不得宣称
「全栈自研 / 100% 自研 / 自研语音识别模型 / 无第三方依赖 / 底层解析引擎自研」。

---

## 2. 随包分发的第三方组件

### 2.1 解析引擎

| 组件 | 版本 | 许可 | 用途 | 位置 |
|---|---|---|---|---|
| yt-dlp | 2026.10.26 (stable, `c91f8de0`) | **Unlicense**（源码/wheel） | 链接解析与媒体下载 | `vendor/yt_dlp/`，**原样内嵌、未改一行** |

### 2.2 ASR 运行时（按实测导入探针精确裁剪，176 MB / 23 个包）

> **r5 更正（2026-11-14）**：上一版只列了 8 个包。`faster-whisper 1.2.1` 把下载层从
> `requests` 换成 `httpx`，实际随包为 **23 个包**（含 `httpx` 及其 `anyio` / `h11` /
> `httpcore` / `idna` / `certifi` 传递依赖，以及 `safetensors` / `regex` / `filelock` /
> `fsspec` / `PyYAML` / `typing_extensions` / `requests` / `urllib3` /
> `charset-normalizer`）。**漏声明即 Apache-2.0 §4 与 MIT 署名义务未履行**，
> 故本节按打包产物的实测结果重列。

| 包 | 版本 | 许可（取自包内 `*.dist-info/METADATA`） |
|---|---|---|
| anyio | 4.16.0 | MIT |
| av (PyAV) | 19.0.1 | BSD-3-Clause |
| certifi | 2026.10.23 | MPL-2.0 |
| charset-normalizer | 3.5.2 | MIT |
| ctranslate2 | 4.8.2 | MIT |
| faster-whisper | 1.2.1 | MIT |
| filelock | 4.2.10 | MIT |
| fsspec | 2026.9.0 | BSD-3-Clause |
| h11 | 0.16.0 | MIT |
| httpcore | 1.0.9 | BSD-3-Clause |
| httpx | 0.28.1 | BSD-3-Clause |
| huggingface-hub | 1.33.0 | Apache-2.0 |
| idna | 3.20 | BSD-3-Clause |
| numpy | 2.5.3 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| onnxruntime | 1.30.0 | MIT |
| PyYAML | 6.0.3 | MIT |
| regex | 2026.11.9 | Apache-2.0 AND CNRI-Python |
| requests | 2.34.2 | Apache-2.0 |
| safetensors | 0.8.0 | Apache-2.0（按 METADATA 的 License Classifier） |
| tokenizers | 0.23.2 | Apache-2.0（同上；包内无独立 LICENSE 文件） |
| tqdm | 4.70.1 | MPL-2.0 AND MIT（双许可） |
| typing_extensions | 4.16.0 | PSF-2.0 |
| urllib3 | 2.8.0 | MIT |

> 裁剪依据：`tools/asr_worker.py` 转写自有中文语音时的运行期导入探针。
> `torch`(568M) / `mlx`(211M) / `llvmlite`(125M) / `scipy`(87M) 等未进入发布包。
> **因此离线包只提供 faster 引擎，MLX 引擎不可用**，这是有意的工程取舍，不是缺陷。
> 完整来源见 `runtime/MANIFEST.json`。

### 2.3 语音模型权重

| 组件 | 来源 | 许可 | 位置 |
|---|---|---|---|
| faster-whisper-large-v3-turbo (CT2) | `mobiuslabsgmbh/faster-whisper-large-v3-turbo` | **MIT**（模型卡 `license: mit`，2026-11-12 核验） | `models/faster-whisper-large-v3-turbo/`，~1.5 GiB，**默认档** |
| faster-whisper-tiny (CT2) | `Systran/faster-whisper-tiny` | **MIT**（模型卡 `license: mit`） | `models/faster-whisper-tiny/`，~75 MiB，轻量备用档 |
| Whisper large-v3-turbo 官方权重 | `openai/whisper-large-v3-turbo` | **MIT**（模型卡） | turbo 转换版对应的官方发布 |
| Whisper large-v3 基座 | `openai/whisper-large-v3` | **Apache-2.0**（模型卡，同期核验） | 官方 turbo 的 `base_model` |
| Whisper tiny 基座 | `openai/whisper-tiny` | **Apache-2.0**（模型卡） | tiny 转换版所基于的上游模型 |

**证据链结论（2026-11-12 逐仓核验）**：两份 CT2 转换版权重自身均标 MIT，
但其上游基座分别标 Apache-2.0（tiny / large-v3）。转换版权重是否同时继承基座
义务，属法律判断；本包**按最保守口径同时履行 MIT 与 Apache-2.0 义务**：
许可全文与来源以上游仓库公布的模型卡与许可证为准，本仓库不重复分发。
**签大单前仍请许可书面复核**，本包不作法律结论。

### 2.4 系统依赖（未捆绑）

| 组件 | 提供方式 | 许可 |
|---|---|---|
| ffmpeg / ffprobe | 由用户系统 PATH 提供，本包**未捆绑**任何二进制 | LGPL/GPL，取决于编译开关 |
| CPython 3.13 | 由用户系统提供；换机器由 `doctor.py --repair` 自动重接解释器 | PSF License |

### 2.5 许可归属速查

本仓库根目录 `LICENSE` 为纯 MIT 正文，仅覆盖 Hyht-Team 自主研发部分。
下列资产由第三方提供，**不适用** `LICENSE`，各自按其原始许可执行：

| 资产 | 位置 | 许可 |
|---|---|---|
| yt-dlp 2026.10.26 源码副本 | `vendor/yt_dlp/` | Unlicense |
| Python 运行时依赖 | `runtime/site-packages/` | 各自开源许可（见 2.2 表） |
| 语音识别权重 | `models/` | MIT（上游基座 Apache-2.0） |
| ffmpeg | 由用户系统提供 | 本仓库未捆绑二进制 |

使用本软件即表示你理解：本工具仅用于处理你**有权访问、有权处理**的内容。

---

## 3. 许可要点（务请保留）

- yt-dlp 的 **wheel / sdist 为 Unlicense**，允许复制、修改、闭源、销售，不强制署名；
  本包内嵌的是源码副本，合规。
- **官方 PyInstaller 二进制整体为 GPLv3+**。本包**没有**捆绑任何官方 exe/dmg；
  若日后自行打包并捆绑官方二进制，将触发 GPL 义务，须重新做许可评估。
- Unlicense 虽不强制署名，本包仍保留 `vendor/LICENSE-yt-dlp-Unlicense.txt`
  与上游 `Changelog.md`、`README.md`，作为供应链溯源与商业诚信凭证。
- 上游仓库有 `.NO_AI` 政策：**禁止 AI 生成的改动回灌 issue / PR**。
  本包对上游是只读内嵌 + 官方扩展点定制，不产生上游贡献；若需回灌，
  须由团队人工提交。
- `runtime/site-packages/` 是**实体文件**而非软链，发布包不依赖外卷或本机
  site-packages；整包可移动、可压缩交付。其许可义务随二进制分发一并转移，
  请按上表保留各包的 `*.dist-info`。

---

## 4. 交付前已补齐 / 仍待补齐

**本轮已补齐（相对上一版）**

1. ✅ 模型权重许可核验（MIT / Apache-2.0 均已核到模型卡，不再写「待核验」）；
2. ✅ 新增 large-v3-turbo 档位：**1.5 GB MIT 权重随包**，默认走大模型，
   中文实测精度明显高于 tiny（「期见版」→「旗舰版」）；
3. ✅ MIT / Apache-2.0 许可边界与来源已在 NOTICE 中说明；
4. ✅ 模型权重随包预置，断网转写成立（此前首次运行会因 `HfHubHTTPError` 整块失败）；
5. ✅ ASR 运行时实体化并列出 **23 个包**各自许可（r5 更正：上一版只列 8 个）；
6. ✅ 自研边界：`LICENSE`、版权头、git 基线；
7. ✅ 平台 Cookie 通道与安全边界（`cookies/README.md`，cookie 文件强制限定包内）；
8. ✅ CI 静态检查与单测门禁，见 `.github/workflows/ci.yml`；
9. ✅ 在线漏洞核查双源重跑（r5）：OSV 精确版本查询 **24 个组件 0 命中**、
   历史漏洞 156 条已逐条核验修复点；PyPI Advisory 第二源 **23 个组件 0 条**
   （附 `requests 2.31.0` 阳性对照命中 6 条，证明链路有效）。
   原始证据与报告保留在本地交付档案中，不随源码仓库分发。

**仍待补齐（本包未代做）**

1. 若捆绑 ffmpeg 静态二进制，按其编译开关确认 LGPL/GPL 义务；
2. 转换版权重是否同时受基座 Apache-2.0 约束，**请许可书面签字**
   （证据链已备齐、义务已按最保守口径履行，但法律结论不由本包代作）；
3. 已做在线 CVE 双源查询，但**未做**源码级漏洞比对、未跑 Trivy / Grype
   （本机无二进制、GitHub Releases 不可达），不得把「查询无命中」写成「通过安全审计」；
4. 软件著作权登记、「自主知识产权」认定所需的主管部门材料；
5. Windows 运行时真机验收（脚本已备，本机 macOS 无法实测）。
