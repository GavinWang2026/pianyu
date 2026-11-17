# AVD Flagship · 能力矩阵与使用边界

> 正式发布前请与客户共同确认本清单。未列出的能力默认**不承诺**。

## 能做什么

| 能力 | 说明 | 限制 | 可感知替代 |
| --- | --- | --- | --- |
| 本地离线转写 | faster-whisper，随包权重 | 转写并发 = **1**（常驻池串行） | 排队可见 + ETA；批量任务自动排队 |
| 平台字幕优先 | 有字幕则不跑 ASR | 受站点字幕可用性影响 | 无字幕自动落本地 ASR |
| 取消转写 | 软取消（chunk 边界）+ 检查点续跑 | 终态不可逆 | 取消后重试从检查点继续，不付冷启动 |
| 知识稿导出 | MD / SRT / TXT / JSON / DOCX / PDF | PDF 需系统 CJK 字体 | 无字体时回退附件方案；DOCX 含 `styles.xml`（Heading1–3/Title/Caption）+ 真 `TOC` 域 + 页脚页码域，Word 导航与自动目录可用 |
| 批量 / 定时 | 多 URL 排队、到点开跑 | 流水线槽 4；转写仍串行 | 界面显示排队位次 |
| 三方推送 | Notion 分块续写、飞书分块+全文链接 | 飞书卡片仍限块数 | 回执标 truncated + `file://` 全文 |
| 说话人 | 可选；有 pyannote 用神经网络 | 启发式「未校准」；停顿不足或 A/B 不均衡则撤回 | 默认关闭；面板可见 confidence / balance |
| 素材库检索 | FTS5 trigram + 命中高亮 + 时间戳 | 问答基于检索片段 | 可开本地 LLM 归纳，答案强制带出处 |
| 合集成册 | 一集合 → MD/DOCX/PDF 同源 LayoutSpec | 单合集最多 200 篇 | `/api/library/export-booklet`（`fmt=all`） |
| 章节切片 | 按章节批量导出 MP4 | 需保留媒体文件 | 未下载媒体时明确报错提示 |
| 转写续跑 | 分块检查点 + 软取消；流水线产物目录持久化 | 真机长视频耗时随素材 | `resume_chunk`；`GET /api/pipeline/recoverable`；合成实证 + `estimate_long_resume` 量化 |
| 规则摘要 | 断网抽取式：导语 + Top-K 要点（词频打分） | 非 LLM 生成式 | 正文标「规则摘要」；有 LLM 时走 enrich |
| 知识稿正文 | 流水线调用 `polish.local_polish` 分段 + 时间锚 | 有说话人时仍按句渲染 | `meta.paragraphs`；段首 ``#t=`` |
| 本地上传 | 旗舰面板拖放 / 选文件 → `/api/pipeline/upload` | 需 ASR 环境 | 与 URL 路径共用进度面板 |
| 产物分级 | C：md/srt/json；B：+docx/pdf；A：校对后 dual | 未校对不出 dual | `content_level` |
| 界面语言 | 顶栏中英切换（`static/i18n.js`） | 旗舰面板核心文案 | `localStorage avd_lang` |
| Windows 路径 | `setup-windows-runtime.bat` + 字体候选 + CI 冒烟 | 真机 ASR runtime 另验 | `verify_windows_paths.py` |
| 崩溃现场 | worker/流水线异常写 `asr-crash-*.json` | 不保证跨机器迁移 | 诊断包默认附带；可续跑扫描可见 |
| 诊断包 | 环境 / 日志 / 失败样本 / 崩溃现场 zip + 工单号 | 不含 Cookie / API Key / 媒体二进制 | `ticket_id` 本地回执；`POST /api/diagnose/submit`（需 `AVD_SUPPORT_URL`，否则 local_only） |
| 低内容素材 | 字数 < 80 或（段 ≤1 且时长 > 60s）判为 C 级：只出元数据 + 转写原文 | 不生成摘要 / 要点 / 术语表（纯音乐、MV、纯片头本身没有知识点） | 文稿顶部「信息量不足」声明；`low_content` / `content_level` 进任务快照 |
| 凭据隔离 | 包内 `cookies/cookies.txt` 只读源；yt-dlp 回写落在 `.state/cookies.work.txt` | `.state/` 不进 git、不进 .app | `health` 里 `cookie_readonly=true`；出包自检 `tools/secrets_scan.py`（默认 strict） |

## 不做什么 / 不承诺

- **不**承诺多路并行转写（GPU 权重只常驻一份）→ 替代：排队 ETA
- **不**把启发式说话人当作法庭级分离结果 → 替代：标注未校准 / 可关
- **不**代用户登录第三方、不存第三方 Cookie
- **不**解析内网 / 未白名单站点（合规边界）→ 可用 `AVD_ALLOWED_*` 按合同收紧
- **不**在无 LLM 配置时生成「大模型摘要」（会标明规则摘要）
- **不**把片头 / MV / 纯音乐当知识稿：这类素材只出元数据与原文，并显式声明「信息量不足」
- Windows ASR 需单独运行时；默认交付以 macOS arm64 为准

## 话术建议

- 可以说：「本地离线完成转写与知识稿；转写支持取消并续跑（检查点 + 合成实证）；PDF/DOCX/成册可交付；诊断包不含密钥，工单可本地对账或经支持端点上传；素材库命中带时间戳出处。」
- 不要说：「多任务并行转写」「飞书推送保证全文无截断」「任意时长取消后秒级无损续跑已在真机长视频实测」（合成协议已证；真机素材另验）「已用真实 Key 验收全部 LLM 分支」。

## 许可 / 合规要点

1. 仅处理用户有权使用的公开或自有素材。
2. 白名单站点见 `fusion/guards.py`；可按合同收紧 `AVD_ALLOWED_*`。
3. 发布包不含客户 Cookie、LLM Key、config 明文密钥。
4. 第三方组件许可证见 `NOTICE.md` / `LICENSES/`。
