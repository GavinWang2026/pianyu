// Copyright (c) 2026 Hyht-Team. All rights reserved.
// See LICENSE (proprietary) and NOTICE.md (third-party components).
/* AVD Flagship v2.0.0 — 旗舰版增量界面
   独立注入，不改动原版 index.html / app.js / style.css 的任何既有逻辑。
   提供「一次点击 → 解析 → 字幕/转写 → 整理 → 结构化知识稿」的一条产线。 */
(function () {
  'use strict';
  if (window.__avdFlagshipLoaded) return;
  window.__avdFlagshipLoaded = true;

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
    // 样式走 /static/flagship.css（CSP style-src 'self'，无内联 style/style 标签注入）
    var fab = document.createElement('button');
    fab.className = 'avd-fab';
    fab.type = 'button';
    fab.setAttribute('data-i18n-aria', 'fab.open');
    fab.setAttribute('aria-label', '打开旗舰版一键流水线');
    fab.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2 4 14h7l-1 8 9-12h-7z"/></svg><span data-i18n="fab.open">旗舰版 · 一键知识稿</span>';
    document.body.appendChild(fab);

    var panel = document.createElement('div');
    panel.className = 'avd-panel';
    panel.innerHTML = [
      '<div class="avd-h"><h2 data-i18n="flagship.title">一键生成结构化知识稿</h2><span class="avd-badge" id="avdEngine">检测中</span></div>',
      '<p class="avd-sub" data-i18n="flagship.sub">一次解析、全链复用：画质 / 字幕轨 / 章节 / 出处一次取回，产出带 front matter 的 Markdown，可直接进知识库。</p>',
      '<div class="avd-row"><input class="avd-input" id="avdUrl" placeholder="粘贴素材链接（单条）或拖入本地文件" spellcheck="false" /><button class="avd-btn" id="avdGo" data-i18n="flagship.go">生成</button></div>',
      '<div class="avd-row avd-row-wrap"><label class="avd-btn ghost avd-file-label" for="avdFile">选择本地文件<input type="file" id="avdFile" accept="audio/*,video/*,.mp4,.mov,.mkv,.mp3,.m4a,.wav,.webm" hidden /></label><span class="avd-muted" id="avdFileHint">也可拖到本面板</span></div>',
      '<textarea class="avd-ta" id="avdBatch" placeholder="批量：每行一条链接（最多 50）" spellcheck="false"></textarea>',
      '<div class="avd-row avd-row-wrap">',
      '<button class="avd-btn ghost" id="avdBatchGo" type="button">批量排队</button>',
      '<label class="avd-inline-label">延迟(秒)<input class="avd-input avd-w-88" id="avdDelay" type="number" min="0" max="604800" step="60" value="0" /></label>',
      '<button class="avd-btn ghost" id="avdDiagnose" type="button">先诊断</button>',
      '<button class="avd-btn ghost" id="avdCancel" type="button" disabled>取消</button>',
      '</div>',
      '<div class="avd-opt">',
      '<label><input type="checkbox" id="avdDownload" checked /> 同时下载媒体（默认开启，兑现一次解析）</label>',
      '<label><input type="checkbox" id="avdPolish" checked /> 离线整理</label>',
      '<label><input type="checkbox" id="avdExpand" /> 展开播放列表</label>',
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
      '<button class="avd-btn ghost" data-fmt="docx">DOCX</button>',
      '<button class="avd-btn ghost" data-fmt="pdf">PDF</button>',
      '<button class="avd-btn ghost" id="avdObsidian" type="button">写入 Obsidian</button>',
      '<button class="avd-btn ghost" id="avdPushNotion" type="button">推送 Notion</button>',
      '<button class="avd-btn ghost" id="avdPushFeishu" type="button">推送飞书</button>',
      '<button class="avd-btn ghost" id="avdProof" type="button">校对回写</button>',
      '<button class="avd-btn ghost" id="avdClip" type="button">切片导出</button>',
      '</div>',
      '<div id="avdClipBox" class="avd-row avd-box">',
      '<label class="avd-inline-label">起<input class="avd-input avd-w-72" id="avdClipStart" type="number" min="0" step="0.1" value="0" /></label>',
      '<label class="avd-inline-label">止<input class="avd-input avd-w-72" id="avdClipEnd" type="number" min="0" step="0.1" value="15" /></label>',
      '<button class="avd-btn" id="avdClipGo" type="button">导出切片</button>',
      // R2-3：按章节批量切片（/api/pipeline/export-chapters 此前无人调用，
      // 而且它自己还坏着——见 fusion/pipeline.py to_dict 的 chapters 修复）
      '<button class="avd-btn ghost" id="avdClipChapters" type="button">按章节批量</button>',
      '</div>',
      '<div id="avdProofBox" class="avd-box">',
      '<p class="avd-sub avd-sub-tight">编辑每行「开始 结束 文本」，保存后重写 SRT/Markdown。</p>',
      '<textarea class="avd-ta avd-ta-tall" id="avdProofTa" spellcheck="true"></textarea>',
      '<div class="avd-row avd-mt-8"><button class="avd-btn" id="avdProofSave" type="button">保存校对</button>',
      '<button class="avd-btn ghost" id="avdProofCancel" type="button">取消</button></div>',
      '</div>',
      '<div class="avd-tools">',
      '<div class="avd-tools-h">知识库 / 诊断</div>',
      '<div class="avd-row avd-row-wrap">',
      '<input class="avd-input" id="avdLibQ" placeholder="检索素材库（如：转化路径）" spellcheck="false" />',
      '<button class="avd-btn ghost" id="avdLibSearch" type="button" data-i18n="flagship.search">搜索</button>',
      '<button class="avd-btn ghost" id="avdLibAsk" type="button" data-i18n="flagship.ask">问答</button>',
      '</div>',
      '<div class="avd-row avd-row-wrap">',
      '<input class="avd-input" id="avdLibColl" placeholder="合集名（成册）" spellcheck="false" />',
      '<input class="avd-input" id="avdLibTags" placeholder="标签（逗号分隔）" spellcheck="false" />',
      '<input class="avd-input" id="avdLibIds" placeholder="素材 id（逗号分隔，空=最近检索）" spellcheck="false" />',
      '<button class="avd-btn ghost" id="avdLibBook" type="button" data-i18n="flagship.book">成册</button>',
      '<button class="avd-btn ghost" id="avdLibTag" type="button" data-i18n="flagship.tag">打标</button>',
      '<button class="avd-btn ghost" id="avdLibCluster" type="button" data-i18n="flagship.cluster">自动归类</button>',
      '</div>',
      // R2-3：素材库浏览（此前 /api/library/collections 与 /api/library/materials
      // 后端已实现但前端零调用 —— 用户只能靠检索框盲猜，看不到库里到底有什么）
      '<div class="avd-row avd-row-wrap">',
      '<select class="avd-input avd-w-140" id="avdCollSel" aria-label="素材合集">',
      '<option value="">全部合集</option>',
      '</select>',
      '<button class="avd-btn ghost" id="avdCollReload" type="button">刷新合集</button>',
      '<button class="avd-btn ghost" id="avdMatsList" type="button">浏览素材</button>',
      '</div>',
      '<div class="avd-row avd-row-wrap">',
      '<button class="avd-btn ghost" id="avdDiagPack" type="button" data-i18n="flagship.diag">诊断包</button>',
      '<button class="avd-btn ghost" id="avdDiagTickets" type="button" data-i18n="flagship.tickets">工单</button>',
      '<button class="avd-btn ghost" id="avdDiagSubmit" type="button" data-i18n="flagship.submit">提交工单</button>',
      '<button class="avd-btn ghost" id="avdRecoverable" type="button" data-i18n="flagship.recoverable">可续跑</button>',
      '<button class="avd-btn ghost" id="avdRetry" type="button" data-i18n="flagship.retry">重试续跑</button>',
      '<button class="avd-btn ghost" id="avdUpdates" type="button" data-i18n="flagship.updates">检查更新</button>',
      '</div>',
      // R2-3：任务历史（/api/pipeline/tasks 之前无人调用：重启后面板只认当前任务，
      // 昨天跑过的稿子要点开就得靠记忆 id）
      '<div class="avd-row avd-row-wrap">',
      '<button class="avd-btn ghost" id="avdTaskList" type="button">任务历史</button>',
      '<button class="avd-btn ghost" id="avdStorage" type="button">存储占用</button>',
      '</div>',
      '<div class="avd-lib-out" id="avdLibOut"></div>',
      '</div>',
      '<div class="avd-foot" id="avdFoot">引擎内嵌锁定 · 数据不出本机 · 不提供第三方内容采集与分发</div>'
    ].join('');
    document.body.appendChild(panel);

    fab.addEventListener('click', function () { panel.classList.toggle('open'); });
    if (window.AVD_I18N) {
      window.AVD_I18N.apply(document);
      window.addEventListener('avd:lang', function () { window.AVD_I18N.apply(document); });
    }
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
    var lastSearchIds = [];

    urlInput.value = guessUrl();

    function setStatus(text) { status.textContent = text; }
    function setProgress(pct) { fill.style.width = Math.max(0, Math.min(100, pct)) + '%'; }

    function showResult(task) {
      if (task && (task.stale || task.exists === false)) {
        setStatus('产物已删除（历史记录仍保留）');
        preview.classList.remove('show');
        return;
      }
      if (!task.files || !task.files.md) return;
      api('/api/pipeline/result?id=' + encodeURIComponent(task.id) + '&fmt=md')
        .then(function (data) {
          preview.textContent = data.content.slice(0, 2400) + (data.content.length > 2400 ? '\n…（已截断预览，下载文件为全文）' : '');
          preview.classList.add('show');
        }).catch(function (e) { setStatus('预览读取失败：' + e.message); });
    }

    function fillNoteSafe(lines) {
      note.textContent = '';
      lines.forEach(function (row) {
        var div = document.createElement('div');
        div.className = row.cls || 'avd-line';
        if (row.bold) {
          var b = document.createElement('b');
          b.textContent = row.bold;
          div.appendChild(b);
        }
        if (row.text) div.appendChild(document.createTextNode(row.text));
        note.appendChild(div);
      });
      note.classList.add('show');
    }

    function seekLibraryHit(hit) {
      if (!hit) return;
      var at = hit.at;
      var clock = hit.at_clock || '';
      try {
        window.dispatchEvent(new CustomEvent('avd:library-seek', {
          detail: {
            taskId: hit.id || '',
            at: at,
            clock: clock,
            title: hit.title || '',
            url: hit.webpage_url || '',
            snippet: hit.snippet || ''
          }
        }));
      } catch (_) { /* ignore */ }
      var tip = clock || (at != null ? (Number(at).toFixed(1) + 's') : '');
      setStatus(tip
        ? ('定位 ' + tip + ' · 《' + (hit.title || '') + '》')
        : ('打开 《' + (hit.title || '') + '》'));
    }

    function setLibOut(rows) {
      var box = el('avdLibOut');
      if (!box) return;
      box.textContent = '';
      (rows || []).forEach(function (row) {
        var div = document.createElement('div');
        div.className = row.cls || 'avd-lib-hit';
        if (row.hit) {
          var hit = row.hit;
          var title = document.createElement('span');
          title.className = 'avd-lib-title';
          title.textContent = '《' + (hit.title || '') + '》';
          div.appendChild(title);
          if (hit.at_clock || hit.at != null) {
            var clockBtn = document.createElement('button');
            clockBtn.type = 'button';
            clockBtn.className = 'avd-lib-clock';
            clockBtn.textContent = hit.at_clock || (Number(hit.at).toFixed(1) + 's');
            clockBtn.title = '跳到时间轴 / 片段';
            clockBtn.addEventListener('click', function (ev) {
              ev.preventDefault();
              seekLibraryHit(hit);
            });
            div.appendChild(clockBtn);
          }
          var snip = document.createElement('span');
          snip.className = 'avd-lib-snip';
          snip.textContent = ' ' + (hit.snippet || '');
          div.appendChild(snip);
          div.classList.add('avd-lib-clickable');
          div.addEventListener('click', function (ev) {
            if (ev.target && ev.target.closest && ev.target.closest('.avd-lib-clock')) return;
            seekLibraryHit(hit);
          });
        } else {
          if (row.badge) {
            var pill = document.createElement('span');
            pill.className = 'avd-pill avd-pill-' + (row.badge.level || 'note');
            pill.textContent = row.badge.label || row.badge.level || '';
            if (row.badge.hint) pill.title = row.badge.hint;
            div.appendChild(pill);
            div.appendChild(document.createTextNode(' '));
          }
          div.appendChild(document.createTextNode(row.text || ''));
          if (row.meta) {
            var meta = document.createElement('span');
            meta.className = 'avd-muted avd-lib-meta';
            meta.textContent = ' ' + row.meta;
            div.appendChild(meta);
          }
          if (typeof row.onClick === 'function') {
            div.classList.add('avd-lib-clickable');
            div.addEventListener('click', row.onClick);
          }
        }
        box.appendChild(div);
      });
    }

    var cancelBtn = el('avdCancel');
    var diagnoseBtn = el('avdDiagnose');

    // R2-2：降级项现在是 Degradation(level, cause, retriable, action)。
    // 旧实现只读 it.impact（新结构里是 cause），且把「已剔除 3 段口播」和
    // 「语音识别环境未就绪」平铺成同样份量的两行 —— 用户看不出哪条必须处理。
    var LEVEL_LABEL = { note: '提示', warn: '告警', major: '降级产出', block: '能力缺失' };
    var LEVEL_HINT = {
      note: '无需处理，知情即可',
      warn: '建议处理，不处理也能用',
      major: '产出已被削减，建议按动作处理',
      block: '关键能力不可用，产出可能只是骨架'
    };

    function normalizeDegraded(task) {
      var items = (task && task.degraded_items) || [];
      if (items.length) {
        return items.map(function (it) {
          return {
            level: it.level || 'note',
            cause: it.cause || it.impact || String(it),
            action: it.action || '',
            retriable: !!it.retriable,
            code: it.code || ''
          };
        });
      }
      return ((task && task.degraded) || []).map(function (d) {
        return { level: 'note', cause: String(d), action: '', retriable: false, code: '' };
      });
    }

    function showDegraded(task) {
      var items = normalizeDegraded(task);
      note.textContent = '';
      note.classList.remove('show');
      if (!items.length) return;

      var rank = { note: 0, warn: 1, major: 2, block: 3 };
      var worst = 'note';
      var retriable = 0;
      items.forEach(function (it) {
        if (rank[it.level] > rank[worst]) worst = it.level;
        if (it.retriable) retriable += 1;
      });

      var head = document.createElement('div');
      head.className = 'avd-deg-head';
      var wpill = document.createElement('span');
      wpill.className = 'avd-pill avd-pill-' + worst;
      wpill.textContent = LEVEL_LABEL[worst] || worst;
      head.appendChild(wpill);
      head.appendChild(document.createTextNode(
        ' 共 ' + items.length + ' 条' + (retriable ? (' · ' + retriable + ' 条可重试') : '')
      ));
      note.appendChild(head);

      // 先列 block/major（要处理的），note 垫底，避免真正的问题被口水信息埋掉
      var order = items.slice().sort(function (a, b) {
        return rank[b.level] - rank[a.level];
      });
      order.forEach(function (it) {
        var line = document.createElement('div');
        line.className = 'avd-line avd-deg-line';
        var pill = document.createElement('span');
        pill.className = 'avd-pill avd-pill-' + it.level;
        pill.textContent = LEVEL_LABEL[it.level] || it.level;
        pill.title = LEVEL_HINT[it.level] || '';
        line.appendChild(pill);
        line.appendChild(document.createTextNode(' ' + it.cause));
        if (it.action) {
          var act = document.createElement('div');
          act.className = 'avd-muted avd-deg-act';
          act.textContent = '→ ' + it.action + (it.retriable ? '（可重试）' : '');
          line.appendChild(act);
        }
        note.appendChild(line);
      });
      note.classList.add('show');
    }

    function applyTask(task) {
      if (!task) return;
      setProgress(task.percent || 0);
      setStatus((STAGE_LABEL[task.stage] || task.stage) + (task.message ? ' · ' + task.message : ''));
      showDegraded(task);
      if (task.error) setStatus('失败：' + task.error);
      var running = ['done', 'error', 'cancelled'].indexOf(task.stage) < 0;
      cancelBtn.disabled = !running;
      if (!running) {
        if (timer) { clearInterval(timer); timer = null; }
        if (es) { try { es.close(); } catch (e) {} es = null; }
        go.disabled = false; go.textContent = '生成';
        if (task.stage === 'done') {
          showResult(task);
          if (task.speakers && task.speaker_stats) {
            var st = task.speaker_stats;
            var bal = (st.balance != null) ? (' · 均衡 ' + Number(st.balance).toFixed(2)) : '';
            var conf = (task.speaker_confidence != null)
              ? (' · 置信 ' + Number(task.speaker_confidence).toFixed(2)) : '';
            setStatus(
              (STAGE_LABEL.done || '完成')
              + ' · 说话人 ' + (task.speaker_source || 'heuristic')
              + conf + bal
            );
          } else if (task.summary_kind) {
            setStatus((STAGE_LABEL.done || '完成') + ' · 摘要 ' + task.summary_kind);
          }
        }
        if (task.stage === 'cancelled') setStatus('已取消');
      }
    }

    function poll() {
      if (!currentId) return;
      api('/api/pipeline/status?id=' + encodeURIComponent(currentId))
        .then(function (data) { applyTask(data.task); })
        .catch(function (e) {
          if (timer) { clearInterval(timer); timer = null; }
          go.disabled = false; go.textContent = '生成';
          setStatus('状态查询失败：' + e.message);
        });
    }

    // SSE-01：优先 EventSource；不支持时回退 550ms 轮询
    var es = null;
    function watch(taskId) {
      if (timer) { clearInterval(timer); timer = null; }
      if (es) { try { es.close(); } catch (e) {} es = null; }
      if (typeof EventSource === 'undefined') {
        timer = setInterval(poll, 550);
        poll();
        return;
      }
      es = new EventSource('/api/pipeline/progress?id=' + encodeURIComponent(taskId));
      es.onmessage = function (ev) {
        try {
          var data = JSON.parse(ev.data);
          applyTask(data.task || data);
        } catch (err) { /* ignore malformed */ }
      };
      es.addEventListener('end', function () {
        if (es) { try { es.close(); } catch (e) {} es = null; }
      });
      es.onerror = function () {
        if (es) { try { es.close(); } catch (e) {} es = null; }
        if (!timer) { timer = setInterval(poll, 550); poll(); }
      };
    }

    function beginPipelineWatch(task) {
      currentId = task.id;
      var batch = task.batch;
      if (batch && batch.count > 1) {
        setStatus('已展开播放列表：' + batch.count + ' 条进入队列，跟踪首条…');
      }
      watch(currentId);
    }

    function uploadLocalFile(file) {
      if (!file) return;
      go.disabled = true; go.textContent = '运行中';
      cancelBtn.disabled = false;
      note.classList.remove('show'); note.innerHTML = '';
      preview.classList.remove('show');
      setProgress(2); setStatus('正在上传 ' + file.name + '…');
      var q = '?language=zh&want_polish=' + (el('avdPolish').checked ? '1' : '0');
      fetch('/api/pipeline/upload' + q, {
        method: 'POST',
        headers: Object.assign(tokenHeaders(), {
          'X-File-Name': encodeURIComponent(file.name),
          'Content-Type': 'application/octet-stream'
        }),
        body: file
      }).then(function (res) { return res.json().then(function (data) {
        if (!res.ok || data.ok === false) throw new Error(data.error || ('HTTP ' + res.status));
        return data;
      }); }).then(function (data) {
        setStatus('本地文件已入队，开始转写…');
        beginPipelineWatch(data.task);
      }).catch(function (e) {
        go.disabled = false; go.textContent = '生成';
        cancelBtn.disabled = true;
        setStatus('上传失败：' + e.message);
      });
    }

    var fileInput = el('avdFile');
    if (fileInput) {
      fileInput.addEventListener('change', function () {
        var f = fileInput.files && fileInput.files[0];
        if (f) uploadLocalFile(f);
        fileInput.value = '';
      });
    }
    // 面板拖放：优先走知识稿流水线
    panel.addEventListener('dragover', function (e) {
      if (!e.dataTransfer || !Array.prototype.includes.call(e.dataTransfer.types || [], 'Files')) return;
      e.preventDefault();
      e.stopPropagation();
      panel.classList.add('avd-drop-hot');
    });
    panel.addEventListener('dragleave', function () { panel.classList.remove('avd-drop-hot'); });
    panel.addEventListener('drop', function (e) {
      if (!e.dataTransfer || !e.dataTransfer.files || !e.dataTransfer.files.length) return;
      e.preventDefault();
      e.stopPropagation();
      panel.classList.remove('avd-drop-hot');
      panel.classList.add('open');
      uploadLocalFile(e.dataTransfer.files[0]);
    });

    go.addEventListener('click', function () {
      var url = urlInput.value.trim();
      if (!url) { setStatus('请先粘贴链接或选择本地文件'); urlInput.focus(); return; }
      go.disabled = true; go.textContent = '运行中';
      cancelBtn.disabled = false;
      note.classList.remove('show'); note.innerHTML = '';
      preview.classList.remove('show');
      setProgress(2); setStatus('正在启动流水线…');
      var body = {
        url: url,
        want_download: el('avdDownload').checked,
        want_polish: el('avdPolish').checked
      };
      if (el('avdExpand') && el('avdExpand').checked) body.expand_playlist = true;
      api('/api/pipeline/start', {
        method: 'POST',
        body: JSON.stringify(body)
      }).then(function (data) {
        beginPipelineWatch(data.task);
      }).catch(function (e) {
        go.disabled = false; go.textContent = '生成';
        cancelBtn.disabled = true;
        setStatus('启动失败：' + e.message);
      });
    });

    el('avdBatchGo').addEventListener('click', function () {
      var text = (el('avdBatch').value || '').trim();
      var single = urlInput.value.trim();
      if (!text && single) text = single;
      if (!text) { setStatus('请在批量框粘贴多条链接（每行一条）'); el('avdBatch').focus(); return; }
      go.disabled = true; go.textContent = '运行中';
      el('avdBatchGo').disabled = true;
      cancelBtn.disabled = false;
      note.classList.remove('show'); note.innerHTML = '';
      preview.classList.remove('show');
      setProgress(2); setStatus('正在批量排队…');
      var delaySec = parseInt((el('avdDelay') && el('avdDelay').value) || '0', 10) || 0;
      api('/api/pipeline/batch', {
        method: 'POST',
        body: JSON.stringify({
          text: text,
          want_download: el('avdDownload').checked,
          want_polish: el('avdPolish').checked,
          delay_seconds: delaySec > 0 ? delaySec : undefined
        })
      }).then(function (data) {
        var tasks = data.tasks || [];
        var skipped = data.skipped || [];
        if (!tasks.length) throw new Error('没有可启动的任务');
        currentId = tasks[0].id;
        var msg = '已排队 ' + tasks.length + ' 条';
        if (skipped.length) msg += '（跳过 ' + skipped.length + '）';
        if (data.not_before) msg += ' · 定时 ' + new Date(data.not_before * 1000).toLocaleString();
        msg += ' · 跟踪首条进度';
        setStatus(msg);
        if (skipped.length) {
          fillNoteSafe(skipped.slice(0, 8).map(function (s) {
            return { cls: 'avd-line-sm', bold: '跳过：', text: (s.url || '') + ' — ' + (s.reason || '') };
          }));
        }
        watch(currentId);
      }).catch(function (e) {
        go.disabled = false; go.textContent = '生成';
        cancelBtn.disabled = true;
        setStatus('批量启动失败：' + e.message);
      }).finally(function () {
        el('avdBatchGo').disabled = false;
      });
    });

    cancelBtn.addEventListener('click', function () {
      if (!currentId) return;
      cancelBtn.disabled = true;
      setStatus('正在取消…');
      api('/api/pipeline/cancel', {
        method: 'POST',
        body: JSON.stringify({ id: currentId })
      }).then(function () {
        setStatus('已请求取消');
        poll();
      }).catch(function (e) {
        cancelBtn.disabled = false;
        setStatus('取消失败：' + e.message);
      });
    });

    diagnoseBtn.addEventListener('click', function () {
      var url = urlInput.value.trim();
      if (!url) { setStatus('请先粘贴链接'); urlInput.focus(); return; }
      diagnoseBtn.disabled = true;
      setStatus('正在诊断站点要求…');
      note.classList.remove('show'); note.innerHTML = '';
      api('/api/platform/diagnose', {
        method: 'POST',
        body: JSON.stringify({ url: url })
      }).then(function (data) {
        diagnoseBtn.disabled = false;
        var diag = data.diagnosis || {};
        var ready = data.ready;
        var rows = [
          { bold: ready ? '可以一键出稿' : (diag.title || '未就绪'), text: '' }
        ];
        if (diag.detail) rows.push({ cls: 'avd-mt-4', text: String(diag.detail) });
        (diag.actions || []).forEach(function (a, i) {
          rows.push({ bold: '动作 ' + (i + 1) + '：', text: String(a) });
        });
        var auth = data.auth || {};
        if (auth.cookies_from_browser || auth.cookie_file_present) {
          rows.push({
            cls: 'avd-mt-6 avd-muted',
            text: '凭据：浏览器=' + (auth.cookies_from_browser || '无')
              + ' · cookie 文件=' + (auth.cookie_file_present ? '有' : '无')
          });
        }
        fillNoteSafe(rows);
        setStatus(ready ? '诊断通过 · 可点「生成」' : ('诊断：' + (diag.code || '未就绪')));
      }).catch(function (e) {
        diagnoseBtn.disabled = false;
        setStatus('诊断失败：' + e.message);
      });
    });

    el('avdLibSearch').addEventListener('click', function () {
      var q = (el('avdLibQ').value || '').trim();
      if (!q) { setStatus('请输入检索词'); return; }
      setStatus('检索中…');
      api('/api/library/search?q=' + encodeURIComponent(q))
        .then(function (data) {
          var hits = data.results || [];
          lastSearchIds = hits.map(function (h) { return h.id; }).filter(Boolean);
          if (!hits.length) { setLibOut([{ text: '无命中' }]); setStatus('无命中'); return; }
          setLibOut(hits.slice(0, 8).map(function (h) {
            return { hit: h, cls: 'avd-lib-hit' };
          }));
          setStatus('命中 ' + hits.length + ' 条 · 点击时间戳可定位');
        }).catch(function (e) { setStatus('检索失败：' + e.message); });
    });

    function resolveLibIds() {
      var raw = (el('avdLibIds') && el('avdLibIds').value || '').trim();
      if (raw) {
        return raw.split(/[,，\s]+/).map(function (s) { return s.trim(); }).filter(Boolean);
      }
      return lastSearchIds.slice(0, 50);
    }

    el('avdLibTag').addEventListener('click', function () {
      var tagsRaw = (el('avdLibTags') && el('avdLibTags').value || '').trim();
      var tags = tagsRaw.split(/[,，]/).map(function (s) { return s.trim(); }).filter(Boolean);
      var ids = resolveLibIds();
      if (!tags.length) { setStatus('请填写标签'); return; }
      if (!ids.length) { setStatus('请先搜索或填写素材 id'); return; }
      setStatus('批量打标中…');
      api('/api/library/batch', {
        method: 'POST',
        body: JSON.stringify({ action: 'add_tags', ids: ids, tags: tags })
      }).then(function (data) {
        setLibOut([{ text: '已打标 ' + (data.updated || 0) + ' 条 · ' + tags.join('、') }]);
        setStatus('批量打标完成');
      }).catch(function (e) { setStatus('打标失败：' + e.message); });
    });

    el('avdLibCluster').addEventListener('click', function () {
      setStatus('自动归类中…');
      api('/api/library/batch', {
        method: 'POST',
        body: JSON.stringify({ action: 'auto_cluster', ids: [] })
      }).then(function (data) {
        setLibOut([{
          text: '自动归类 updated=' + (data.updated || 0)
            + (data.clusters ? (' · clusters=' + (data.clusters.length || 0)) : '')
        }]);
        setStatus('自动归类完成');
      }).catch(function (e) { setStatus('归类失败：' + e.message); });
    });

    // ------------------------------------------------ 素材库浏览（R2-3）
    function collectionLabel(row) {
      var name = (row && (row.collection || row.name)) || '(未命名)';
      var count = (row && (row.count != null ? row.count : row.materials));
      return name + (count != null ? '（' + count + '）' : '');
    }

    function loadCollections() {
      api('/api/library/collections')
        .then(function (data) {
          var rows = data.collections || [];
          var sel = el('avdCollSel');
          var current = sel.value;
          sel.textContent = '';
          var all = document.createElement('option');
          all.value = '';
          all.textContent = '全部合集（' + rows.length + '）';
          sel.appendChild(all);
          rows.forEach(function (row) {
            var name = (row && (row.collection || row.name)) || '';
            if (!name) return;
            var opt = document.createElement('option');
            opt.value = name;
            opt.textContent = collectionLabel(row);
            sel.appendChild(opt);
          });
          if (current) sel.value = current;
          // 一台新机器第一次打开时「合集为空」是正常态，得说清楚不是坏了
          setStatus(rows.length
            ? ('合集 ' + rows.length + ' 个 · 选一个再点「浏览素材」')
            : '素材库还是空的：跑一次流水线就会自动入库');
          setLibOut(rows.length
            ? rows.slice(0, 12).map(function (row) {
              var name = (row && (row.collection || row.name)) || '';
              return {
                text: collectionLabel(row),
                onClick: function () {
                  el('avdCollSel').value = name;
                  loadMaterials();
                }
              };
            })
            : [{ text: '暂无合集（素材库为空）' }]);
        }).catch(function (e) {
          setStatus('合集读取失败：' + e.message);
          setLibOut([{ text: '合集读取失败：' + e.message }]);
        });
    }

    function loadMaterials() {
      var coll = el('avdCollSel').value || '';
      var q = '/api/library/materials?limit=50' + (coll ? ('&collection=' + encodeURIComponent(coll)) : '');
      setStatus(coll ? ('列出合集「' + coll + '」的素材…') : '列出全部素材…');
      api(q).then(function (data) {
        var mats = data.materials || [];
        lastSearchIds = mats.map(function (m) { return m.id; }).filter(Boolean);
        if (!mats.length) {
          setLibOut([{ text: '该合集暂无素材' }]);
          setStatus('没有素材');
          return;
        }
        setLibOut(mats.slice(0, 20).map(function (m) {
          var title = m.title || m.id || '';
          var extra = m.meta || {};
          var meta = [];
          if (extra.source_kind) meta.push(extra.source_kind);
          if (extra.duration) meta.push(Math.round(Number(extra.duration)) + 's');
          if (m.preview) meta.push(m.preview.length + ' 字预览');
          if (m.collection) meta.push(m.collection);
          return {
            text: '《' + title + '》',
            meta: meta.join(' · '),
            onClick: function () {
              // 点一行 = 把它填进 id 输入框，下一步可以直接成册/打标
              el('avdLibIds').value = m.id || '';
              el('avdLibColl').value = m.collection || coll || '';
              setStatus('已选中 ' + (m.id || '') + ' · 可继续成册 / 打标');
            }
          };
        }));
        setStatus('素材 ' + mats.length + ' 条 · 点击某行可选中');
      }).catch(function (e) {
        setStatus('素材列表失败：' + e.message);
        setLibOut([{ text: '素材列表失败：' + e.message }]);
      });
    }

    el('avdCollReload').addEventListener('click', loadCollections);
    el('avdMatsList').addEventListener('click', loadMaterials);

    // ------------------------------------------------ 任务历史（R2-3）
    var TASK_STAGE_LABEL = {
      queued: '排队中', probe: '解析中', download: '下载中', transcript: '转写中',
      polish: '整理中', knowledge: '生成中', done: '已完成', error: '失败',
      cancelled: '已取消'
    };

    function loadTaskHistory() {
      setStatus('读取任务历史…');
      api('/api/pipeline/tasks').then(function (data) {
        var tasks = data.tasks || [];
        if (!tasks.length) {
          setLibOut([{ text: '还没有任务历史' }]);
          setStatus('暂无历史任务');
          return;
        }
        setLibOut(tasks.slice(0, 15).map(function (t) {
          var sum = t.degraded_summary || {};
          var badge = sum.worst
            ? { level: sum.worst, label: sum.worst_label || sum.worst,
                hint: LEVEL_HINT[sum.worst] || '' }
            : null;
          var bits = [TASK_STAGE_LABEL[t.stage] || t.stage];
          if (t.segment_count) bits.push(t.segment_count + ' 段');
          if (t.stale) bits.push('产物已删');
          if (sum.total) bits.push(sum.total + ' 条降级');
          return {
            badge: badge,
            text: (t.title || t.id || '(无标题)'),
            meta: '· ' + bits.join(' · '),
            cls: t.stale ? 'avd-lib-hit avd-stale' : 'avd-lib-hit',
            onClick: function () {
              if (t.stale) {
                setStatus('《' + (t.title || t.id) + '》产物已删除，仅剩历史记录');
              }
              currentId = t.id;
              applyTask(t);
              if (t.stage === 'done' && !t.stale) showResult(t);
              setStatus('已切换到 ' + (t.title || t.id));
            }
          };
        }));
        setStatus('历史 ' + tasks.length + ' 条 · 点击可切换并预览');
      }).catch(function (e) {
        setStatus('任务历史读取失败：' + e.message);
        setLibOut([{ text: '任务历史读取失败：' + e.message }]);
      });
    }

    el('avdTaskList').addEventListener('click', loadTaskHistory);

    el('avdStorage').addEventListener('click', function () {
      setStatus('统计存储占用…');
      api('/api/storage/stats').then(function (data) {
        var policy = data.policy || {};
        var rows = [{
          text: '任务快照 ' + (data.total || 0) + ' 条 · 输出目录 '
            + fmtBytes(data.outputs_bytes || 0)
        }];
        Object.keys(data.by_kind || {}).forEach(function (kind) {
          rows.push({ text: kind + ' · ' + data.by_kind[kind] + ' 条', meta: '' });
        });
        if (policy.task_retain_days != null) {
          rows.push({
            badge: { level: 'note', label: '策略' },
            text: '保留 ' + policy.task_retain_days + ' 天 · 上限 '
              + (policy.outputs_max_gb != null ? policy.outputs_max_gb + ' GB' : '不限'),
            meta: data.path ? ('· ' + data.path) : ''
          });
        }
        setLibOut(rows);
        setStatus('存储占用已统计');
      }).catch(function (e) {
        setStatus('存储统计失败：' + e.message);
      });
    });

    function fmtBytes(n) {
      var v = Number(n) || 0;
      var units = ['B', 'KB', 'MB', 'GB', 'TB'];
      var i = 0;
      while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
      return (i === 0 ? v.toFixed(0) : v.toFixed(1)) + ' ' + units[i];
    }

    el('avdUpdates').addEventListener('click', function () {
      setStatus('检查更新…');
      api('/api/updates/check')
        .then(function (data) {
          var msg = data.message || (data.update_available
            ? ('有新版本 ' + (data.latest || ''))
            : (data.enabled ? '已是最新' : '未启用自动更新'));
          setLibOut([{ text: msg + (data.current ? (' · 当前 ' + data.current) : '') }]);
          setStatus(data.update_available ? '发现更新' : '更新检查完成');
        }).catch(function (e) { setStatus('更新检查失败：' + e.message); });
    });

    el('avdLibAsk').addEventListener('click', function () {
      var q = (el('avdLibQ').value || '').trim();
      if (!q) { setStatus('请输入问题'); return; }
      setStatus('问答生成中…');
      api('/api/library/ask', { method: 'POST', body: JSON.stringify({ q: q }) })
        .then(function (data) {
          setLibOut([{ text: data.answer || '无答案' }]);
          setStatus((data.source === 'llm' ? 'LLM 问答' : '规则问答') + (data.cached ? '（缓存）' : ''));
        }).catch(function (e) { setStatus('问答失败：' + e.message); });
    });

    el('avdLibBook').addEventListener('click', function () {
      var coll = (el('avdLibColl').value || '').trim();
      if (!coll) { setStatus('请填写合集名'); return; }
      setStatus('正在成册…');
      api('/api/library/export-booklet', {
        method: 'POST',
        body: JSON.stringify({ collection: coll, fmt: 'all', template: 'delivery' })
      }).then(function (data) {
        var bits = [];
        if (data.md) bits.push('MD');
        if (data.docx) bits.push('DOCX');
        if (data.pdf) bits.push('PDF');
        var pathHint = data.pdf || data.docx || data.md || '';
        setLibOut([{
          text: '成册完成（' + (bits.join('+') || data.fmt || '') + '）：' + pathHint
            + (data.pdf_error ? ' · PDF跳过:' + data.pdf_error : '')
        }]);
        setStatus('合集成册 ' + (data.count || 0) + ' 篇 · 模板 ' + (data.template || 'delivery'));
      }).catch(function (e) { setStatus('成册失败：' + e.message); });
    });

    el('avdDiagPack').addEventListener('click', function () {
      setStatus('正在生成诊断包…');
      api('/api/diagnose/pack', {
        method: 'POST', body: JSON.stringify({ id: currentId || '' })
      }).then(function (data) {
        setLibOut([{ text: 'ticket_id=' + (data.ticket_id || '') + ' · ' + (data.path || '') }]);
        setStatus('诊断包已生成');
      }).catch(function (e) { setStatus('诊断包失败：' + e.message); });
    });

    el('avdDiagTickets').addEventListener('click', function () {
      api('/api/diagnose/tickets')
        .then(function (data) {
          var tickets = data.tickets || [];
          if (!tickets.length) { setLibOut([{ text: '暂无工单回执' }]); return; }
          setLibOut(tickets.slice(0, 10).map(function (t) {
            var st = t.submit_status || 'local';
            return {
              text: (t.ticket_id || '') + (t.exists ? '' : '（zip 已删）')
                + ' · ' + st + ' · ' + (t.generated_at || ''),
              cls: t.exists ? 'avd-lib-hit' : 'avd-lib-hit avd-stale'
            };
          }));
          setStatus('工单 ' + tickets.length + ' 条');
        }).catch(function (e) { setStatus('工单列表失败：' + e.message); });
    });

    el('avdDiagSubmit').addEventListener('click', function () {
      setStatus('读取最近工单…');
      api('/api/diagnose/tickets')
        .then(function (data) {
          var tickets = data.tickets || [];
          var hit = tickets.find(function (t) { return t.exists; }) || tickets[0];
          if (!hit || !hit.ticket_id) throw new Error('没有可提交的工单，请先生成诊断包');
          setStatus('提交工单 ' + hit.ticket_id + '…');
          return api('/api/diagnose/submit', {
            method: 'POST', body: JSON.stringify({ ticket_id: hit.ticket_id })
          }).then(function (res) {
            setLibOut([{
              text: (res.status || '') + ' · ' + (res.ticket_id || '')
                + (res.remote_receipt ? (' · remote=' + res.remote_receipt) : '')
                + (res.message ? (' · ' + res.message) : '')
            }]);
            setStatus(res.status === 'submitted' ? '工单已上传' : (res.message || '本地闭环'));
          });
        }).catch(function (e) { setStatus('提交失败：' + e.message); });
    });

    el('avdRecoverable').addEventListener('click', function () {
      api('/api/pipeline/recoverable')
        .then(function (data) {
          var items = data.items || [];
          if (!items.length) { setLibOut([{ text: '暂无未完成检查点' }]); setStatus('无可续跑任务'); return; }
          setLibOut(items.slice(0, 10).map(function (it) {
            var src = String(it.source || '');
            var label = src ? src.replace(/^https?:\/\//, '').slice(0, 46) : '（旧检查点无来源）';
            var meta = 'resume_chunk=' + (it.resume_chunk != null ? it.resume_chunk : '?')
              + '/' + (it.chunks_total || '?')
              + ' · ' + (it.segments != null ? it.segments : '?') + ' 段'
              + (it.has_crash ? ' · 有崩溃现场' : '')
              + ' · ' + label;
            return {
              text: (it.task_id || '') + '  ',
              meta: meta,
              cls: 'avd-lib-hit' + (src ? '' : ' avd-lib-disabled'),
              // 点一行就续跑这一条：把 task_id 交给 retry，后端按检查点恢复。
              onClick: src ? function () { resumeTask(it.task_id); } : null
            };
          }));
          var usable = items.filter(function (it) { return !!it.source; }).length;
          setStatus('可续跑 ' + items.length + ' 条' + (usable < items.length
            ? '（其中 ' + usable + ' 条可点，旧检查点未记录来源）'
            : '，点一条即续跑'));
        }).catch(function (e) { setStatus('扫描失败：' + e.message); });
    });

    function resumeTask(id) {
      if (!id) return;
      setStatus('正在从检查点续跑 ' + id + '…');
      api('/api/pipeline/retry', {
        method: 'POST', body: JSON.stringify({ id: id })
      }).then(function (data) {
        var task = data.task || {};
        currentId = task.id || id;
        go.disabled = true; go.textContent = '运行中';
        cancelBtn.disabled = false;
        watch(currentId);
        setStatus('已续跑 · task_id=' + currentId);
      }).catch(function (e) { setStatus('续跑失败：' + e.message); });
    }

    el('avdRetry').addEventListener('click', function () {
      if (!currentId) { setStatus('没有可重试的任务'); return; }
      setStatus('正在重试续跑…');
      api('/api/pipeline/retry', {
        method: 'POST', body: JSON.stringify({ id: currentId })
      }).then(function (data) {
        var task = data.task || {};
        currentId = task.id || currentId;
        go.disabled = true; go.textContent = '运行中';
        cancelBtn.disabled = false;
        watch(currentId);
        setStatus('已重试 · 复用 task_id=' + currentId);
      }).catch(function (e) { setStatus('重试失败：' + e.message); });
    });

    el('avdExports').addEventListener('click', function (ev) {
      var btn = ev.target.closest('[data-fmt]');
      if (!btn || !currentId) { setStatus('请先生成一份知识稿'); return; }
      var fmt = btn.getAttribute('data-fmt');
      api('/api/pipeline/result?id=' + encodeURIComponent(currentId) + '&fmt=' + fmt)
        .then(function (data) {
          var blob;
          if (fmt === 'docx' || fmt === 'pdf' || data.encoding === 'base64') {
            var bin = atob(data.content || '');
            var arr = new Uint8Array(bin.length);
            for (var i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
            var mime = fmt === 'pdf'
              ? 'application/pdf'
              : 'application/vnd.openxmlformats-officedocument.wordprocessingml.document';
            blob = new Blob([arr], { type: mime });
          } else {
            blob = new Blob([data.content], { type: 'text/plain;charset=utf-8' });
          }
          var href = URL.createObjectURL(blob);
          var link = document.createElement('a');
          link.href = href;
          link.download = '知识稿.' + fmt;
          document.body.appendChild(link); link.click(); link.remove();
          setTimeout(function () { URL.revokeObjectURL(href); }, 1500);
          setStatus('已导出 ' + fmt.toUpperCase());
        }).catch(function (e) { setStatus('导出失败：' + e.message); });
    });

    el('avdObsidian').addEventListener('click', function () {
      if (!currentId) { setStatus('请先生成一份知识稿'); return; }
      setStatus('正在写入 Obsidian…');
      api('/api/pipeline/export-obsidian', {
        method: 'POST',
        body: JSON.stringify({ id: currentId })
      }).then(function (data) {
        setStatus('已写入 Obsidian：' + (data.path || ''));
      }).catch(function (e) {
        setStatus('Obsidian 写入失败：' + e.message + '（请在设置中配置库目录）');
      });
    });

    function pushExport(target, label) {
      if (!currentId) { setStatus('请先生成一份知识稿'); return; }
      setStatus('正在推送 ' + label + '…');
      api('/api/pipeline/export-push', {
        method: 'POST',
        body: JSON.stringify({ id: currentId, target: target })
      }).then(function (data) {
        var msg = (data && data.message) || ('已推送 ' + label);
        if (data && data.truncated) msg += ' · 未完整写入卡片/页，请用全文链接或本地稿';
        setStatus(msg);
      }).catch(function (e) {
        setStatus(label + '推送失败：' + e.message + '（请在设置中配置）');
      });
    }
    el('avdPushNotion').addEventListener('click', function () { pushExport('notion', 'Notion'); });
    el('avdPushFeishu').addEventListener('click', function () { pushExport('feishu', '飞书'); });

    el('avdClip').addEventListener('click', function () {
      if (!currentId) { setStatus('请先生成一份知识稿（并勾选下载媒体）'); return; }
      el('avdClipBox').classList.add('open-flex');
      setStatus('填写起止秒后点「导出切片」');
    });
    el('avdClipGo').addEventListener('click', function () {
      if (!currentId) return;
      var start = parseFloat(el('avdClipStart').value || '0');
      var end = parseFloat(el('avdClipEnd').value || '0');
      setStatus('正在切片…');
      api('/api/pipeline/export-clip', {
        method: 'POST',
        body: JSON.stringify({ id: currentId, start: start, end: end })
      }).then(function (data) {
        setStatus('切片已保存：' + (data.path || '') + '（' + (data.mode || '') + '）');
      }).catch(function (e) {
        setStatus('切片失败：' + e.message);
      });
    });

    el('avdClipChapters').addEventListener('click', function () {
      if (!currentId) { setStatus('请先生成一份知识稿（并勾选下载媒体）'); return; }
      el('avdClipChapters').disabled = true;
      setStatus('按章节批量切片中…');
      api('/api/pipeline/export-chapters', {
        method: 'POST',
        body: JSON.stringify({ id: currentId })
      }).then(function (data) {
        var clips = data.clips || [];
        var ok = clips.filter(function (c) { return !c.error; });
        var bad = clips.filter(function (c) { return c.error; });
        setLibOut(clips.slice(0, 20).map(function (c) {
          if (c.error) {
            return {
              badge: { level: 'warn', label: '失败' },
              text: (c.title || '') + ' · ' + c.error
            };
          }
          return {
            badge: { level: 'note', label: '已切' },
            text: (c.title || ('第 ' + c.start + 's')) + ' · '
              + Math.round(Number(c.end) - Number(c.start)) + 's',
            meta: c.mode || ''
          };
        }));
        setStatus('章节切片完成：成功 ' + ok.length
          + (bad.length ? (' · 失败 ' + bad.length) : '')
          + ' · 目录 ' + (data.dir || ''));
      }).catch(function (e) {
        setStatus('章节切片失败：' + e.message + '（需已下载媒体且平台提供章节）');
      }).finally(function () {
        el('avdClipChapters').disabled = false;
      });
    });

    var proofSegs = [];
    el('avdProof').addEventListener('click', function () {
      if (!currentId) { setStatus('请先生成一份知识稿'); return; }
      setStatus('加载分段以便校对…');
      api('/api/pipeline/result?id=' + encodeURIComponent(currentId) + '&fmt=json')
        .then(function (data) {
          var parsed = {};
          try { parsed = JSON.parse(data.content); } catch (err) { throw new Error('JSON 解析失败'); }
          proofSegs = Array.isArray(parsed.segments) ? parsed.segments : [];
          if (!proofSegs.length) throw new Error('没有可校对的分段');
          el('avdProofTa').value = proofSegs.map(function (s) {
            return (Number(s.start) || 0).toFixed(2) + '\t' + (Number(s.end) || 0).toFixed(2) + '\t' + (s.text || '');
          }).join('\n');
          el('avdProofBox').classList.add('open-block');
          setStatus('已进入校对：改完后点「保存校对」');
        }).catch(function (e) { setStatus('无法打开校对：' + e.message); });
    });

    el('avdProofCancel').addEventListener('click', function () {
      el('avdProofBox').classList.remove('open-block');
      setStatus('已取消校对');
    });

    el('avdProofSave').addEventListener('click', function () {
      if (!currentId) return;
      var lines = (el('avdProofTa').value || '').split(/\n/);
      var segs = [];
      lines.forEach(function (line) {
        line = line.trim();
        if (!line) return;
        var parts = line.split(/\t+/);
        if (parts.length < 3) {
          var m = line.match(/^([\d.]+)\s+([\d.]+)\s+(.+)$/);
          if (!m) return;
          parts = [m[1], m[2], m[3]];
        }
        segs.push({
          start: parseFloat(parts[0]) || 0,
          end: parseFloat(parts[1]) || 0,
          text: parts.slice(2).join('\t').trim()
        });
      });
      if (!segs.length) { setStatus('校对内容为空'); return; }
      setStatus('正在回写校对结果…');
      api('/api/pipeline/proofread', {
        method: 'POST',
        body: JSON.stringify({ id: currentId, segments: segs })
      }).then(function (data) {
        el('avdProofBox').classList.remove('open-block');
        setStatus('校对已保存 · ' + (data.task.segment_count || segs.length) + ' 句');
        showResult(data.task || { id: currentId, files: { md: true } });
      }).catch(function (e) { setStatus('校对保存失败：' + e.message); });
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
