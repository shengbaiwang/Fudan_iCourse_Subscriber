/* Offline model-management interaction test. Uses synthetic API responses only.
   PLAYWRIGHT_MODULE=/path/to/playwright node tests/browser_models.cjs */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(process.env.MODEL_STATIC_ROOT || path.join(__dirname, '../local_web/static'));
(async () => {
  const server = http.createServer((req, res) => {
    const file = path.join(root, new URL(req.url, 'http://localhost').pathname === '/' ? 'index.html' : new URL(req.url, 'http://localhost').pathname);
    if (!file.startsWith(root + '/') || !fs.existsSync(file)) { res.writeHead(404); res.end(); return; }
    res.setHeader('Content-Type', {'.html':'text/html', '.js':'text/javascript', '.css':'text/css'}[path.extname(file)] || 'text/plain');
    res.end(fs.readFileSync(file));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  let browser;
  try {
    browser = await chromium.launch({headless:true, channel:process.env.BROWSER_CHANNEL || 'chrome'});
    const page = await browser.newPage({viewport:{width:1280,height:1000}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('dialog', dialog => dialog.accept());
    let saved, failSave = false, failDirectory = true;
    let providers = [
      {name:'alpha',base_url:'https://alpha.example/v1',api_key_env:'LLM_ALPHA_API_KEY',models:['alpha-pro','alpha-fast'],enabled:true,api_key_configured:true},
      {name:'beta',base_url:'https://beta.example/v1',api_key_env:'LLM_BETA_API_KEY',models:['beta-chat'],enabled:false,api_key_configured:true},
    ];
    await page.route('**/*', async route => {
      const request=route.request(), url=new URL(request.url());
      const json = data => route.fulfill({json:data});
      if (url.origin !== origin) return route.abort();
      if (!url.pathname.startsWith('/api/local/')) return route.continue();
      switch (url.pathname.slice('/api/local'.length)) {
        case '/status': return json({configured:true,database_ready:true,repository:{owner:'test',repo:'fixture'},database:{courses:0,lectures:0,ready:0},update:{state:'current'}});
        case '/courses': case '/workflows': return json([]);
        case '/course-zones': return json({zones:{}});
        case '/lecture-names': return json({names:{}});
        case '/model-providers':
          if (request.method() === 'PUT') {
            if (failSave) return route.fulfill({status:502,json:{detail:'synthetic save failure'}});
            saved = request.postDataJSON();
            providers = saved.providers.map(({api_key, ...provider}) => ({...provider,api_key_configured:true}));
            return json({ok:true});
          }
          return json({source:'github-variable',providers});
        case '/model-providers/models':
          assert.equal(request.postDataJSON().base_url, 'https://api.xiaomimimo.com/v1');
          if (failDirectory) { failDirectory = false; return route.fulfill({status:400,json:{detail:'synthetic directory failure'}}); }
          return json({models:['alpha-pro','alpha-directory','other-model']});
        case '/model-providers/test':
          assert.equal(request.postDataJSON().base_url, 'https://api.xiaomimimo.com/v1');
          return json({model:request.postDataJSON().model,latency_ms:12});
        default: throw new Error(`Unexpected request ${url.pathname}`);
      }
    });
    await page.goto(origin);
    await page.locator('.desktop-nav [data-view="settings"]').click();
    await page.locator('#model-button').click();
    await page.locator('.provider-card').waitFor();
    assert.equal(await page.locator('.provider-card').count(),1);
    assert.equal(await page.locator('.provider-nav-item').count(),2);
    assert.equal(await page.locator('#model-save-button').isDisabled(),true);
    await page.getByLabel('Base URL', {exact:true}).fill('https://api.xiaomimimo.com');
    await page.locator('#model-provider-list').getByLabel('API Key', {exact:true}).fill('synthetic-key');
    assert.equal(await page.getByLabel('Base URL', {exact:true}).inputValue(), 'https://api.xiaomimimo.com/v1');
    await page.getByRole('button',{name:'获取模型',exact:true}).click();
    await page.getByText(/synthetic directory failure/).waitFor();
    await page.getByRole('button',{name:'重新获取',exact:true}).click();
    await page.getByRole('button',{name:'已添加目录模型 alpha-pro',exact:true}).waitFor();
    await page.getByLabel('搜索模型目录').fill('directory');
    await page.getByRole('button',{name:'添加目录模型 alpha-directory',exact:true}).click();
    assert.equal(await page.getByRole('button',{name:'已添加目录模型 alpha-directory',exact:true}).isDisabled(),true);
    await page.getByRole('button',{name:'完成',exact:true}).click();
    await page.locator('.model-directory-dialog').waitFor({state:'detached'});
    assert.equal(await page.locator('.provider-model-row').count(),3);
    await page.getByRole('button',{name:'移除模型 alpha-directory',exact:true}).click();
    await page.getByLabel('添加模型 ID').fill('alpha-new');
    await page.getByLabel('添加模型 ID').press('Enter');
    await page.getByLabel('添加模型 ID').fill('alpha-new');
    await page.getByLabel('添加模型 ID').press('Enter');
    assert.equal(await page.locator('.provider-model-row').count(),3);
    await page.getByLabel('搜索已添加的模型').fill('new');
    await page.getByRole('button',{name:'上移模型 alpha-new',exact:true}).click();
    await page.getByLabel('搜索已添加的模型').fill('');
    assert.deepEqual(await page.locator('.model-id').allTextContents(),['alpha-pro','alpha-new','alpha-fast']);
    await page.getByRole('button',{name:'移除模型 alpha-fast',exact:true}).click();
    await page.locator('.provider-nav-item').filter({hasText:'beta'}).click();
    await page.locator('.provider-nav-item').filter({hasText:'alpha'}).click();
    assert.equal(await page.locator('#model-provider-list').getByLabel('API Key',{exact:true}).inputValue(),'synthetic-key');
    await page.locator('#model-close-button').click();
    await page.locator('#model-button').click();
    assert.equal(await page.locator('#model-provider-list').getByLabel('API Key',{exact:true}).inputValue(),'synthetic-key');
    await page.getByRole('button',{name:'测试首个模型',exact:true}).click();
    await page.getByText('连接成功 · alpha-pro · 12 ms',{exact:true}).waitFor();
    await page.getByRole('button',{name:'↓ 下移',exact:true}).click();
    assert.match(await page.locator('.provider-nav-item.active').innerText(),/alpha/);
    await page.locator('#model-add-button').click();
    await page.getByLabel('供应商名称').fill('gamma');
    await page.getByLabel('Base URL').fill('https://gamma.example/v1');
    await page.getByLabel('添加模型 ID').fill('gamma-chat');
    await page.getByLabel('添加模型 ID').press('Enter');
    await page.getByLabel('搜索服务商').fill('alpha');
    assert.equal(await page.locator('.provider-nav-item').count(),1);
    await page.getByLabel('搜索服务商').fill('');
    await page.locator('.provider-nav-item').filter({hasText:'alpha'}).click();
    failSave = true;
    await page.locator('#model-save-button').click();
    await page.getByText('synthetic save failure',{exact:true}).waitFor();
    assert.equal(await page.locator('#model-provider-list').getByLabel('API Key',{exact:true}).inputValue(),'synthetic-key');
    failSave = false;
    await page.locator('#model-save-button').click();
    await page.waitForFunction(()=>document.querySelector('#model-save-button').disabled && !document.querySelector('.model-workspace').inert);
    assert.deepEqual(saved.providers.map(p=>p.name),['beta','alpha','gamma']);
    assert.deepEqual(saved.providers[1].models,['alpha-pro','alpha-new']);
    assert.equal(saved.providers[1].base_url,'https://api.xiaomimimo.com/v1');
    assert.equal(saved.providers[1].api_key,'synthetic-key');
    assert.equal(await page.locator('#model-provider-list').getByLabel('API Key',{exact:true}).inputValue(),'');
    await page.locator('#message').waitFor({state:'hidden'});
    for (const theme of ['light','dark']) {
      await page.evaluate(theme=>document.documentElement.dataset.theme=theme,theme);
      await page.screenshot({path:`/tmp/icourse-models-${theme}.png`,fullPage:true});
    }
    for (const width of [390,320]) {
      await page.setViewportSize({width,height:844});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
      await page.screenshot({path:`/tmp/icourse-models-${width}.png`,fullPage:true});
    }
    assert.deepEqual(errors,[]);
    console.log('Model management: drafts, search, add, duplicates, ordering, removal, test, failed/successful save, key clearing, themes and mobile passed.');
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve=>server.close(resolve));
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
