/* Real local API + synthetic cloud/models, isolated from personal data. */
const assert = require('node:assert/strict');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');

(async () => {
  const server = spawn(path.join(root,'.venv-course/bin/python'), ['-u','tests/serve_note_jobs.py'], {cwd:root,stdio:['ignore','pipe','pipe']});
  let logs = '';
  server.stderr.on('data', data => { logs += data; });
  const base = await new Promise((resolve,reject) => {
    const timer = setTimeout(()=>reject(new Error(`fixture startup timeout: ${logs}`)),15000);
    server.stdout.on('data', data => {
      const match = data.toString().match(/READY (http:\/\/\S+)/);
      if (match) { clearTimeout(timer); resolve(match[1]); }
    });
    server.on('exit', code => {clearTimeout(timer);reject(new Error(`fixture exit ${code}: ${logs}`));});
  });
  const browser = await chromium.launch({headless:true,channel:process.env.BROWSER_CHANNEL || 'chrome'});
  const page = await browser.newPage({viewport:{width:1320,height:900}});
  const errors = [];
  page.on('pageerror', error=>errors.push(error.message));
  await page.route('https://**',route=>route.abort());
  try {
    await page.goto(base);
    await page.locator('.course-open').first().click();
    await page.locator('.lecture-open').first().click();
    await page.locator('#detail-blind-compare').click();
    await page.getByLabel('fixture / alpha',{exact:true}).check();
    await page.getByLabel('fixture / beta',{exact:true}).check();
    await page.locator('#blind-generate-button').click();
    await page.waitForFunction(()=>document.querySelector('#blind-job-list').textContent.includes('生成中'));
    assert.equal(await page.locator('.blind-panel').count(),0);
    await page.getByRole('button',{name:'重试此模型',exact:true}).waitFor();
    await page.waitForFunction(()=>document.querySelectorAll('.blind-panel').length===2);
    assert.equal(await page.locator('.blind-panel .available').count(),0);
    await page.getByRole('button',{name:'重试此模型',exact:true}).click();
    await page.waitForFunction(()=>document.querySelectorAll('.blind-panel').length===3);
    await page.locator('#blind-history summary').click();
    await page.locator('#blind-history-list input').first().uncheck();
    assert.equal(await page.locator('.blind-panel').count(),2);
    await page.locator('#blind-refresh-versions').click();
    await page.waitForFunction(()=>document.querySelectorAll('.blind-panel').length===2);
    assert.equal(await page.locator('.blind-panel').count(),2);
    await page.screenshot({path:'/tmp/sea-blind-models.png',fullPage:true});

    await page.locator('.desktop-nav [data-view="talks"]').click();
    await page.locator('#talk-upload-button').click();
    await page.locator('#talk-upload-file').setInputFiles(['第一场','第二场','第三场'].map(name=>({name:`${name}.mp3`,mimeType:'audio/mpeg',buffer:Buffer.from('ID3synthetic audio')})));
    await page.locator('#talk-upload-save').click();
    assert.equal(await page.locator('#talk-upload').evaluate(node=>node.open),false);
    await page.locator('.desktop-nav [data-view="settings"]').click();
    await page.waitForFunction(()=>window.SeaTalkUploads && document.querySelectorAll('.talk-intake-card').length===0);
    await page.locator('.desktop-nav [data-view="talks"]').click();
    await page.waitForFunction(()=>document.querySelectorAll('#talk-list .lecture-card').length===3);
    await page.waitForFunction(()=>document.querySelector('#talk-list').textContent.includes('排队中'));
    await page.waitForFunction(()=>[...document.querySelectorAll('#talk-list .state-badge')].filter(node=>node.textContent==='转写中').length===2);
    const queued = page.locator('#talk-list .lecture-card').filter({hasText:'排队中'}).first();
    await queued.getByRole('button',{name:'暂停',exact:true}).click();
    await page.getByRole('button',{name:'继续',exact:true}).waitFor();
    await page.reload();
    await page.locator('.desktop-nav [data-view="talks"]').click();
    await page.getByRole('button',{name:'继续',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#talk-list').textContent.includes('排队中'));
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await page.screenshot({path:'/tmp/sea-recording-queue-mobile.png',fullPage:true});
    assert.deepEqual(errors,[]);
    console.log('Note jobs: model selection, anonymous results, isolated retry, historical selection, batch recordings, page navigation, pause/resume, reload and mobile passed');
  } catch (error) {
    console.error(JSON.stringify({errors, blind: await page.evaluate(()=>({jobs:blindCompare?.jobs, included:[...(blindCompare?.includedKeys || [])], order:blindCompare?.order}))}));
    await page.screenshot({path:'/tmp/sea-note-jobs-failure.png',fullPage:true});
    throw error;
  } finally {
    await browser.close();
    server.kill('SIGTERM');
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
