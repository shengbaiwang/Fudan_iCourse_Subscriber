/* Course folders organize complete courses, including during filtered searches. */
const assert = require('node:assert/strict');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');

(async()=>{
  const server=spawn(path.join(root,'.venv-course/bin/python'),['-u','tests/serve_note_library.py'],{
    cwd:root,env:{...process.env,SEA_TEST_COURSE_GROUPS:'1'},stdio:['ignore','pipe','pipe'],
  });
  let logs='',browser;
  server.stderr.on('data',data=>logs+=data);
  try {
    const base=await new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>reject(new Error(`Fixture timeout: ${logs}`)),15000);
      server.stdout.on('data',data=>{const match=data.toString().match(/READY (http:\/\/\S+)/);if(match){clearTimeout(timer);resolve(match[1]);}});
      server.on('exit',code=>{clearTimeout(timer);reject(new Error(`Fixture exited ${code}: ${logs}`));});
    });
    browser=await chromium.launch({headless:true,channel:process.env.BROWSER_CHANNEL || 'chrome'});
    const context=await browser.newContext({viewport:{width:1440,height:960},reducedMotion:'reduce'});
    await context.route('https://**',route=>route.abort());
    const page=await context.newPage(),errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    const course=page.locator('.note-course-row[data-course-id="history"]');
    const branch=page.locator('[data-tree-course-id="course-group:history"]');
    const locations=async()=>{
      const data=await (await page.request.get(`${base}/api/local/note-library?kind=course`)).json();
      return Object.fromEntries(data.notes.map(row=>[row.source_id,row.folder_id]));
    };
    const waitFolder=target=>page.waitForFunction(target=>document.querySelector('.note-course-row[data-course-id="history"]')?.closest('[data-tree-folder-id]')?.dataset.treeFolderId===target,target);
    const undo=async()=>{await page.locator('#message').getByRole('button',{name:'撤销',exact:true}).click();};
    await page.goto(base);
    await page.locator('#open-personal-library').click();
    await course.waitFor();
    assert.equal(await page.locator('.note-course-row').count(),2);
    assert.equal(await branch.getAttribute('aria-expanded'),'false');
    await course.click();
    await page.locator('[data-note-id="course:two"]').waitFor();
    assert.equal(await branch.locator('.note-list-row').count(),2);
    assert.equal(await branch.locator('.note-course-count').innerText(),'2 篇');
    assert.equal(await page.locator('[data-note-id="course:two"]').getAttribute('draggable'),null);

    await page.locator('#note-folder-new').click();
    await page.locator('#note-folder-name').fill('历史');
    await page.locator('#note-folder-name').press('Enter');
    const folder=page.locator('.note-folder-row').filter({hasText:'历史'});
    await folder.waitFor();
    const folderId=await folder.getAttribute('data-folder-id');
    await course.dragTo(folder);
    await waitFolder(folderId);
    assert.deepEqual(await locations(),{one:folderId,two:folderId,'other-one':''});
    assert.equal(await page.locator('.note-course-row[data-course-id="history"]').count(),1);
    assert.equal(await folder.locator('.note-folder-count').innerText(),'1');

    await page.locator('[data-note-id="course:two"]').click();
    await page.waitForFunction(()=>document.querySelector('#note-folder-path').textContent.includes('历史 / 近代中国史'));
    await page.locator('#note-more').click();
    await page.getByRole('menuitem',{name:'移动整门课程…',exact:true}).click();
    await page.getByRole('menuitem',{name:'收件箱',exact:true}).click();
    await waitFolder('');
    assert.deepEqual(await locations(),{one:'',two:'','other-one':''});
    await undo();await waitFolder(folderId);
    assert.deepEqual(await locations(),{one:folderId,two:folderId,'other-one':''});
    await page.waitForFunction(()=>document.querySelector('#note-folder-path').textContent.includes('历史 / 近代中国史'));

    // Search only matches one lecture; moving its course must still move both.
    await page.locator('#note-search').fill('第二课次关键词');
    await page.waitForFunction(()=>document.querySelectorAll('.note-list-row').length===1);
    assert.equal(await branch.locator('.note-course-count').innerText(),'1 篇');
    await course.click({button:'right'});
    await page.getByRole('menuitem',{name:'收件箱',exact:true}).click();
    await waitFolder('');
    assert.deepEqual(await locations(),{one:'',two:'','other-one':''});
    await undo();await waitFolder(folderId);
    await page.locator('#note-search').fill('');
    await page.waitForFunction(()=>document.querySelectorAll('.note-list-row').length===5);

    await branch.focus();await page.keyboard.press('ArrowLeft');
    assert.equal(await branch.getAttribute('aria-expanded'),'false');
    await page.keyboard.press('ArrowRight');
    assert.equal(await branch.getAttribute('aria-expanded'),'true');
    await page.keyboard.press('Shift+F10');
    await page.getByRole('menuitem',{name:'收件箱',exact:true}).waitFor();
    await page.keyboard.press('Escape');
    await course.dispatchEvent('pointerdown',{pointerType:'touch',clientX:20,clientY:20});
    await page.getByRole('menuitem',{name:'收件箱',exact:true}).waitFor();
    await course.dispatchEvent('pointerup',{pointerType:'touch'});
    await page.keyboard.press('Escape');
    await page.reload();
    await page.waitForFunction(()=>document.querySelector('#note-folder-path').textContent.includes('历史 / 近代中国史'));
    assert.equal(await branch.locator('.note-list-row').count(),2);
    assert.deepEqual(await locations(),{one:folderId,two:folderId,'other-one':''});

    await folder.click({button:'right'});
    await page.getByRole('menuitem',{name:'删除文件夹',exact:true}).click();
    await waitFolder('');
    assert.deepEqual(await locations(),{one:'',two:'','other-one':''});
    await undo();await waitFolder(folderId);
    assert.deepEqual(await locations(),{one:folderId,two:folderId,'other-one':''});
    await page.evaluate(()=>document.documentElement.setAttribute('data-theme','dark'));
    await page.screenshot({path:'/tmp/sea-course-folders-desktop.png',fullPage:true});
    await page.setViewportSize({width:390,height:844});
    await page.locator('#note-index-back').click();
    await course.waitFor();
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true);
    await page.screenshot({path:'/tmp/sea-course-folders-mobile.png',fullPage:true});
    assert.deepEqual(errors,[]);
    console.log('PASS course folders: whole-course drag, menus, filtered moves, undo, reload, keyboard and touch');
  } finally {
    if(browser)await browser.close();
    server.kill('SIGTERM');
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
