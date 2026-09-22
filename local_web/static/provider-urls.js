/* Keep MiMo normalization aligned with src/runtime/model_config.py. */
(() => {
  window.ICourseProviderURLs = {
    normalize(value) {
      const base = String(value || '').trim().replace(/\/+$/, '');
      let url;
      try { url = new URL(base); } catch (_) { return base; }
      // Do not repair invalid URLs in a way that bypasses server validation.
      if (url.protocol !== 'https:' || url.username || url.password || /[?#\\\s]/.test(base)) return base;
      if (url.hostname === 'api.xiaomimimo.com' &&
          ['/', '/v1', '/chat/completions', '/v1/chat/completions', '/models', '/v1/models'].includes(url.pathname)) {
        return `${url.origin}/v1`;
      }
      return base;
    },
  };
})();
