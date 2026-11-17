/* ============================================================================
   素材工作台 · 前端逻辑
   ========================================================================== */

(() => {
  'use strict';

  // ------------------------------------------------------------- DOM 引用
  const $ = (id) => document.getElementById(id);

  const el = {
    urlInput: $('urlInput'),
    urlField: $('urlField'),
    btnPaste: $('btnPaste'),
    btnParse: $('btnParse'),

    hero: $('hero'),
    emptyHint: $('emptyHint'),

    preview: $('preview'),
    thumbImg: $('thumbImg'),
    mediaFallback: $('mediaFallback'),
    platformBadge: $('platformBadge'),
    previewTitle: $('previewTitle'),
    previewMeta: $('previewMeta'),
    qualityGrid: $('qualityGrid'),
    qualityHint: $('qualityHint'),
    btnDownload: $('btnDownload'),
    btnReset: $('btnReset'),

    progressPanel: $('progressPanel'),
    pThumb: $('pThumb'),
    pTitle: $('pTitle'),
    pSubtitle: $('pSubtitle'),
    btnCancel: $('btnCancel'),
    stageTrack: $('stageTrack'),
    barPercent: $('barPercent'),
    barNote: $('barNote'),
    bar: document.querySelector('.bar'),
    barFill: $('barFill'),
    statSpeed: $('statSpeed'),
    statSize: $('statSize'),
    statEta: $('statEta'),
    statName: $('statName'),
    logPre: $('logPre'),
    donePanel: $('donePanel'),
    doneName: $('doneName'),
    btnOpenFile: $('btnOpenFile'),
    btnOpenFolder: $('btnOpenFolder'),

    btnTheme: $('btnTheme'),
    btnLang: $('btnLang'),
    btnHistory: $('btnHistory'),
    btnSettings: $('btnSettings'),
    historyBadge: $('historyBadge'),

    historyDrawer: $('historyDrawer'),
    historyList: $('historyList'),
    btnClearHistory: $('btnClearHistory'),

    settingsDrawer: $('settingsDrawer'),
    setDir: $('setDir'),
    setRetainDays: $('setRetainDays'),
    setOutputsMaxGb: $('setOutputsMaxGb'),
    btnStoragePurge: $('btnStoragePurge'),
    storageStatsHint: $('storageStatsHint'),
    setProxyMode: $('setProxyMode'),
    setProxyUrl: $('setProxyUrl'),
    setCookies: $('setCookies'),
    setPreferSubtitle: $('setPreferSubtitle'),
    setAsrEngine: $('setAsrEngine'),
    setAsrHotwords: $('setAsrHotwords'),
    setPlaylistMax: $('setPlaylistMax'),
    setObsidianDir: $('setObsidianDir'),
    setLiveRecord: $('setLiveRecord'),
    setDownloadRate: $('setDownloadRate'),
    setAsrDiarize: $('setAsrDiarize'),
    setNotionToken: $('setNotionToken'),
    setNotionDb: $('setNotionDb'),
    setFeishuWebhook: $('setFeishuWebhook'),
    asrEngineTip: $('asrEngineTip'),
    btnTsEdit: $('btnTsEdit'),
    btnTsUndo: $('btnTsUndo'),
    btnTsRedo: $('btnTsRedo'),
    setLlmUrl: $('setLlmUrl'),
    setLlmModel: $('setLlmModel'),
    setLlmKey: $('setLlmKey'),
    setLlmNoThink: $('setLlmNoThink'),
    llmModelList: $('llmModelList'),
    btnUseLocalLlm: $('btnUseLocalLlm'),
    btnLoadModels: $('btnLoadModels'),
    btnRevealDir: $('btnRevealDir'),
    btnUseEnvProxy: $('btnUseEnvProxy'),
    btnSaveSettings: $('btnSaveSettings'),
    setSavedHint: $('setSavedHint'),

    footDir: $('footDir'),
    btnOpenDir: $('btnOpenDir'),

    btnExtract: $('btnExtract'),
    btnExtractDone: $('btnExtractDone'),
    transcriptPanel: $('transcriptPanel'),
    tsTitle: $('tsTitle'),
    tsMeta: $('tsMeta'),
    tsSource: $('tsSource'),
    tsMode: $('tsMode'),
    tsLang: $('tsLang'),
    tsRunning: $('tsRunning'),
    tsBarFill: $('tsBarFill'),
    tsPercent: $('tsPercent'),
    tsMessage: $('tsMessage'),
    btnTsCancel: $('btnTsCancel'),
    tsLive: $('tsLive'),
    tsDone: $('tsDone'),
    tsViewSwitch: $('tsViewSwitch'),
    tsCount: $('tsCount'),
    btnTsCopy: $('btnTsCopy'),
    btnTsExport: $('btnTsExport'),
    tsExportMenu: $('tsExportMenu'),
    tsBody: $('tsBody'),
    btnTsClose: $('btnTsClose'),
    dropOverlay: $('dropOverlay'),

    polishPanel: $('polishPanel'),
    polishOps: $('polishOps'),
    polishNote: $('polishNote'),
    polishLive: $('polishLive'),
    btnPolishRun: $('btnPolishRun'),
    btnPolishRevert: $('btnPolishRevert'),
    polishSummary: $('polishSummary'),
    polishEngine: $('polishEngine'),
    polishSummaryText: $('polishSummaryText'),
    polishKeywords: $('polishKeywords'),

    termsOverlay: $('termsOverlay'),
    termsAgree: $('termsAgree'),
    btnTermsOk: $('btnTermsOk'),

    toastWrap: $('toastWrap'),
  };

  // ------------------------------------------------------------- 状态
  const state = {
    url: '',
    info: null,
    selected: null,
    taskId: null,
    es: null,
    config: {},
    envProxy: '',
    busy: false,
  };

  const TERMINAL = new Set(['done', 'error', 'cancelled']);

  // 文案提取（平台字幕 + 语音转文字）状态
  const asr = {
    available: false,
    maxUpload: 0,
    taskId: null,
    doneTaskId: null,   // 已完成的文案任务 id —— 整理时要靠它把原文取回来
    es: null,
    language: 'zh',
    mode: 'auto',       // auto（字幕优先）| subtitle（仅字幕）| asr（仅语音）
    title: '',
    source: null,      // { url, title, thumbnail, extractor } —— 供切换语言后重新提取
    segments: [],
    text: '',
    view: 'plain',
    running: false,
    sourceKind: '',     // subtitle | asr
    subtitleKind: '',   // manual | auto
    subtitleLang: '',
    engineUsed: '',
    editing: false,
    undoStack: [],
    redoStack: [],
  };

  // 文案后处理状态
  const polish = {
    ready: false,          // 后端是否配好了模型服务
    model: '',             // 当前配置的模型名
    suggestedUrl: '',
    operations: [],        // 后端下发的可选动作
    ops: { clean: true, summary: true, keywords: true },
    taskId: null,
    es: null,
    running: false,
    applied: false,        // 当前展示的是否为整理后的结果
    paragraphs: [],
    summary: '',
    keywords: [],
    engine: '',
  };

  // ------------------------------------------------------------- 工具函数
  function fmtBytes(n) {
    if (!n || n <= 0) return '—';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0;
    let v = n;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
    const digits = i === 0 ? 0 : v >= 100 ? 0 : 1;
    return `${v.toFixed(digits)} ${units[i]}`;
  }

  function fmtSpeed(bps) {
    if (!bps || bps <= 0) return '—';
    return `${fmtBytes(bps)}/s`;
  }

  function fmtDuration(sec) {
    if (!sec || sec <= 0) return '';
    const s = Math.floor(sec % 60);
    const m = Math.floor((sec / 60) % 60);
    const h = Math.floor(sec / 3600);
    const pad = (x) => String(x).padStart(2, '0');
    return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
  }

  function fmtEta(sec) {
    if (sec === null || sec === undefined || sec < 0) return '—';
    if (sec < 60) return `${Math.round(sec)} 秒`;
    const m = Math.floor(sec / 60);
    const s = Math.round(sec % 60);
    if (m < 60) return s ? `${m} 分 ${s} 秒` : `${m} 分`;
    return `${Math.floor(m / 60)} 时 ${m % 60} 分`;
  }

  function fmtCount(n) {
    if (!n || n <= 0) return '';
    if (n >= 1e8) return `${(n / 1e8).toFixed(1)} 亿`;
    if (n >= 1e4) return `${(n / 1e4).toFixed(1)} 万`;
    return String(n);
  }

  function fmtDate(yyyymmdd) {
    if (!yyyymmdd || yyyymmdd.length !== 8) return '';
    return `${yyyymmdd.slice(0, 4)}-${yyyymmdd.slice(4, 6)}-${yyyymmdd.slice(6, 8)}`;
  }

  // 提取文案用的时间格式：mm:ss 与 SRT 的 HH:MM:SS,mmm
  function fmtStamp(sec) {
    const total = Math.max(0, Math.floor(sec || 0));
    const pad = (n) => String(n).padStart(2, '0');
    return `${pad(Math.floor(total / 60))}:${pad(total % 60)}`;
  }

  function fmtSrtTime(sec) {
    const value = Math.max(0, sec || 0);
    const ms = Math.round((value - Math.floor(value)) * 1000);
    const total = Math.floor(value);
    const pad = (n, w = 2) => String(n).padStart(w, '0');
    return `${pad(Math.floor(total / 3600))}:${pad(Math.floor(total / 60) % 60)}:${pad(total % 60)},${pad(ms, 3)}`;
  }

  // 中文句子直接拼接；英文之间补一个空格
  const _LATIN_TAIL = /[A-Za-z0-9]$/;
  const _LATIN_HEAD = /^[A-Za-z0-9]/;

  function joinText(buffer, piece) {
    if (!buffer) return piece;
    return _LATIN_TAIL.test(buffer) && _LATIN_HEAD.test(piece) ? `${buffer} ${piece}` : buffer + piece;
  }

  // 把逐句结果按长度聚合成自然段
  function groupParagraphs(limit = 120) {
    const paras = [];
    let buffer = '';
    for (const seg of asr.segments) {
      buffer = joinText(buffer, seg.text);
      if (buffer.length >= limit) {
        paras.push(buffer);
        buffer = '';
      }
    }
    if (buffer) paras.push(buffer);
    return paras;
  }

  function safeFilename(name, fallback = '文案') {
    const cleaned = String(name || '').replace(/[\\/:*?"<>|\n\r\t]/g, '_').trim();
    return (cleaned || fallback).slice(0, 60);
  }

  function isUrl(text) {
    return /^https?:\/\/\S+$/i.test((text || '').trim());
  }

  function thumbSrc(url) {
    if (!url) return '';
    return `/api/thumb?url=${encodeURIComponent(url)}`;
  }

  // ------------------------------------------------------------- 网络
  async function api(path, options = {}) {
    const res = await fetch(path, {
      method: options.method || 'GET',
      headers: options.body ? { 'Content-Type': 'application/json' } : undefined,
      body: options.body ? JSON.stringify(options.body) : undefined,
    });
    let data = {};
    try { data = await res.json(); } catch (_) { /* 忽略非 JSON 响应 */ }
    if (!res.ok || data.ok === false) {
      throw new Error(data.error || `请求失败（${res.status}）`);
    }
    return data;
  }

  // ------------------------------------------------------------- Toast
  function toast(message, type = '') {
    const node = document.createElement('div');
    node.className = `toast ${type}`.trim();
    node.textContent = message;
    el.toastWrap.appendChild(node);
    setTimeout(() => {
      node.classList.add('leaving');
      setTimeout(() => node.remove(), 320);
    }, type === 'error' ? 4200 : 2600);
  }

  // ------------------------------------------------------------- 主题
  function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    try { localStorage.setItem('vd-theme', theme); } catch (_) { /* 忽略 */ }
  }

  function initTheme() {
    let saved = 'dark';
    try { saved = localStorage.getItem('vd-theme') || 'dark'; } catch (_) { /* 忽略 */ }
    applyTheme(saved);
  }

  // ------------------------------------------------------------- 使用边界确认
  // 首次启动弹一次，用户确认后记在本地。
  // 版本号变更时（条款有实质修改）会重新弹 —— 这也是留一份「已告知」的证据。
  const TERMS_KEY = 'vd-terms-version';
  const TERMS_VERSION = '1';

  function initTerms() {
    let accepted = '';
    try { accepted = localStorage.getItem(TERMS_KEY) || ''; } catch (_) { /* 忽略 */ }
    if (accepted === TERMS_VERSION) return;

    el.termsOverlay.hidden = false;
    el.termsAgree.checked = false;
    el.btnTermsOk.disabled = true;

    el.termsAgree.addEventListener('change', () => {
      el.btnTermsOk.disabled = !el.termsAgree.checked;
    });

    el.btnTermsOk.addEventListener('click', () => {
      if (!el.termsAgree.checked) return;
      try { localStorage.setItem(TERMS_KEY, TERMS_VERSION); } catch (_) { /* 忽略 */ }
      el.termsOverlay.hidden = true;
      el.urlInput.focus();
    });
  }

  // ------------------------------------------------------------- 平台名
  function platformName(extractor) {
    if (!extractor) return '视频';
    const map = {
      BiliBili: 'B站', Bilibili: 'B站', Douyin: '抖音', Kuaishou: '快手',
      XiaoHongShu: '小红书', Weibo: '微博', Youtube: 'YouTube', Iqiyi: '爱奇艺',
      Youku: '优酷', Twitter: 'X', TikTok: 'TikTok', Instagram: 'Instagram',
      Douyu: '斗鱼', Huya: '虎牙', AcFun: 'AcFun', QQ: '腾讯视频',
    };
    return map[extractor] || extractor;
  }

  // ------------------------------------------------------------- 渲染：解析结果
  function renderPreview(info) {
    state.info = info;
    state.selected = (info.qualities.find((q) => q.recommended) || info.qualities[0] || null);

    el.platformBadge.textContent = platformName(info.extractor);
    el.previewTitle.textContent = info.title;

    // 元信息行
    const meta = [];
    if (info.uploader) meta.push(info.uploader);
    if (info.duration) meta.push(fmtDuration(info.duration));
    if (info.view_count) meta.push(`${fmtCount(info.view_count)}次播放`);
    if (info.upload_date) meta.push(fmtDate(info.upload_date));
    el.previewMeta.innerHTML = meta
      .map((t) => `<span class="pill">${escapeHtml(t)}</span>`)
      .join('');

    if (info.is_live) {
      const secs = info.live_record_seconds || 0;
      el.previewMeta.insertAdjacentHTML(
        'beforeend',
        `<span class="pill pill-sub" title="直播将限时录制一段，非完整回放">` +
        `直播 · 录制 ${secs || '?'} 秒</span>`,
      );
      toast(`检测到直播：将限时录制约 ${secs} 秒片段`, 'info');
    }

    // 字幕可用性：来自解析时的同一次元信息请求，零额外开销
    const subs = info.subtitles || {};
    const hasSub = !!(subs.manual && subs.manual.length) || !!(subs.auto && subs.auto.length);
    if (hasSub) {
      const manual = (subs.manual || []).length > 0;
      const langs = (manual ? subs.manual : subs.auto) || [];
      const zh = langs.find((l) => /^zh/i.test(l)) || langs[0] || '';
      el.previewMeta.insertAdjacentHTML(
        'beforeend',
        `<span class="pill pill-sub" title="提取文案时会直接取用平台字幕，秒出且零误差">` +
        `${manual ? '自带字幕' : '自动字幕'}${zh ? ` · ${escapeHtml(zh)}` : ''}</span>`,
      );
    }
    if (el.btnExtract) {
      el.btnExtract.title = hasSub
        ? '该视频有平台字幕，提取会很快'
        : '从视频语音中提取文字稿';
    }

    // 封面
    el.thumbImg.classList.remove('loaded');
    el.mediaFallback.hidden = false;
    if (info.thumbnail) {
      el.thumbImg.src = thumbSrc(info.thumbnail);
      el.thumbImg.onload = () => {
        el.thumbImg.classList.add('loaded');
        el.mediaFallback.hidden = true;
      };
      el.thumbImg.onerror = () => { el.mediaFallback.hidden = false; };
    } else {
      el.thumbImg.removeAttribute('src');
    }

    renderQualities();
    el.preview.hidden = false;
    el.emptyHint.hidden = true;
  }

  function renderQualities() {
    const qualities = state.info ? state.info.qualities : [];
    el.qualityGrid.innerHTML = '';
    el.qualityHint.textContent = `${qualities.length} 个可选档位`;

    qualities.forEach((q) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'quality-item' + (state.selected && state.selected.id === q.id ? ' selected' : '');
      btn.dataset.id = q.id;

      let tag = '';
      if (q.recommended) tag = '<span class="q-tag">推荐</span>';
      else if (q.audio_only) tag = '<span class="q-tag audio">音频</span>';
      else if (q.needs_merge) tag = '<span class="q-tag merge">合并</span>';

      const sizeText = q.filesize ? `≈ ${fmtBytes(q.filesize)}` : '体积未知';
      const bits = [q.resolution, q.dynamic_range, q.audio_language, q.tbr_mbps ? `${q.tbr_mbps} Mbps` : '']
        .filter(Boolean).join(' · ');
      btn.title = bits || q.label;
      btn.innerHTML = `${tag}<span class="q-label">${escapeHtml(q.label)}</span><span class="q-size">${sizeText}</span>`;

      btn.addEventListener('click', () => {
        state.selected = q;
        renderQualities();
      });
      el.qualityGrid.appendChild(btn);
    });
  }

  function escapeHtml(text) {
    return String(text == null ? '' : text).replace(/[&<>"']/g, (ch) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[ch]));
  }

  // ------------------------------------------------------------- 动作：解析
  async function doParse() {
    const url = el.urlInput.value.trim();
    if (!isUrl(url)) {
      el.urlField.classList.add('shake');
      setTimeout(() => el.urlField.classList.remove('shake'), 460);
      toast('请输入以 http:// 或 https:// 开头的素材链接', 'error');
      return;
    }
    if (state.busy) return;

    state.busy = true;
    state.url = url;
    el.btnParse.classList.add('loading');
    el.btnParse.disabled = true;

    try {
      const data = await api('/api/parse', { method: 'POST', body: { url } });
      renderPreview(data.info);
      el.preview.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    } catch (err) {
      toast(err.message || '解析失败', 'error');
    } finally {
      state.busy = false;
      el.btnParse.classList.remove('loading');
      el.btnParse.disabled = false;
    }
  }

  // ------------------------------------------------------------- 动作：下载
  async function startDownload() {
    if (!state.info || !state.selected) {
      toast('请先解析链接并选择清晰度', 'error');
      return;
    }
    const q = state.selected;

    el.btnDownload.disabled = true;
    el.donePanel.hidden = true;
    el.logPre.textContent = '';

    try {
      const data = await api('/api/download', {
        method: 'POST',
        body: {
          url: state.url,
          height: q.audio_only ? null : q.height,
          audio_only: !!q.audio_only,
          format_id: q.format_id || '',
          audio_format_id: q.audio_format_id || '',
          resolution: q.resolution || '',
          tier: q.tier || 0,
          title: state.info.title,
          thumbnail: state.info.thumbnail || '',
          extractor: state.info.extractor || '',
          label: q.label,
          is_live: !!state.info.is_live,
          live_record_seconds: state.info.live_record_seconds || 0,
        },
      });

      state.taskId = data.task.id;
      showProgress(data.task);
      watchProgress(data.task.id);
      refreshHistory();
    } catch (err) {
      toast(err.message || '无法启动下载', 'error');
      el.btnDownload.disabled = false;
    }
  }

  function showProgress(task) {
    el.progressPanel.hidden = false;
    el.emptyHint.hidden = true;

    el.pTitle.textContent = task.title || state.info?.title || '下载任务';
    el.pSubtitle.textContent = task.label ? `画质：${task.label}` : '准备中…';

    if (task.thumbnail) el.pThumb.src = thumbSrc(task.thumbnail);
    else el.pThumb.removeAttribute('src');

    setStage('download');
    updateProgressUI(task);
    el.progressPanel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  // ------------------------------------------------------------- SSE 订阅
  function watchProgress(taskId) {
    if (state.es) { state.es.close(); state.es = null; }

    const es = new EventSource(`/api/progress?id=${encodeURIComponent(taskId)}`);
    state.es = es;

    es.onmessage = (event) => {
      let payload;
      try { payload = JSON.parse(event.data); } catch (_) { return; }

      if (payload.type === 'log') {
        appendLog(payload.message);
        return;
      }
      if (payload.type === 'task') {
        handleTask(payload.task);
      }
    };

    es.addEventListener('end', () => {
      es.close();
      if (state.es === es) state.es = null;
    });

    es.onerror = () => {
      // 任务已进入终态时服务端会主动断开，这属正常现象
      if (!state.taskId) return;
    };
  }

  function appendLog(message) {
    const line = String(message || '').trim();
    if (!line) return;
    const atBottom = el.logPre.scrollTop + el.logPre.clientHeight >= el.logPre.scrollHeight - 24;
    el.logPre.textContent += `${line}\n`;
    if (atBottom) el.logPre.scrollTop = el.logPre.scrollHeight;
  }

  function handleTask(task) {
    if (task.id !== state.taskId) return;

    el.pTitle.textContent = task.title || el.pTitle.textContent;
    if (task.label) el.pSubtitle.textContent = `画质：${task.label}`;
    updateProgressUI(task);

    if (task.stage === 'downloading') setStage('download');
    else if (task.stage === 'postprocess') setStage('merge');
    else if (task.stage === 'done') {
      setStage('done');
      finishTask(task, true);
    } else if (task.stage === 'error') {
      finishTask(task, false);
    } else if (task.stage === 'cancelled') {
      finishTask(task, false, true);
    }
  }

  function updateProgressUI(task) {
    const pct = typeof task.percent === 'number' ? task.percent : null;

    if (pct === null) {
      el.bar.classList.add('indeterminate');
      el.barPercent.textContent = '处理中';
      el.barFill.style.width = '34%';
    } else {
      el.bar.classList.remove('indeterminate');
      el.barPercent.textContent = `${Math.floor(pct)}%`;
      el.barFill.style.width = `${Math.max(0, Math.min(100, pct))}%`;
    }

    if (task.stage === 'downloading' && task.stream_count > 1) {
      el.barNote.textContent = `第 ${task.stream_index}/${task.stream_count} 条流`;
    } else if (task.stage === 'postprocess') {
      el.barNote.textContent = task.message || '正在处理…';
    } else if (task.stage === 'done') {
      el.barNote.textContent = '已完成';
    } else if (task.stage === 'cancelled') {
      el.barNote.textContent = '已取消';
    } else if (task.stage === 'error') {
      el.barNote.textContent = '失败';
    } else {
      el.barNote.textContent = '';
    }

    el.statSpeed.textContent = fmtSpeed(task.speed);
    el.statSize.textContent = task.total_bytes
      ? `${fmtBytes(task.downloaded_bytes)} / ${fmtBytes(task.total_bytes)}`
      : fmtBytes(task.downloaded_bytes);
    el.statEta.textContent = task.stage === 'downloading' ? fmtEta(task.eta) : '—';
    el.statName.textContent = task.filename || '—';
    el.statName.title = task.filename || '';

    if (task.stage === 'done') {
      el.statSpeed.textContent = '—';
      el.statEta.textContent = '—';
    }
  }

  // ------------------------------------------------------------- 阶段轨道
  const STAGE_ORDER = ['parse', 'download', 'merge', 'done'];

  function setStage(current) {
    const nodes = Array.from(el.stageTrack.querySelectorAll('.stage-node'));
    const lines = Array.from(el.stageTrack.querySelectorAll('.stage-line'));
    const currentIndex = STAGE_ORDER.indexOf(current);

    nodes.forEach((node, i) => {
      node.classList.toggle('done', i < currentIndex);
      node.classList.toggle('active', i === currentIndex && current !== 'done');
      if (current === 'done') node.classList.toggle('done', true);
    });
    lines.forEach((line, i) => line.classList.toggle('filled', i < currentIndex));
  }

  // ------------------------------------------------------------- 收尾
  function finishTask(task, success, cancelled = false) {
    if (state.es) { state.es.close(); state.es = null; }
    state.taskId = null;
    el.btnDownload.disabled = false;

    if (success) {
      el.doneName.textContent = task.filename || task.filepath || '文件已保存';
      el.donePanel.hidden = false;
      el.donePanel.dataset.path = task.filepath || '';
      toast('下载完成', 'success');
      refreshHistory();
      return;
    }

    if (cancelled) {
      toast('已取消下载');
    } else {
      toast(task.message || '下载失败', 'error');
      appendLog(`✕ ${task.message || '下载失败'}`);
    }
    refreshHistory();
  }

  // ------------------------------------------------------------- 文案提取
  async function loadAsrStatus() {
    try {
      const data = await api('/api/asr/status');
      asr.available = !!data.available;
      asr.maxUpload = data.max_upload || 0;

      // 引擎可用性：在设置页给出「这台机器实际能用哪个引擎」的准确提示
      const engines = (data.engines && data.engines.engines) || {};
      const installed = Object.keys(engines).filter((key) => engines[key]);
      if (el.asrEngineTip) {
        if (!data.available) {
          el.asrEngineTip.textContent = '语音识别环境未就绪，请先运行一次安装脚本。';
        } else if (!installed.length) {
          el.asrEngineTip.textContent = '识别环境里还没装引擎，请重新运行安装脚本。';
        } else {
          const labels = { mlx: 'MLX（Apple 芯片）', faster: 'Faster-Whisper（通用）' };
          const names = installed.map((key) => labels[key] || key).join('、');
          const rec = data.engines.recommended || 'auto';
          el.asrEngineTip.textContent = `本机已装：${names}。推荐使用「${rec === 'auto' ? '自动选择' : labels[rec] || rec}」。`;
        }
      }
    } catch (_) {
      asr.available = false;
    }

    const hint = asr.available ? '从视频语音中提取文字稿' : '语音识别环境未就绪，请先运行一次安装脚本';
    [el.btnExtract, el.btnExtractDone].forEach((btn) => {
      if (!btn) return;
      // 不置灰：即使没装识别环境，「仅用平台字幕」这条路依然可用
      btn.classList.toggle('needs-setup', !asr.available);
      btn.title = hint;
    });
  }

  function setSourceBadge(task) {
    if (!el.tsSource) return;
    const kind = (task && task.source_kind) || asr.sourceKind;
    if (!kind) {
      el.tsSource.hidden = true;
      return;
    }
    const subKind = (task && task.subtitle_kind) || asr.subtitleKind;
    const lang = (task && task.subtitle_lang) || asr.subtitleLang;
    const engine = (task && task.engine_used) || asr.engineUsed;

    let label = '';
    let cls = '';
    if (kind === 'subtitle') {
      const manual = subKind !== 'auto';
      label = manual ? '平台字幕' : '平台自动字幕';
      cls = manual ? 'is-subtitle' : 'is-subtitle-auto';
      if (lang) label += ` · ${lang}`;
    } else {
      label = '语音识别';
      cls = 'is-asr';
      if (engine) label += ` · ${engine === 'faster' ? 'Faster-Whisper' : 'MLX'}`;
    }
    el.tsSource.textContent = label;
    el.tsSource.className = `ts-source ${cls}`;
    el.tsSource.hidden = false;
    el.tsSource.title = kind === 'subtitle'
      ? (subKind === 'auto'
        ? '使用平台自动生成的字幕，可能没有标点、专有名词有误，建议用「整理文案」修一遍'
        : '使用平台提供的字幕，内容准确、时间轴精确')
      : '平台没有字幕，已由本地语音识别转写';
  }

  function resetTranscript() {
    asr.segments = [];
    asr.text = '';
    asr.view = 'plain';
    asr.sourceKind = '';
    asr.subtitleKind = '';
    asr.subtitleLang = '';
    asr.engineUsed = '';
    asr.editing = false;
    asr.undoStack = [];
    asr.redoStack = [];
    el.tsLive.innerHTML = '';
    el.tsBody.innerHTML = '';
    el.tsCount.textContent = '';
    el.tsExportMenu.hidden = true;
    if (el.tsSource) el.tsSource.hidden = true;
    el.tsViewSwitch.querySelectorAll('button').forEach((btn) => {
      btn.classList.toggle('active', btn.dataset.view === 'plain');
    });
    updateEditButtons();
    resetPolishState();
  }

  // ------------------------------------------------------------- 文案整理
  function resetPolishState() {
    polish.applied = false;
    polish.paragraphs = [];
    polish.summary = '';
    polish.keywords = [];
    polish.engine = '';
    if (polish.es) { polish.es.close(); polish.es = null; }
    polish.taskId = null;
    polish.running = false;
    if (el.polishLive) { el.polishLive.hidden = true; el.polishLive.textContent = ''; }
    if (el.polishSummary) el.polishSummary.hidden = true;
    if (el.btnPolishRevert) el.btnPolishRevert.hidden = true;
    if (el.btnPolishRun) { el.btnPolishRun.disabled = false; el.btnPolishRun.textContent = '开始整理'; }
  }

  async function loadPolishStatus() {
    try {
      const data = await api('/api/polish/status');
      const cap = data.capability || {};
      polish.ready = !!cap.llm_ready;
      polish.model = cap.model || '';
      polish.suggestedUrl = data.suggested_base_url || '';
      polish.operations = cap.operations || [];
    } catch (_) {
      polish.ready = false;
    }
    renderPolishOps();
    renderPolishNote();
  }

  function renderPolishOps() {
    if (!el.polishOps) return;
    const ops = polish.operations.length
      ? polish.operations
      : [
        { id: 'clean', label: '标点修复 + 自动分段', needs_llm: false },
        { id: 'summary', label: '内容摘要', needs_llm: true },
        { id: 'keywords', label: '关键词', needs_llm: true },
      ];
    el.polishOps.innerHTML = ops.map((op) => (
      `<button type="button" class="polish-op" data-op="${op.id}" data-needs-llm="${op.needs_llm ? 1 : 0}"` +
      ` aria-pressed="${polish.ops[op.id] ? 'true' : 'false'}"` +
      ` title="${op.needs_llm ? '需要配置模型服务' : '本地完成，无需联网'}">${escapeHtml(op.label)}</button>`
    )).join('');
  }

  function renderPolishNote() {
    if (!el.polishNote) return;
    const needsLlm = ['summary', 'keywords'].some((key) => polish.ops[key]);
    if (polish.ready) {
      el.polishNote.className = 'polish-note';
      const who = polish.model ? `已接上 ${escapeHtml(polish.model)}` : '已接上大模型';
      el.polishNote.innerHTML =
        `${who}，可修复标点、按语义分段并生成摘要；未勾选的动作会跳过。`;
      return;
    }
    el.polishNote.className = 'polish-note is-warn';
    el.polishNote.innerHTML = needsLlm
      ? '未配置模型服务，摘要与关键词将跳过。本地仍会完成去重、分段与标点归一 —— ' +
        '到 <button class="link-btn" id="polishOpenSettings">设置</button> 里填一个模型地址即可解锁全部能力。'
      : '未配置模型服务，将仅使用本地规则整理。';
    const link = $('polishOpenSettings');
    if (link) {
      link.addEventListener('click', (e) => { e.stopPropagation(); openDrawer(el.settingsDrawer); });
    }
  }

  async function runPolish() {
    if (!asr.doneTaskId) { toast('还没有可整理的文案', 'error'); return; }
    const operations = Object.keys(polish.ops).filter((key) => polish.ops[key]);
    if (!operations.length) { toast('请至少选择一项整理内容', 'error'); return; }
    if (!polish.ready) {
      const needLlm = operations.filter((key) => key !== 'clean');
      if (needLlm.length) { /* 后端会跳过，本地整理照常跑 */ }
    }

    polish.running = true;
    polish.applied = false;
    if (el.polishLive) { el.polishLive.hidden = false; el.polishLive.textContent = ''; }
    if (el.btnPolishRun) { el.btnPolishRun.disabled = true; el.btnPolishRun.textContent = '整理中…'; }

    try {
      const data = await api('/api/polish/start', {
        method: 'POST',
        body: { source_id: asr.doneTaskId, operations },
      });
      polish.taskId = data.task.id;
      watchPolish(data.task.id);
    } catch (err) {
      polish.running = false;
      if (el.btnPolishRun) { el.btnPolishRun.disabled = false; el.btnPolishRun.textContent = '开始整理'; }
      if (el.polishLive) el.polishLive.hidden = true;
      toast(err.message || '无法启动整理', 'error');
    }
  }

  function watchPolish(taskId) {
    if (polish.es) { polish.es.close(); polish.es = null; }
    const es = new EventSource(`/api/polish/progress?id=${encodeURIComponent(taskId)}`);
    polish.es = es;

    es.onmessage = (event) => {
      let payload;
      try { payload = JSON.parse(event.data); } catch (_) { return; }
      if (payload.type === 'task') {
        handlePolishTask(payload.task);
      } else if (payload.type === 'delta') {
        if (payload.reset) {
          if (el.polishLive) el.polishLive.textContent = '';
        } else if (payload.text && el.polishLive) {
          el.polishLive.textContent += payload.text;
          el.polishLive.scrollTop = el.polishLive.scrollHeight;
        }
      } else if (payload.type === 'result') {
        applyPolishResult(payload);
      } else if (payload.type === 'log') {
        toast(payload.message);
      }
    };

    es.addEventListener('end', () => {
      es.close();
      if (polish.es === es) polish.es = null;
    });
    es.onerror = () => { /* 终态时服务端主动断开，属正常 */ };
  }

  function applyPolishResult(payload) {
    polish.paragraphs = Array.isArray(payload.paragraphs) ? payload.paragraphs : [];
    polish.summary = payload.summary || '';
    polish.keywords = Array.isArray(payload.keywords) ? payload.keywords : [];
    polish.applied = polish.paragraphs.length > 0 || !!polish.summary;
    if (polish.applied) {
      renderPolishSummary();
      renderAsrBody();
      if (el.btnPolishRevert) el.btnPolishRevert.hidden = false;
      const before = asr.text.length;
      const after = (payload.text || '').length;
      if (before && after && Math.abs(after - before) / before > 0.4) {
        // 字数变化过大通常意味着模型顺手改写了内容，提醒一下
        toast('整理后字数变化较大，建议对照原文检查', 'error');
      }
    }
  }

  function renderPolishSummary() {
    if (!el.polishSummary) return;
    const hasAny = polish.summary || polish.keywords.length;
    el.polishSummary.hidden = !hasAny;
    if (!hasAny) return;
    el.polishSummaryText.textContent = polish.summary || '（模型未返回摘要）';
    el.polishSummaryText.hidden = !polish.summary;
    el.polishEngine.textContent = polish.engine ? `由 ${polish.engine} 生成` : '';
    el.polishKeywords.innerHTML = polish.keywords
      .map((word) => `<span class="kw-chip">${escapeHtml(word)}</span>`)
      .join('');
  }

  function revertPolish() {
    polish.applied = false;
    if (el.polishSummary) el.polishSummary.hidden = true;
    if (el.btnPolishRevert) el.btnPolishRevert.hidden = true;
    renderAsrBody();
    toast('已显示原文');
  }

  function handlePolishTask(task) {
    if (!task) return;
    if (polish.taskId && task.id !== polish.taskId) return;
    const engineChanged = !!task.engine && task.engine !== polish.engine;
    polish.engine = task.engine || polish.engine;
    // 引擎名（模型名）是在 result 事件之后才广播的，这里补一次渲染，
    // 否则摘要角标会一直停在「本地规则」。
    if (engineChanged && (polish.summary || polish.keywords.length)) {
      renderPolishSummary();
    }

    if (task.stage === 'done') {
      polish.running = false;
      if (polish.es) { polish.es.close(); polish.es = null; }
      polish.taskId = null;
      if (el.polishLive) el.polishLive.hidden = true;
      if (el.btnPolishRun) { el.btnPolishRun.disabled = false; el.btnPolishRun.textContent = '重新整理'; }
      toast('文案整理完成', 'success');
    } else if (task.stage === 'error') {
      polish.running = false;
      if (polish.es) { polish.es.close(); polish.es = null; }
      polish.taskId = null;
      if (el.btnPolishRun) { el.btnPolishRun.disabled = false; el.btnPolishRun.textContent = '重试'; }
      if (el.polishLive) el.polishLive.hidden = true;
      toast(task.message || '整理失败', 'error');
    } else if (task.stage === 'cancelled') {
      polish.running = false;
      if (polish.es) { polish.es.close(); polish.es = null; }
      polish.taskId = null;
      if (el.btnPolishRun) { el.btnPolishRun.disabled = false; el.btnPolishRun.textContent = '开始整理'; }
      if (el.polishLive) el.polishLive.hidden = true;
    } else if (task.message && el.polishLive) {
      el.polishLive.hidden = false;
    }
  }

  function showTranscript(title) {
    el.transcriptPanel.hidden = false;
    el.tsTitle.textContent = title || '文案提取';
    el.tsMeta.textContent = '准备中…';
    el.tsRunning.hidden = false;
    el.tsDone.hidden = true;
    el.tsPercent.textContent = '0%';
    el.tsMessage.textContent = '准备中…';
    el.tsBarFill.style.width = '0%';
    el.transcriptPanel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  function hideTranscript() {
    if (asr.es) { asr.es.close(); asr.es = null; }
    if (polish.es) { polish.es.close(); polish.es = null; }
    asr.taskId = null;
    asr.running = false;
    el.transcriptPanel.hidden = true;
  }

  function setTsProgress(percent, message) {
    const pct = Math.max(0, Math.min(100, Number(percent) || 0));
    el.tsBarFill.style.width = `${pct}%`;
    el.tsPercent.textContent = `${Math.floor(pct)}%`;
    if (message) {
      el.tsMessage.textContent = message;
      el.tsMeta.textContent = message;
    }
  }

  function appendLive(text) {
    const node = document.createElement('div');
    node.className = 'ts-live-item';
    node.textContent = text;
    el.tsLive.appendChild(node);
    while (el.tsLive.childElementCount > 40) el.tsLive.firstElementChild.remove();
    el.tsLive.scrollTop = el.tsLive.scrollHeight;
  }

  function watchAsr(taskId) {
    if (asr.es) { asr.es.close(); asr.es = null; }
    const es = new EventSource(`/api/asr/progress?id=${encodeURIComponent(taskId)}`);
    asr.es = es;

    es.onmessage = (event) => {
      let payload;
      try { payload = JSON.parse(event.data); } catch (_) { return; }
      if (payload.type === 'task') {
        handleAsrTask(payload.task);
      } else if (payload.type === 'segment') {
        asr.segments.push(payload.segment);
        appendLive(payload.segment.text);
      } else if (payload.type === 'subtitle') {
        // 命中平台字幕：先亮出来源徽标，让用户知道这份稿子是官方文本
        asr.sourceKind = 'subtitle';
        asr.subtitleKind = payload.kind || '';
        asr.subtitleLang = payload.language || '';
        setSourceBadge(null);
        appendLive(payload.message || '已获取平台字幕');
      } else if (payload.type === 'result') {
        // 后端下发的完整结果最权威，用它覆盖增量累积
        asr.text = payload.text || '';
        if (Array.isArray(payload.segments) && payload.segments.length) {
          asr.segments = payload.segments;
        }
      }
    };

    es.addEventListener('end', () => {
      es.close();
      if (asr.es === es) asr.es = null;
    });
    es.onerror = () => { /* 终态时服务端主动断开，属正常 */ };
  }

  function handleAsrTask(task) {
    if (!task) return;
    if (asr.taskId && task.id !== asr.taskId) return;
    setTsProgress(task.percent, task.message);

    // 来源信息可能在任务快照里补齐（比 subtitle 事件晚到）
    if (task.source_kind) {
      asr.sourceKind = task.source_kind;
      asr.subtitleKind = task.subtitle_kind || asr.subtitleKind;
      asr.subtitleLang = task.subtitle_lang || asr.subtitleLang;
    }
    if (task.engine_used) asr.engineUsed = task.engine_used;
    if (asr.sourceKind) setSourceBadge(task);

    if (task.stage === 'done') {
      asr.running = false;
      asr.title = task.title || el.tsTitle.textContent;
      asr.doneTaskId = task.id;
      if (!asr.text) asr.text = task.text || '';
      setSourceBadge(task);
      if (asr.es) { asr.es.close(); asr.es = null; }
      asr.taskId = null;
      renderTranscript();
      const fromSubtitle = asr.sourceKind === 'subtitle';
      toast(fromSubtitle ? `已取到平台字幕 · ${asr.segments.length} 句` : '文案提取完成', 'success');
    } else if (task.stage === 'error') {
      asr.running = false;
      if (asr.es) { asr.es.close(); asr.es = null; }
      asr.taskId = null;
      el.btnTsCancel.hidden = true;
      el.tsMessage.classList.add('error');
      setTsProgress(0, task.message || '提取失败');
      toast(task.message || '提取失败', 'error');
    } else if (task.stage === 'cancelled') {
      asr.running = false;
      if (asr.es) { asr.es.close(); asr.es = null; }
      asr.taskId = null;
      toast('已取消提取');
      hideTranscript();
    }
  }

  function renderTranscript() {
    el.tsRunning.hidden = true;
    el.tsDone.hidden = false;
    if (asr.segments.length) {
      const summary = `共 ${asr.segments.length} 句 · ${asr.text.length} 字`;
      el.tsMeta.textContent = summary;
      el.tsCount.textContent = summary;
    } else {
      el.tsMeta.textContent = '未识别到语音内容';
      el.tsCount.textContent = '';
    }
    renderAsrBody();
  }

  function syncAsrText() {
    asr.text = asr.segments.map((s) => (s.text || '').trim()).filter(Boolean).join('\n');
  }

  function snapshotSegments() {
    return asr.segments.map((s) => ({
      start: s.start, end: s.end, text: s.text || '',
    }));
  }

  function pushUndo() {
    asr.undoStack.push(snapshotSegments());
    if (asr.undoStack.length > 40) asr.undoStack.shift();
    asr.redoStack = [];
    updateEditButtons();
  }

  function updateEditButtons() {
    if (el.btnTsEdit) {
      el.btnTsEdit.classList.toggle('btn-editing', !!asr.editing);
      el.btnTsEdit.textContent = asr.editing ? '完成校对' : '校对';
    }
    if (el.btnTsUndo) {
      el.btnTsUndo.hidden = !asr.editing;
      el.btnTsUndo.disabled = !asr.undoStack.length;
    }
    if (el.btnTsRedo) {
      el.btnTsRedo.hidden = !asr.editing;
      el.btnTsRedo.disabled = !asr.redoStack.length;
    }
  }

  function applySegmentEdit(index, nextText) {
    if (!asr.segments[index]) return;
    const prev = asr.segments[index].text || '';
    const text = String(nextText || '').replace(/\s+/g, ' ').trim();
    if (text === prev.trim()) return;
    pushUndo();
    const seg = asr.segments[index];
    const oldLen = Math.max(1, (prev || '').length);
    const newLen = Math.max(1, text.length);
    const span = Math.max(0.4, (Number(seg.end) || 0) - (Number(seg.start) || 0));
    // 改字后按长度比例微调本段时长，保持与邻段不重叠
    let newEnd = Number(seg.start) + span * (newLen / oldLen);
    const next = asr.segments[index + 1];
    if (next && Number(next.start) > Number(seg.start)) {
      newEnd = Math.min(newEnd, Number(next.start) - 0.05);
    }
    seg.text = text;
    seg.end = Math.max(Number(seg.start) + 0.4, Math.round(newEnd * 100) / 100);
    syncAsrText();
    const summary = `共 ${asr.segments.length} 句 · ${asr.text.length} 字（已校对）`;
    el.tsMeta.textContent = summary;
    el.tsCount.textContent = summary;
  }

  function toggleProofread() {
    if (!asr.segments.length) {
      toast('没有可校对的句子', 'error');
      return;
    }
    if (polish.applied) {
      toast('请先点「看原文」再校对时间轴句子', 'error');
      return;
    }
    asr.editing = !asr.editing;
    if (asr.editing) {
      asr.view = 'timed';
      document.querySelectorAll('#tsViewSwitch [data-view]').forEach((btn) => {
        btn.classList.toggle('active', btn.getAttribute('data-view') === 'timed');
      });
    }
    updateEditButtons();
    renderAsrBody();
    toast(asr.editing ? '可直接改字；导出 SRT 会带上修改' : '已退出校对', 'success');
  }

  function undoProofread() {
    if (!asr.undoStack.length) return;
    asr.redoStack.push(snapshotSegments());
    asr.segments = asr.undoStack.pop();
    syncAsrText();
    updateEditButtons();
    renderAsrBody();
  }

  function redoProofread() {
    if (!asr.redoStack.length) return;
    asr.undoStack.push(snapshotSegments());
    asr.segments = asr.redoStack.pop();
    syncAsrText();
    updateEditButtons();
    renderAsrBody();
  }

  function renderAsrBody() {
    if (!asr.segments.length) {
      el.tsBody.innerHTML = '<div class="ts-empty">没有识别到可用的语音内容，试试换一种语言或换一个视频</div>';
      return;
    }

    const usePolish = polish.applied;

    if (asr.view === 'timed') {
      // 时间戳视图：整理后的段落也带时间轴（按文本比例对齐回原始分段）
      const rows = (usePolish && polish.paragraphs.length) ? polish.paragraphs : asr.segments;
      const editable = asr.editing && !usePolish;
      const hint = editable
        ? '<p class="ts-edit-hint">校对模式：点击句子改字，失焦即保存；撤销/重做可用。导出 SRT 使用当前时间轴。</p>'
        : '';
      el.tsBody.innerHTML = hint + rows.map((row, i) => (
        `<div class="ts-line${usePolish ? ' is-cleaned' : ''}${editable ? ' is-editing' : ''}" data-idx="${i}">` +
        `<span class="ts-stamp">${fmtStamp(row.start)}</span>` +
        `<p${editable ? ' contenteditable="true" spellcheck="true"' : ''}>${escapeHtml(row.text)}</p></div>`
      )).join('');
      if (editable) {
        el.tsBody.querySelectorAll('.ts-line[data-idx] p[contenteditable]').forEach((node) => {
          node.addEventListener('blur', () => {
            const wrap = node.closest('[data-idx]');
            const idx = wrap ? parseInt(wrap.getAttribute('data-idx'), 10) : -1;
            if (idx >= 0) applySegmentEdit(idx, node.textContent || '');
          });
          node.addEventListener('keydown', (ev) => {
            if (ev.key === 'Enter') {
              ev.preventDefault();
              node.blur();
            }
          });
        });
      }
      return;
    }

    if (usePolish && polish.paragraphs.length) {
      el.tsBody.innerHTML = polish.paragraphs.map((para) => (
        `<div class="ts-para is-cleaned"><span class="ts-stamp">${fmtStamp(para.start)}</span>` +
        `<p>${escapeHtml(para.text)}</p></div>`
      )).join('');
      return;
    }

    el.tsBody.innerHTML = groupParagraphs()
      .map((para) => `<p>${escapeHtml(para)}</p>`)
      .join('');
  }

  async function beginExtract(payload) {
    // 只有「需要跑语音识别」的模式才依赖本地环境
    if (!asr.available && asr.mode !== 'subtitle') {
      const hasSub = !!(state.info && state.info.subtitles
        && ((state.info.subtitles.manual || []).length || (state.info.subtitles.auto || []).length));
      toast(hasSub
        ? '识别环境未就绪，但该视频自带字幕，可把文案来源改为「仅用平台字幕」'
        : '语音识别环境未就绪，请先运行一次安装脚本', 'error');
      return;
    }
    resetTranscript();
    showTranscript(payload.title || '文案提取');
    el.btnTsCancel.hidden = false;
    el.tsMessage.classList.remove('error');
    asr.running = true;
    asr.doneTaskId = null;
    asr.source = payload;
    asr.mode = el.tsMode.value;
    asr.language = el.tsLang.value;
    setTsProgress(0, asr.mode === 'subtitle' ? '正在获取平台字幕…' : '正在创建任务…');

    try {
      const body = Object.assign(
        { language: asr.language, mode: asr.mode },
        payload,
      );
      const data = await api('/api/asr/start', { method: 'POST', body });
      asr.taskId = data.task.id;
      watchAsr(data.task.id);
      handleAsrTask(data.task);
    } catch (err) {
      asr.running = false;
      el.btnTsCancel.hidden = true;
      el.tsMessage.classList.add('error');
      setTsProgress(0, err.message || '无法启动提取');
      toast(err.message || '无法启动提取', 'error');
    }
  }

  async function extractFromCurrent() {
    if (!state.info || !state.url) {
      toast('请先解析一个素材链接', 'error');
      return;
    }
    await beginExtract({
      url: state.url,
      title: state.info.title || '',
      thumbnail: state.info.thumbnail || '',
      extractor: state.info.extractor || '',
    });
  }

  async function extractFromFile(file) {
    if (!file) return;
    if (!asr.available) {
      toast('语音识别环境未就绪，请先运行一次安装脚本', 'error');
      return;
    }
    if (asr.maxUpload && file.size > asr.maxUpload) {
      toast('文件过大，超出可处理上限', 'error');
      return;
    }

    resetTranscript();
    showTranscript(file.name);
    el.btnTsCancel.hidden = false;
    el.tsMessage.classList.remove('error');
    asr.running = true;
    asr.source = { uploadName: file.name };
    setTsProgress(0, `正在上传 ${file.name}…`);

    try {
      const res = await fetch(
        `/api/asr/upload?language=${encodeURIComponent(asr.language)}&mode=asr`,
        {
          method: 'POST',
          headers: {
            'X-File-Name': encodeURIComponent(file.name),
            'Content-Type': 'application/octet-stream',
          },
          body: file,
        },
      );
      const data = await res.json().catch(() => ({}));
      if (!res.ok || data.ok === false) throw new Error(data.error || '上传失败');
      asr.taskId = data.task.id;
      watchAsr(data.task.id);
      handleAsrTask(data.task);
    } catch (err) {
      asr.running = false;
      el.btnTsCancel.hidden = true;
      el.tsMessage.classList.add('error');
      setTsProgress(0, err.message || '上传失败');
      toast(err.message || '上传失败', 'error');
    }
  }

  function buildSrt() {
    return asr.segments.map((seg, index) => (
      `${index + 1}\n${fmtSrtTime(seg.start)} --> ${fmtSrtTime(seg.end)}\n${seg.text}\n`
    )).join('\n');
  }

  // 纯文本形态的正文：整理过就用整理后的段落
  function plainParagraphs() {
    if (polish.applied && polish.paragraphs.length) {
      return polish.paragraphs.map((para) => para.text);
    }
    return groupParagraphs();
  }

  function buildMarkdown() {
    const meta = asr.source || {};
    const lines = [`# ${asr.title || '文案'}`, ''];
    if (meta.extractor) lines.push(`- 平台：${platformName(meta.extractor)}`);
    if (meta.url) lines.push(`- 来源：${meta.url}`);
    if (meta.uploadName) lines.push(`- 来源：本地文件 ${meta.uploadName}`);
    if (asr.sourceKind === 'subtitle') {
      lines.push(`- 文案来源：${asr.subtitleKind === 'auto' ? '平台自动字幕' : '平台字幕'}${asr.subtitleLang ? `（${asr.subtitleLang}）` : ''}`);
    } else if (asr.sourceKind === 'asr') {
      lines.push(`- 文案来源：本地语音识别${asr.engineUsed ? `（${asr.engineUsed === 'faster' ? 'Faster-Whisper' : 'MLX'}）` : ''}`);
    }
    if (polish.applied) {
      lines.push(`- 整理：${polish.engine || '本地规则'}`);
    }
    lines.push(`- 句数：${asr.segments.length}`);
    lines.push(`- 字数：${asr.text.length}`);
    lines.push(`- 提取时间：${new Date().toLocaleString('zh-CN')}`);
    lines.push('', '---', '');

    // 摘要在正文之前 —— 归档时一眼能看到重点
    if (polish.applied && polish.summary) {
      lines.push('## 摘要', '', polish.summary, '');
      if (polish.keywords.length) {
        lines.push(`**关键词**：${polish.keywords.join('、')}`, '');
      }
      lines.push('---', '');
    }
    for (const para of plainParagraphs()) lines.push(para, '');
    return lines.join('\n');
  }

  function exportAs(fmt) {
    if (!asr.text && !asr.segments.length) {
      toast('还没有可导出的内容', 'error');
      return;
    }
    const base = safeFilename(asr.title || '文案');
    // 纯文本按自然段分行，避免导出一大坨连续文字
    let content = plainParagraphs().join('\n\n');
    let mime = 'text/plain;charset=utf-8';
    let ext = 'txt';
    let bom = '\ufeff';   // 让 Windows 记事本正确识别 UTF-8

    if (fmt === 'srt') {
      content = buildSrt();
      ext = 'srt';
    } else if (fmt === 'md') {
      content = buildMarkdown();
      mime = 'text/markdown;charset=utf-8';
      ext = 'md';
      bom = '';
    }

    const blob = new Blob([bom + content], { type: mime });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `${base}.${ext}`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1500);
    el.tsExportMenu.hidden = true;
    toast(`已导出 ${ext.toUpperCase()} 文件`, 'success');
  }

  async function copyTranscript() {
    if (!asr.text) { toast('还没有可复制的内容', 'error'); return; }
    // 复制「界面上看到的」那份：整理过就复制整理后的
    const payload = (polish.applied && polish.paragraphs.length)
      ? plainParagraphs().join('\n\n')
      : asr.text;
    try {
      await navigator.clipboard.writeText(payload);
      toast(polish.applied ? '已复制整理后的全文' : '已复制全文', 'success');
    } catch (_) {
      const area = document.createElement('textarea');
      area.value = payload;
      area.style.position = 'fixed';
      area.style.opacity = '0';
      document.body.appendChild(area);
      area.select();
      try {
        document.execCommand('copy');
        toast('已复制全文', 'success');
      } catch (err) {
        toast('复制失败，请手动选择文本', 'error');
      }
      area.remove();
    }
  }

  function bindDropZone() {
    let depth = 0;
    const hasFiles = (e) => Array.from((e.dataTransfer && e.dataTransfer.types) || []).includes('Files');

    window.addEventListener('dragenter', (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      depth += 1;
      el.dropOverlay.hidden = false;
    });
    window.addEventListener('dragover', (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy';
    });
    window.addEventListener('dragleave', (e) => {
      if (!hasFiles(e)) return;
      depth = Math.max(0, depth - 1);
      if (depth === 0) el.dropOverlay.hidden = true;
    });
    window.addEventListener('drop', (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      depth = 0;
      el.dropOverlay.hidden = true;
      const file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (file) extractFromFile(file);
    });
  }

  // ------------------------------------------------------------- 历史记录
  async function refreshHistory() {
    try {
      const data = await api('/api/tasks');
      const tasks = data.tasks || [];
      state.history = tasks;
      renderHistory(tasks);

      const running = tasks.filter((t) => !TERMINAL.has(t.stage)).length;
      el.historyBadge.hidden = running === 0;
    } catch (_) { /* 静默失败 */ }
  }

  const STATUS_TEXT = {
    done: '已完成', error: '失败', cancelled: '已取消',
    downloading: '下载中', postprocess: '处理中', queued: '排队中',
  };

  function renderHistory(tasks) {
    if (!tasks.length) {
      el.historyList.innerHTML = '<div class="drawer-empty">还没有下载记录<br />粘贴一个素材链接开始吧</div>';
      return;
    }

    el.historyList.innerHTML = tasks.map((t) => {
      const statusClass = TERMINAL.has(t.stage) ? t.stage : 'running';
      const display = STATUS_TEXT[t.stage] || t.stage;
      const pct = typeof t.percent === 'number' && !TERMINAL.has(t.stage)
        ? ` · ${Math.floor(t.percent)}%`
        : (t.label ? ` · ${t.label}` : '');
      const thumb = t.thumbnail ? thumbSrc(t.thumbnail) : '';
      return `
        <div class="history-item">
          <img class="history-thumb" src="${thumb}" alt="" data-hide-on-error="1" />
          <div class="history-info">
            <b title="${escapeHtml(t.title || t.url)}">${escapeHtml(t.title || '未命名')}</b>
            <span>${escapeHtml(platformName(t.extractor))}${escapeHtml(pct)}</span>
          </div>
          <span class="history-status ${statusClass}">${display}</span>
        </div>`;
    }).join('');
    // CSP：不用内联 onerror，改事件委托
    el.historyList.querySelectorAll('img[data-hide-on-error]').forEach((img) => {
      img.addEventListener('error', () => { img.style.visibility = 'hidden'; }, { once: true });
    });
  }

  // ------------------------------------------------------------- 设置
  async function loadConfig() {
    try {
      const data = await api('/api/config');
      state.config = data.config;
      state.envProxy = data.env_proxy || '';

      el.setDir.value = data.config.download_dir || '';
      if (el.setRetainDays) el.setRetainDays.value = data.config.task_retain_days ?? 7;
      if (el.setOutputsMaxGb) el.setOutputsMaxGb.value = data.config.outputs_max_gb ?? 5;
      el.setProxyMode.value = data.config.proxy_mode || 'never';
      refreshStorageStats();
      el.setProxyUrl.value = data.config.proxy_url || '';
      el.setCookies.value = data.config.cookies_from_browser || '';
      if (el.setPreferSubtitle) el.setPreferSubtitle.checked = data.config.prefer_subtitle !== false;
      if (el.setAsrEngine) el.setAsrEngine.value = data.config.asr_engine || 'auto';
      if (el.setAsrHotwords) el.setAsrHotwords.value = data.config.asr_hotwords || '';
      if (el.setPlaylistMax) el.setPlaylistMax.value = data.config.playlist_max_entries || 1;
      if (el.setObsidianDir) el.setObsidianDir.value = data.config.obsidian_dir || '';
      if (el.setLiveRecord) el.setLiveRecord.value = data.config.live_record_seconds ?? 300;
      if (el.setDownloadRate) el.setDownloadRate.value = data.config.download_rate_kbps ?? 0;
      if (el.setAsrDiarize) el.setAsrDiarize.checked = !!data.config.asr_diarize;
      if (el.setNotionToken) el.setNotionToken.value = data.config.notion_token || '';
      if (el.setNotionDb) el.setNotionDb.value = data.config.notion_database_id || '';
      if (el.setFeishuWebhook) el.setFeishuWebhook.value = data.config.feishu_webhook || '';
      if (el.setLlmUrl) el.setLlmUrl.value = data.config.llm_base_url || '';
      if (el.setLlmModel) el.setLlmModel.value = data.config.llm_model || '';
      if (el.setLlmKey) el.setLlmKey.value = data.config.llm_api_key || '';
      if (el.setLlmNoThink) el.setLlmNoThink.checked = data.config.llm_disable_thinking !== false;

      const dir = data.config.download_dir || '';
      const short = dir.length > 46 ? `…${dir.slice(-44)}` : dir;
      el.footDir.textContent = `保存到 ${short}`;
    } catch (_) {
      el.footDir.textContent = '下载目录读取失败';
    }
  }

  async function refreshStorageStats() {
    if (!el.storageStatsHint) return;
    try {
      const data = await api('/api/storage/stats');
      const gb = ((data.outputs_bytes || 0) / (1024 ** 3)).toFixed(2);
      const max = (data.policy && data.policy.outputs_max_gb) || 0;
      el.storageStatsHint.textContent = max > 0
        ? `当前产物约 ${gb} GB / 上限 ${max} GB`
        : `当前产物约 ${gb} GB（不限）`;
    } catch (_) {
      el.storageStatsHint.textContent = '';
    }
  }

  async function saveConfig() {
    try {
      const data = await api('/api/config', {
        method: 'POST',
        body: {
          download_dir: el.setDir.value.trim(),
          task_retain_days: el.setRetainDays ? (parseInt(el.setRetainDays.value, 10) || 7) : 7,
          outputs_max_gb: el.setOutputsMaxGb ? (parseFloat(el.setOutputsMaxGb.value) || 0) : 5,
          proxy_mode: el.setProxyMode.value,
          proxy_url: el.setProxyUrl.value.trim(),
          cookies_from_browser: el.setCookies.value,
          prefer_subtitle: el.setPreferSubtitle ? el.setPreferSubtitle.checked : true,
          asr_engine: el.setAsrEngine ? el.setAsrEngine.value : 'auto',
          asr_hotwords: el.setAsrHotwords ? el.setAsrHotwords.value : '',
          playlist_max_entries: el.setPlaylistMax ? (parseInt(el.setPlaylistMax.value, 10) || 1) : 1,
          obsidian_dir: el.setObsidianDir ? el.setObsidianDir.value.trim() : '',
          live_record_seconds: el.setLiveRecord ? (parseInt(el.setLiveRecord.value, 10) || 0) : 300,
          download_rate_kbps: el.setDownloadRate ? (parseInt(el.setDownloadRate.value, 10) || 0) : 0,
          asr_diarize: el.setAsrDiarize ? el.setAsrDiarize.checked : false,
          notion_token: el.setNotionToken && el.setNotionToken.value !== '***' ? el.setNotionToken.value.trim() : undefined,
          notion_database_id: el.setNotionDb ? el.setNotionDb.value.trim() : '',
          feishu_webhook: el.setFeishuWebhook && el.setFeishuWebhook.value !== '***' ? el.setFeishuWebhook.value.trim() : undefined,
          llm_base_url: el.setLlmUrl ? el.setLlmUrl.value.trim() : '',
          llm_model: el.setLlmModel ? el.setLlmModel.value.trim() : '',
          llm_api_key: el.setLlmKey ? el.setLlmKey.value.trim() : '',
          llm_disable_thinking: el.setLlmNoThink ? el.setLlmNoThink.checked : true,
        },
      });
      state.config = data.config;
      el.setSavedHint.hidden = false;
      setTimeout(() => { el.setSavedHint.hidden = true; }, 2000);
      toast('设置已保存', 'success');
      const dir = data.config.download_dir || '';
      const short = dir.length > 46 ? `…${dir.slice(-44)}` : dir;
      el.footDir.textContent = `保存到 ${short}`;
      // 模型配置可能变了，刷新整理能力与提示
      loadPolishStatus();
    } catch (err) {
      toast(err.message || '保存失败', 'error');
    }
  }

  async function loadLlmModels() {
    const url = el.setLlmUrl.value.trim();
    if (!url) { toast('请先填写模型服务地址', 'error'); return; }
    el.btnLoadModels.disabled = true;
    el.btnLoadModels.textContent = '拉取中…';
    try {
      const data = await api('/api/llm/models', {
        method: 'POST',
        body: { base_url: url, api_key: el.setLlmKey.value.trim() },
      });
      const models = data.models || [];
      el.llmModelList.innerHTML = models
        .map((name) => `<option value="${escapeHtml(name)}"></option>`)
        .join('');
      if (!el.setLlmModel.value && models.length) el.setLlmModel.value = models[0];
      toast(`取到 ${models.length} 个模型，点开输入框可下拉选择`, 'success');
    } catch (err) {
      toast(err.message || '拉取模型列表失败', 'error');
    } finally {
      el.btnLoadModels.disabled = false;
      el.btnLoadModels.textContent = '拉取模型列表';
    }
  }

  // ------------------------------------------------------------- 抽屉
  function openDrawer(drawer) {
    drawer.hidden = false;
    document.body.style.overflow = 'hidden';
    if (drawer === el.historyDrawer) refreshHistory();
  }

  function closeDrawer(drawer) {
    drawer.hidden = true;
    document.body.style.overflow = '';
  }

  // ------------------------------------------------------------- 事件绑定
  function bind() {
    el.btnParse.addEventListener('click', doParse);
    el.urlInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); doParse(); }
    });
    el.urlInput.addEventListener('paste', (e) => {
      const text = (e.clipboardData || window.clipboardData)?.getData('text') || '';
      if (isUrl(text) && !state.busy) setTimeout(doParse, 80);
    });

    el.btnPaste.addEventListener('click', async () => {
      try {
        const text = await navigator.clipboard.readText();
        if (!text) { toast('剪贴板为空'); return; }
        el.urlInput.value = text.trim();
        el.urlInput.focus();
        if (isUrl(text)) doParse();
      } catch (_) {
        toast('无法读取剪贴板，请手动粘贴', 'error');
        el.urlInput.focus();
      }
    });

    el.btnDownload.addEventListener('click', startDownload);

    el.btnReset.addEventListener('click', () => {
      el.preview.hidden = true;
      el.progressPanel.hidden = true;
      el.emptyHint.hidden = false;
      el.urlInput.value = '';
      el.urlInput.focus();
      state.info = null;
      state.selected = null;
    });

    el.btnCancel.addEventListener('click', async () => {
      if (!state.taskId) return;
      try {
        await api('/api/cancel', { method: 'POST', body: { id: state.taskId } });
        toast('正在取消…');
      } catch (err) {
        toast(err.message || '取消失败', 'error');
      }
    });

    el.btnOpenFolder.addEventListener('click', () => {
      const path = el.donePanel.dataset.path || state.config.download_dir || '';
      api('/api/reveal', { method: 'POST', body: { path } }).catch(() => toast('打开失败', 'error'));
    });

    el.btnOpenFile.addEventListener('click', () => {
      const path = el.donePanel.dataset.path || '';
      if (!path) { toast('文件路径未知', 'error'); return; }
      api('/api/open-file', { method: 'POST', body: { path } })
        .catch(() => toast('打开失败', 'error'));
    });

    el.btnOpenDir.addEventListener('click', () => {
      api('/api/reveal', { method: 'POST', body: { path: state.config.download_dir || '' } })
        .catch(() => toast('打开失败', 'error'));
    });

    if (el.btnLang) {
      el.btnLang.addEventListener('click', () => {
        if (window.AVD_I18N) window.AVD_I18N.toggle();
      });
    }

    el.btnTheme.addEventListener('click', () => {
      const now = document.documentElement.getAttribute('data-theme');
      applyTheme(now === 'dark' ? 'light' : 'dark');
    });

    el.btnHistory.addEventListener('click', () => openDrawer(el.historyDrawer));
    el.btnSettings.addEventListener('click', () => openDrawer(el.settingsDrawer));

    el.btnClearHistory.addEventListener('click', async () => {
      try {
        await api('/api/clear', { method: 'POST', body: {} });
        refreshHistory();
      } catch (_) { /* 忽略 */ }
    });

    el.btnRevealDir.addEventListener('click', () => {
      api('/api/reveal', { method: 'POST', body: { path: el.setDir.value.trim() } })
        .catch(() => toast('打开失败', 'error'));
    });

    el.btnUseEnvProxy.addEventListener('click', () => {
      if (!state.envProxy) { toast('未检测到系统代理', 'error'); return; }
      el.setProxyUrl.value = state.envProxy;
      toast('已填入系统代理');
    });

    el.btnUseLocalLlm.addEventListener('click', async () => {
      try {
        const data = await api('/api/polish/status');
        const url = data.suggested_base_url;
        if (!url) {
          toast('未检测到本机运行的模型服务（Ollama / LM Studio 等）', 'error');
          return;
        }
        el.setLlmUrl.value = url;
        toast(`已填入 ${url}`);
        if (!el.setLlmModel.value) loadLlmModels();
      } catch (err) {
        toast(err.message || '检测失败', 'error');
      }
    });

    el.btnLoadModels.addEventListener('click', loadLlmModels);

    el.btnSaveSettings.addEventListener('click', saveConfig);
    if (el.btnStoragePurge) {
      el.btnStoragePurge.addEventListener('click', async () => {
        try {
          el.btnStoragePurge.disabled = true;
          const data = await api('/api/storage/purge', { method: 'POST', body: {} });
          const freed = ((data.trim && data.trim.freed) || 0) / (1024 * 1024);
          toast(`已清理：删除 ${(data.trim && data.trim.deleted) || 0} 个媒体文件 · 释放 ${freed.toFixed(1)} MB`, 'success');
          refreshStorageStats();
        } catch (err) {
          toast(err.message || '清理失败', 'error');
        } finally {
          el.btnStoragePurge.disabled = false;
        }
      });
    }

    // ---------------------------------------------------------- 文案提取
    el.btnExtract.addEventListener('click', extractFromCurrent);
    el.btnExtractDone.addEventListener('click', extractFromCurrent);
    el.btnTsClose.addEventListener('click', hideTranscript);

    el.tsMode.addEventListener('change', () => {
      asr.mode = el.tsMode.value;
      if (asr.mode === 'subtitle' && !asr.available) {
        toast('「仅用平台字幕」无需本地识别环境，可直接使用', 'success');
      }
      if (asr.source && asr.source.url && !asr.running) {
        // 有来源链接时立即用新模式重跑，语义最直白
        beginExtract(asr.source);
      }
    });

    // ---------------------------------------------------------- 文案整理
    el.polishOps.addEventListener('click', (e) => {
      const btn = e.target.closest('.polish-op');
      if (!btn) return;
      const op = btn.dataset.op;
      polish.ops[op] = !polish.ops[op];
      btn.setAttribute('aria-pressed', polish.ops[op] ? 'true' : 'false');
      renderPolishNote();
    });

    el.btnPolishRun.addEventListener('click', runPolish);
    el.btnPolishRevert.addEventListener('click', revertPolish);

    el.btnTsCancel.addEventListener('click', async () => {
      if (polish.running && polish.taskId) {
        try {
          await api('/api/polish/cancel', { method: 'POST', body: { id: polish.taskId } });
          toast('正在取消整理…');
        } catch (err) {
          toast(err.message || '取消失败', 'error');
        }
        return;
      }
      if (!asr.taskId) { hideTranscript(); return; }
      try {
        await api('/api/asr/cancel', { method: 'POST', body: { id: asr.taskId } });
        toast('正在取消…');
      } catch (err) {
        toast(err.message || '取消失败', 'error');
      }
    });

    el.tsViewSwitch.addEventListener('click', (e) => {
      const btn = e.target.closest('button[data-view]');
      if (!btn) return;
      asr.view = btn.dataset.view;
      if (asr.editing && asr.view !== 'timed') {
        asr.editing = false;
        updateEditButtons();
      }
      el.tsViewSwitch.querySelectorAll('button').forEach((node) => {
        node.classList.toggle('active', node === btn);
      });
      renderAsrBody();
    });

    // 知识库检索 → 时间轴互跳雏形（旗舰面板发 avd:library-seek）
    window.addEventListener('avd:library-seek', (ev) => {
      const detail = (ev && ev.detail) || {};
      const at = Number(detail.at);
      const panel = $('transcriptPanel') || el.tsBody;
      if (Number.isFinite(at) && asr.segments && asr.segments.length) {
        asr.view = 'timed';
        if (el.tsViewSwitch) {
          el.tsViewSwitch.querySelectorAll('button').forEach((node) => {
            node.classList.toggle('active', node.getAttribute('data-view') === 'timed');
          });
        }
        renderAsrBody();
        const source = (polish.applied && polish.paragraphs.length)
          ? polish.paragraphs
          : asr.segments;
        const rows = el.tsBody ? el.tsBody.querySelectorAll('.ts-line[data-idx]') : [];
        let best = null;
        let bestDist = Infinity;
        rows.forEach((node) => {
          const idx = parseInt(node.getAttribute('data-idx') || '-1', 10);
          const seg = source[idx];
          if (!seg) return;
          const dist = Math.abs(Number(seg.start) - at);
          if (dist < bestDist) {
            bestDist = dist;
            best = node;
          }
        });
        if (el.tsBody) {
          el.tsBody.querySelectorAll('.ts-line.is-seek-target').forEach((n) => {
            n.classList.remove('is-seek-target');
          });
        }
        if (best) {
          best.classList.add('is-seek-target');
          best.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
        if (panel && panel.scrollIntoView) {
          panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        }
        toast(`已定位到 ${detail.clock || `${at.toFixed(1)}s`}`, 'success');
        return;
      }
      // 无本地分段时：尽量用网页深链 #t= 打开出处
      const url = String(detail.url || '').trim();
      if (url && /^https?:\/\//i.test(url)) {
        const deep = url.includes('#') ? url : `${url}#t=${Math.max(0, Math.floor(at || 0))}`;
        window.open(deep, '_blank', 'noopener,noreferrer');
        toast('已打开出处链接（带时间戳）', 'success');
        return;
      }
      toast(detail.clock
        ? `命中 ${detail.clock}，当前无本地时间轴可跳`
        : '当前无本地时间轴可跳', 'error');
    });

    if (el.btnTsEdit) el.btnTsEdit.addEventListener('click', toggleProofread);
    if (el.btnTsUndo) el.btnTsUndo.addEventListener('click', undoProofread);
    if (el.btnTsRedo) el.btnTsRedo.addEventListener('click', redoProofread);
    document.addEventListener('keydown', (e) => {
      if (!asr.editing) return;
      const mod = e.metaKey || e.ctrlKey;
      if (!mod) return;
      if (e.key === 'z' && !e.shiftKey) { e.preventDefault(); undoProofread(); }
      if (e.key === 'z' && e.shiftKey) { e.preventDefault(); redoProofread(); }
      if (e.key === 'y') { e.preventDefault(); redoProofread(); }
    });

    el.btnTsCopy.addEventListener('click', copyTranscript);

    el.btnTsExport.addEventListener('click', (e) => {
      e.stopPropagation();
      el.tsExportMenu.hidden = !el.tsExportMenu.hidden;
    });

    el.tsExportMenu.addEventListener('click', (e) => {
      const btn = e.target.closest('button[data-fmt]');
      if (!btn) return;
      e.stopPropagation();
      exportAs(btn.dataset.fmt);
    });

    document.addEventListener('click', () => {
      if (!el.tsExportMenu.hidden) el.tsExportMenu.hidden = true;
    });

    el.tsLang.addEventListener('change', () => {
      asr.language = el.tsLang.value;
      if (asr.source && asr.source.url) {
        // 链接来源可以直接用新语言重跑
        beginExtract(asr.source);
      } else if (asr.source && asr.source.uploadName) {
        toast('语言已切换，请重新拖入文件提取');
      } else {
        toast('已切换识别语言');
      }
    });

    document.querySelectorAll('[data-close]').forEach((node) => {
      node.addEventListener('click', () => {
        closeDrawer(el.historyDrawer);
        closeDrawer(el.settingsDrawer);
      });
    });

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        closeDrawer(el.historyDrawer);
        closeDrawer(el.settingsDrawer);
      }
    });
  }

  // ------------------------------------------------------------- 启动
  function init() {
    initTheme();
    initTerms();
    bind();
    bindDropZone();
    loadConfig();
    loadAsrStatus();
    loadPolishStatus();
    refreshHistory();
    setInterval(refreshHistory, 5000);
    el.urlInput.focus();
  }

  document.addEventListener('DOMContentLoaded', init);
})();
