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
  const catalogInsert = database.prepare('INSERT INTO all_courses(course_id,term,title,teacher,dept) VALUES (?, ?, ?, ?, ?)');
  for (let i = 0; i < 207; i++) catalogInsert.run([
    `catalog-${String(i).padStart(3,'0')}`, i < 205 ? '2026-秋' : '2026-春', '目录课程', '目录教师', '目录学院',
  ]);
  catalogInsert.run(['field-id','2026-秋','另一课程','独特教师','独特学院']);
  catalogInsert.free();
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
  const rows = (sql, params = []) => {
    const statement = database.prepare(sql);
    try {
      statement.bind(params);
      const result = [];
      while (statement.step()) result.push(statement.getAsObject());
      return result;
    } finally { statement.free(); }
  };
  const courses = rows("SELECT c.*,COUNT(l.sub_id) total_count,SUM(l.summary IS NOT NULL) summary_count FROM courses c LEFT JOIN lectures l USING(course_id) GROUP BY c.course_id");
  const lectures = rows("SELECT *,summary IS NOT NULL has_summary,transcript IS NOT NULL transcript_available FROM lectures WHERE course_id='1'");
  const lecture = {...rows("SELECT l.*,c.title course_title,c.teacher FROM lectures l JOIN courses c USING(course_id) WHERE sub_id='10'")[0],summary_versions:rows("SELECT * FROM summary_versions"),ppt_pages:rows("SELECT * FROM ppt_pages")};
  let sections = [{id:'study', name:'学习区'}];
  let zones = {}, names = {}, version = 'commit-1', missingShard = false, legacy = false, liveShard = false;
  let localRevision = 0, localJobs = [], runRequests = [], preferences = {course_ids:['1'],lecture_order:'api'};
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
          {type:'blob',path:'data/icourse-index.enc',sha:liveShard?'indexLive':'index'},
          ...(!missingShard ? [{type:'blob',path:'data/shards/test.db.gz.enc',sha:liveShard?'shardLive':'shard'}] : []),
        ]});
        if (p.startsWith('/git/blobs/')) return route.fulfill({body:blobs[p.split('/').at(-1)],contentType:'application/octet-stream'});
        if (p==='/actions/runs') return json({workflow_runs:[]});
        if (p==='/actions/variables/MODEL_PROVIDERS_JSON') return json({value:JSON.stringify({version:1,providers:[provider]})});
        if (p==='/actions/variables/LECTURE_ORDER') return json({value:'api'});
        if (p==='/actions/secrets') return json({secrets:[{name:'LLM_TEST_API_KEY'}]});
        if (p.endsWith('/dispatches')) {dispatches.push({path:p,body:request.postDataJSON()});return route.fulfill({status:204});}
        throw new Error(`Unexpected GitHub request: ${request.method()} ${p}`);
      }
      if (url.origin===origin && url.pathname.startsWith('/api/local/')) {
        const p=url.pathname.slice('/api/local'.length);
        if (p==='/status') return json({configured:true,keychain_available:false,database_ready:true,repository:{owner:'alice',repo:'fork',branch:'data'},database:{courses:2,lectures:3,ready:2,failed:0,commit_sha:'fixture',revision:localRevision},update:{state:'current'}});
        if (p==='/sync') return json({unchanged:true});
        if (p==='/run-capabilities') return json({local:true,github:true,process_ready:true,summary_ready:true});
        if (p==='/run-preferences') {if(request.method()==='PUT') preferences=request.postDataJSON();return json(preferences);}
        if (p==='/runs') {
          if (request.method()==='POST') {
            const payload=request.postDataJSON();runRequests.push(payload);
            if(payload.target==='github') dispatches.push({path:p,body:{inputs:{course_ids:payload.course_ids.join(','),lecture_order:payload.lecture_order}}});
            else localJobs=[{id:'local-job',kind:payload.kind,status:'in_progress',total:2,completed:1,failed:0,current:'合成处理中',logs:['合成日志']}];
            return json({ok:true,target:payload.target,job:localJobs[0]});
          }
          return json({jobs:localJobs});
        }
        if(p==='/runs/local-job/cancel') {localJobs[0].status='cancelled';return json(localJobs[0]);}
        if (p==='/courses') return json(courses);
        if (p==='/lecture-names') {if(request.method()==='PUT') {const body=request.postDataJSON();names[body.sub_id]=body.name;} return json({names});}
        if (p==='/course-zones') {if(request.method()==='PUT') {const body=request.postDataJSON();zones[body.course_id]=body.zone;} return json({zones,sections,revision:0,default_zone:"unassigned"});}
        if (p==='/workflows') return json([]);
        if (p==='/workflow-approvals') return json({approved:[],errors:[]});
        if (p==='/courses/1/lectures') return json(lectures);
        if (p==='/lectures/10') return json(lecture);
        if (p==='/model-providers') return json({source:'github-variable',providers:[provider]});
        if (p==='/search') return json({total:1,page:1,has_more:false,results:[{sub_id:'10',course_id:'1',course_title:'现代思想史',sub_title:lecture.sub_title,hit_field:'ocr',snippet:'专属 OCR 关键词'}]});
        if (p==='/subscriptions') return json({course_ids:['1'],courses:[courses[0]]});
        if (p==='/subscription-catalog') {
          const q = `%${(url.searchParams.get('q') || '').trim()}%`, term = url.searchParams.get('term') || '';
          const page = Math.max(1, Number(url.searchParams.get('page')) || 1);
          const size = Math.max(1, Math.min(Number(url.searchParams.get('limit')) || 100, 200));
          const where = '(title LIKE ? OR teacher LIKE ? OR dept LIKE ? OR course_id LIKE ?)' + (term ? ' AND term = ?' : '');
          const params = [q,q,q,q,...(term ? [term] : [])];
          const total = rows(`SELECT COUNT(*) n FROM all_courses WHERE ${where}`,params)[0].n;
          return json({terms:['2026-秋','2026-春'],total,page,page_size:size,has_more:page*size<total,
            courses:rows(`SELECT course_id,term,title,teacher,dept FROM all_courses WHERE ${where} ORDER BY term DESC,title COLLATE NOCASE,course_id LIMIT ? OFFSET ?`,[...params,size,(page-1)*size])});
        }
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
      if (mode === 'fork') await page.evaluate(async () => { await api('/api/local/course-sections', {method:'PUT', body:JSON.stringify({sections:[{id:'study',name:'学习区'}],revision:0})}); await loadCourseZones(); await loadCourses(); });
      await page.locator('.course-card').filter({hasText:'现代思想史'}).locator('select').selectOption('study');
      await page.locator('#course-sidebar [data-filter-kind="zone"][data-filter-value="study"]').click();
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
      await page.evaluate(() => {
        const preview = document.createElement('div');
        preview.id = 'note-color-check';
        preview.className = 'summary';
        preview.innerHTML = renderMarkdown('## 主标题\n\n### 次级标题\n\n正文与**重点**，以及 `code`。\n\n> 引用内容');
        document.querySelector('#detail-content').append(preview);
      });
      const noteColors = () => page.evaluate(() => {
        const color = selector => getComputedStyle(document.querySelector(`#note-color-check ${selector}`)).color;
        return {
          title: color('h2'), subtitle: color('h3'), emphasis: color('strong'), code: color('code'), quote: color('blockquote'),
          quoteBackground: getComputedStyle(document.querySelector('#note-color-check blockquote')).backgroundColor,
        };
      });
      assert.deepEqual(await noteColors(), {
        title:'rgb(147, 197, 253)', subtitle:'rgb(94, 234, 212)', emphasis:'rgb(253, 164, 175)',
        code:'rgb(249, 168, 212)', quote:'rgb(191, 219, 254)', quoteBackground:'rgb(21, 34, 56)',
      });
      await page.locator('#theme-toggle').click();
      assert.equal(await page.evaluate(()=>getComputedStyle(document.documentElement).colorScheme),'light');
      assert.deepEqual(await noteColors(), {
        title:'rgb(29, 78, 216)', subtitle:'rgb(15, 118, 110)', emphasis:'rgb(180, 35, 24)',
        code:'rgb(157, 23, 77)', quote:'rgb(30, 64, 175)', quoteBackground:'rgb(239, 246, 255)',
      });
      await page.locator('#theme-toggle').click();
      await page.locator('#note-color-check').evaluate(node => node.remove());
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
      assert.match(await page.locator('#message').innerText(), /Artifacts.*下载文件/);
      await page.locator('.desktop-nav [data-view="search"]').click();
      for (const domain of ['title', 'summary', 'transcript']) await page.locator(`[data-domain="${domain}"]`).click();
      await page.locator('#search').fill('关键词');
      await page.locator('.search-card').waitFor();
      assert.match(await page.locator('.search-card').innerText(),/专属 OCR/);
      await page.locator('.search-card').click();
      await page.locator('#detail-content .ppt').waitFor();
      assert.match(await page.locator('#detail-content').innerText(),/专属 OCR/);
      await page.locator('.desktop-nav [data-view="subscriptions"]').click();
      await page.locator('#subscription-query').fill('目录课程');
      await page.waitForFunction(()=>document.querySelector('#subscription-catalog-status').textContent==='已显示 100 / 207 门课程');
      assert.equal(await page.locator('#subscription-catalog .subscription-row').count(),100);
      await page.locator('#subscription-catalog-more').click();
      await page.waitForFunction(()=>document.querySelector('#subscription-catalog-status').textContent==='已显示 200 / 207 门课程');
      await page.locator('#subscription-catalog-more').click();
      await page.waitForFunction(()=>document.querySelector('#subscription-catalog-status').textContent==='已显示 207 / 207 门课程');
      assert.equal(await page.locator('#subscription-catalog .subscription-row').count(),207);
      assert.equal(await page.evaluate(()=>new Set(catalogRows.map(row=>row.course_id)).size),207);
      assert.equal(await page.locator('#subscription-catalog-more').isVisible(),false);
      await page.locator('#subscription-catalog .subscription-row').last().getByRole('button',{name:'单次运行',exact:true}).click();
      assert.equal(await page.locator('#single-run-ids').inputValue(),'catalog-206');
      await page.locator('#view-run:not(.hidden)').waitFor();
      await page.locator('#run-clear-selection').click();
      await page.locator('.rerun-course').filter({hasText:'现代思想史'}).locator('.rerun-course-check input').check();
      await page.locator('.rerun-course').filter({hasText:'科学与社会'}).locator('.rerun-course-check input').check();
      await page.getByRole('button',{name:'上移科学与社会',exact:true}).click();
      assert.deepEqual(await page.locator('#run-queue .run-queue-row').evaluateAll(rows=>rows.map(row=>row.dataset.courseId)),['2','1']);
      await page.locator('#run-lecture-order').selectOption('newest');
      await page.locator('#rerun-page-submit').click();
      await page.waitForFunction(()=>document.querySelector('#message').textContent.includes('已开始GitHub Actions'));
      assert.equal(dispatches.at(-1).body.inputs.course_ids,'2,1');
      assert.equal(dispatches.at(-1).body.inputs.lecture_order,'newest');
      await page.locator('#run-target').selectOption('local');
      if(mode==='local') {
        await page.locator('#run-api-key').fill('synthetic-local-key');
        await page.locator('#rerun-page-submit').click();
        await page.getByText('合成处理中',{exact:false}).waitFor();
        assert.equal(runRequests.at(-1).target,'local');
        assert.equal(runRequests.at(-1).api_key,'synthetic-local-key');
        assert.equal(await page.locator('#run-api-key').inputValue(),'');
        await page.locator('#local-runs details summary').click();
        await page.waitForTimeout(3200);
        assert.equal(await page.locator('#local-runs details').evaluate(node=>node.open),true);
        await page.getByRole('button',{name:'停止本地任务'}).click();
        await page.getByText('重新生成笔记 · 已停止',{exact:false}).count();
        await page.waitForFunction(()=>document.querySelector('#local-runs').textContent.includes('已停止'));
        await page.locator('#run-save-priority').click();
        await page.waitForFunction(()=>document.querySelector('#run-priority-status').textContent.includes('已保存'));
        assert.equal(preferences.lecture_order,'newest');
      } else {
        assert.equal(await page.locator('#rerun-page-submit').isDisabled(),true);
        assert.equal(await page.locator('#run-local-link').isVisible(),true);
      }
      await page.evaluate(()=>window.scrollTo(0,0));
      await page.screenshot({path:`/tmp/icourse-${mode}-run-desktop.png`,fullPage:true});
      await page.setViewportSize({width:390,height:844});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
      await page.screenshot({path:`/tmp/icourse-${mode}-run-mobile.png`,fullPage:true});
      await page.setViewportSize({width:1280,height:900});
      await page.locator('.desktop-nav [data-view="subscriptions"]').click();
      await page.locator('#subscription-term').selectOption('2026-春');
      await page.waitForFunction(()=>document.querySelector('#subscription-catalog-status').textContent==='已显示 2 / 2 门课程');
      await page.locator('#subscription-term').selectOption('2026-秋');
      await page.waitForFunction(()=>document.querySelector('#subscription-catalog-status').textContent==='已显示 100 / 205 门课程');
      for (const q of ['独特教师','独特学院','field-id']) {
        await page.locator('#subscription-query').fill(q);
        await page.waitForFunction(()=>document.querySelector('#subscription-catalog-status').textContent==='已显示 1 / 1 门课程');
        assert.equal(await page.evaluate(()=>catalogRows[0].course_id),'field-id');
      }
      await page.locator('#subscription-query').fill('不存在的课程');
      await page.waitForFunction(()=>document.querySelector('#subscription-catalog').textContent==='没有匹配的课程。');
      assert.equal(await page.locator('#subscription-catalog-status').innerText(),'已显示 0 / 0 门课程');

      // A response from the previous query must not replace a newer result.
      await page.evaluate(()=>{
        window.catalogOriginalApi = api;
        api = async (path, options) => {
          if (path.startsWith('/api/local/subscription-catalog') && new URL(path,location.href).searchParams.get('q')==='目录课程') {
            const result = await window.catalogOriginalApi(path,options);
            await new Promise(resolve=>{window.releaseCatalogSearch=resolve;});
            return result;
          }
          return window.catalogOriginalApi(path,options);
        };
      });
      await page.locator('#subscription-query').fill('目录课程');
      await page.waitForFunction(()=>Boolean(window.releaseCatalogSearch));
      await page.locator('#subscription-query').fill('field-id');
      await page.waitForFunction(()=>document.querySelector('#subscription-catalog-status').textContent==='已显示 1 / 1 门课程');
      await page.evaluate(async()=>{window.releaseCatalogSearch(); await new Promise(resolve=>setTimeout(resolve,0)); api=window.catalogOriginalApi;});
      assert.equal(await page.evaluate(()=>catalogRows[0].course_id),'field-id');
      assert.equal(await page.locator('#subscription-catalog-status').innerText(),'已显示 1 / 1 门课程');

      // An initial failure offers retry, which starts at page one.
      await page.evaluate(()=>{
        api = async (path,options) => {
          if (path.startsWith('/api/local/subscription-catalog')) throw new Error('合成目录请求失败');
          return window.catalogOriginalApi(path,options);
        };
      });
      await page.locator('#subscription-query').fill('目录课程');
      await page.locator('#subscription-catalog-more').getByText('重试',{exact:true}).waitFor();
      await page.evaluate(()=>{api=window.catalogOriginalApi;});
      await page.locator('#subscription-catalog-more').click();
      await page.waitForFunction(()=>document.querySelector('#subscription-catalog-status').textContent==='已显示 100 / 205 门课程');
      await page.setViewportSize({width:390,height:844});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
      await page.screenshot({path:`/tmp/icourse-${mode}-subscriptions-mobile.png`,fullPage:true});
      await page.setViewportSize({width:390,height:844});
      await page.locator('#mobile-nav [data-view="courses"]').click();
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
      await page.screenshot({path:`/tmp/icourse-${mode}-mobile.png`,fullPage:true});
      await page.setViewportSize({width:1280,height:900});
      await page.screenshot({path:`/tmp/icourse-${mode}-desktop.png`,fullPage:true});
      if(mode==='local') {
        await page.evaluate(async()=>{selectedSummaryVersionKeys.clear();await openLecture('10');});
        const original = lecture.summary;
        lecture.summary='本地任务自动出现的新笔记';localRevision++;
        await page.waitForFunction(()=>document.querySelector('#detail-content').textContent.includes('本地任务自动出现的新笔记'),{},{timeout:12000});
        lecture.summary=original;
      }
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
        await page.evaluate(async()=>{selectedSummaryVersionKeys.clear();await openLecture('10');});
        database.run("UPDATE lectures SET summary='云端任务自动出现的新笔记', processed_at='2026-10-09' WHERE sub_id='10'");
        blobs.indexLive=blobs.index;blobs.shardLive=encrypt(zlib.gzipSync(Buffer.from(database.export())));
        liveShard=true;legacy=false;version='commit-live';
        await page.waitForFunction(()=>document.querySelector('#detail-content').textContent.includes('云端任务自动出现的新笔记'),{},{timeout:30000});
        await page.locator('.desktop-nav [data-view="settings"]').click();
        await page.locator('#forget-credentials-button').click();
        await page.locator('#setup:not(.hidden)').waitFor();
        assert.equal(await page.locator('#detail-content').innerText(),'');
      }
      assert.deepEqual(errors,[]);
      await page.close();
      console.log(`${mode}: shared UI, zones, ordering, versions, export, search, catalog, run queue, backend selection, live note refresh, mobile layout passed`);
    }
    console.log('Pages: encrypted shards, legacy decryption, rollback, rerun, logout passed');
  } finally {
    if(browser) await browser.close();
    await new Promise(resolve=>server.close(resolve));
    database.close();
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
