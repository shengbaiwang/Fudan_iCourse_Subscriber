/* One UI; the deployment selects its transport before app.js starts. */
(async () => {
  const pages = window.ICOURSE_RUNTIME === 'pages' || new URLSearchParams(location.search).get('runtime') === 'pages';
  window.ICOURSE_RUNTIME = pages ? 'pages' : 'local';
  window.ICOURSE_CAPABILITIES = {obsidian: !pages, providerTest: !pages};
  const script = (src) => new Promise((resolve, reject) => {
    const node = document.createElement('script');
    node.src = src;
    node.onload = resolve;
    node.onerror = () => reject(new Error(`无法加载 ${src}，请检查网络后刷新。`));
    document.head.append(node);
  });
  if (pages) {
    document.querySelector('#edition-label').textContent = 'WEB EDITION';
    document.querySelector('#token-hint').textContent = '当前标签页使用；关闭后需重新输入';
    document.querySelector('#setup-form').elements.token.required = true;
    document.querySelector('#legacy-credentials').classList.remove('hidden');
    document.querySelector('#setup-form button[type="submit"]').textContent = '连接并打开资料库';
    document.querySelector('#forget-credentials-button').textContent = '退出当前会话';
  }
  document.querySelectorAll('[data-capability]').forEach(node => {
    node.hidden = !window.ICOURSE_CAPABILITIES[node.dataset.capability];
  });
  try {
    if (pages) {
      for (const src of [
        'https://cdnjs.cloudflare.com/ajax/libs/sql.js/1.12.0/sql-wasm.js',
        'browser/crypto.js', 'browser/github.js', 'browser/schema.js', 'browser/db.js',
        'browser/transport.js',
      ]) await script(src);
    }
    await script('app.js');
    // Rich Markdown is progressive: an offline local console still opens immediately.
    try {
      for (const src of [
        'https://cdn.jsdelivr.net/npm/marked@15/marked.min.js',
        'https://cdn.jsdelivr.net/npm/dompurify@3/dist/purify.min.js',
        'https://cdn.jsdelivr.net/npm/katex@0.16.21/dist/katex.min.js',
        'https://cdn.jsdelivr.net/npm/katex@0.16.21/dist/contrib/auto-render.min.js',
        'browser/render.js',
      ]) await script(src);
      const css = document.createElement('link');
      css.rel = 'stylesheet';
      css.href = 'https://cdn.jsdelivr.net/npm/katex@0.16.21/dist/katex.min.css';
      document.head.append(css);
      window.dispatchEvent(new Event('icourse-render-ready'));
    } catch (error) { console.info('使用内置 Markdown 排版', error.message); }
  } catch (error) {
    const node = document.querySelector('#message');
    node.textContent = error.message;
    node.className = 'message error';
  }
})();
