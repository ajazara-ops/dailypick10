// Translations are authored in static HTML. No translation service is used.
(function () {
  'use strict';
  const key = 'ajazara.language';
  function applyLanguage(language, remember) {
    if (language !== 'en' && language !== 'ko') return;
    document.documentElement.setAttribute('data-site-lang', language);
    if (document.documentElement.getAttribute('data-company-page') === 'true') {
      document.documentElement.lang = language;
      document.title = document.documentElement.getAttribute('data-title-' + language);
      const description = document.documentElement.getAttribute('data-description-' + language);
      ['meta[name="description"]', 'meta[property="og:description"]'].forEach(function (selector) {
        const meta = document.querySelector(selector); if (meta) meta.setAttribute('content', description);
      });
      const titleMeta = document.querySelector('meta[property="og:title"]'); if (titleMeta) titleMeta.setAttribute('content', document.title);
    }
    document.querySelectorAll('[data-language]').forEach(function (button) {
      button.setAttribute('aria-pressed', String(button.getAttribute('data-language') === language));
    });
    if (remember) { try { localStorage.setItem(key, language); } catch (error) {} }
  }
  function init() { applyLanguage(document.documentElement.getAttribute('data-site-lang'), false); }
  document.addEventListener('click', function (event) {
    const button = event.target.closest('[data-language]');
    if (button) applyLanguage(button.getAttribute('data-language'), true);
  });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
  window.addEventListener('storage', function (event) { if (event.key === key) applyLanguage(event.newValue || document.documentElement.getAttribute('data-default-language'), false); });
})();
