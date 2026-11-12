# 第三方组件声明 · AVD Flagship v2.0.0

本产品由 Hyht-Team 自主研发的部分，与随包分发的第三方组件，边界如下。
逐文件归属与行数见 `作者信息`；专有许可见 `LICENSE`。
**本文件随发布包一起提供，请勿删除。**

---

## 1. 自主研发部分（Copyright © 2026 Hyht-Team）

| 类别 | 内容 | 行数 |
|---|---|---|
| A 原版自研未改动 | UI（app.js / style.css）、`downloader/` 8 模块、`tools/` 2 模块、`app.py` | 7,435 |
| B 本轮新增 | `fusion/` 融合层、统一入口、环境自检、离线验收、`static/flagship.js`、测试、`yt_dlp_plugins/` | 2,656 |
| D 原版 + 精确补丁 | `server.py`（10 处补丁）、`static/index.html`（+1 行 script） | 1,324 |

**合计 11,415 行自研源码。** UI、交互、业务编排、合规话术、文字稿结构化均为自主研发；
使用第三方库不影响本产品作为「自主研发产品」对外表述，但不得宣称
「全栈自研 / 100% 自研 / 自研语音识别模型 / 无第三方依赖 / 底层解析引擎自研」。

---

## 2. 随包分发的第三方组件

### 2.1 解析引擎

| 组件 | 版本 | 许可 | 用途 | 位置 |
|---|---|---|---|---|
| yt-dlp | 2026.10.26 (stable, `c91f8de0`) | **Unlicense**（源码/wheel） | 链接解析与媒体下载 | `vendor/yt_dlp/`，**原样内嵌、未改一行** |

### 2.2 ASR 运行时（按实测导入探针精确裁剪，156.8 MB）

| 包 | 版本 | 许可（取自包内 `*.dist-info/METADATA`） |
|---|---|---|
| av (PyAV) | 19.0.1 | BSD-3-Clause |
| ctranslate2 | 4.8.2 | MIT |
| faster-whisper | 1.2.1 | MIT |
| huggingface-hub | 1.33.0 | Apache-2.0 |
| numpy | 2.5.3 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| onnxruntime | 1.30.0 | MIT |
| tokenizers | 0.23.2 | Apache-2.0（按 METADATA 的 License Classifier；包内无独立 LICENSE 文件） |
| tqdm | 4.70.1 | MPL-2.0 AND MIT（双许可） |

> 裁剪依据：`tools/asr_worker.py` 转写自有中文语音时的运行期导入探针，
> 实测只加载上述 8 个包。`torch`(568M) / `mlx`(211M) / `llvmlite`(125M) /
> `scipy`(87M) 等未进入发布包。**因此离线包只提供 faster 引擎，MLX 引擎不可用**，
> 这是有意的工程取舍，不是缺陷。完整来源见 `runtime/MANIFEST.json`。

### 2.3 语音模型权重

| 组件 | 来源 | 许可 | 位置 |
|---|---|---|---|
| faster-whisper-tiny (CT2) | `Systran/faster-whisper-tiny` | **MIT**（模型卡 `license: mit`，2026-11-12 核验） | `models/faster-whisper-tiny/`，84 MB |
| Whisper tiny 基座 | `openai/whisper-tiny` | **Apache-2.0**（模型卡，同期核验） | 转换版所基于的上游模型 |

> 两处许可都如实列出。转换版权重是否同时受基座许可约束，属实务判断，
> **签大单前请许可复核**，本包不作法律结论。

### 2.4 系统依赖（未捆绑）

| 组件 | 提供方式 | 许可 |
|---|---|---|
| ffmpeg / ffprobe | 由用户系统 PATH 提供，本包**未捆绑**任何二进制 | LGPL/GPL，取决于编译开关 |
| CPython 3.13 | 由用户系统提供；换机器由 `doctor.py --repair` 自动重接解释器 | PSF License |

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
2. ✅ 模型权重随包预置，断网转写成立（此前首次运行会因 `HfHubHTTPError` 整块失败）；
3. ✅ ASR 运行时实体化并列出 8 个包各自许可；
4. ✅ 自研边界：`LICENSE`、`作者信息`、B 类文件版权头、git 基线。

**仍待补齐（本包未代做）**

1. 若捆绑 ffmpeg 静态二进制，按其编译开关确认 LGPL/GPL 义务；
2. 转换版权重是否同时受基座 Apache-2.0 约束，请许可复核；
3. `vendor/yt_dlp/` 内嵌代码的完整 SCA 扫描报告；
4. 软件著作权登记、「自主知识产权」认定所需的主管部门材料。
