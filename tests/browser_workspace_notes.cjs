/* Workspace panels and editable notes, using isolated synthetic sources. */
const assert=require('node:assert/strict');
const path=require('node:path');
const {spawn}=require('node:child_process');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root=path.resolve(__dirname,'..');
(async()=>{
  const server=spawn(path.join(root,'.venv-course/bin/python'),['-u','tests/serve_note_library.py'],{cwd:root,stdio:['ignore','pipe','pipe']});
  let logs='',browser;server.stderr.on('data',data=>logs+=data);
  try{
    const base=await new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>reject(new Error(`Fixture timeout: ${logs}`)),15000);
      server.stdout.on('data',data=>{const match=data.toString().match(/READY (http:\/\/\S+)/);if(match){clearTimeout(timer);resolve(match[1]);}});
      server.on('exit',code=>{clearTimeout(timer);reject(new Error(`Fixture exited ${code}: ${logs}`));});
    });
    browser=await chromium.launch({headless:true,channel:process.env.BROWSER_CHANNEL || 'chrome'});
    const context=await browser.newContext({viewport:{width:1440,height:960},reducedMotion:'reduce'});
    await context.route('https://**',route=>route.abort());
    const page=await context.newPage(),errors=[];
    page.on('pageerror',error=>errors.push(error.message));page.on('dialog',dialog=>dialog.accept());
    await page.goto(base);await page.locator('#open-personal-library').click();
    await page.waitForFunction(()=>window.SeaNotebook && window.ICS?.render);
    await page.evaluate(()=>SeaNotebook.open('course:one'));
    const width=selector=>page.locator(selector).evaluate(node=>Math.round(node.getBoundingClientRect().width));
    const noOverflow=()=>page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1);
    assert.ok(await noOverflow());
    const navWidth=await width('#app-sidebar');
    await page.locator('.panel-resizer-nav').focus();await page.keyboard.press('ArrowRight');
    assert.equal(await width('#app-sidebar'),navWidth+8);
    const handle=await page.locator('.panel-resizer-notes').boundingBox();
    await page.mouse.move(handle.x+handle.width/2,handle.y+120);await page.mouse.down();await page.mouse.move(handle.x+handle.width/2+30,handle.y+120);await page.mouse.up();
    const listWidth=await width('#notebook-index');assert.equal(listWidth,294);
    await page.locator('.panel-resizer-outline').focus();await page.keyboard.press('ArrowLeft');
    assert.equal(await width('#note-outline'),216);
    await page.locator('#notes-panel-toggle').click();await page.locator('#outline-panel-toggle').click();await page.locator('#nav-panel-toggle').click();
    assert.equal(await page.locator('#notebook-index').isVisible(),false);
    assert.equal(await page.locator('#note-outline').isVisible(),false);
    assert.equal(await page.locator('#app-sidebar').isVisible(),false);
    assert.equal(await page.locator('main').evaluate(node=>getComputedStyle(node).marginLeft),'0px');
    await page.reload();await page.locator('#note-editor:not(.hidden)').waitFor();
    assert.equal(await page.locator('#notes-panel-toggle').getAttribute('aria-expanded'),'false');
    assert.equal(await page.locator('#outline-panel-toggle').getAttribute('aria-expanded'),'false');
    await page.locator('#nav-panel-toggle').click();await page.locator('#notes-panel-toggle').click();await page.locator('#outline-panel-toggle').click();
    assert.equal(await width('#notebook-index'),listWidth);assert.equal(await width('#note-outline'),216);
    // Direct editing stays in the same article, saves the source revision, and supports undo.
    const original=await page.locator('#note-preview').innerText();
    await page.locator('#note-preview').dblclick();await page.locator('#note-body').waitFor();
    const body='# 研究札记\n\n勘误与档案。\n\n## 材料比较\n\n'+('较长的研究材料与说明。\n\n'.repeat(35))+'\n第三部分\n--------\n\n勘误的下一处。\n\n```md\n# 代码内标题\n```';
    await page.locator('#note-title').fill('课程手动修订');await page.locator('#note-body').fill(body);
    await page.waitForFunction(()=>document.querySelector('#note-save-state').textContent==='已保存');
    await page.waitForFunction(()=>document.querySelectorAll('.note-outline-link').length===3);
    await page.locator('#note-undo').click();
    assert.ok(!(await page.locator('#note-body').inputValue()).includes('勘误'));
    await page.locator('#note-redo').click();assert.equal(await page.locator('#note-body').inputValue(),body);
    await page.locator('#note-body').focus();await page.keyboard.press('Control+z');assert.ok(!(await page.locator('#note-body').inputValue()).includes('勘误'));
    await page.keyboard.press('Control+Shift+z');assert.equal(await page.locator('#note-body').inputValue(),body);
    await page.evaluate(()=>SeaNotebook.flush());
    const edited=await (await page.request.get(`${base}/api/local/note-library/course/one`)).json();
    assert.equal(edited.title,'课程手动修订');assert.equal(edited.body,body);
    assert.ok(edited.versions.some(row=>row.summary.includes('课程当前正文')));
    // Editor navigation selects the corresponding Markdown source heading.
    await page.getByRole('button',{name:'第三部分',exact:true}).click();
    assert.equal(await page.locator('#note-body').evaluate(node=>node.selectionStart),body.indexOf('第三部分'));
    await page.locator('#note-mode').click();
    await page.getByRole('button',{name:'第三部分',exact:true}).click();
    assert.ok(await page.locator('#note-preview h2').last().evaluate(node=>{const y=node.getBoundingClientRect().top;return y>=49 && y<innerHeight-60;}));
    await page.keyboard.press('Control+f');await page.locator('#note-find').fill('勘误');
    assert.equal(await page.locator('#note-preview mark.note-search-hit').count(),2);
    assert.equal(await page.locator('#note-find-count').innerText(),'1 / 2');
    await page.locator('#note-find').press('Enter');assert.equal(await page.locator('#note-find-count').innerText(),'2 / 2');
    await page.locator('#note-find').fill('[.*');assert.equal(await page.locator('#note-find-count').innerText(),'0 处');
    await page.locator('#note-find-close').click();
    await page.locator('#note-search').fill('勘误');
    await page.waitForFunction(()=>document.querySelectorAll('.note-list-row').length===1);
    assert.ok(await page.locator('.note-search-excerpt mark').count()>0);
    await page.locator('[data-note-id="course:one"]').click();await page.waitForFunction(()=>document.querySelector('#note-find').value==='勘误');
    await page.locator('#note-search').fill('');await page.locator('#note-find-close').click();
    // A source conflict keeps the editor draft, and the fallback saves every word.
    await page.locator('#note-mode').click();await page.locator('#note-body').fill('# 本窗口草稿\n\n尚未保存的内容。');
    const external=await page.request.put(`${base}/api/local/note-library/course/one/content`,{data:{title:edited.title,body:'另一窗口已保存',revision:edited.edit_revision}});
    assert.equal(external.status(),200);
    await page.getByRole('button',{name:'保留为新笔记'}).click();
    await page.waitForFunction(()=>document.querySelector('#message').textContent.includes('已保存为新笔记'));
    assert.match(await page.locator('#note-body').inputValue(),/本窗口草稿/);
    assert.equal((await (await page.request.get(`${base}/api/local/note-library/course/one`)).json()).body,'另一窗口已保存');
    // Generated and manual history can both be restored in place.
    await page.evaluate(()=>SeaNotebook.open('course:one'));
    await page.locator('#note-more').click();await page.getByRole('menuitem',{name:'历史版本',exact:true}).click();
    await page.locator('.note-history-row').filter({hasText:'fixture/current'}).getByRole('button',{name:'恢复此版本'}).click();
    await page.waitForFunction(()=>document.querySelector('#note-body').value.includes('课程当前正文'));
    // Source notes in another category share the same editing path.
    const talk=(await (await page.request.get(`${base}/api/local/note-library?kind=talk`)).json()).notes[0];
    await page.evaluate(key=>SeaNotebook.open(key),talk.id);await page.locator('#note-mode').click();
    await page.locator('#note-body').fill('# 讲座修订\n\n独立保存的讲座笔记。');await page.evaluate(()=>SeaNotebook.flush());
    await page.reload();await page.locator('#note-preview h1').waitFor();
    assert.match(await page.locator('#note-preview').innerText(),/独立保存/);
    await page.screenshot({path:'/tmp/sea-workspace-notes-desktop.png',fullPage:true});
    await page.locator('#theme-toggle').click();await page.screenshot({path:'/tmp/sea-workspace-notes-dark.png',fullPage:true});
    for(const w of [1024,820,390]){await page.setViewportSize({width:w,height:844});assert.ok(await noOverflow(),`overflow at ${w}`);}
    assert.equal(await page.locator('#note-outline').isVisible(),false);
    await page.locator('#outline-panel-toggle').click();assert.equal(await page.locator('#note-outline').isVisible(),true);
    await page.getByRole('button',{name:'讲座修订',exact:true}).click();assert.equal(await page.locator('#note-outline').isVisible(),false);
    await page.screenshot({path:'/tmp/sea-workspace-notes-mobile.png',fullPage:true});
    await page.locator('#note-index-back').click();assert.equal(await page.locator('#notebook-index').isVisible(),true);
    await page.setViewportSize({width:1440,height:960});
    await page.locator('.desktop-nav [data-view="courses"]').click();
    await page.locator('#course-sidebar-open').click();assert.equal(await page.locator('#course-sidebar').isVisible(),false);
    await page.locator('#course-sidebar-open').click();assert.equal(await page.locator('#course-sidebar').isVisible(),true);
    await page.locator('.desktop-nav [data-view="settings"]').click();await page.locator('#model-button').click();
    await page.locator('.provider-panel-toggle').click();assert.equal(await page.locator('#provider-sidebar').isVisible(),false);
    await page.locator('.provider-panel-toggle').click();assert.equal(await page.locator('#provider-sidebar').isVisible(),true);
    assert.deepEqual(errors,[]);
    console.log('PASS workspace notes: independent panels, resize/reload, source edits, undo, source conflict, history, full-text hits, outline, dark and mobile');
  }finally{if(browser)await browser.close();server.kill('SIGTERM');}
})().catch(error=>{console.error(error);process.exitCode=1;});
