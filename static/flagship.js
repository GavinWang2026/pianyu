// Copyright (c) 2026 Hyht-Team. All rights reserved.
// See LICENSE (proprietary) and NOTICE.md (third-party components).
/* AVD Flagship v2.0.0 — 旗舰版增量界面
   独立注入，不改动原版 index.html / app.js / style.css 的任何既有逻辑。
   提供「一次点击 → 解析 → 字幕/转写 → 整理 → 结构化知识稿」的一条产线。 */
(function () {
  'use strict';
  if (window.__avdFlagshipLoaded) return;
  window.__avdFlagshipLoaded = true;

  var CSS = [
    '.avd-fab{position:fixed;right:24px;bottom:24px;z-index:9998;display:flex;align-items:center;gap:9px;',
    'padding:11px 17px;border-radius:14px;border:1px solid rgba(139,124,246,.42);',
    'background:rgba(28,26,42,.92);color:#f2f0ff;font:500 13px/1.2 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;',
    'cursor:pointer;backdrop-filter:blur(18px);transition:transform .18s ease,border-color .18s ease;',
    'box-shadow:0 8px 28px rgba(10,8,24,.34)}',
    '.avd-fab:hover{transform:translateY(-2px);border-color:rgba(157,143,255,.78)}',
    '.avd-fab svg{width:16px;height:16px;flex:none}',
    '.avd-panel{position:fixed;right:24px;bottom:82px;z-index:9997;width:418px;max-width:calc(100vw - 36px);',
    'max-height:min(78vh,720px);overflow:auto;padding:20px;border-radius:18px;',
    'border:1px solid rgba(139,124,246,.32);background:rgba(24,22,36,.96);color:#e9e7f5;',
    'backdrop-filter:blur(24px);box-shadow:0 20px 60px rgba(8,6,20,.5);',
    'font:400 13px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;display:none}',
    '.avd-panel.open{display:block}',
    '[data-theme="light"] .avd-panel{background:rgba(255,255,255,.97);color:#221f33;border-color:rgba(109,94,252,.28)}',
    '[data-theme="light"] .avd-fab{background:rgba(255,255,255,.95);color:#221f33}',
    '.avd-h{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin-bottom:4px}',
    '.avd-h h2{margin:0;font-size:15px;font-weight:500;letter-spacing:.2px}',
    '.avd-badge{font-size:11px;padding:3px 9px;border-radius:999px;border:1px solid rgba(109,94,252,.4);color:#b9acff}',
    '[data-theme="light"] .avd-badge{color:#5b4bd6}',
    '.avd-sub{margin:0 0 14px;font-size:11.5px;opacity:.62;line-height:1.55}',
    '.avd-row{display:flex;gap:8px;align-items:center}',
    '.avd-input{flex:1;min-width:0;padding:10px 12px;border-radius:10px;font-size:12.5px;',
    'border:1px solid rgba(150,140,200,.26);background:rgba(255,255,255,.05);color:inherit;outline:none}',
    '.avd-input:focus{border-color:rgba(139,124,246,.8)}',
    '[data-theme="light"] .avd-input{background:rgba(20,16,40,.04)}',
    '.avd-btn{padding:10px 14px;border-radius:10px;border:1px solid transparent;cursor:pointer;',
    'font:500 12.5px/1.2 inherit;background:#6d5efc;color:#fff;white-space:nowrap;transition:opacity .15s ease}',
    '.avd-btn:hover{opacity:.86}.avd-btn:disabled{opacity:.45;cursor:not-allowed}',
    '.avd-btn.ghost{background:transparent;border-color:rgba(150,140,200,.3);color:inherit}',
    '.avd-opt{display:flex;gap:16px;align-items:center;margin:11px 0 13px;font-size:11.5px;opacity:.78;flex-wrap:wrap}',
    '.avd-opt label{display:flex;gap:5px;align-items:center;cursor:pointer}',
    '.avd-track{height:5px;border-radius:99px;background:rgba(150,140,200,.18);overflow:hidden;margin:6px 0 8px}',
    '.avd-fill{height:100%;width:0;border-radius:99px;background:linear-gradient(90deg,#6d5efc,#a58bff);transition:width .3s ease}',
    '.avd-status{font-size:11.5px;opacity:.8;min-height:18px}',
    '.avd-note{margin:8px 0 0;padding:9px 11px;border-radius:10px;font-size:11.5px;line-height:1.55;',
    'background:rgba(233,176,73,.09);border:1px solid rgba(233,176,73,.24);display:none}',
    '.avd-note.show{display:block}',
    '.avd-pre{margin:12px 0 0;padding:12px;border-radius:11px;max-height:220px;overflow:auto;',
    'font:400 11.5px/1.65 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;word-break:break-word;',
    'background:rgba(10,8,20,.32);border:1px solid rgba(150,140,200,.16);display:none}',
    '[data-theme="light"] .avd-pre{background:rgba(20,16,40,.04)}',
    '.avd-pre.show{display:block}',
    '.avd-exports{display:flex;gap:7px;margin-top:11px;flex-wrap:wrap}',
    '.avd-exports .avd-btn{padding:7px 12px;font-size:11.5px}',
    '.avd-foot{margin-top:13px;padding-top:11px;border-top:1px solid rgba(150,140,200,.16);font-size:11px;opacity:.55}'
  ].join('');

  var STAGE_LABEL = {
    queued: '排队中', probe: '解析素材（全任务仅此一次）', download: '下载媒体（复用已解析信息）',
    transcript: '取得文字稿（字幕优先）', polish: '整理文稿', knowledge: '生成结构化知识稿',
    done: '完成', error: '失败', cancelled: '已取消'
  };

  function el(id) { return document.getElementById(id); }

  function tokenHeaders(extra) {
    var h = Object.assign({ 'Content-Type': 'application/json' }, extra || {});
    var t = sessionStorage.getItem('avd_token') || '';
    if (t) h['X-AVD-Token'] = t;
    return h;
  }

  async function api(path, options) {
    var opt = options || {};
    var res = await fetch(path, Object.assign({}, opt, { headers: tokenHeaders(opt.headers) }));
    var data = await res.json();
    if (!data.ok) throw new Error(data.error || ('HTTP ' + res.status));
    return data;
  }

  function build() {
    var style = document.createElement('style');
    style.textContent = CSS;
    document.head.appendChild(style);

    var fab = document.createElement('button');
    fab.className = 'avd-fab';
    fab.type = 'button';
    fab.setAttribute('aria-label', '打开旗舰版一键流水线');
    fab.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2 4 14h7l-1 8 9-12h-7z"/></svg><span>旗舰版 · 一键知识稿</span>';
    document.body.appendChild(fab);

    var panel = document.createElement('div');
    panel.className = 'avd-panel';
    panel.innerHTML = [
      '<div class="avd-h"><h2>一键生成结构化知识稿</h2><span class="avd-badge" id="avdEngine">检测中</span></div>',
      '<p class="avd-sub">一次解析、全链复用：画质 / 字幕轨 / 章节 / 出处一次取回，产出带 front matter 的 Markdown，可直接进知识库。</p>',
      '<div class="avd-row"><input class="avd-input" id="avdUrl" placeholder="粘贴素材链接" spellcheck="false" /><button class="avd-btn" id="avdGo">生成</button></div>',
      '<div class="avd-opt">',
      '<label><input type="checkbox" id="avdDownload" /> 同时下载媒体（验证零重复解析）</label>',
      '<label><input type="checkbox" id="avdPolish" checked /> 离线整理</label>',
      '</div>',
      '<div class="avd-track"><div class="avd-fill" id="avdFill"></div></div>',
      '<div class="avd-status" id="avdStatus">等待启动</div>',
      '<div class="avd-note" id="avdNote"></div>',
      '<pre class="avd-pre" id="avdPreview"></pre>',
      '<div class="avd-exports" id="avdExports">',
      '<button class="avd-btn" data-fmt="md">下载 Markdown</button>',
      '<button class="avd-btn ghost" data-fmt="srt">SRT</button>',
      '<button class="avd-btn ghost" data-fmt="txt">TXT</button>',
      '<button class="avd-btn ghost" data-fmt="json">JSON</button>',
      '</div>',
      '<div class="avd-foot" id="avdFoot">引擎内嵌锁定 · 数据不出本机 · 不提供第三方内容采集与分发</div>'
    ].join('');
    document.body.appendChild(panel);

    fab.addEventListener('click', function () { panel.classList.toggle('open'); });
    return panel;
  }

  function guessUrl() {
    var ids = ['urlInput', 'videoUrl', 'url', 'linkInput'];
    for (var i = 0; i < ids.length; i++) {
      var node = el(ids[i]);
      if (node && node.value && /^https?:\/\//.test(node.value)) return node.value.trim();
    }
    return '';
  }

  function start(panel) {
    var urlInput = el('avdUrl');
    var go = el('avdGo');
    var fill = el('avdFill');
    var status = el('avdStatus');
    var note = el('avdNote');
    var preview = el('avdPreview');
    var currentId = '';
    var timer = null;

    urlInput.value = guessUrl();

    function setStatus(text) { status.textContent = text; }
    function setProgress(pct) { fill.style.width = Math.max(0, Math.min(100, pct)) + '%'; }

    function showResult(task) {
      if (!task.files || !task.files.md) return;
      api('/api/pipeline/result?id=' + encodeURIComponent(task.id) + '&fmt=md')
        .then(function (data) {
          preview.textContent = data.content.slice(0, 2400) + (data.content.length > 2400 ? '\n…（已截断预览，下载文件为全文）' : '');
          preview.classList.add('show');
        }).catch(function (e) { setStatus('预览读取失败：' + e.message); });
    }

    function poll() {
      if (!currentId) return;
      api('/api/pipeline/status?id=' + encodeURIComponent(currentId))
        .then(function (data) {
          var task = data.task;
          setProgress(task.percent || 0);
          setStatus((STAGE_LABEL[task.stage] || task.stage) + (task.message ? ' · ' + task.message : ''));
          if (task.degraded && task.degraded.length) {
            note.textContent = '降级说明：' + task.degraded.join('；');
            note.classList.add('show');
          } else {
            note.classList.remove('show');
          }
          if (task.error) setStatus('失败：' + task.error);
          if (['done', 'error', 'cancelled'].indexOf(task.stage) >= 0) {
            clearInterval(timer); timer = null;
            go.disabled = false; go.textContent = '生成';
            if (task.stage === 'done') showResult(task);
          }
        })
        .catch(function (e) {
          clearInterval(timer); timer = null;
          go.disabled = false; go.textContent = '生成';
          setStatus('状态查询失败：' + e.message);
        });
    }

    go.addEventListener('click', function () {
      var url = urlInput.value.trim();
      if (!url) { setStatus('请先粘贴链接'); urlInput.focus(); return; }
      go.disabled = true; go.textContent = '运行中';
      note.classList.remove('show');
      preview.classList.remove('show');
      setProgress(2); setStatus('正在启动流水线…');
      api('/api/pipeline/start', {
        method: 'POST',
        body: JSON.stringify({
          url: url,
          want_download: el('avdDownload').checked,
          want_polish: el('avdPolish').checked
        })
      }).then(function (data) {
        currentId = data.task.id;
        clearInterval(timer);
        timer = setInterval(poll, 550);
        poll();
      }).catch(function (e) {
        go.disabled = false; go.textContent = '生成';
        setStatus('启动失败：' + e.message);
      });
    });

    el('avdExports').addEventListener('click', function (ev) {
      var btn = ev.target.closest('[data-fmt]');
      if (!btn || !currentId) { setStatus('请先生成一份知识稿'); return; }
      var fmt = btn.getAttribute('data-fmt');
      api('/api/pipeline/result?id=' + encodeURIComponent(currentId) + '&fmt=' + fmt)
        .then(function (data) {
          var blob = new Blob([data.content], { type: 'text/plain;charset=utf-8' });
          var href = URL.createObjectURL(blob);
          var link = document.createElement('a');
          link.href = href;
          link.download = '知识稿.' + fmt;
          document.body.appendChild(link); link.click(); link.remove();
          setTimeout(function () { URL.revokeObjectURL(href); }, 1500);
          setStatus('已导出 ' + fmt.toUpperCase());
        }).catch(function (e) { setStatus('导出失败：' + e.message); });
    });

    api('/api/fusion/health').then(function (data) {
      var h = data.health || {};
      el('avdEngine').textContent = 'yt-dlp ' + (h.engine_version || '?') + ' 内嵌锁定';
      var bits = [];
      bits.push('插件 ' + (((h.plugins || {}).files || {}).postprocessor || []).length + ' 个');
      bits.push('语音识别 ' + ((h.asr || {}).available ? '就绪' : '未就绪'));
      if (!h.ffmpeg) bits.push('ffmpeg 未安装');
      el('avdFoot').textContent = bits.join(' · ') + ' · 数据不出本机';
    }).catch(function () {
      el('avdEngine').textContent = '扩展未就绪';
    });
  }

  function ready() {
    var panel = build();
    start(panel);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', ready);
  } else {
    ready();
  }
})();
