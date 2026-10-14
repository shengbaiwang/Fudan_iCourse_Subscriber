/* Browser policy persistence and full-course dispatch, entirely offline. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '../local_web/static');
const selected = Array.from({length:45}, (_, i) => String(100 + i));
const values = new Map(), dispatches = [];
let config = {version:1, providers:[{name:'test',api_key_env:'LLM_TEST_API_KEY',default_base_url:'https://example.test/v1',models:['saved-model'],enabled:true}]};
const context = vm.createContext({window:{ICS:{
  github:{detectRepo:()=>({owner:'test',repo:'library'}),fetchShardManifest:async()=>({format:'sharded',commitSha:'fixture',index:{sha:'index'},shards:[]}),fetchBlobBytes:async()=>new TextEncoder().encode('{"shards":[]}')},
  crypto:{buildPassword:async()=>'',decrypt:async bytes=>bytes,isJsonObj:()=>true},
  db:{initEmpty:async()=>{},getLecture:id=>({sub_id:id,course_id:'1',date:'2026-09-01',transcript:'材料'}),getLectures:()=>selected.map(id=>({sub_id:id,transcript_available:true}))},
}},URL,TextDecoder,location:{origin:'http://localhost'},localStorage:{
  getItem:key=>values.get(key)??null,setItem:(key,value)=>values.set(key,value),removeItem:key=>values.delete(key),
},fetch:async(url, options={})=>{
  const route = new URL(url).pathname.replace('/repos/test/library','');
  if (route.endsWith('/dispatches')) { dispatches.push(JSON.parse(options.body)); return {ok:true,status:204}; }
  if (route === '/actions/secrets') return {ok:true,status:200,json:async()=>({secrets:[{name:'LLM_TEST_API_KEY'}]})};
  if (route === '/actions/variables/MODEL_PROVIDERS_JSON') {
    if (options.method === 'PATCH') config = JSON.parse(JSON.parse(options.body).value);
    return {ok:true,status:200,json:async()=>({value:JSON.stringify(config)})};
  }
  assert.equal(route,''); return {ok:true,status:200,json:async()=>({})};
}});
for (const script of ['provider-urls.js','generation-policy.js','browser/transport.js']) vm.runInContext(fs.readFileSync(path.join(root,script),'utf8'),context);
const policy = context.window.ICourseGenerationPolicy;
assert.equal(policy.normalize().timeout_seconds,600);
assert.throws(()=>policy.normalize({output_tokens:100,max_output_tokens:99}),/扩容上限/);
assert.throws(()=>policy.normalize({max_retries:-1}),/整数/);
const api = context.window.ICOURSE_API;
const post = (route,payload)=>api(`/api/local/${route}`,{method:'POST',body:JSON.stringify(payload)});
(async()=>{
  await post('configure',{owner:'test',repo:'library',token:'fixture',stuid:'fixture',uispsw:'fixture'});
  await api('/api/local/model-providers',{method:'PUT',body:JSON.stringify({providers:[{...config.providers[0],generation:{timeout_seconds:900,max_retries:4,output_tokens:8192,max_output_tokens:16384}}]})});
  assert.equal(config.providers[0].generation.timeout_seconds,900);
  assert.equal((await api('/api/local/model-providers')).providers[0].generation.max_retries,4);
  await post('summary-reruns',{course_ids:['1']});
  assert.deepEqual(dispatches.at(-1).inputs.resummarize_sub_ids.split(','),selected);
  await post('runs',{target:'github',kind:'rerun',sub_ids:selected,lecture_order:'api'});
  assert.deepEqual(dispatches.at(-1).inputs.resummarize_sub_ids.split(','),selected);
  console.log('Browser generation settings and 45-lecture dispatch passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
