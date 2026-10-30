/* Heading colors across app themes and the generated Obsidian snippet. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const css = name => fs.readFileSync(path.join(root, 'local_web/static', name), 'utf8');
const headings = Array.from({length:6}, (_, i) => `<h${i+1}>标题 <strong>强调 <a href="#">链接</a></strong> <a href="#"><b>粗体链接</b></a> <em>斜体</em> <code>代码</code> <del>删除</del></h${i+1}>`).join('');

(async () => {
  const browser = await chromium.launch({headless:true, channel:process.env.BROWSER_CHANNEL || 'chrome'});
  try {
    const page = await browser.newPage();
    for (const theme of ['light', 'dark']) {
      const expected = theme === 'light' ? 'rgb(33, 97, 166)' : 'rgb(155, 200, 241)';
      await page.setContent(`<html data-theme="${theme}"><head><style>${css('styles.css')}\n${css('sea.css')}</style></head><body>
        <main><h1>页面标题</h1><div class="note-welcome"><h2>欢迎语</h2></div><article class="summary">${headings}<p><strong id="emphasis">正文强调</strong></p></article>
        <input class="note-title-input" value="笔记标题"><div class="header-title"><strong>当前页</strong></div>
        <div class="course-card"><strong class="course-title">课程</strong></div><div class="lecture-row"><strong>课次</strong></div>
        <div class="note-list-row"><strong>笔记</strong></div><div class="note-list-row active"><strong>选中笔记</strong></div>
        <div class="note-history-row"><strong>历史</strong></div><div class="note-folder-row"><span class="note-folder-label">文件夹</span></div>
        <div class="note-outline-heading">大纲</div><button class="note-outline-link">章节</button>
        <div class="local-run"><strong>本地任务</strong></div><div class="note-conflict-review"><strong>比较笔记版本</strong></div>
        <div class="course-drag-ghost"><strong>拖拽标题</strong></div><button class="sidebar-section-toggle"><span>分区</span></button><span class="sidebar-label">分区名</span></main></body></html>`);
      const colors = await page.locator('h1,h2,h3,h4,h5,h6,.summary :is(h1,h2,h3,h4,h5,h6) *,.note-title-input,.header-title strong,.course-title,.lecture-row strong,.note-list-row>strong,.note-history-row strong,.note-folder-label,.note-outline-heading,.note-outline-link,.local-run>strong,.note-conflict-review>strong,.course-drag-ghost>strong,.sidebar-section-toggle>span,.sidebar-label').evaluateAll(nodes => nodes.map(node => [node.outerHTML, getComputedStyle(node).color]));
      for (const [html, color] of colors) assert.equal(color, expected, `${theme}: ${html}`);
      assert.notEqual(await page.locator('#emphasis').evaluate(node => getComputedStyle(node).color), expected);
      const snippet = execFileSync(path.join(root, '.venv-course/bin/python'), ['-c', 'from local_web.obsidian_appearance import render_snippet; print(render_snippet())'], {cwd:root, encoding:'utf8'});
      await page.setContent(`<html><head><style>${snippet}</style></head><body class="theme-${theme}">
        <div class="inline-title">笔记标题</div><article class="markdown-rendered">${headings}</article>
        <div class="markdown-source-view"><span class="cm-header cm-strong">标题 <a href="#">链接</a></span><span class="cm-header"><span class="cm-strong"><span class="cm-hmd-internal-link">内部链接</span></span></span></div></body></html>`);
      const obsidianColors = await page.locator('.inline-title,h1,h2,h3,h4,h5,h6,.markdown-rendered :is(h1,h2,h3,h4,h5,h6) *,.cm-header,.cm-header *').evaluateAll(nodes => nodes.map(node => [node.outerHTML, getComputedStyle(node).color]));
      for (const [html, color] of obsidianColors) assert.equal(color, expected, `Obsidian ${theme}: ${html}`);
    }
    console.log('PASS blue titles: all six levels, nested emphasis/links/code, inline editor, lists, outline and generated Obsidian CSS in light/dark');
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode=1;});
