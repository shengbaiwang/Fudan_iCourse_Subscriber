/* Organization persistence and tree operations without network or a real account. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const values = new Map();
let failWrites = false;
const context = vm.createContext({window: {ICS: {
  github: {detectRepo: () => ({owner:'test',repo:'library'}),
    fetchShardManifest: async()=>({format:'sharded',commitSha:'fixture',index:{sha:'index'},shards:[]}),
    fetchBlobBytes: async()=>new TextEncoder().encode('{"shards":[]}')},
  crypto: {buildPassword:async()=>'',decrypt:async bytes=>bytes,isJsonObj:()=>true},
  db: {initEmpty:async()=>{}},
}}, URL, TextDecoder, fetch:async url=>{
  assert.equal(url,'https://api.github.com/repos/test/library');
  return {ok:true,status:200,json:async()=>({})};
}, location: {origin:'http://localhost'}, localStorage: {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => { if (failWrites) throw new Error('storage unavailable'); values.set(key, value); },
    removeItem: key=>values.delete(key),
  }});
for (const name of ['organization.js', 'browser/transport.js']) {
  vm.runInContext(fs.readFileSync(path.join(root, 'local_web/static', name), 'utf8'), context);
}
const org = context.window.ICS.organization, api = context.window.ICOURSE_API;
const plain = value => JSON.parse(JSON.stringify(value));
const A = {id:'section-' + 'a'.repeat(32),name:'学习'};
const B = {id:'section-' + 'b'.repeat(32),name:'资料库'};
const C = {id:'section-' + 'c'.repeat(32),name:'英语',parent_id:A.id};
const D = {id:'section-' + 'd'.repeat(32),name:'语法',parent_id:C.id};
const key = 'icourse:test/library/data:organization';
const put = (route, payload) => api(`/api/local/${route}`, {method:'PUT',body:JSON.stringify(payload)});

(async () => {
  await api('/api/local/configure',{method:'POST',body:JSON.stringify({owner:'test',repo:'library',token:'fixture',stuid:'fixture',uispsw:'fixture'})});
  assert.deepEqual(plain(await api('/api/local/course-zones')), {zones:{},sections:[],default_zone:'unassigned',revision:0});
  values.set('icourse:test/library/data:zones', JSON.stringify({'1':'学习区','2':'整理区','3':'archive'}));
  let state = await api('/api/local/course-zones');
  assert.equal(state.default_zone,'unassigned');
  assert.deepEqual(plain(state.zones), {'1':'study','2':'unassigned','3':'archive'});
  assert.deepEqual(plain(state.sections), [{id:'study',name:'学习区'},{id:'reference',name:'资料库'}]);
  values.set(key, JSON.stringify({zones:{'1':'organize','2':'reference'}, sections:[
    {id:'organize',name:'项目'}, {id:'reference',name:'查阅区'}, B],revision:4,default_zone:'organize'}));
  state = await api('/api/local/course-zones');
  assert.equal(state.sections[0].name,'项目');
  assert.equal(state.sections[1].name,'资料库 2');
  assert.equal(state.zones['1'],'organize');
  assert.equal(state.default_zone,'unassigned');
  values.delete(key);
  state = await put('course-sections', {sections:[A,C,D,B],revision:0});
  assert.deepEqual(plain(state.sections), [A,C,D,B]);
  await put('course-zones', {course_id:'1',zone:C.id});
  await put('course-zones', {course_id:'2',zone:A.id});
  assert.deepEqual(plain(org.sectionTree(state.sections)).map(row=>row.path), ['学习','学习 / 英语','学习 / 英语 / 语法','资料库']);
  // Reordering a parent moves its entire subtree, and moving a subtree preserves membership.
  assert.deepEqual(plain(org.reorderSections([A,C,D,B],A.id,B.id,true)).map(s=>s.id),[B.id,A.id,C.id,D.id]);
  const moved = org.reparentSections([A,C,D,B],C.id,B.id);
  assert.equal(moved.find(s=>s.id===C.id).parent_id,B.id);
  assert.equal(moved.find(s=>s.id===D.id).parent_id,C.id);
  assert.equal(org.canParent([A,C,D,B],A.id,D.id),false);
  assert.throws(()=>org.reorderSections([A,C,D,B],A.id,D.id));
  state = await put('course-sections', {sections:moved,revision:1});
  assert.equal(state.zones['1'],C.id);
  assert.equal(state.zones['2'],A.id);
  const snapshot = values.get(key);
  for (const sections of [[{...A,parent_id:A.id}], [{...A,parent_id:B.id},{...B,parent_id:A.id}],
    [{...A,parent_id:'unassigned'}], [{...A,name:'待整理'}], [A,{...B,name:'学习'}]]) {
    await assert.rejects(put('course-sections',{sections,revision:2}));
    assert.equal(values.get(key),snapshot);
  }
  await assert.rejects(put('course-sections',{sections:[],revision:1}));
  assert.equal(values.get(key),snapshot);
  failWrites = true;
  await assert.rejects(put('course-sections',{sections:[],revision:2}),/storage unavailable/);
  assert.equal(values.get(key),snapshot);
  failWrites = false;
  state = await put('course-sections',{sections:moved.filter(s=>s.id!==A.id),revision:2});
  assert.equal(state.zones['1'],C.id);
  assert.equal(state.zones['2'],'unassigned');
  const chain = Array.from({length:7},(_,i)=>({id:`section-${i.toString(16).padStart(32,'0')}`,name:`层级 ${i}`,
    ...(i?{parent_id:`section-${(i-1).toString(16).padStart(32,'0')}`}:{})}));
  assert.equal(org.validateSections(chain.slice(0,6)).length,6);
  assert.throws(()=>org.validateSections(chain),/6 层/);
  console.log('Course organization: inbox migration, persistence, subtree moves, stale edits and hierarchy validation passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
