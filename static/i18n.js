// Copyright (c) 2026 Hyht-Team. All rights reserved.
/* 轻量中英切换：data-i18n 属性 + AVD_I18N API。默认跟随 html[lang]。 */
(function () {
  'use strict';
  if (window.AVD_I18N) return;

  var DICT = {
    zh: {
      'brand.title': '素材工作台',
      'brand.sub': '本地提取 · 数据不出机',
      'fab.open': '旗舰版 · 一键知识稿',
      'flagship.title': '一键生成结构化知识稿',
      'flagship.sub': '一次解析、全链复用：画质 / 字幕轨 / 章节 / 出处一次取回，产出带 front matter 的 Markdown，可直接进知识库。',
      'flagship.go': '生成',
      'flagship.search': '搜索',
      'flagship.ask': '问答',
      'flagship.book': '成册',
      'flagship.tag': '打标',
      'flagship.cluster': '自动归类',
      'flagship.diag': '诊断包',
      'flagship.tickets': '工单',
      'flagship.submit': '提交工单',
      'flagship.recoverable': '可续跑',
      'flagship.retry': '重试续跑',
      'flagship.updates': '检查更新',
      'lang.toggle': 'EN',
    },
    en: {
      'brand.title': 'Media Workbench',
      'brand.sub': 'On-device · data stays local',
      'fab.open': 'Flagship · One-click notes',
      'flagship.title': 'One-click structured knowledge notes',
      'flagship.sub': 'Parse once, reuse everywhere: quality / captions / chapters / provenance → Markdown ready for your library.',
      'flagship.go': 'Run',
      'flagship.search': 'Search',
      'flagship.ask': 'Ask',
      'flagship.book': 'Booklet',
      'flagship.tag': 'Tag',
      'flagship.cluster': 'Auto-cluster',
      'flagship.diag': 'Diag pack',
      'flagship.tickets': 'Tickets',
      'flagship.submit': 'Submit ticket',
      'flagship.recoverable': 'Resumable',
      'flagship.retry': 'Retry resume',
      'flagship.updates': 'Check updates',
      'lang.toggle': '中文',
    }
  };

  function current() {
    var stored = '';
    try { stored = localStorage.getItem('avd_lang') || ''; } catch (_) {}
    if (stored === 'zh' || stored === 'en') return stored;
    var lang = (document.documentElement.getAttribute('lang') || 'zh').toLowerCase();
    return lang.indexOf('en') === 0 ? 'en' : 'zh';
  }

  function t(key) {
    var lang = current();
    return (DICT[lang] && DICT[lang][key]) || (DICT.zh && DICT.zh[key]) || key;
  }

  function apply(root) {
    var scope = root || document;
    scope.querySelectorAll('[data-i18n]').forEach(function (node) {
      var key = node.getAttribute('data-i18n');
      if (!key) return;
      var val = t(key);
      if (node.tagName === 'INPUT' || node.tagName === 'TEXTAREA') {
        node.setAttribute('placeholder', val);
      } else {
        node.textContent = val;
      }
    });
    scope.querySelectorAll('[data-i18n-aria]').forEach(function (node) {
      var key = node.getAttribute('data-i18n-aria');
      if (key) node.setAttribute('aria-label', t(key));
    });
    var btn = document.getElementById('btnLang');
    if (btn) btn.textContent = t('lang.toggle');
  }

  function setLang(lang) {
    var next = lang === 'en' ? 'en' : 'zh';
    document.documentElement.setAttribute('lang', next === 'en' ? 'en' : 'zh-CN');
    try { localStorage.setItem('avd_lang', next); } catch (_) {}
    apply(document);
    try {
      window.dispatchEvent(new CustomEvent('avd:lang', { detail: { lang: next } }));
    } catch (_) {}
    return next;
  }

  function toggle() {
    return setLang(current() === 'en' ? 'zh' : 'en');
  }

  window.AVD_I18N = { t: t, apply: apply, setLang: setLang, toggle: toggle, current: current, DICT: DICT };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { apply(document); });
  } else {
    apply(document);
  }
})();
