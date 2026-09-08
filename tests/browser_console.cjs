/* Synthetic integration test. Set PLAYWRIGHT_MODULE and SQLJS_DIR to installed dependencies.
   Example: SQLJS_DIR=/tmp/icourse-console-test-assets node tests/browser_console.cjs
   No request reaches GitHub and all workflow dispatches are intercepted. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const vm = require('node:vm');
const crypto = require('node:crypto');
const zlib = require('node:zlib');
const {execFileSync} = require('node:child_process');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const sqlDir = process.env.SQLJS_DIR;
if (!sqlDir) throw new Error('Set SQLJS_DIR to a directory containing sql-wasm.js and sql-wasm.wasm (sql.js 1.12.0)');

(async () => {
  execFileSync('python3', ['scripts/build_frontend.py'], {cwd: root});
  const SQL = await require(path.join(sqlDir, 'sql-wasm.js'))({locateFile: name => path.join(sqlDir, name)});
  const schemaContext = {window: {}};
  vm.runInNewContext(fs.readFileSync(path.join(root, 'local_web/static/browser/schema.js'), 'utf8'), schemaContext);
  const database = new SQL.Database();
  database.exec(schemaContext.window.ICS.schema.SCHEMA_SQL);
  database.run("INSERT INTO courses VALUES ('1', '现代思想史', '陈老师'), ('2', '科学与社会', '李老师')");
  database.run("INSERT INTO all_courses(course_id,term,title,teacher,dept) VALUES ('1','2026-秋','现代思想史','陈老师','历史系'), ('2','2026-秋','科学与社会','李老师','社会学院')");
  database.run("INSERT INTO meta VALUES ('subscribed_course_ids','1')");
  database.run("INSERT INTO lectures(sub_id,course_id,sub_title,summary,transcript,processed_at,summary_model) VALUES ('10','1','2026-03-09第11-12节', '# 导论\n\n**理论**与实践。<script>window.pwned=1</script>','专属转录关键词','2026-09-08','test/model-a'), ('11','1','2026-03-09第6-8节','第二篇笔记','第二份转录','2026-09-07','test/model-b')");
  database.run("INSERT INTO lectures(sub_id,course_id,sub_title,error_stage) VALUES ('12','1','2026-03-10第1-2节','no_video')");
  database.run("INSERT INTO summary_versions VALUES ('10','test/model-a','# 导论\n\n理论与实践','2026-09-08'), ('10','test/model-b','# 另一个版本\n\n观点比较','2026-09-07'), ('10','test/model-a','# 历史版本\n\n第一次输出','2026-09-06')");
  database.run("INSERT INTO ppt_pages(sub_id,page_num,created_sec,text,ocr_status) VALUES ('10',1,62,'专属 OCR 关键词','done')");
  const bytes = Buffer.from(database.export());
  const password = crypto.createHash('sha256').update('ICSv2:student:password').digest('hex');
  const encrypt = input => {
    const salt = Buffer.from('testSalt');
    const key = crypto.pbkdf2Sync(password, salt, 100000, 48, 'sha256');
    const cipher = crypto.createCipheriv('aes-256-cbc', key.subarray(0, 32), key.subarray(32));
    return Buffer.concat([Buffer.from('Salted__'), salt, cipher.update(input), cipher.final()]);
  };
  const blobs = {index: encrypt(Buffer.from(JSON.stringify({shards: [{name:'test.db.gz.enc'}]}))), shard: encrypt(zlib.gzipSync(bytes)), legacy: encrypt(zlib.gzipSync(bytes))};
  const provider = {name:'test',base_url:'https://example.test/v1',api_key_env:'LLM_TEST_API_KEY',models:['model-a','model-b'],enabled:true,api_key_configured:true};
  const rows = sql => { const result=database.exec(sql)[0]; return result ? result.values.map(values=>Object.fromEntries(result.columns.map((name,i)=>[name,values[i]]))) : []; };
  const courses = rows("SELECT c.*,COUNT(l.sub_id) total_count,SUM(l.summary IS NOT NULL) summary_count FROM courses c LEFT JOIN lectures l USING(course_id) GROUP BY c.course_id");
  const lectures = rows("SELECT *,summary IS NOT NULL has_summary,transcript IS NOT NULL transcript_available FROM lectures WHERE course_id='1'");
  const lecture = {...rows("SELECT l.*,c.title course_title,c.teacher FROM lectures l JOIN courses c USING(course_id) WHERE sub_id='10'")[0],summary_versions:rows("SELECT * FROM summary_versions"),ppt_pages:rows("SELECT * FROM ppt_pages")};
  let zones = {}, names = {}, version = 'commit-1', missingShard = false, legacy = false;
  let dispatches = [];
  const server = http.createServer((req,res) => {
    const pathname = new URL(req.url, 'http://localhost').pathname;
    const relative = pathname.replace(/^\/(fork|local)\//, '') || 'index.html';
    const base = pathname.startsWith('/fork/') ? 'dist/frontend' : 'local_web/static';
    const file = path.join(root, base, relative.endsWith('/') ? relative + 'index.html' : relative);
    if (!file.startsWith(path.join(root,base)) || !fs.existsSync(file)) {res.writeHead(404);res.end();return;}
    const ext=path.extname(file);
    res.setHeader('Content-Type', {'.html':'text/html','.js':'text/javascript','.css':'text/css','.json':'application/json'}[ext] || 'application/octet-stream');
    res.end(fs.readFileSync(file));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  let browser;
  try {
    browser = await chromium.launch({headless:true, channel: process.env.BROWSER_CHANNEL || 'chrome'});
    const context = await browser.newContext({viewport: {width:1280,height:900}, colorScheme: 'dark'});
    await context.route('**/*', async route => {
      const request=route.request(), url=new URL(request.url());
      const json = data => route.fulfill({json:data});
      if (url.hostname==='cdnjs.cloudflare.com') {
        const name = path.basename(url.pathname);
        if (['sql-wasm.js','sql-wasm.wasm'].includes(name)) return route.fulfill({path:path.join(sqlDir,name), contentType:name.endsWith('.wasm')?'application/wasm':'text/javascript'});
      }
      if (url.hostname==='api.github.com') {
        const p=url.pathname.replace('/repos/alice/fork','');
        if (!p) return json({full_name:'alice/fork'});
        if (p.startsWith('/git/ref/')) return json({object:{sha:version}});
        if (p.startsWith('/git/commits/')) return json({tree:{sha:'tree'}});
        if (p.startsWith('/git/trees/')) return json({tree:legacy ? [{type:'blob',path:'data/icourse.db.gz.enc',sha:'legacy'}] : [
          {type:'blob',path:'data/icourse-index.enc',sha:'index'},
          ...(!missingShard ? [{type:'blob',path:'data/shards/test.db.gz.enc',sha:'shard'}] : []),
        ]});
        if (p.startsWith('/git/blobs/')) return route.fulfill({body:blobs[p.split('/').at(-1)],contentType:'application/octet-stream'});
        if (p==='/actions/runs') return json({workflow_runs:[]});
        if (p==='/actions/variables/MODEL_PROVIDERS_JSON') return json({value:JSON.stringify({version:1,providers:[provider]})});
        if (p==='/actions/secrets') return json({secrets:[{name:'LLM_TEST_API_KEY'}]});
        if (p.endsWith('/dispatches')) {dispatches.push({path:p,body:request.postDataJSON()});return route.fulfill({status:204});}
        throw new Error(`Unexpected GitHub request: ${request.method()} ${p}`);
      }
      if (url.origin===origin && url.pathname.startsWith('/api/local/')) {
        const p=url.pathname.slice('/api/local'.length);
        if (p==='/status') return json({configured:true,keychain_available:false,database_ready:true,repository:{owner:'alice',repo:'fork',branch:'data'},database:{courses:2,lectures:3,ready:2,failed:0,commit_sha:'fixture'},update:{state:'current'}});
        if (p==='/courses') return json(courses);
        if (p==='/lecture-names') {if(request.method()==='PUT') {const body=request.postDataJSON();names[body.sub_id]=body.name;} return json({names});}
        if (p==='/course-zones') {if(request.method()==='PUT') {const body=request.postDataJSON();zones[body.course_id]=body.zone;} return json({zones});}
        if (p==='/workflows') return json([]);
        if (p==='/courses/1/lectures') return json(lectures);
        if (p==='/lectures/10') return json(lecture);
        if (p==='/model-providers') return json({source:'github-variable',providers:[provider]});
        if (p==='/search') return json({total:1,page:1,has_more:false,results:[{sub_id:'10',course_id:'1',course_title:'现代思想史',sub_title:lecture.sub_title,hit_field:'ocr',snippet:'专属 OCR 关键词'}]});
        if (p==='/subscriptions') return json({course_ids:['1'],courses:[courses[0]]});
        if (p==='/subscription-catalog') return json({terms:['2026-秋'],courses});
        if (p.endsWith('/dispatch')) {dispatches.push({path:p,body:request.postDataJSON()});return json({ok:true});}
        throw new Error(`Unexpected local request: ${p}`);
      }
      if (url.origin===origin) return route.continue();
      // Keep the test deterministic/offline, including progressive rich Markdown.
      return route.abort();
    });
    for (const mode of ['local','fork']) {
      const page=await context.newPage();
      const errors=[];
      page.on('pageerror', error => errors.push(error.message));
      page.on('dialog', dialog=>dialog.accept());
      await page.goto(`${origin}/${mode}/`);
      if(mode==='fork') {
        await page.locator('#setup:not(.hidden)').waitFor();
        for(const [name,value] of Object.entries({owner:'alice',repo:'fork',token:'synthetic-token',stuid:'student',uispsw:'password'})) await page.locator(`[name="${name}"]`).fill(value);
        await page.locator('#setup-form button[type="submit"]').click();
      }
      await page.locator('.course-card').first().waitFor();
      assert.equal(await page.locator('.course-card').count(),2);
      assert.equal(await page.evaluate(()=>getComputedStyle(document.documentElement).colorScheme),'dark');
      await page.locator('.course-card').filter({hasText:'现代思想史'}).locator('select').selectOption('study');
      await page.locator('[data-course-zone="study"]').click();
      await page.waitForFunction(()=>document.querySelectorAll('.course-card').length===1);
      assert.equal(await page.locator('.course-card').count(),1);
      await page.locator('.course-open').click();
      await page.locator('.lecture-open').first().waitFor();
      assert.match(await page.locator('.lecture-open').first().innerText(), /第6-8节/);
      assert.equal(await page.getByText('暂无录播',{exact:true}).count(),1);
      await page.locator('.lecture-open').filter({hasText:'第11-12节'}).click();
      await page.locator('.summary-version-choice').first().waitFor();
      // Three stored reruns plus the distinct active summary must all remain selectable.
      assert.equal(await page.locator('.summary-version-choice').count(),4);
      await page.locator('.summary-version-choice').last().locator('input').check();
      assert.equal(await page.locator('.summary-version-panel').count(),2);
      assert.equal(await page.evaluate(()=>window.pwned),undefined);
      await page.locator('#theme-toggle').click();
      assert.equal(await page.evaluate(()=>getComputedStyle(document.documentElement).colorScheme),'light');
      await page.locator('#theme-toggle').click();
      const markdown = await page.evaluate(()=>renderMarkdown('| A | B |\n| --- | --- |\n| x | y |\n\n$P_n$ <img src=x onerror=alert(1)>'));
      assert.match(markdown, /<table>/);
      assert.match(markdown, /P_n/);
      assert.ok(!markdown.includes('<img'));
      await page.evaluate(async()=>{
        await api('/api/local/lecture-names',{method:'PUT',body:JSON.stringify({sub_id:'10',name:'自定义名称'})});
        await loadLectureNames(); await openLecture('10');
      });
      assert.equal(await page.locator('#detail-title').innerText(),'自定义名称');
      await page.locator('#detail-data-actions').click();
      await page.locator('#data-actions-dialog').waitFor();
      assert.equal(await page.locator('#data-actions-all').isChecked(),false);
      await page.locator('#data-export-button').click();
      await page.locator('#data-actions-dialog').waitFor({state:'hidden'});
      assert.equal(dispatches.at(-1).body.inputs.sub_ids,'10');
      assert.equal(dispatches.at(-1).body.inputs.export_type,'PDF');
      await page.locator('.desktop-nav [data-view="search"]').click();
      for (const domain of ['title', 'summary', 'transcript']) await page.locator(`[data-domain="${domain}"]`).click();
      await page.locator('#search').fill('关键词');
      await page.locator('.search-card').waitFor();
      assert.match(await page.locator('.search-card').innerText(),/专属 OCR/);
      await page.locator('.search-card').click();
      await page.locator('#detail-content .ppt').waitFor();
      assert.match(await page.locator('#detail-content').innerText(),/专属 OCR/);
      await page.setViewportSize({width:390,height:844});
      await page.locator('#mobile-nav [data-view="courses"]').click();
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
      await page.screenshot({path:`/tmp/icourse-${mode}-mobile.png`,fullPage:true});
      await page.setViewportSize({width:1280,height:900});
      await page.screenshot({path:`/tmp/icourse-${mode}-desktop.png`,fullPage:true});
      if(mode==='fork') {
        assert.equal(await page.locator('#obsidian-button').isVisible(),false);
        assert.equal(await page.evaluate(()=>localStorage.getItem('ics_creds')),null);
        const unchanged=await page.evaluate(()=>window.ICOURSE_API('/api/local/sync',{method:'POST'}));
        assert.equal(unchanged.unchanged,true);
        version='commit-2';missingShard=true;
        const failed=await page.evaluate(async()=>{try{await window.ICOURSE_API('/api/local/sync',{method:'POST'});return '';}catch(e){return e.message;}});
        assert.match(failed,/缺少分片/);
        assert.equal((await page.evaluate(()=>window.ICOURSE_API('/api/local/courses'))).length,2);
        const filtered = await page.evaluate(()=>window.ICOURSE_API('/api/local/search?q=理论%20实践&domains=summary&course_id=1&page_size=1'));
        assert.equal(filtered.total,1);
        assert.equal(filtered.results[0].sub_id,'10');
        assert.equal(filtered.has_more,false);
        const missed = await page.evaluate(()=>window.ICOURSE_API('/api/local/search?q=理论%20缺失&domains=summary'));
        assert.equal(missed.total,0);
        missingShard=false;legacy=true;version='commit-3';
        await page.evaluate(()=>window.ICOURSE_API('/api/local/sync',{method:'POST'}));
        assert.equal((await page.evaluate(()=>window.ICOURSE_API('/api/local/lectures/10'))).summary_versions.length,3);
        const badLogin=await page.evaluate(async()=>{try{await window.ICOURSE_API('/api/local/configure',{method:'POST',body:JSON.stringify({owner:'alice',repo:'fork',token:'synthetic-token',stuid:'student',uispsw:'wrong'})});return '';}catch(e){return e.message;}});
        assert.ok(badLogin);
        assert.equal((await page.evaluate(()=>window.ICOURSE_API('/api/local/status'))).database_ready,true);
        await page.evaluate(()=>window.ICOURSE_API('/api/local/summary-reruns',{method:'POST',body:JSON.stringify({course_ids:['1'],provider:'test',model:'model-a'})}));
        assert.equal(dispatches.at(-1).body.inputs.resummarize_sub_ids,'11,10');
        await page.locator('.desktop-nav [data-view="settings"]').click();
        await page.locator('#forget-credentials-button').click();
        await page.locator('#setup:not(.hidden)').waitFor();
        assert.equal(await page.locator('#detail-content').innerText(),'');
      }
      assert.deepEqual(errors,[]);
      await page.close();
      console.log(`${mode}: shared UI, zones, ordering, versions, export, search, mobile layout passed`);
    }
    console.log('Pages: encrypted shards, legacy decryption, rollback, rerun, logout passed');
  } finally {
    if(browser) await browser.close();
    await new Promise(resolve=>server.close(resolve));
    database.close();
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
