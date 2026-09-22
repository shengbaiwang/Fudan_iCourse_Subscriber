const assert = require('node:assert/strict');
const {eligible, reconcile} = require('../local_web/static/browser/workflow-approvals.js');
const now = Date.parse('2026-09-22T12:00:00Z');
const run = {id:42,status:'completed',conclusion:'action_required',run_attempt:1,event:'workflow_dispatch',
  head_branch:'main',head_sha:'trusted',path:'.github/workflows/single_run.yml',pull_requests:[],
  repository:{full_name:'owner/repo'},head_repository:{full_name:'owner/repo'},
  actor:{login:'owner'},triggering_actor:{login:'owner'},created_at:'2026-09-22T11:59:00Z'};
function client({fresh=run, head='trusted', user='owner', denied=false} = {}) {
  const posts = [];
  const request = async (path, options={}) => {
    if (options.method === 'POST') {
      posts.push(path);
      if (denied) throw new Error('GitHub 403');
      return null;
    }
    if (path.includes('?')) return {workflow_runs:[run]};
    if (path === '/user') return {login:user};
    if (path.endsWith('/42')) return fresh;
    if (path.endsWith('/main')) return {object:{sha:head}};
    throw new Error(`Unexpected request: ${path}`);
  };
  return {request, posts};
}
(async () => {
  const ok = client();
  assert.deepEqual(await reconcile(ok.request,'owner','repo',()=>true,now),{approved:[42],errors:[]});
  assert.deepEqual(ok.posts,['/repos/owner/repo/actions/runs/42/approve']);
  for (const changes of [{event:'pull_request'},{event:'push'},{event:'schedule'},
    {path:'.github/workflows/check.yml'},{path:'.github/workflows/talk_transcribe.yml'},
    {head_branch:'dev'},{actor:{login:'other'}},
    {triggering_actor:{login:'other'}},{repository:{full_name:'owner/other'}},{head_repository:null},
    {head_repository:{full_name:'outsider/repo'}},{pull_requests:[{}]},{pull_requests:null},
    {path:'.github/workflows/delete_course.yml'},{path:'.github/workflows/deploy-frontend.yml'},
    {status:'waiting'},{conclusion:'failure'},{conclusion:'success'},{run_attempt:2},
    {created_at:'2026-09-20T00:00:00Z'},{created_at:'2026-09-23T00:00:00Z'},{created_at:'invalid'},{id:'../other'}]) {
    const fresh = {...run,...changes};
    assert.equal(eligible(fresh,'owner','repo',now),false,JSON.stringify(changes));
    const api = client({fresh});
    await reconcile(api.request,'owner','repo',()=>true,now);
    assert.equal(api.posts.length,0);
  }
  for (const options of [{head:'changed'},{user:'collaborator'},{}]) {
    const api = client(options);
    await reconcile(api.request,'owner','repo',()=>Object.keys(options).length>0,now);
    assert.equal(api.posts.length,0);
  }
  const denied = client({denied:true});
  const result = await reconcile(denied.request,'owner','repo',()=>true,now);
  assert.equal(result.approved.length,0);
  assert.match(result.errors[0].message,/403/);
  assert.equal(denied.posts.length,1);
  // Exercise the actual Pages route, session binding, empty 201 response and
  // throttling, rather than testing only the standalone approval algorithm.
  const fs = require('node:fs'), vm = require('node:vm');
  const storage = new Map(), requests = [];
  const context = {URL, console, location:{origin:'https://example.test'},
    localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
    window:{ICourseWorkflowApprovals:{eligible,reconcile},ICS:{
      github:{detectRepo:()=>null,fetchShardManifest:async()=>({format:'legacy',commitSha:'data',legacy:{sha:'blob'}}),fetchBlobBytes:async()=>new Uint8Array()},
      crypto:{decryptWithFallback:async()=>({data:new Uint8Array()}),isSqlite:()=>true},
      db:{initDB:async()=>{},close:()=>{}},
    }},
    fetch:async(url, options={})=>{
      const path = new URL(url).pathname;
      requests.push({path,method:options.method || 'GET',token:options.headers.Authorization});
      let value;
      const current = {...run,created_at:new Date(Date.now()-1000).toISOString()};
      if (path === '/repos/owner/repo') value = {full_name:'owner/repo'};
      else if (path.endsWith('/actions/runs')) value = {workflow_runs:[current]};
      else if (path === '/user') value = {login:'owner'};
      else if (path.endsWith('/42')) value = current;
      else if (path.endsWith('/main')) value = {object:{sha:'trusted'}};
      else if (path.endsWith('/approve')) return new Response(null,{status:201});
      else throw new Error(`Unexpected transport request: ${path}`);
      return new Response(JSON.stringify(value),{status:200});
    }};
  vm.runInNewContext(fs.readFileSync(require.resolve('../local_web/static/browser/transport.js'),'utf8'),context);
  const api = context.window.ICOURSE_API;
  await api('/api/local/configure',{method:'POST',body:JSON.stringify({owner:'owner',repo:'repo',token:'test-token',stuid:'test',uispsw:'test'})});
  assert.equal((await api('/api/local/workflow-approvals',{method:'POST'})).approved[0],42);
  await api('/api/local/workflow-approvals',{method:'POST'});
  assert.equal(requests.filter(r=>r.path.endsWith('/approve')).length,1);
  assert.equal(requests.find(r=>r.path.endsWith('/approve')).token,'Bearer test-token');
  await api('/api/local/credentials/forget',{method:'POST'});
  await assert.rejects(api('/api/local/workflow-approvals',{method:'POST'}),/请先连接/);
  console.log('Workflow approval guard and recovery tests passed');
})().catch(error => {console.error(error);process.exitCode=1;});
