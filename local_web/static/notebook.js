(() => {
  const el = id => document.getElementById(id);
  const act = task => Promise.resolve().then(task).catch(error => message(error.message, true));
  const request = (path, options = {}) => api(`/api/local/notebook${path}`, options);
  const library = (path = '', options = {}) => api(`/api/local/note-library${path}`, options);
  const folderRequest = (path = '', options = {}) => api(`/api/local/note-folders${path}`, options);
  const json = (method, body) => ({method, body: JSON.stringify(body)});
  const sourcePath = row => `/${row.kind}/${encodeURIComponent(row.source_id)}`;
  const draftKey = row => isPersonal(row) ? `sea-note-draft:${row.source_id}` : `sea-note-draft:${row.kind}:${row.source_id}`;
  const revisionOf = row => isPersonal(row) ? row.revision : row.edit_revision;
  const editable = () => selected && !selected.deleted && contentTab==='summary' && versionKey==='active';
  const isPersonal = row => row?.kind === 'personal';
  const isCourse = row => row?.kind === 'course-group' || !shared.enabled && row?.kind === 'course';
  let shared = {enabled:false}, pendingMerge = null;
  const labels = {personal:'个人', course:'课程', talk:'讲座'};
  const formatDate = value => {
    const date = new Date(value);
    return value && !Number.isNaN(date.getTime()) ? date.toLocaleDateString('zh-CN', {year:'numeric',month:'long',day:'numeric'}) : '日期未记录';
  };
  const snippet = body => window.ICS?.render?.plainSnippet(body, 120) || body.replace(/[#*`>_]/g, '').replace(/\s+/g, ' ').slice(0,120);
  let notes = [], folders = [], selected = null, kind = 'all', filter = 'all', folder = '';
  let reading = false, dirty = false, saving = null, saveTimer, searchTimer, menu = null;
  let editHistory=[], editIndex=-1, outlineTimer, findMatches=[], findIndex=-1, outlineObserver;
  let expanded = new Set(['']);
  try { expanded = new Set(['',...JSON.parse(localStorage.getItem('sea-note-folders-open') || '[]')]); } catch (_) {}
  let activeLoads=0, polling=false;
  let loadId = 0, openId = 0, searchId = 0, versionKey = 'active', contentTab = 'summary', folderEdit = null;

  const editorSnapshot = () => ({title:el('note-title').value,body:el('note-body').value});
  function updateUndo() {
    el('note-undo').disabled=!editable() || editIndex<=0;
    el('note-redo').disabled=!editable() || editIndex>=editHistory.length-1;
  }
  function resetUndo() { editHistory=[editorSnapshot()];editIndex=0;updateUndo(); }
  function recordEdit() {
    const snapshot=editorSnapshot(),previous=editHistory[editIndex];
    if(previous?.title===snapshot.title && previous?.body===snapshot.body)return;
    editHistory=editHistory.slice(0,editIndex+1);editHistory.push(snapshot);
    if(editHistory.length>50)editHistory.shift();editIndex=editHistory.length-1;updateUndo();
  }
  function undoEdit(direction) {
    if(!editable())return;
    const next=editIndex+direction;if(next<0 || next>=editHistory.length)return;
    editIndex=next;const snapshot=editHistory[editIndex];
    el('note-title').value=snapshot.title;el('note-body').value=snapshot.body;
    markDirty();updateMode();updateUndo();
  }
  function expression(query) {
    const terms=[...new Set(query.trim().split(/\s+/).filter(Boolean))].sort((a,b)=>b.length-a.length);
    return terms.length ? new RegExp(terms.map(term=>term.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')).join('|'),'giu') : null;
  }
  function highlight(root,query) {
    const pattern=expression(query);if(!pattern)return [];
    const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT),nodes=[];
    while(walker.nextNode())if(!walker.currentNode.parentElement.closest('script,style,.katex,mark'))nodes.push(walker.currentNode);
    const matches=[];
    for(const node of nodes){
      pattern.lastIndex=0;const hits=[...node.textContent.matchAll(pattern)];if(!hits.length)continue;
      const fragment=document.createDocumentFragment();let last=0;
      for(const hit of hits){
        fragment.append(document.createTextNode(node.textContent.slice(last,hit.index)));
        const mark=document.createElement('mark');mark.className='note-search-hit';mark.textContent=hit[0];fragment.append(mark);matches.push(mark);last=hit.index+hit[0].length;
      }
      fragment.append(document.createTextNode(node.textContent.slice(last)));node.replaceWith(fragment);
    }
    return matches;
  }
  function updateFind(jump=false) {
    const root=el('note-preview');root.querySelectorAll('mark.note-search-hit').forEach(mark=>mark.replaceWith(document.createTextNode(mark.textContent)));root.normalize();
    const query=el('note-find').value.trim(),pattern=expression(query);
    findMatches=[];findIndex=-1;
    if(selected && pattern){
      if(reading)findMatches=highlight(root,query);
      else findMatches=[...el('note-body').value.matchAll(pattern)].map(hit=>({start:hit.index,end:hit.index+hit[0].length}));
    }
    el('note-find-count').textContent=query ? `${findMatches.length} 处` : '';
    el('note-find-prev').disabled=el('note-find-next').disabled=!findMatches.length;
    if(jump && findMatches.length)stepFind(1);
  }
  function stepFind(direction) {
    if(!findMatches.length)return;
    findIndex=(findIndex+direction+findMatches.length)%findMatches.length;
    el('note-find-count').textContent=`${findIndex+1} / ${findMatches.length}`;
    if(reading){
      findMatches.forEach((node,index)=>node.classList.toggle('current',index===findIndex));
      findMatches[findIndex].scrollIntoView({block:'center'});
    }else selectSource(findMatches[findIndex].start,findMatches[findIndex].end);
  }
  function selectSource(start,end=start) {
    const node=el('note-body');node.focus({preventScroll:true});node.setSelectionRange(start,end);
    // The editor grows with its content, so use a matching mirror to locate a
    // source offset without changing text, scroll position, or undo history.
    const mirror=document.createElement('div'),style=getComputedStyle(node);
    for(const key of ['fontFamily','fontSize','fontWeight','lineHeight','letterSpacing','padding','boxSizing','wordBreak','overflowWrap'])mirror.style[key]=style[key];
    Object.assign(mirror.style,{position:'absolute',visibility:'hidden',whiteSpace:'pre-wrap',width:`${node.clientWidth}px`,overflowWrap:'break-word'});
    mirror.textContent=node.value.slice(0,start);const caret=document.createElement('span');caret.textContent='\u200b';mirror.append(caret);document.body.append(mirror);
    const y=caret.offsetTop;mirror.remove();window.scrollTo({top:node.getBoundingClientRect().top+scrollY+y-140});
  }
  function headingOffsets(body) {
    const rows=body.split('\n'),result=[];let offset=0,fence=null;
    for(let index=0;index<rows.length;index++){
      const line=rows[index],marker=/^ {0,3}(`{3,}|~{3,})/.exec(line);
      if(marker){if(!fence)fence=marker[1];else if(marker[1][0]===fence[0] && marker[1].length>=fence.length && !line.slice(marker[0].length).trim())fence=null;}
      else if(!fence && (/^ {0,3}#{1,6}(?:\s|$)/.test(line) || line.trim() && /^ {0,3}(?:=+|-+)\s*$/.test(rows[index+1] || '')))result.push(offset);
      offset+=line.length+1;
    }
    return result;
  }
  function updateOutline() {
    outlineObserver?.disconnect();const root=el('note-outline-list');root.replaceChildren();
    if(!selected || contentTab==='transcript'){
      const text=document.createElement('p');text.className='meta';text.textContent=selected ? '转写为纯文本，暂无标题大纲' : '打开笔记后显示标题大纲';root.append(text);return;
    }
    let preview=el('note-preview');
    if(!reading){preview=document.createElement('div');preview.innerHTML=renderMarkdown(currentBody());}
    const headings=[...preview.querySelectorAll('h1,h2,h3,h4,h5,h6')],offsets=headingOffsets(currentBody());
    if(!headings.length){const text=document.createElement('p');text.className='meta';text.textContent='使用 #、## 等 Markdown 标题组织笔记，大纲会自动更新';root.append(text);return;}
    const base=Math.min(...headings.map(node=>Number(node.tagName[1])));
    const buttons=headings.map((heading,index)=>{
      heading.id=`note-heading-${index}`;const button=document.createElement('button');button.type='button';button.className='note-outline-link';button.textContent=heading.textContent;
      button.style.setProperty('--level',Number(heading.tagName[1])-base);button.setAttribute('aria-current',String(index===0));
      button.onclick=()=>{
        if(reading){heading.scrollIntoView({block:'start'});heading.tabIndex=-1;heading.focus({preventScroll:true});}
        else selectSource(offsets[index] ?? 0);
        buttons.forEach((item,idx)=>item.setAttribute('aria-current',String(idx===index)));
        document.body.classList.remove('mobile-outline-open');el('outline-panel-toggle').setAttribute('aria-expanded',String(innerWidth>700));
        if(innerWidth<=700)el('outline-panel-toggle').textContent='展开大纲';
      };root.append(button);return button;
    });
    if(reading && 'IntersectionObserver' in window){
      outlineObserver=new IntersectionObserver(()=>{
        const current=headings.reduce((active,node,index)=>node.getBoundingClientRect().top<=160 ? index : active,0);
        buttons.forEach((button,index)=>button.setAttribute('aria-current',String(index===current)));
      },{rootMargin:'-100px 0px -65% 0px',threshold:0});headings.forEach(node=>outlineObserver.observe(node));
    }
  }

  function changed() {
    try { localStorage.setItem('sea-notes-changed', String(Date.now())); } catch (_) {}
  }
  function undoMessage(text, undo) {
    message(text);
    el('message').append(createButton('撤销', () => act(undo)));
    clearTimeout(messageTimer);
    messageTimer = setTimeout(() => el('message').classList.add('hidden'), 12000);
  }
  function keepDraft() {
    if (!selected || !dirty) return;
    try { localStorage.setItem(draftKey(selected), JSON.stringify({title:el('note-title').value, body:el('note-body').value, revision:revisionOf(selected)})); }
    catch (_) { el('note-save-state').textContent = '请保持窗口打开，正在保存'; }
  }
  function resizeBody() {
    if (!selected || reading) return;
    const node = el('note-body');
    node.style.height = 'auto';
    node.style.height = `${Math.max(node.scrollHeight,innerHeight * .45)}px`;
  }
  function markDirty() {
    if (!editable()) return;
    dirty = true;
    el('note-save-state').textContent = '正在保存…';
    keepDraft(); clearTimeout(saveTimer);
    saveTimer = setTimeout(() => act(flush), 650);
    resizeBody();
    clearTimeout(outlineTimer); outlineTimer=setTimeout(()=>{updateOutline();updateFind(false);},250);
  }
  function failedSave(conflict = false) {
    el('note-save-state').textContent = conflict ? '版本冲突 · 草稿已保留' : '未保存 · 草稿已保留';
    if(conflict)el('note-save-state').append(createButton('比较并合并',()=>act(()=>reviewConflict()),'text-button'));
    el('note-save-state').append(createButton(conflict ? '保留为新笔记' : '重试', () => act(async () => {
      if (!conflict) return flush();
      const previous = selected;
      const row = await request('', json('POST', {title:el('note-title').value, body:el('note-body').value}));
      if (previous.folder_id || previous.folder_candidates?.length) await library(`/personal/${row.id}/folder`, json('PUT', {folder_id:previous.folder_id || '',revision:0,candidates:previous.folder_candidates || []}));
      localStorage.removeItem(draftKey(previous)); dirty = false; selected = null;
      resetFilters(); await load(); await open(`personal:${row.id}`,{reading:false});
      message('已保存为新笔记，另一窗口的版本已保留');
    }), 'text-button'));
  }
  async function flush() {
    clearTimeout(saveTimer);
    if (saving) await saving;
    if (!dirty || !selected) return;
    const row = selected;
    const snapshot = {title:el('note-title').value, body:el('note-body').value, revision:revisionOf(row)};
    keepDraft();
    saving = pendingMerge?.raw_hash ? api('/api/local/shared-notes/resolve',json('POST',{note_id:row.id,...snapshot,raw_hash:pendingMerge.raw_hash,app_hash:pendingMerge.app_hash})) : isPersonal(row) ? request(`/${row.source_id}`, json('PUT', snapshot)) : library(`${sourcePath(row)}/content`,json('PUT',snapshot));
    try {
      const result = await saving;
      pendingMerge=null;el('note-conflict-review').classList.add('hidden');
      if (selected?.id === row.id) {
        selected = {...row, ...result, id:row.id, source_id:row.source_id};
        dirty = snapshot.title !== el('note-title').value || snapshot.body !== el('note-body').value;
        if (!dirty) {
          localStorage.removeItem(draftKey(row));
          el('note-save-state').textContent = '已保存';
          el('note-date').textContent = `${isPersonal(row) ? '个人笔记' : '手动修订'} · 更新于 ${formatDate(result.updated_at)}`;
          if(!isPersonal(row))fillSourceControls();
        } else keepDraft();
      }
      notes = notes.map(note => note.id === row.id ? {...note, title:result.title, excerpt:snippet(result.body), updated_at:result.updated_at} : note);
      renderList(); changed();
    } catch (error) {
      failedSave(error.message.includes('另一窗口'));
      throw error;
    } finally { saving = null; }
    if (dirty) await flush();
  }
  function syncFilters() {
    document.querySelectorAll('[data-note-kind]').forEach(node => {
      node.classList.toggle('active', node.dataset.noteKind === kind);
      node.setAttribute('aria-pressed', String(node.dataset.noteKind === kind));
    });
    document.querySelectorAll('[data-note-filter]').forEach(node => {
      const active = node.dataset.noteFilter === filter;
      node.classList.toggle('active', active);
      node.setAttribute('aria-pressed', String(active));
    });
  }
  function resetFilters() {
    kind = 'all'; filter = 'all'; folder = ''; el('note-search').value = ''; syncFilters();
  }
  function bindContext(node, callback) {
    node.oncontextmenu = event => { event.preventDefault(); callback(); };
    node.addEventListener('keydown', event => {
      if (event.key === 'ContextMenu' || event.shiftKey && event.key === 'F10') { event.preventDefault(); callback(); }
    });
    let timer, point;
    node.addEventListener('pointerdown', event => {
      if (event.pointerType !== 'touch') return;
      point = {x:event.clientX,y:event.clientY};
      timer = setTimeout(() => { node.dataset.longPressed = '1'; callback(); }, 600);
    });
    node.addEventListener('pointermove', event => { if (point && Math.hypot(event.clientX-point.x,event.clientY-point.y)>10) clearTimeout(timer); });
    ['pointerup','pointercancel','pointerleave'].forEach(name => node.addEventListener(name, () => clearTimeout(timer)));
    node.addEventListener('click', event => {
      if (node.dataset.longPressed) { delete node.dataset.longPressed; event.preventDefault(); event.stopImmediatePropagation(); }
    }, true);
  }
  function folderPath(key) {
    const names=[], seen=new Set();
    while (key && !seen.has(key)) {
      seen.add(key); const row=folders.find(item=>item.id===key); if (!row) break;
      names.unshift(row.name); key=row.parent_id;
    }
    return names.join(' / ') || '收件箱';
  }
  function rememberExpanded() {
    try { localStorage.setItem('sea-note-folders-open',JSON.stringify([...expanded])); } catch (_) {}
  }
  function revealFolder(key) {
    const seen=new Set();
    while (key && !seen.has(key)) {
      seen.add(key); expanded.add(key); key=folders.find(row=>row.id===key)?.parent_id;
    }
    expanded.add(''); rememberExpanded();
  }
  function renderSharedStatus() {
    const node=el('shared-notes-status');node.classList.toggle('hidden',!shared.enabled);
    if(!shared.enabled)return;
    const count=shared.conflicts?.length || 0;
    node.textContent=shared.error ? '共享已暂停 · 查看原因' : count ? `${count} 篇笔记需要处理` : '已连接 Obsidian · 本地共享';
    node.title=shared.error || shared.path;node.classList.toggle('error',!!shared.error || !!count);
    node.onclick=()=>act(async()=>{
      await load();
      if(shared.error){message(`${shared.error}。目录：${shared.path}`,true);return;}
      const conflict=shared.conflicts?.[0];
      if(conflict){await open(conflict.id);await reviewConflict();}
      else message(`共享目录：${shared.path} · 修改保存后自动更新`);
    });
  }
  async function reviewConflict() {
    if(!selected)return;
    const key=selected.id,draft=editorSnapshot();
    const unresolved=shared.conflicts?.some(item=>item.id===key);
    const comparison=unresolved ? await api(`/api/local/shared-notes/conflict?note_id=${encodeURIComponent(key)}`) : null;
    const live=await library(sourcePath(selected));if(selected?.id!==key)return;
    const file=comparison?.file || {title:live.title,body:live.body};
    const app=dirty ? draft : comparison?.app || draft;
    const root=el('note-conflict-review');root.replaceChildren();root.classList.remove('hidden');
    const title=document.createElement('strong');title.textContent='比较笔记版本';root.append(title);
    const columns=document.createElement('div');columns.className='note-conflict-columns';
    for(const [label,row] of [['文件中已保存的版本',file],['应用版本 / 当前草稿',app]]){
      const column=document.createElement('div'),caption=document.createElement('p'),text=document.createElement('pre');
      caption.textContent=label;text.textContent=`${row.title}\n\n${row.body}`;column.append(caption,text);columns.append(column);
    }
    root.append(columns);
    const adopt=async value=>{
      selected=live;pendingMerge=comparison;versionKey='active';contentTab='summary';
      el('note-title').value=value.title;el('note-body').value=value.body;
      dirty=true;keepDraft();await flush();await load();await open(key);
    };
    root.append(createButton('采用文件版本',()=>act(()=>adopt(file)),'text-button'),createButton('采用应用版本',()=>act(()=>adopt(app)),'text-button'),createButton('在正文中编辑合并',()=>{
      selected=live;pendingMerge=comparison;versionKey='active';contentTab='summary';reading=false;
      el('note-title').value=app.title;el('note-body').value=app.body;dirty=true;keepDraft();resetUndo();updateMode();
      el('note-save-state').textContent='合并稿 · 编辑后自动保存，⌘S 保存';el('note-body').focus();
    },'text-button'));
    root.scrollIntoView({block:'nearest'});
  }
  const imageStub='data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7';
  let sharedImages=[];
  function renderSharedMarkdown(body) {
    sharedImages=[];
    if(!shared.enabled || !selected?.shared_path)return renderMarkdown(body);
    const parts=body.split(/(```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`)/g);
    body=parts.map((part,index)=>{
      if(index%2)return part;
      part=part.replace(/(!?)\[\[([^\n]*?)\]\](?!\])/g,(_,embed,target)=>{
        const [path,label]=target.split('|');
        return embed && /\.(png|jpe?g|gif|webp)$/i.test(path) ? `![${label || path}](${encodeURI(path)})` : `[${label || path}](#sea-link:${encodeURIComponent(path)})`;
      });
      return part.replace(/!\[([^\]]*)\]\(([^\s)]+)\)/g,(match,alt,target)=>{
        if(/^(https?:|data:|\/\/)/i.test(target))return match;
        const index=sharedImages.push(target)-1;return `![${alt}](${imageStub} "sea-image-${index}")`;
      });
    }).join('');
    return renderMarkdown(body);
  }
  function hydrateShared(root) {
    if(!shared.enabled || !selected?.shared_path)return;
    const key=selected.id,images=[...sharedImages];
    root.querySelectorAll('img[title^="sea-image-"]').forEach(img=>{
      const target=images[Number(img.title.slice(10))];img.removeAttribute('title');
      api(`/api/local/shared-notes/asset?note_id=${encodeURIComponent(key)}&target=${encodeURIComponent(target)}`).then(result=>{if(img.isConnected)img.src=result.data_url;}).catch(()=>{img.alt=`图片暂不可用：${target}`;});
    });
    root.querySelectorAll('a').forEach(link=>{
      const href=link.getAttribute('href') || '';
      if(!href || /^(?:[a-z]+:|\/\/)/i.test(href) || href.startsWith('#') && !href.startsWith('#sea-link:'))return;
      link.onclick=event=>{event.preventDefault();act(async()=>{
        const target=href.startsWith('#sea-link:') ? decodeURIComponent(href.slice(10)) : href;
        const result=await api(`/api/local/shared-notes/link?note_id=${encodeURIComponent(key)}&target=${encodeURIComponent(target)}`);
        await open(result.id);
        if(result.heading){const heading=[...el('note-preview').querySelectorAll('h1,h2,h3,h4,h5,h6')].find(node=>node.textContent===result.heading);heading?.scrollIntoView({block:'start'});}
      });};
    });
  }
  function courseEntry(row) {
    return {...row,id:row.location_reference || `course-group:${row.course_id}`,kind:'course-group',source_id:row.course_id,title:row.source_label};
  }
  function courseGroups() {
    if(shared.enabled)return [];
    const groups=new Map();
    notes.filter(row=>row.kind==='course').forEach(row=>{
      if(!groups.has(row.course_id))groups.set(row.course_id,{...courseEntry(row),notes:[]});
      groups.get(row.course_id).notes.push(row);
    });
    for(const row of groups.values())row.notes.sort((a,b)=>`${a.lecture_date} ${a.lecture_label}`.localeCompare(`${b.lecture_date} ${b.lecture_label}`,'zh-CN',{numeric:true}) || a.id.localeCompare(b.id));
    return [...groups.values()];
  }
  function folderNotes(key) {
    const rows=notes.filter(note=>note.folder_id===key && (shared.enabled || note.kind!=='course'));
    const order=row=>/^(\d{2,})\s+\S/.exec(row.title);
    return rows.sort((a,b)=>{
      const left=order(a),right=order(b);
      if(left && right)return Number(left[1])-Number(right[1]) || a.title.localeCompare(b.title,'zh-CN');
      return left ? -1 : right ? 1 : 0;
    });
  }
  function bindDrop(node,target,foldersAllowed=true) {
    node.ondragover=event=>{
      const types=[...event.dataTransfer.types];
      if (types.includes('application/x-sea-note') || foldersAllowed && types.includes('application/x-sea-folder')) {
        event.preventDefault(); event.stopPropagation(); event.dataTransfer.dropEffect='move'; node.classList.add('drop-target');
      }
    };
    node.ondragleave=()=>node.classList.remove('drop-target');
    node.ondrop=event=>{
      const key=event.dataTransfer.getData('application/x-sea-note');
      const folderKey=event.dataTransfer.getData('application/x-sea-folder');
      if (!key && !folderKey) return;
      event.preventDefault(); event.stopPropagation(); node.classList.remove('drop-target');
      if (key) { const row=notes.find(item=>item.id===key) || courseGroups().find(item=>item.id===key); if (row) act(()=>moveNote(row,target)); }
      else if (foldersAllowed) { const row=folders.find(item=>item.id===folderKey); if (row) act(()=>moveFolder(row,target)); }
    };
  }
  function noteNode(row,level) {
    const button=document.createElement('button'); button.type='button';
    button.className=`note-list-row${selected?.id===row.id ? ' active' : ''}`;
    button.dataset.noteId=row.id; button.dataset.noteSource=row.kind;
    button.setAttribute('role','treeitem'); button.setAttribute('aria-level',level);
    button.setAttribute('aria-current',String(selected?.id===row.id));
    const title=document.createElement('strong'); title.textContent=`${row.starred ? '☆ ' : ''}${row.title}`;
    const meta=document.createElement('div'); meta.className='note-row-meta';
    const source=document.createElement('span'); source.textContent=row.kind==='course' ? `课程 · ${row.source_label}` : labels[row.kind];
    const status=document.createElement('span'); status.textContent=row.folder_candidates?.length ? '待整理' : '';
    meta.append(source,status); button.append(title,meta);
    if(el('note-search').value.trim()){const excerpt=document.createElement('div');excerpt.className='note-search-excerpt';excerpt.textContent=row.excerpt;highlight(excerpt,el('note-search').value);highlight(title,el('note-search').value);button.append(excerpt);}
    button.title=snippet(row.excerpt || '') || row.title;
    button.onclick=()=>act(()=>open(row.id));
    button.ondblclick=()=>act(async()=>{await open(row.id);if(editable()){reading=false;updateMode();el('note-title').focus();el('note-title').select();}});
    bindContext(button,()=>act(async()=>{await open(row.id);openMenu(button);}));
    button.addEventListener('keydown',event=>{
      if(event.key==='F2'){event.preventDefault();button.ondblclick();}
    });
    if(!row.deleted && (shared.enabled || row.kind!=='course')){
      button.draggable=true;
      button.ondragstart=event=>{event.stopPropagation();event.dataTransfer.setData('application/x-sea-note',row.id);event.dataTransfer.effectAllowed='move';};
    }
    return button;
  }
  function courseNode(row,level) {
    const branch=document.createElement('div');branch.className='note-tree-branch note-course-branch';branch.tabIndex=0;
    branch.dataset.treeCourseId=row.id;branch.setAttribute('role','treeitem');branch.setAttribute('aria-level',level);branch.setAttribute('aria-label',row.title);
    const isOpen=!!el('note-search').value.trim() || expanded.has(row.id);
    branch.setAttribute('aria-expanded',String(isOpen));
    const button=document.createElement('div');button.className='note-course-row';button.tabIndex=-1;button.dataset.courseId=row.course_id;
    const arrow=document.createElement('span');arrow.className='note-folder-arrow';arrow.textContent=isOpen ? '▾' : '▸';arrow.setAttribute('aria-hidden','true');
    const name=document.createElement('span');name.className='note-folder-label';name.textContent=row.title;
    const count=document.createElement('span');count.className='note-course-count';count.textContent=`${row.notes.length} 篇${row.folder_candidates?.length ? ' · 待整理' : ''}`;
    button.append(arrow,name,count);button.title='展开课次笔记；拖拽或右键移动整门课程';
    button.onclick=()=>act(async()=>{
      await flush();folder=row.folder_id;
      if(expanded.has(row.id))expanded.delete(row.id);else expanded.add(row.id);
      rememberExpanded();renderList();el('note-list').querySelector(`[data-tree-course-id="${CSS.escape(row.id)}"]`)?.focus();
    });
    button.draggable=true;
    button.ondragstart=event=>{event.stopPropagation();event.dataTransfer.setData('application/x-sea-note',row.id);event.dataTransfer.effectAllowed='move';};
    bindContext(button,()=>openFolderMenu(button,row));
    const group=document.createElement('div');group.className='note-course-children';group.setAttribute('role','group');group.hidden=!isOpen;
    row.notes.forEach(note=>group.append(noteNode(note,level+1)));
    branch.append(button,group);
    branch.addEventListener('keydown',event=>{
      if(event.target!==branch)return;
      if(['Enter',' '].includes(event.key)){event.preventDefault();button.click();}
      if(event.key==='ContextMenu' || event.shiftKey && event.key==='F10')button.oncontextmenu(event);
    });
    return branch;
  }
  function folderNode(row,level,courses) {
    const key=row.id;
    const branch=document.createElement('div'); branch.className='note-tree-branch';branch.tabIndex=0;branch.dataset.treeFolderId=key;
    branch.setAttribute('role','treeitem');branch.setAttribute('aria-label',row.name);branch.setAttribute('aria-level',level);
    const button=document.createElement('div'); button.tabIndex=-1; button.className=`note-folder-row${folder===key ? ' active' : ''}`;
    button.dataset.folderId=key; button.setAttribute('role','none');
    const searching=!!el('note-search').value.trim();
    const isOpen=searching || expanded.has(key);
    branch.setAttribute('aria-expanded',String(isOpen)); branch.setAttribute('aria-selected',String(folder===key));
    const arrow=document.createElement('span');arrow.className='note-folder-arrow';arrow.textContent=isOpen ? '▾' : '▸';arrow.setAttribute('aria-hidden','true');
    const name=document.createElement('span');name.className='note-folder-label';name.textContent=row.name;
    const count=document.createElement('span');count.className='note-folder-count';count.textContent=notes.filter(note=>note.folder_id===key && (shared.enabled || note.kind!=='course')).length+courses.filter(row=>row.folder_id===key).length || '';
    button.append(arrow,name,count);button.title=key ? `${folderPath(key)} · 双击改名，拖入笔记移动` : '未指定文件夹的笔记';
    button.onclick=event=>act(async()=>{
      await flush(); folder=key;
      if(event.target===arrow || expanded.has(key))expanded.delete(key);else expanded.add(key);
      rememberExpanded();renderList();el('note-list').querySelector(`[data-tree-folder-id="${key}"]`)?.focus();
    });
    if(key){
      button.ondblclick=()=>beginFolderEdit(row);
      button.draggable=true;button.ondragstart=event=>{event.stopPropagation();event.dataTransfer.setData('application/x-sea-folder',key);event.dataTransfer.effectAllowed='move';};
    }
    bindContext(button,()=>showMenu(button,[
      ['新建笔记',async()=>{folder=key;await create();}],
      ['新建子文件夹',()=>beginFolderEdit(null,null,key)],
      ...(key ? [['重命名文件夹',()=>beginFolderEdit(row)],['移动文件夹…',()=>openFolderMoveMenu(row,button)],['删除文件夹',()=>deleteFolder(row),true]] : []),
    ]));
    bindDrop(button,key,!!key); // The inbox is a note location; folders live at the root.
    const group=document.createElement('div');group.className='note-folder-children';group.setAttribute('role','group');group.hidden=!isOpen;
    if(key)folders.filter(item=>item.parent_id===key).forEach(item=>group.append(folderNode(item,level+1,courses)));
    courses.filter(row=>row.folder_id===key).forEach(row=>group.append(courseNode(row,level+1)));
    folderNotes(key).forEach(note=>group.append(noteNode(note,level+1)));
    branch.append(button,group);
    branch.addEventListener('keydown',event=>{
      if(event.target!==branch)return;
      if(['Enter',' '].includes(event.key)){event.preventDefault();button.click();}
      if(event.key==='ContextMenu' || event.shiftKey && event.key==='F10')button.oncontextmenu(event);
    });
    if(folderEdit?.row?.id===key) mountFolderInput(button,folderEdit);
    if(folderEdit && !folderEdit.row && folderEdit.parent===key && key) mountFolderInput(group,folderEdit,true);
    return branch;
  }
  function renderList() {
    const root=el('note-list'); const scroll=root.scrollTop; root.replaceChildren();
    const courses=courseGroups(),courseCount=courses.length,otherCount=notes.filter(row=>shared.enabled || row.kind!=='course').length;
    el('note-count').textContent=courseCount ? `${courseCount} 门课程 · ${otherCount} 篇笔记` : `${otherCount} 篇笔记`;
    if(filter==='trash') notes.forEach(row=>root.append(noteNode(row,1)));
    else {
      root.append(folderNode({id:'',name:'收件箱'},1,courses));
      folders.filter(row=>!row.parent_id).forEach(row=>root.append(folderNode(row,1,courses)));
      if(folderEdit && !folderEdit.row && !folderEdit.parent)mountFolderInput(root,folderEdit,true);
    }
    if(!notes.length){const empty=document.createElement('p');empty.className='note-list-empty';empty.textContent=el('note-search').value.trim() ? '没有找到相关笔记' : filter==='trash' ? '笔记回收站是空的' : filter==='starred' ? '还没有收藏的笔记' : '新建笔记，或拖入 Markdown 文件';root.append(empty);}
    root.scrollTop=scroll;
  }
  function renderNoteFolder() {
    const root=el('note-folder-path');root.replaceChildren();if(!selected)return;
    const location=createButton(`${folderPath(selected.folder_id)}${isCourse(selected) ? ` / ${selected.source_label}` : ''} ▾`,event=>openFolderMenu(event.currentTarget),'text-button');
    location.disabled=!!selected.deleted;location.setAttribute('aria-label',`${isCourse(selected) ? '移动整门课程' : '移动笔记'}，当前位置：${folderPath(selected.folder_id)}`);root.append(location);
    if(selected.folder_candidates?.length){
      const hint=document.createElement('span');hint.className='note-folder-pending';
      hint.textContent=`${isCourse(selected) ? '整门课程待整理' : '待整理'} · 可选位置：${selected.folder_candidates.map(folderPath).join('、')}`;
      root.append(hint);
    }
  }
  async function load() {
    const id = ++loadId;activeLoads++;
    try {
    const params = new URLSearchParams({q:el('note-search').value.trim(), kind, starred:String(filter==='starred'), trash:String(filter==='trash')});
    const result = await library(`?${params}`);
    if (id !== loadId) return;
    notes=result.notes; folders=result.folders;
    shared=result.shared || {enabled:false};renderSharedStatus();
    if (folder && !folders.some(row=>row.id===folder)) { folder=''; return load(); }
    syncFilters(); renderNoteFolder(); renderList();
    } finally {activeLoads--;}
  }
  async function refreshSelectedMetadata() {
    if (!selected || dirty) return;
    try {
      const row = await library(sourcePath(selected));
      if (selected?.id !== row.id) return;
      // Adopt the content together with its revision. Updating only the revision
      // would let a stale editor overwrite another window's changes unnoticed.
      if (dirty) return;
      if (!isPersonal(row) && versionKey !== 'active') {
        const previous=selected.versions.find(item=>item.key===versionKey);
        versionKey=row.versions.find(item=>item.summary===previous?.summary && item.model===previous?.model)?.key || 'active';
      }
      if (revisionOf(row)!==revisionOf(selected)) {
        el('note-title').value=row.title;
        el('note-body').value=row.body;
        el('note-date').textContent=`${row.kind==='course' ? `课程 · ${row.source_label}` : row.kind==='talk' ? '讲座笔记' : '个人笔记'} · 更新于 ${formatDate(row.updated_at)}`;
        resetUndo();
      }
      selected = row;
      el('note-save-state').textContent=row.deleted ? '已移入回收站' : isPersonal(row) ? '已保存' : row.manually_edited ? '手动修订已保存' : '与来源同步';
      el('note-title').readOnly=!!row.deleted;
      el('note-body').readOnly=!!row.deleted;
      if (row.deleted) reading=true;
      if (!isPersonal(row)) fillSourceControls();
      updateMode();
      updateStar(); renderNoteFolder();
    } catch (error) {
      if (error.message.includes('不存在')) { clearSelection(); await load(); }
      else throw error;
    }
  }
  function currentBody() {
    if (!selected) return '';
    if (isPersonal(selected) || contentTab==='summary' && versionKey==='active') return el('note-body').value;
    return contentTab === 'transcript' ? selected.transcript : selected.versions.find(row=>row.key===versionKey)?.summary || selected.body;
  }
  function updateMode() {
    const personal = isPersonal(selected);
    if (!editable()) reading=true;
    el('note-mode').classList.remove('hidden');
    el('note-mode').textContent = reading ? '编辑' : '阅读';
    el('note-mode').disabled = !editable();
    el('note-title').readOnly = !editable() || reading;
    el('note-body').readOnly = !editable();
    el('note-body').classList.toggle('hidden', reading);
    el('note-preview').classList.toggle('hidden', !reading);
    el('note-source-controls').classList.toggle('hidden', personal);
    el('note-preview').classList.toggle('transcript', !personal && contentTab==='transcript');
    if (reading) {
      const root=el('note-preview');
      if (!personal && contentTab==='transcript') root.textContent=currentBody() || '暂无转写';
      else {
        root.innerHTML = currentBody().trim() ? renderSharedMarkdown(currentBody()) : '<p class="meta">还没有内容</p>';
        activateMath(root);hydrateShared(root);
      }
    } else resizeBody();
    updateOutline(); updateFind(false); updateUndo();
    if (!personal) {
      el('note-source-tab').textContent=contentTab==='transcript' ? '返回笔记' : '查看转写';
      el('note-version-label').classList.toggle('hidden', contentTab==='transcript');
    }
  }
  function updateStar() {
    el('note-star').textContent = selected?.starred ? '★' : '☆';
    el('note-star').setAttribute('aria-label', selected?.starred ? '取消收藏笔记' : '收藏笔记');
    el('note-star').disabled = !!selected?.deleted;
  }
  function fillSourceControls() {
    if (isPersonal(selected)) return;
    el('note-source-link').textContent=selected.kind==='course' ? '返回课程来源 →' : '返回讲座来源 →';
    el('note-source-tab').disabled=!selected.transcript;
    el('note-source-tab').title=selected.transcript ? '' : '来源尚无转写文本';
    const select=el('note-version'); select.replaceChildren();
    selected.versions.forEach(row => {
      const option=document.createElement('option'); option.value=row.key;
      option.textContent=`${row.active ? '当前 · ' : ''}${row.model || '未记录模型'} · ${formatDate(row.generated_at)}`;
      select.append(option);
    });
    if (!selected.versions.some(row=>row.key===versionKey)) versionKey='active';
    select.value=versionKey;
  }
  function clearSelection() {
    selected=null; dirty=false;
    el('note-editor').classList.add('hidden'); el('note-welcome').classList.remove('hidden'); el('view-notes').classList.remove('note-open');
    updateOutline();updateFind(false);updateUndo();
    try { localStorage.removeItem('sea-last-note'); } catch (_) {}
  }
  async function open(key, options = {}) {
    const id=++openId;
    await flush();
    if (!key.includes(':')) key=`personal:${key}`;
    const split=key.indexOf(':');
    const path=`/${key.slice(0,split)}/${encodeURIComponent(key.slice(split+1))}`;
    let row=await library(path);
    if (id!==openId) return;
    if (dirty) { await flush(); row=await library(path); }
    if (id!==openId) return;
    selected=row; dirty=false; versionKey='active'; contentTab='summary';pendingMerge=null;el('note-conflict-review').classList.add('hidden');
    el('note-title').value=['未命名手记','未命名笔记'].includes(row.title) && isPersonal(row) ? '' : row.title;
    el('note-body').value=row.body;
    el('note-title').readOnly=!!row.deleted;
    el('note-body').readOnly=!!row.deleted;
    el('note-date').textContent=`${row.kind==='course' ? `课程 · ${row.source_label}` : row.kind==='talk' ? '讲座笔记' : '个人笔记'} · 更新于 ${formatDate(row.updated_at)}`;
    el('note-save-state').textContent=row.deleted ? '已移入回收站' : isPersonal(row) ? '已保存' : row.manually_edited ? '手动修订已保存' : '与来源同步';
    {
      try {
        const draft=JSON.parse(localStorage.getItem(draftKey(row)));
        if (draft && !row.deleted) {
          el('note-title').value=draft.title; el('note-body').value=draft.body; dirty=true;
          el('note-save-state').textContent='已恢复未保存草稿';
          if (draft.revision===revisionOf(row)) saveTimer=setTimeout(()=>act(flush),1000);
          else { if(isPersonal(row))selected.revision=draft.revision;else selected.edit_revision=draft.revision;failedSave(true); }
        }
      } catch (_) {}
    }
    el('note-history').classList.add('hidden'); el('note-history').open=false;
    el('note-editor').classList.remove('hidden'); el('note-welcome').classList.add('hidden'); el('view-notes').classList.add('note-open');
    reading=options.reading ?? true;
    if (row.deleted) reading=true;
    if (dirty) reading=false;
    folder=row.folder_id;revealFolder(folder);
    if(row.kind==='course'){expanded.add(row.location_reference);rememberExpanded();}
    resetUndo(); updateStar(); fillSourceControls(); updateMode(); renderNoteFolder(); renderList();
    try { localStorage.setItem('sea-last-note',key); } catch (_) {}
    window.scrollTo(0,0);
    const query=options.query ?? el('note-search').value.trim();
    el('note-find').value=query;
    el('note-find-bar').classList.toggle('hidden',!query);
    updateFind(!!query);
  }
  async function create(title='',body='') {
    await flush(); const activeFolder=folder;
    const row=await request('',json('POST',{title,body}));
    if (activeFolder) await library(`/personal/${row.id}/folder`,json('PUT',{folder_id:activeFolder,revision:0}));
    resetFilters(); folder=activeFolder; await load(); await open(`personal:${row.id}`,{reading:false}); changed(); el('note-title').focus();
  }
  async function flag(name,value) {
    await flush();
    const row=await request(`/${selected.source_id}`,json('PUT',{title:el('note-title').value,body:el('note-body').value,revision:selected.revision,[name]:value}));
    selected={...selected,...row,id:selected.id,source_id:row.id}; changed();
    return selected;
  }
  function download(data,filename,type='text/markdown;charset=utf-8') {
    const url=URL.createObjectURL(data instanceof Blob ? data : new Blob([data],{type}));
    const link=document.createElement('a'); link.href=url; link.download=filename; document.body.append(link); link.click(); link.remove();
    setTimeout(()=>URL.revokeObjectURL(url),60000);
  }
  function exportNote() {
    const title=el('note-title').value || '未命名笔记';
    download(`# ${title}\n\n${currentBody()}\n`,`${title.replace(/[/\\:*?"<>|]/g,'_')}${contentTab==='transcript' ? '-转写' : ''}.md`);
  }
  async function remove() {
    const row=await flag('deleted',true); clearSelection(); await load();
    undoMessage('笔记已移入回收站', async () => {
      const current=await request(`/${row.source_id}`);
      await request(`/${row.source_id}`,json('PUT',{...current,deleted:false}));
      resetFilters(); await load(); await open(row.id); changed(); message('已恢复笔记');
    });
  }
  async function history() {
    await flush(); const target=selected.id;
    const rows=isPersonal(selected) ? await request(`/${selected.source_id}/versions`) : [
      ...await library(`${sourcePath(selected)}/versions`),
      ...selected.versions.filter(row=>!row.active).map(row=>({title:selected.title,body:row.summary,saved_at:row.generated_at,label:row.model}))
    ];
    if (target!==selected?.id) return;
    const root=el('note-history-list'); root.replaceChildren();
    if (!rows.length) root.textContent='还没有历史版本';
    rows.forEach(row => {
      const item=document.createElement('div'); item.className='note-history-row';
      const text=document.createElement('div');
      const title=document.createElement('strong'); title.textContent=row.title;
      const date=document.createElement('p'); date.textContent=`${formatDate(row.saved_at)} · ${row.body.length} 字${row.label ? ` · ${row.label}` : ''}`;
      const restore=createButton('恢复此版本',()=>act(async()=>{
        if (selected?.id!==target) return;
        await flush(); el('note-title').value=row.title; el('note-body').value=row.body; versionKey='active'; contentTab='summary'; recordEdit(); markDirty(); await flush();
        reading=false; updateMode(); el('note-history').classList.add('hidden'); message('已恢复版本，原内容保存在历史版本中');
      }));
      text.append(title,date); item.append(text,restore); root.append(item);
    });
    el('note-history').classList.remove('hidden'); el('note-history').open=true;
    el('note-history').scrollIntoView({block:'nearest',behavior:'smooth'});
  }
  function closeMenu() { if (menu) { menu.remove(); menu=null; } }
  function showMenu(anchor,items) {
    closeMenu(); menu=document.createElement('div'); menu.className='course-context-menu note-context-menu'; menu.setAttribute('role','menu');
    items.forEach(([label,action,danger=false])=>{
      const button=createButton(label,()=>{closeMenu();act(action);},danger ? 'danger' : ''); button.setAttribute('role','menuitem'); menu.append(button);
    });
    document.body.append(menu); const rect=anchor.getBoundingClientRect();
    menu.style.left=`${Math.min(Math.max(8,rect.right-210),innerWidth-218)}px`;
    menu.style.top=`${Math.max(8,Math.min(rect.bottom+5,innerHeight-menu.offsetHeight-8))}px`;
    menu.firstElementChild?.focus();
    menu.onkeydown=event=>{
      const buttons=[...menu.querySelectorAll('button')], index=buttons.indexOf(document.activeElement);
      if (event.key==='Escape') { event.preventDefault(); closeMenu(); anchor.focus(); }
      if (['ArrowDown','ArrowUp'].includes(event.key)) { event.preventDefault(); buttons[(index+(event.key==='ArrowDown' ? 1 : -1)+buttons.length)%buttons.length].focus(); }
    };
  }
  function openMenu(anchor=el('note-more')) {
    if (!selected) return;
    const items=[['导出 Markdown',exportNote]];
    if(selected.obsidian_url)items.unshift(['在 Obsidian 中打开',async()=>{await flush();const row=await library(sourcePath(selected));location.href=row.obsidian_url;}]);
    if(shared.conflicts?.some(item=>item.id===selected.id))items.unshift(['比较并合并',reviewConflict]);
    if(!selected.deleted)items.push(['历史版本',history]);
    if (!selected.deleted) items.push([isCourse(selected) ? '移动整门课程…' : '移动到文件夹…',()=>openFolderMenu(anchor)]);
    if (shared.enabled) {
      if(selected.deleted)items.push(['恢复笔记',async()=>{await api('/api/local/shared-notes/restore',json('POST',{note_id:selected.id}));resetFilters();await load();await open(selected.id);}]);
      else items.push(['移入回收站',async()=>{await flush();const key=selected.id;await api('/api/local/shared-notes/trash',json('POST',{note_id:key}));clearSelection();await load();undoMessage('已移入回收站',async()=>{await api('/api/local/shared-notes/restore',json('POST',{note_id:key}));await load();await open(key);});},true]);
    } else if (isPersonal(selected)) {
      if (selected.deleted) items.push(['恢复笔记',async()=>{const row=await flag('deleted',false);resetFilters();await load();await open(row.id);}]);
      else items.push(['移入回收站',remove,true]);
    } else items.push(['查看来源',goSource]);
    showMenu(anchor,items);
  }
  function folderChoices() {
    return [...folders].sort((a,b)=>folderPath(a.id).localeCompare(folderPath(b.id),'zh-CN'));
  }
  function openFolderMenu(anchor,row=selected) {
    if(!row || row.deleted)return;
    const note=row.kind==='course' && !shared.enabled ? courseEntry(row) : row;
    showMenu(anchor,[['收件箱',()=>moveNote(note,'')],...folderChoices().map(row=>[`${note.folder_id===row.id ? '✓ ' : ''}${folderPath(row.id)}`,()=>moveNote(note,row.id)]),['新建文件夹…',()=>beginFolderEdit(null,note,'')]]);
  }
  async function moveNote(note,target) {
    await flush();
    const previous=note.folder_id || '',candidates=note.folder_candidates || [];
    const result=await library(`${sourcePath(note)}/folder`,json('PUT',{folder_id:target,revision:note.location_revision}));
    if(!result.location_changed){message('已在此文件夹');return;}
    if(selected?.id===note.id)selected=result;
    folder=target;revealFolder(target);await load();await refreshSelectedMetadata();changed();
    undoMessage(`${isCourse(note) ? '整门课程已移至' : '已移至'}${folderPath(target)}`,async()=>{
      const restored=await library(`${sourcePath(note)}/folder`,json('PUT',{folder_id:previous,revision:result.location_revision,candidates}));
      if(selected?.id===note.id)selected=restored;
      folder=previous;revealFolder(previous);await load();await refreshSelectedMetadata();changed();
    });
  }
  function openFolderMoveMenu(row,anchor) {
    const excluded=new Set([row.id]);
    for(let size=-1;size!==excluded.size;){size=excluded.size;folders.forEach(item=>{if(excluded.has(item.parent_id))excluded.add(item.id);});}
    showMenu(anchor,[['根目录',()=>moveFolder(row,'')],...folderChoices().filter(item=>!excluded.has(item.id)).map(item=>[folderPath(item.id),()=>moveFolder(row,item.id)])]);
  }
  async function moveFolder(row,parent) {
    if(row.parent_id===parent){message('已在此位置');return;}
    await folderRequest(`/${row.id}/move`,json('PUT',{parent_id:parent,expected_parent:row.parent_id}));
    revealFolder(parent);await load();changed();
    undoMessage('已移动文件夹',async()=>{await folderRequest(`/${row.id}/move`,json('PUT',{parent_id:row.parent_id,expected_parent:parent}));revealFolder(row.parent_id);await load();changed();});
  }
  async function deleteFolder(row) {
    await flush();const snapshot=await folderRequest(`/${row.id}`,{method:'DELETE'});
    if(snapshot.folders.some(item=>item.id===folder))folder='';
    revealFolder('');await load();await refreshSelectedMetadata();changed();
    undoMessage('文件夹已删除，其中笔记已移回收件箱',async()=>{await folderRequest('/restore',json('POST',snapshot));await load();await refreshSelectedMetadata();changed();});
  }
  function beginFolderEdit(row=null,assignTo=null,parent=folder) {
    filter='all';syncFilters();
    folderEdit={row,assignTo,parent:row?.parent_id ?? parent,value:row?.name || ''};
    revealFolder(parent);renderList();
    const input=el('note-folder-name');input?.focus();input?.select();input?.scrollIntoView({block:'nearest'});
  }
  function mountFolderInput(anchor,editing,append=false) {
    const input=document.createElement('input');input.id='note-folder-name';input.className='note-folder-name';input.maxLength=60;
    input.setAttribute('aria-label','文件夹名称');input.placeholder='文件夹名称';input.value=editing.value;
    if(append)anchor.append(input);else anchor.replaceWith(input);
    input.oninput=()=>{editing.value=input.value;};
    input.onkeydown=event=>{
      event.stopPropagation();
      if(event.key==='Enter'){event.preventDefault();act(finishFolderEdit);}
      if(event.key==='Escape'){event.preventDefault();folderEdit=null;renderList();el('note-list').querySelector(`[data-tree-folder-id="${folder}"]`)?.focus();}
    };
    input.onblur=()=>{if(folderEdit===editing)act(finishFolderEdit);};
  }
  async function finishFolderEdit() {
    if(!folderEdit || folderEdit.busy)return;
    const editing=folderEdit,name=editing.value.trim();
    if(!name){folderEdit=null;renderList();return;}
    editing.busy=true;
    try{
      const result=await folderRequest(editing.row ? `/${editing.row.id}` : '',json(editing.row ? 'PUT' : 'POST',{name,parent_id:editing.parent}));
      folderEdit=null;folder=result.id;revealFolder(result.id);await load();changed();
      if(editing.assignTo)await moveNote(editing.assignTo,result.id);
    }catch(error){editing.busy=false;el('note-folder-name')?.focus();throw error;}
  }
  async function goSource() {
    await flush();
    if (selected.kind==='course') await openLecture(selected.source_id,{tab:contentTab});
    else await openTalk(selected.source_id,{tab:contentTab});
  }
  async function importFiles(files) {
    await flush();
    const documents=files.filter(file=>/\.(md|markdown|txt)$/i.test(file.name));
    if (!documents.length) throw new Error('请选择 Markdown 或文本文件；引用的图片可以同时选入');
    const images=files.filter(file=>/\.(png|jpe?g|gif|webp)$/i.test(file.name));
    const dataUrls=new Map();
    const normalize=value=>{try{return decodeURIComponent(value).replace(/\\/g,'/').replace(/^(\.\/)+/,'');}catch(_){return value;}};
    const imageData=async file=>{
      if (dataUrls.has(file)) return dataUrls.get(file);
      if (file.size>1_400_000) throw new Error(`图片 ${file.name} 过大，请缩小后再导入（单篇笔记含图片最多 200 万字）`);
      const value=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=()=>reject(new Error(`无法读取 ${file.name}`));reader.readAsDataURL(file);});
      const extension=file.name.split('.').pop().toLowerCase();
      const mime={png:'png',jpg:'jpeg',jpeg:'jpeg',gif:'gif',webp:'webp'}[extension];
      const bitmap=value.replace(/^data:[^;]*;base64,/,`data:image/${mime};base64,`);
      dataUrls.set(file,bitmap); return bitmap;
    };
    let missing=0;
    const prepared=[];
    for (const file of documents) {
      if (file.size>2_000_000) throw new Error(`${file.name} 超过 2 MB`);
      let text=(await file.text()).replace(/^\uFEFF/,'').replace(/\r\n?/g,'\n');
      // Leave Markdown examples in code untouched while embedding companion images.
      const pieces=text.split(/(`{3,}[^\n]*\n[\s\S]*?\n`{3,}|~{3,}[^\n]*\n[\s\S]*?\n~{3,}|`+[^`\n]*`+)/g);
      for (let index=0;index<pieces.length;index+=2) {
        const expression=/!\[[^\]\n]*\]\(\s*(?:<([^>]+)>|([^\s)]+))(?:\s+["'][^"']*["'])?\s*\)/g;
        const matches=[...pieces[index].matchAll(expression)];
        for (const match of matches.reverse()) {
          const url=match[1] || match[2];
          if (/^(?:[a-z][a-z0-9+.-]*:|\/\/|#)/i.test(url)) continue;
          const path=normalize(url), basename=path.split('/').pop();
          const exact=images.filter(image=>normalize(image.webkitRelativePath || image.name)===path);
          const candidates=exact.length ? exact : images.filter(image=>image.name===basename);
          if (candidates.length!==1) { missing++; continue; }
          const data=await imageData(candidates[0]);
          const replacement=match[0].replace(url,data);
          pieces[index]=pieces[index].slice(0,match.index)+replacement+pieces[index].slice(match.index+match[0].length);
        }
      }
      text=pieces.join('');
      const heading=/^#\s+(.+?)\s*#*\s*(?:\n|$)/.exec(text);
      const body=heading ? text.slice(heading[0].length).replace(/^\s*\n/,'') : text;
      if (body.length>2_000_000) throw new Error(`${file.name} 连同图片超过笔记容量，请缩小图片后再导入`);
      prepared.push({title:heading?.[1] || file.name.replace(/\.[^.]+$/,''),body});
    }
    const activeFolder=folder; let last, imported=0;
    try {
      for (const item of prepared) {
        last=await request('',json('POST',item)); imported++; changed();
        if (activeFolder) await library(`/personal/${last.id}/folder`,json('PUT',{folder_id:activeFolder,revision:0}));
      }
    } catch(error) { if (imported) { await load(); message(`已导入 ${imported} 篇，后续导入失败：${error.message}`,true); } throw error; }
    resetFilters(); folder=activeFolder; await load(); if(last) await open(`personal:${last.id}`,{reading:true});
    message(`已导入 ${imported} 篇笔记${missing ? `；${missing} 张相对路径图片未附带，可同时选择 Markdown 和引用图片重新导入` : ''}`);
  }

  for(const node of [el('note-title'),el('note-body')]){
    node.oninput=event=>{if(!event.isComposing){recordEdit();markDirty();}};
    node.oncompositionstart=()=>clearTimeout(saveTimer);
    node.oncompositionend=()=>{recordEdit();markDirty();};
  }
  el('note-undo').onclick=()=>undoEdit(-1);el('note-redo').onclick=()=>undoEdit(1);
  el('note-preview').ondblclick=()=>{if(editable()){reading=false;updateMode();el('note-body').focus({preventScroll:true});}};
  el('note-title').ondblclick=()=>{if(editable()){reading=false;updateMode();el('note-title').focus();}};
  el('note-find-toggle').onclick=()=>{el('note-find-bar').classList.remove('hidden');el('note-find').focus();el('note-find').select();};
  el('note-find').oninput=()=>updateFind(true);
  el('note-find-prev').onclick=()=>stepFind(-1);el('note-find-next').onclick=()=>stepFind(1);
  el('note-find-close').onclick=()=>{el('note-find-bar').classList.add('hidden');el('note-find').value='';updateFind(false);(reading ? el('note-preview') : el('note-body')).focus({preventScroll:true});};
  el('note-find').onkeydown=event=>{if(event.key==='Enter'){event.preventDefault();stepFind(event.shiftKey ? -1 : 1);}if(event.key==='Escape'){event.preventDefault();el('note-find-close').click();}};
  el('note-new').onclick=el('note-first').onclick=()=>act(()=>create());
  el('note-search').oninput=()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>act(async()=>{await flush();await load();}),180);};
  el('note-mode').onclick=()=>act(async()=>{await flush();reading=!reading;updateMode();});
  el('note-star').onclick=()=>act(async()=>{
    await flush(); const row=await library(`${sourcePath(selected)}/star`,json('PUT',{value:!selected.starred}));
    selected=row; updateStar(); await load(); changed();
  });
  el('note-more').onclick=()=>openMenu();
  el('note-index-back').onclick=()=>act(async()=>{await flush();el('view-notes').classList.remove('note-open');});
  el('note-source-link').onclick=()=>act(goSource);
  el('note-source-tab').onclick=()=>act(async()=>{await flush();contentTab=contentTab==='summary' ? 'transcript' : 'summary';updateMode();});
  el('note-version').onchange=()=>act(async()=>{await flush();versionKey=el('note-version').value;updateMode();});
  el('note-folder-new').onclick=()=>beginFolderEdit();
  document.querySelectorAll('[data-note-kind]').forEach(node=>node.onclick=()=>act(async()=>{await flush();kind=node.dataset.noteKind;if(filter==='trash' && kind!=='personal' && kind!=='all')filter='all';syncFilters();await load();}));
  document.querySelectorAll('[data-note-filter]').forEach(node=>node.onclick=()=>act(async()=>{await flush();filter=node.dataset.noteFilter;if(filter==='trash')kind=shared.enabled ? 'all' : 'personal';syncFilters();await load();}));
  bindDrop(document.querySelector('.note-folders-heading'),'',true);
  el('note-list').addEventListener('keydown',event=>{
    if(event.target.tagName==='INPUT')return;
    const current=event.target.closest('[role="treeitem"]');if(!current)return;
    const visible=[...el('note-list').querySelectorAll('[role="treeitem"]')].filter(node=>node.getClientRects().length);
    const index=visible.indexOf(current),folderKey=current.dataset.treeFolderId,key=folderKey ?? current.dataset.treeCourseId;
    if(['ArrowDown','ArrowUp','Home','End'].includes(event.key)){
      event.preventDefault();const next=event.key==='Home' ? 0 : event.key==='End' ? visible.length-1 : Math.max(0,Math.min(visible.length-1,index+(event.key==='ArrowDown' ? 1 : -1)));visible[next]?.focus();
    }
    if(key!==undefined && ['ArrowRight','ArrowLeft'].includes(event.key)){
      event.preventDefault();
      if(event.key==='ArrowRight' && !expanded.has(key)){expanded.add(key);renderList();}
      else if(event.key==='ArrowLeft' && expanded.has(key)){expanded.delete(key);renderList();}
      else if(event.key==='ArrowRight'){visible[index+1]?.focus();return;}
      else {current.closest('[role="group"]')?.previousElementSibling?.parentElement?.focus();return;}
      rememberExpanded();el('note-list').querySelector(`[data-tree-folder-id="${CSS.escape(key)}"],[data-tree-course-id="${CSS.escape(key)}"]`)?.focus();
    }else if(event.key==='ArrowLeft')current.closest('[role="group"]')?.previousElementSibling?.parentElement?.focus();
    if(event.key==='F2' && folderKey){event.preventDefault();beginFolderEdit(folders.find(row=>row.id===folderKey));}
  });
  el('note-import').onclick=()=>el('note-import-files').click();
  el('note-import-files').onchange=()=>act(async()=>{try{await importFiles([...el('note-import-files').files]);}finally{el('note-import-files').value='';}});
  el('note-list').addEventListener('dragover',event=>{if([...event.dataTransfer.types].includes('Files')){event.preventDefault();el('note-list').classList.add('drop-target');}});
  el('note-list').addEventListener('dragleave',()=>el('note-list').classList.remove('drop-target'));
  el('note-list').addEventListener('drop',event=>{if(!event.dataTransfer.files.length)return;event.preventDefault();el('note-list').classList.remove('drop-target');act(()=>importFiles([...event.dataTransfer.files]));});
  el('notebook-backup').onclick=()=>act(async()=>{
    await flush(); const response=await fetch('/api/local/notebook/backup');
    if(!response.ok)throw new Error('备份未完成，请重试');
    download(await response.blob(),`丰饶之海-笔记-${new Date().toLocaleDateString('sv-SE')}.zip`);
    el('notebook-backup-status').textContent='备份已生成，包含个人笔记、手动修订、回收站、历史版本、文件夹与收藏。';
  });
  el('notebook-restore').onclick=()=>el('notebook-restore-file').click();
  el('notebook-restore-file').onchange=()=>act(async()=>{
    await flush();const file=el('notebook-restore-file').files[0];if(!file)return;
    const data=new FormData();data.append('file',file);
    const response=await fetch('/api/local/notebook/restore',{method:'POST',body:data});
    const result=await response.json(); if(!response.ok)throw new Error(result.detail || '导入失败');
    await load(); await refreshSelectedMetadata(); changed();
    el('notebook-backup-status').textContent=`已导入 ${result.imported} 篇笔记或修订及文件夹归类，已有内容已保留。`;el('notebook-restore-file').value='';
  });
  el('open-personal-library').onclick=()=>act(async()=>{localStorage.setItem('sea-personal-library','1');setupRequested=false;activeView='notes';await refreshStatus();await load();});
  el('library-home').onclick=()=>{showView('notes');act(load);};
  document.addEventListener('pointerdown',event=>{if(menu && !menu.contains(event.target) && !event.target.closest('#note-more'))closeMenu();});
  document.addEventListener('keydown',event=>{
    const command=event.metaKey || event.ctrlKey;
    if(command && event.key.toLowerCase()==='n' && !document.querySelector('dialog[open]')){event.preventDefault();showView('notes');act(()=>create());}
    if(command && event.key.toLowerCase()==='s' && selected && activeView==='notes'){event.preventDefault();act(flush);}
    if(command && event.key.toLowerCase()==='e' && selected && activeView==='notes'){event.preventDefault();el('note-mode').click();}
    if(command && event.key.toLowerCase()==='k'){event.preventDefault();showView('search');el('search').focus();}
    if(command && event.key.toLowerCase()==='f' && selected && activeView==='notes'){event.preventDefault();el('note-find-toggle').click();}
    if(command && event.key.toLowerCase()==='z' && selected && activeView==='notes' && !reading && [el('note-title'),el('note-body')].includes(event.target)){event.preventDefault();undoEdit(event.shiftKey ? 1 : -1);}
    if(event.key==='Escape'){closeMenu();document.body.classList.remove('mobile-outline-open');}
  });
  // Preserve the source pages' transcript and version controls, with a direct
  // route back to this same note rather than creating a second notebook record.
  const sourceButtons={};
  for(const [view,selector] of [['detail','#view-detail .heading-actions'],['talkDetail','#view-talk-detail .heading-actions']]){
    const button=createButton('在笔记中打开',()=>act(async()=>{
      const key=view==='detail' ? `course:${currentLecture.sub_id}` : `talk:${currentTalk.id}`;
      showView('notes');await open(key);await load();
    }));
    button.dataset.capability='notes';document.querySelector(selector).prepend(button);sourceButtons[view]=button;
  }
  window.addEventListener('sea-view',event=>{
    closeMenu(); if(event.detail.changed && dirty)act(flush);
    if(event.detail.view==='notes' && event.detail.changed)act(async()=>{await load();await refreshSelectedMetadata();});
    sourceButtons.detail.classList.toggle('hidden',!currentLecture?.summary?.trim());
    sourceButtons.talkDetail.classList.toggle('hidden',!currentTalk?.summary?.trim());
  });
  window.addEventListener('beforeunload',event=>{keepDraft();if(dirty){event.preventDefault();event.returnValue='';}});
  document.addEventListener('visibilitychange',()=>{if(document.hidden && dirty)act(flush);});
  window.addEventListener('resize',resizeBody);
  window.addEventListener('sea-panel-resize',resizeBody);
  window.addEventListener('icourse-render-ready',()=>{if(selected && reading)updateMode();});
  window.addEventListener('storage',event=>{if(event.key==='sea-notes-changed' && activeView==='notes' && !folderEdit)act(async()=>{await load();if(!dirty)await refreshSelectedMetadata();});});
  window.addEventListener('sea-course-data-deleted',()=>{if(activeView==='notes')act(async()=>{await load();await refreshSelectedMetadata();});});
  setInterval(()=>{if(activeView==='notes' && !document.hidden && !dirty && !folderEdit && !activeLoads && !polling){polling=true;act(async()=>{try{await load();if(selected)await refreshSelectedMetadata();}finally{polling=false;}});}},2500);
  async function search(query,coursesOnly=false){
    const id=++searchId,root=el('search-personal-results');root.replaceChildren();root.classList.toggle('hidden',!query || coursesOnly);
    if(!query || coursesOnly)return;
    try{
      const result=await library(`?q=${encodeURIComponent(query)}`);if(id!==searchId)return;
      const rows=result.notes.filter(row=>row.kind!=='course' || !statusState?.configured);
      const heading=document.createElement('p');heading.className='meta';heading.textContent=`笔记 · ${rows.length} 篇`;root.append(heading);
      rows.slice(0,50).forEach(row=>{
        const button=document.createElement('button');button.type='button';button.className='personal-search-card';
        const title=document.createElement('strong');title.textContent=`${labels[row.kind]} · ${row.title}`;
        const text=document.createElement('p');text.textContent=row.excerpt;button.append(title,text);
        highlight(title,query);highlight(text,query);button.onclick=()=>act(()=>window.SeaNotebook.open(row.id,{query}));root.append(button);
      });
    }catch(error){if(id===searchId)root.textContent='笔记搜索暂不可用';}
  }
  window.SeaNotebook={load:()=>act(load),flush,search,open:async (key,options={})=>{showView('notes');await open(key,options);}};
  act(async()=>{
    await load();const last=localStorage.getItem('sea-last-note');
    if(last){const key=last.includes(':') ? last : `personal:${last}`;if(notes.some(row=>row.id===key))await open(key);}
  });
})();
