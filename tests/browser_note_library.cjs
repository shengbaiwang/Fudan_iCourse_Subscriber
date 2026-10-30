/* Unified notebook acceptance, with synthetic source stores and no real account. */
const assert = require('node:assert/strict');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root=path.resolve(__dirname,'..');
(async()=>{
  const server=spawn(path.join(root,'.venv-course/bin/python'),['-u','tests/serve_note_library.py'],{cwd:root,stdio:['ignore','pipe','pipe']});
  let logs='';server.stderr.on('data',data=>logs+=data);
  let browser;
  try {
    const base=await new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>reject(new Error(`Fixture timeout: ${logs}`)),15000);
      server.stdout.on('data',data=>{const match=data.toString().match(/READY (http:\/\/\S+)/);if(match){clearTimeout(timer);resolve(match[1]);}});
      server.on('exit',code=>{clearTimeout(timer);reject(new Error(`Fixture exited ${code}: ${logs}`));});
    });
    browser=await chromium.launch({headless:true,channel:process.env.BROWSER_CHANNEL || 'chrome'});
    const context=await browser.newContext({viewport:{width:1440,height:960},acceptDownloads:true});
    await context.route('https://**',route=>route.abort());
    const page=await context.newPage(),errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    await page.goto(base);
    await page.locator('#open-personal-library').click();
    await page.locator('#view-notes:not(.hidden)').waitFor();
    await page.waitForFunction(()=>window.ICS?.render);
    await page.locator('.note-course-row[data-course-id="history"]').click();
    await page.locator('.note-list-row').nth(2).waitFor();
    assert.equal(await page.locator('.desktop-nav [data-view="notes"]').innerText(),'笔记');
    assert.equal(await page.locator('.note-list-row').count(),3);
    for(const [kind,count] of [['course',1],['talk',1],['personal',1],['all',3]]){
      await page.locator(`[data-note-kind="${kind}"]`).click();
      await page.waitForFunction(count=>document.querySelectorAll('.note-list-row').length===count,count);
    }
    await page.locator('#note-folder-new').click();
    await page.locator('#note-folder-name').fill('历史研究');
    await page.locator('#note-folder-name').press('Enter');
    await page.locator('.note-folder-row[data-folder-id]:not([data-folder-id=""])').waitFor();
    const folderId=await page.locator('.note-folder-row[data-folder-id]:not([data-folder-id=""])').getAttribute('data-folder-id');
    const initial=(await (await page.request.get(`${base}/api/local/note-library`)).json()).notes;
    for(const row of initial){
      await page.locator(`[data-note-id="${row.id}"]`).click();
      await page.locator('#note-folder-path button').click();
      await page.getByRole('menuitem',{name:'历史研究',exact:true}).click();
      await page.waitForFunction(()=>document.querySelector('#note-folder-path').textContent.includes('历史研究'));
      await page.locator('#note-star').click();
    }
    // Folder assignment is singular and the expanded tree includes notes from every source.
    await page.waitForFunction(()=>document.querySelectorAll('.note-list-row').length===3);
    await page.locator('[data-note-filter="starred"]').click();
    assert.equal(await page.locator('.note-list-row').count(),3);
    const course=page.locator('[data-note-id="course:one"]');
    await course.click();
    await page.waitForFunction(()=>document.querySelector('#note-preview').textContent.includes('课程当前正文'));
    assert.equal(await page.locator('#note-preview strong').innerText(),'市场定价未必理性。');
    assert.equal(await page.locator('#note-title').getAttribute('readonly'),'');
    assert.equal(await page.locator('#note-mode').isVisible(),true);
    await page.locator('#note-version').selectOption({index:1});
    await page.waitForFunction(()=>document.querySelector('#note-preview').textContent.includes('课程旧版本正文'));
    await page.locator('#note-source-tab').click();
    await page.waitForFunction(()=>document.querySelector('#note-preview').textContent.includes('课堂转写'));
    await page.locator('#note-source-link').click();
    await page.locator('#view-detail:not(.hidden)').waitFor();
    assert.equal(await page.locator('[data-detail-tab="transcript"]').getAttribute('aria-selected'),'true');
    await page.locator('[data-detail-tab="summary"]').click();
    assert.equal(await page.locator('#view-detail .summary strong').innerText(),'市场定价未必理性。');
    await page.locator('#view-detail').getByRole('button',{name:'在笔记中打开'}).click();
    await page.waitForFunction(()=>document.querySelector('#note-preview').textContent.includes('课程当前正文'));
    const talk=initial.find(row=>row.kind==='talk');
    await page.locator(`[data-note-id="${talk.id}"]`).click();
    await page.locator('#note-source-tab').click();
    await page.waitForFunction(()=>document.querySelector('#note-preview').textContent.includes('讲座转写'));
    await page.locator('#note-source-link').click();
    await page.locator('#view-talk-detail:not(.hidden)').waitFor();
    await page.locator('#view-talk-detail').getByRole('button',{name:'在笔记中打开'}).click();
    await page.waitForFunction(()=>document.querySelector('#note-preview').textContent.includes('讲座当前正文'));
    assert.equal((await (await page.request.get(`${base}/api/local/notebook`)).json()).length,1);

    await page.locator('#note-folder-path button').click();
    await page.getByRole('menuitem',{name:'新建文件夹…'}).click();
    await page.locator('#note-folder-name').fill('阅读材料');await page.locator('#note-folder-name').press('Enter');
    await page.waitForFunction(()=>document.querySelector('#note-folder-path').textContent.includes('阅读材料'));
    assert.equal((await (await page.request.get(`${base}/api/local/note-library?folder=${folderId}`)).json()).notes.length,2);
    await page.locator('#message').getByRole('button',{name:'撤销'}).click();
    await page.waitForFunction(()=>document.querySelector('#note-folder-path').textContent.includes('历史研究'));
    const other=page.locator('.note-folder-row').filter({hasText:'阅读材料'});
    await other.click({button:'right'});await page.getByRole('menuitem',{name:'删除文件夹',exact:true}).click();
    await page.waitForFunction(()=>!document.querySelector('#note-list').textContent.includes('阅读材料'));

    // Rename inline, then delete and undo without losing locations.
    await page.locator('.note-folder-row[data-folder-id]:not([data-folder-id=""])').dblclick();
    await page.locator('#note-folder-name').fill('档案与记忆');await page.locator('#note-folder-name').press('Enter');
    await page.locator('.note-folder-row[data-folder-id]:not([data-folder-id=""])').filter({hasText:'档案与记忆'}).waitFor();
    await page.locator('.note-folder-row[data-folder-id]:not([data-folder-id=""])').click({button:'right'});
    await page.getByRole('menuitem',{name:'删除文件夹'}).click();
    await page.waitForFunction(()=>document.querySelectorAll('.note-folder-row[data-folder-id]:not([data-folder-id=""])').length===0);
    await page.locator('#message').getByRole('button',{name:'撤销'}).click();
    await page.locator('.note-folder-row[data-folder-id]:not([data-folder-id=""])').waitFor();
    assert.equal((await (await page.request.get(`${base}/api/local/note-library?folder=${folderId}`)).json()).notes.length,3);
    await page.locator('[data-note-filter="all"]').click();

    // Nested folders, current-folder creation, drag/drop, keyboard and touch alternatives.
    const parentFolder=page.locator(`[data-folder-id="${folderId}"]`);
    await parentFolder.click({button:'right'});await page.getByRole('menuitem',{name:'新建子文件夹'}).click();
    await page.locator('#note-folder-name').fill('子资料');await page.locator('#note-folder-name').press('Enter');
    const childFolder=page.locator('.note-folder-row').filter({hasText:'子资料'});await childFolder.waitFor();
    const childId=await childFolder.getAttribute('data-folder-id');
    let metadata=(await (await page.request.get(`${base}/api/local/note-library`)).json());
    assert.equal(metadata.folders.find(row=>row.id===childId).parent_id,folderId);
    await page.locator('#note-new').click();
    await page.locator('#note-title').fill('目录测试笔记');await page.locator('#note-body').fill('当前位置中的新笔记。');
    await page.waitForFunction(()=>document.querySelector('#note-save-state').textContent==='已保存');
    const temporary=(await (await page.request.get(`${base}/api/local/note-library`)).json()).notes.find(row=>row.title==='目录测试笔记');
    assert.equal(temporary.folder_id,childId);
    await page.locator(`[data-note-id="${temporary.id}"]`).dragTo(parentFolder);
    await page.waitForFunction(()=>document.querySelector('#note-folder-path').textContent.trim()==='档案与记忆 ▾');
    await page.locator('#message').getByRole('button',{name:'撤销'}).click();
    await page.waitForFunction(()=>document.querySelector('#note-folder-path').textContent.includes('子资料'));
    await childFolder.dragTo(page.locator('.note-folders-heading'));
    await page.waitForFunction(id=>{const node=document.querySelector(`[data-folder-id="${id}"]`);return node?.closest('.note-tree-branch').parentElement.id==='note-list';},childId);
    await page.locator('#message').getByRole('button',{name:'撤销'}).click();
    await page.waitForFunction(id=>{const node=document.querySelector(`[data-folder-id="${id}"]`);return node?.closest('.note-tree-branch').parentElement.classList.contains('note-folder-children');},childId);
    const childTree=page.locator(`[data-tree-folder-id="${childId}"]`);
    await childTree.focus();await page.keyboard.press('F2');
    await page.locator('#note-folder-name').fill('归档资料');await page.locator('#note-folder-name').press('Enter');
    await page.locator('.note-folder-row').filter({hasText:'归档资料'}).waitFor();
    await childTree.focus();await page.keyboard.press('ArrowLeft');
    assert.equal(await childTree.getAttribute('aria-expanded'),'false');
    await page.keyboard.press('ArrowRight');assert.equal(await childTree.getAttribute('aria-expanded'),'true');
    await page.keyboard.press('Shift+F10');await page.getByRole('menuitem',{name:'移动文件夹…'}).waitFor();await page.keyboard.press('Escape');
    const touchFolder=page.locator(`[data-folder-id="${childId}"]`);
    await touchFolder.dispatchEvent('pointerdown',{pointerType:'touch',clientX:20,clientY:20});
    await page.getByRole('menuitem',{name:'重命名文件夹'}).waitFor();
    await touchFolder.dispatchEvent('pointerup',{pointerType:'touch'});await page.keyboard.press('Escape');
    await page.reload();
    await page.waitForFunction(()=>document.querySelector('#note-folder-path').textContent.includes('归档资料'));
    await page.locator(`[data-note-id="${temporary.id}"]`).waitFor();
    // Deleting a parent moves descendant notes to the inbox and restores them on undo.
    await parentFolder.click({button:'right'});await page.getByRole('menuitem',{name:'删除文件夹',exact:true}).click();
    await page.waitForFunction(()=>document.querySelectorAll('.note-folder-row[data-folder-id]:not([data-folder-id=""])').length===0);
    assert.equal((await (await page.request.get(`${base}/api/local/note-library?folder=`)).json()).notes.length,4);
    await page.locator('#message').getByRole('button',{name:'撤销'}).click();
    await page.locator(`[data-folder-id="${childId}"]`).waitFor();
    await page.locator(`[data-note-id="${temporary.id}"]`).click();await page.locator('#note-more').click();
    await page.getByRole('menuitem',{name:'移入回收站'}).click();
    await page.locator(`[data-folder-id="${childId}"]`).click({button:'right'});await page.getByRole('menuitem',{name:'删除文件夹',exact:true}).click();
    // Source notes and personal notes use the same renderer and keep their original stores.

    const rendered=await page.evaluate(()=>{
      const example='李录的重要洞察：**市场在任何时点的定价，都不必然等于理性、稳定的价值判断。**他以一家人工智能公司为例。';
      const padded='这是**重要判断。 **后续内容。';
      const mixed='**中文与 `a ** b` 代码。**下一句';
      const negative='`**代码。**`与\\*\\*原样标记。\\*\\*';
      return {example:ICS.render.renderMarkdown(example),padded:ICS.render.renderMarkdown(padded),mixed:ICS.render.renderMarkdown(mixed),negative:ICS.render.renderMarkdown(negative)};
    });
    assert.match(rendered.example,/<strong>市场在任何时点的定价，都不必然等于理性、稳定的价值判断。<\/strong>他/);
    assert.match(rendered.padded,/<strong>重要判断。<\/strong>后续/);
    assert.match(rendered.mixed,/<strong>中文与 <code>a \*\* b<\/code> 代码。<\/strong>/);
    assert.ok(!rendered.negative.includes('<strong>'));

    // Import a complete Markdown file and its companion image into a reading page.
    const png=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a1YQAAAAASUVORK5CYII=','base64');
    const markdown='# Markdown 排版检查\n\n李录的重要洞察：**市场定价未必理性。**他继续解释。\n\n第一行\n同一段落。\n\n## 层级标题\n\n- 第一项\n  - 嵌套项目\n\n1. 顺序条目\n2. 下一条目\n\n> 引用内容\n\n| 名称 | 数量 |\n| :--- | ---: |\n| 档案 | 12 |\n\n```js\nconst example = "$P_n$";\nconst amount = "$5";\n```\n\n行内代码 `$x$` 与公式 $P_n$。\n\n$$E=mc^2$$\n\n![示例图片](images/pixel.png)\n\n- [x] 已整理\n- [ ] 待整理\n\n<img src=x onerror="window.__unsafe=1"><script>window.__unsafe=1</script>\n';
    await page.locator('#note-import-files').setInputFiles([{name:'排版.md',mimeType:'text/markdown',buffer:Buffer.from(markdown)},{name:'pixel.png',mimeType:'image/png',buffer:png}]);
    await page.locator('#note-preview h2').waitFor();
    assert.equal(await page.locator('#note-body').isVisible(),false);
    assert.equal(await page.locator('#note-preview strong').innerText(),'市场定价未必理性。');
    assert.equal(await page.locator('#note-mode').innerText(),'编辑');
    assert.equal(await page.locator('#note-preview ul ul li').innerText(),'嵌套项目');
    assert.equal(await page.locator('#note-preview ol li').count(),2);
    assert.equal(await page.locator('#note-preview blockquote').innerText(),'引用内容');
    assert.equal(await page.locator('#note-preview .table-wrap td').count(),2);
    assert.match(await page.locator('#note-preview pre code').innerText(),/\$P_n\$/);
    assert.equal(await page.locator('#note-preview code').last().innerText(),'$x$');
    assert.equal(await page.locator('#note-preview .katex').count(),2);
    assert.equal(await page.locator('#note-preview .markdown-checkbox').count(),2);
    await page.waitForFunction(()=>{const img=document.querySelector('#note-preview img');return img?.complete && img.naturalWidth===1;});
    assert.equal(await page.evaluate(()=>window.__unsafe),undefined);
    assert.equal(await page.locator('#note-preview [onerror], #note-preview script').count(),0);
    await page.screenshot({path:'/tmp/sea-unified-notes-desktop.png',fullPage:true});
    await page.locator('#theme-toggle').click();
    assert.equal(await page.evaluate(()=>document.documentElement.dataset.theme),'dark');
    assert.equal(await page.locator('#note-preview strong').innerText(),'市场定价未必理性。');
    assert.deepEqual(await page.locator('#note-preview strong').evaluate(node=>{
      const style=getComputedStyle(node);return {weight:style.fontWeight,color:style.color};
    }),{weight:'400',color:'rgb(253, 164, 175)'});
    await page.screenshot({path:'/tmp/sea-folders-markdown-dark.png',fullPage:true});
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await page.screenshot({path:'/tmp/sea-unified-markdown-mobile.png',fullPage:true});
    await page.locator('#theme-toggle').click();
    await page.setViewportSize({width:1440,height:960});
    await page.locator('#note-mode').click();
    const importedTitle=await page.locator('#note-title').inputValue();
    await page.locator('#note-body').fill((await page.locator('#note-body').inputValue())+'\n\n个人补充。');
    await page.waitForFunction(()=>document.querySelector('#note-save-state').textContent==='已保存');
    await page.locator('#note-more').click();await page.getByRole('menuitem',{name:'历史版本'}).click();
    await page.locator('.note-history-row').first().getByRole('button',{name:'恢复此版本'}).click();
    assert.ok(!(await page.locator('#note-body').inputValue()).includes('个人补充'));
    await page.locator('#note-more').click();await page.getByRole('menuitem',{name:'移入回收站'}).click();
    await page.locator('#message').getByRole('button',{name:'撤销'}).click();
    await page.waitForFunction(title=>document.querySelector('#note-title').value===title,importedTitle);
    const [download]=await Promise.all([page.waitForEvent('download'),(async()=>{await page.locator('#note-more').click();await page.getByRole('menuitem',{name:'导出 Markdown'}).click();})()]);
    assert.equal(download.suggestedFilename(),'Markdown 排版检查.md');

    // A competing save cannot silently overwrite a dirty editor; retain as a new note.
    const row=(await (await page.request.get(`${base}/api/local/notebook`)).json()).find(row=>row.title===importedTitle);
    await page.locator('#note-mode').click();
    await page.locator('#note-body').fill('本窗口尚未保存的草稿。');
    const external=await page.request.put(`${base}/api/local/notebook/${row.id}`,{data:{...row,body:'另一窗口已经保存的内容。'}});
    assert.equal(external.status(),200);
    await page.getByRole('button',{name:'保留为新笔记'}).waitFor();
    await page.getByRole('button',{name:'保留为新笔记'}).click();
    await page.waitForFunction(()=>document.querySelector('#note-body').value==='本窗口尚未保存的草稿。');
    assert.equal((await (await page.request.get(`${base}/api/local/notebook/${row.id}`)).json()).body,'另一窗口已经保存的内容。');

    await page.locator('.desktop-nav [data-view="search"]').click();await page.locator('#search').fill('讲座当前正文');
    await page.locator('.personal-search-card').first().click();
    await page.waitForFunction(()=>document.querySelector('#note-preview').textContent.includes('讲座当前正文'));
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await page.screenshot({path:'/tmp/sea-unified-notes-mobile.png',fullPage:true});
    await page.locator('#note-index-back').click();assert.equal(await page.locator('#note-list').isVisible(),true);
    await page.setViewportSize({width:1440,height:960});
    await page.locator('.desktop-nav [data-view="settings"]').click();
    const [backup]=await Promise.all([page.waitForEvent('download'),page.locator('#notebook-backup').click()]);
    await backup.saveAs('/tmp/sea-unified-notes-backup.zip');
    await page.locator('#notebook-restore-file').setInputFiles('/tmp/sea-unified-notes-backup.zip');
    await page.waitForFunction(()=>document.querySelector('#notebook-backup-status').textContent.startsWith('已导入 0'));
    assert.deepEqual(errors,[]);
    console.log('Unified notes: sources, folders, Chinese emphasis, undo, versions, transcript, Markdown/images/math/code, edit/history/trash/export, conflict drafts, search, mobile and backup passed');
  } finally {if(browser)await browser.close();server.kill('SIGTERM');}
})().catch(error=>{console.error(error);process.exitCode=1;});
