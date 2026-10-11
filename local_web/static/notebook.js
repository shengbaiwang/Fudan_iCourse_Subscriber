(() => {
  const el = id => document.getElementById(id);
  let notes = [], selected = null, filter = 'all', reading = false, dirty = false;
  let saving = null, saveTimer = null, searchTimer = null, menu = null, loadId = 0;
  const draftKey = id => `sea-note-draft:${id}`;
  const formatDate = date => new Date(date).toLocaleDateString('zh-CN', {year:'numeric',month:'long',day:'numeric'});
  const act = task => Promise.resolve().then(task).catch(error => message(error.message, true));
  const request = (path, options = {}) => api(`/api/local/notebook${path}`, options);
  const snippet = body => body.replace(/[#*`>_]/g, '').replace(/\s+/g, ' ').slice(0,120);

  function keepDraft() {
    if (!selected || !dirty) return;
    try { localStorage.setItem(draftKey(selected.id), JSON.stringify({title:el('note-title').value, body:el('note-body').value, revision:selected.revision})); }
    catch (_) { el('note-save-state').textContent = '请保持窗口打开，正在保存'; }
  }
  function resizeBody() {
    const node = el('note-body');
    node.style.height = 'auto';
    node.style.height = `${Math.max(node.scrollHeight,innerHeight * .45)}px`;
  }
  function markDirty() {
    dirty = true;
    el('note-save-state').textContent = '正在保存…';
    keepDraft();
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => act(flush), 650);
    resizeBody();
  }
  function failedSave(conflict = false) {
    el('note-save-state').textContent = conflict ? '版本冲突 · 草稿已保留' : '未保存 · 草稿已保留';
    const retry = createButton(conflict ? '保留为新手记' : '重试', () => act(async () => {
      if (!conflict) return flush();
      const previousId = selected.id;
      const row = await request('',{method:'POST',body:JSON.stringify({title:el('note-title').value,body:el('note-body').value})});
      localStorage.removeItem(draftKey(previousId)); dirty=false; selected=null;
      filter='all'; syncFilters(); await load(); await open(row.id); message('已保存为新手记，另一窗口的版本已保留');
    }), 'text-button');
    el('note-save-state').append(retry);
  }
  async function flush() {
    clearTimeout(saveTimer);
    if (saving) await saving;
    if (!dirty || !selected) return;
    const row = selected;
    const snapshot = {title:el('note-title').value, body:el('note-body').value, revision:row.revision};
    keepDraft();
    saving = request(`/${row.id}`, {method:'PUT',body:JSON.stringify(snapshot)});
    try {
      const result = await saving;
      if (selected?.id === row.id) {
        selected = result;
        dirty = snapshot.title !== el('note-title').value || snapshot.body !== el('note-body').value;
        if (!dirty) {
          localStorage.removeItem(draftKey(row.id));
          el('note-save-state').textContent = '已保存';
          el('note-date').textContent = `更新于 ${formatDate(result.updated_at)}`;
        } else keepDraft();
      }
      notes = notes.map(note => note.id === row.id ? result : note);
      renderList();
    } catch (error) {
      failedSave(error.message.includes('另一窗口'));
      throw error;
    } finally { saving = null; }
    if (dirty) await flush();
  }
  function renderList() {
    const root = el('note-list');
    root.replaceChildren();
    const terms = el('note-search').value.toLowerCase().trim().split(/\s+/).filter(Boolean);
    const rows = notes.filter(row => (filter !== 'starred' || row.starred) && terms.every(term => (row.title + row.body).toLowerCase().includes(term)));
    el('note-count').textContent = `${rows.length} 篇手记`;
    if (!rows.length) {
      const empty = document.createElement('p');
      empty.className = 'note-list-empty';
      empty.textContent = terms.length ? '没有找到相关手记' : filter === 'trash' ? '回收站是空的' : filter === 'starred' ? '还没有收藏的手记' : '从第一篇手记开始';
      root.append(empty);
    }
    rows.forEach(row => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = `note-list-row${selected?.id === row.id ? ' active' : ''}`;
      button.dataset.noteId = row.id;
      button.setAttribute('aria-current', String(selected?.id === row.id));
      const title = document.createElement('strong'); title.textContent = `${row.starred ? '☆ ' : ''}${row.title}`;
      const excerpt = document.createElement('p'); excerpt.textContent = snippet(row.body) || '空白手记';
      const date = document.createElement('time'); date.dateTime = row.updated_at; date.textContent = formatDate(row.updated_at);
      button.append(title, excerpt, date);
      button.onclick = () => act(() => open(row.id));
      button.oncontextmenu = event => { event.preventDefault(); act(async () => { await open(row.id); openMenu(button); }); };
      button.ondblclick = () => act(async () => { await open(row.id); el('note-title').focus(); el('note-title').select(); });
      root.append(button);
    });
  }
  async function load() {
    const id = ++loadId;
    const result = await request(`?trash=${filter === 'trash'}`);
    if (id !== loadId) return;
    notes = result;
    renderList();
  }
  function updateMode() {
    el('note-mode').textContent = reading ? '编辑' : '阅读';
    el('note-mode').disabled = !!selected?.deleted;
    el('note-body').classList.toggle('hidden', reading);
    el('note-preview').classList.toggle('hidden', !reading);
    if (reading) {
      el('note-preview').innerHTML = renderMarkdown(el('note-body').value);
      window.ICS?.render?.activateKaTeX?.(el('note-preview'));
    } else resizeBody();
  }
  async function open(id) {
    await flush();
    const row = await request(`/${id}`);
    selected = row;
    dirty = false;
    el('note-title').value = row.title === '未命名手记' ? '' : row.title;
    el('note-body').value = row.body;
    el('note-title').readOnly = !!row.deleted;
    el('note-body').readOnly = !!row.deleted;
    el('note-date').textContent = `更新于 ${formatDate(row.updated_at)}`;
    el('note-save-state').textContent = row.deleted ? '已移入回收站' : '已保存';
    try {
      const draft = JSON.parse(localStorage.getItem(draftKey(id)));
      if (draft && !row.deleted) {
        el('note-title').value = draft.title; el('note-body').value = draft.body; dirty = true;
        el('note-save-state').textContent = '已恢复未保存草稿';
        if (draft.revision === row.revision) saveTimer = setTimeout(() => act(flush), 1000);
        else { selected.revision = draft.revision; failedSave(true); }
      }
    } catch (_) {}
    el('note-star').textContent = row.starred ? '★' : '☆';
    el('note-star').setAttribute('aria-label', row.starred ? '取消收藏手记' : '收藏手记');
    el('note-history').classList.add('hidden'); el('note-history').open = false;
    el('note-editor').classList.remove('hidden'); el('note-welcome').classList.add('hidden');
    el('view-notes').classList.add('note-open');
    reading = !!row.deleted; updateMode(); renderList();
    try { localStorage.setItem('sea-last-note', id); } catch (_) {}
    window.scrollTo(0,0);
  }
  async function create(title = '', body = '') {
    await flush();
    const row = await request('', {method:'POST',body:JSON.stringify({title,body})});
    filter = 'all'; syncFilters();
    await load(); await open(row.id);
    el('note-title').focus();
  }
  function syncFilters() {
    document.querySelectorAll('[data-note-filter]').forEach(node => node.classList.toggle('active',node.dataset.noteFilter === filter));
  }
  async function flag(name, value) {
    await flush();
    selected = await request(`/${selected.id}`, {method:'PUT',body:JSON.stringify({...selected,[name]:value})});
    return selected;
  }
  function download(data, filename, type = 'text/markdown;charset=utf-8') {
    const url = URL.createObjectURL(data instanceof Blob ? data : new Blob([data],{type}));
    const link = document.createElement('a'); link.href = url; link.download = filename; document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  }
  function exportNote() {
    const title = el('note-title').value || '未命名手记';
    download(`# ${title}\n\n${el('note-body').value}\n`,`${title.replace(/[/\\:*?"<>|]/g,'_')}.md`);
  }
  async function remove() {
    const row = await flag('deleted', true);
    selected = null; dirty = false;
    el('note-editor').classList.add('hidden'); el('note-welcome').classList.remove('hidden'); el('view-notes').classList.remove('note-open');
    await load();
    message('手记已移入回收站');
    const undo = createButton('撤销', () => act(async () => {
      const current = await request(`/${row.id}`);
      await request(`/${row.id}`,{method:'PUT',body:JSON.stringify({...current,deleted:false})});
      filter = 'all'; syncFilters(); await load(); await open(row.id); message('已恢复手记');
    }));
    el('message').append(undo); clearTimeout(messageTimer); messageTimer = setTimeout(() => el('message').classList.add('hidden'),12000);
  }
  async function history() {
    await flush();
    const rows = await request(`/${selected.id}/versions`);
    const root = el('note-history-list'); root.replaceChildren();
    if (!rows.length) root.textContent = '还没有历史版本';
    rows.forEach(row => {
      const item = document.createElement('div'); item.className = 'note-history-row';
      const text = document.createElement('div');
      const title = document.createElement('strong'); title.textContent = row.title;
      const date = document.createElement('p'); date.textContent = `${formatDate(row.saved_at)} · ${row.body.length} 字`;
      const restore = createButton('恢复此版本', () => act(async () => {
        await flush(); el('note-title').value = row.title; el('note-body').value = row.body; markDirty(); await flush();
        reading = false; updateMode(); el('note-history').classList.add('hidden'); message('已恢复版本，原内容保存在历史版本中');
      }));
      text.append(title,date); item.append(text,restore); root.append(item);
    });
    el('note-history').classList.remove('hidden'); el('note-history').open = true;
    el('note-history').scrollIntoView({block:'nearest',behavior:'smooth'});
  }
  function closeMenu() { if (menu) { menu.remove(); menu = null; } }
  function openMenu(anchor = el('note-more')) {
    closeMenu();
    if (!selected) return;
    menu = document.createElement('div'); menu.className = 'course-context-menu'; menu.setAttribute('role','menu');
    const append = (label, action, danger = false) => {
      const button = createButton(label, () => { closeMenu(); act(action); },danger ? 'danger' : '');
      button.setAttribute('role','menuitem'); menu.append(button);
    };
    append('导出 Markdown', exportNote);
    if (selected.deleted) append('恢复手记', async () => { const row = await flag('deleted',false); filter='all'; syncFilters(); await load(); await open(row.id); });
    else { append('历史版本',history); append('移入回收站',remove,true); }
    document.body.append(menu);
    const rect = anchor.getBoundingClientRect();
    menu.style.left = `${Math.min(Math.max(8,rect.right - 180),innerWidth - 188)}px`;
    menu.style.top = `${Math.min(rect.bottom + 5,innerHeight - menu.offsetHeight - 8)}px`;
    menu.firstElementChild.focus();
    menu.onkeydown = event => {
      const buttons = [...menu.querySelectorAll('button')]; const index = buttons.indexOf(document.activeElement);
      if (event.key === 'Escape') { event.preventDefault(); closeMenu(); anchor.focus(); }
      if (['ArrowDown','ArrowUp'].includes(event.key)) { event.preventDefault(); buttons[(index + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length].focus(); }
    };
  }
  el('note-title').oninput = markDirty; el('note-body').oninput = markDirty;
  el('note-new').onclick = el('note-first').onclick = () => act(create);
  el('note-search').oninput = renderList;
  el('note-mode').onclick = () => act(async () => { await flush(); reading = !reading; updateMode(); });
  el('note-star').onclick = () => act(async () => { const row = await flag('starred',!selected.starred); el('note-star').textContent = row.starred ? '★' : '☆'; el('note-star').setAttribute('aria-label',row.starred ? '取消收藏手记' : '收藏手记'); await load(); });
  el('note-more').onclick = () => openMenu();
  el('note-index-back').onclick = () => act(async () => { await flush(); el('view-notes').classList.remove('note-open'); });
  document.querySelectorAll('[data-note-filter]').forEach(node => node.onclick = () => act(async () => { await flush(); filter = node.dataset.noteFilter; syncFilters(); await load(); }));
  el('note-import').onclick = () => el('note-import-files').click();
  el('note-import-files').onchange = () => act(async () => {
    await flush(); const files = [...el('note-import-files').files]; let last;
    for (const file of files) {
      if (file.size > 2_000_000) throw new Error(`${file.name} 超过 2 MB`);
      const text = await file.text(); const heading = /^#\s+(.+)\r?\n/.exec(text);
      last = await request('',{method:'POST',body:JSON.stringify({title:heading?.[1] || file.name.replace(/\.[^.]+$/,''),body:heading ? text.slice(heading[0].length).replace(/^\s*\n/,'') : text})});
    }
    filter = 'all'; syncFilters(); await load(); if (last) await open(last.id);
    el('note-import-files').value = ''; message(`已导入 ${files.length} 篇手记`);
  });
  el('notebook-backup').onclick = () => act(async () => {
    await flush();
    const response = await fetch('/api/local/notebook/backup');
    if (!response.ok) throw new Error('备份未完成，请重试');
    download(await response.blob(),`丰饶之海-手记-${new Date().toLocaleDateString('sv-SE')}.zip`);
    el('notebook-backup-status').textContent = '备份已生成，包含手记、回收站与历史版本。';
  });
  el('notebook-restore').onclick = () => el('notebook-restore-file').click();
  el('notebook-restore-file').onchange = () => act(async () => {
    await flush(); const file = el('notebook-restore-file').files[0]; if (!file) return;
    const data = new FormData(); data.append('file',file);
    const response = await fetch('/api/local/notebook/restore',{method:'POST',body:data});
    const result = await response.json(); if (!response.ok) throw new Error(result.detail || '导入失败');
    await load(); el('notebook-backup-status').textContent = `已导入 ${result.imported} 篇手记，已有内容已保留。`; el('notebook-restore-file').value = '';
  });
  el('open-personal-library').onclick = () => act(async () => {
    localStorage.setItem('sea-personal-library','1'); setupRequested = false; activeView='notes'; await refreshStatus(); await load();
  });
  el('library-home').onclick = () => { showView('notes'); act(load); };
  document.addEventListener('pointerdown', event => { if (menu && !menu.contains(event.target) && !event.target.closest('#note-more')) closeMenu(); });
  document.addEventListener('keydown', event => {
    const command = event.metaKey || event.ctrlKey;
    if (command && event.key.toLowerCase() === 'n' && !document.querySelector('dialog[open]')) { event.preventDefault(); showView('notes'); act(create); }
    if (command && event.key.toLowerCase() === 's' && selected && activeView === 'notes') { event.preventDefault(); act(flush); }
    if (command && event.key.toLowerCase() === 'e' && selected && activeView === 'notes') { event.preventDefault(); el('note-mode').click(); }
    if (command && event.key.toLowerCase() === 'k') { event.preventDefault(); showView('search'); el('search').focus(); }
    if (event.key === 'Escape') closeMenu();
  });
  window.addEventListener('sea-view', event => { closeMenu(); if (event.detail.changed && dirty) act(flush); });
  window.addEventListener('beforeunload', event => { keepDraft(); if (dirty) { event.preventDefault(); event.returnValue=''; } });
  document.addEventListener('visibilitychange', () => { if (document.hidden && dirty) act(flush); });
  window.addEventListener('resize',resizeBody);
  window.addEventListener('icourse-render-ready', () => { if (selected && reading) updateMode(); });
  async function search(query, coursesOnly = false) {
    const id = ++searchTimer;
    const root = el('search-personal-results');
    root.replaceChildren();
    root.classList.toggle('hidden', !query || coursesOnly);
    if (!query || coursesOnly) return;
    try {
      const rows = await request(`?q=${encodeURIComponent(query)}`);
      if (id !== searchTimer) return;
      const heading = document.createElement('p'); heading.className = 'meta'; heading.textContent = `手记 · ${rows.length} 篇`; root.append(heading);
      rows.slice(0,50).forEach(row => {
        const button = document.createElement('button'); button.type='button'; button.className='personal-search-card';
        const title = document.createElement('strong'); title.textContent=row.title;
        const text = document.createElement('p'); text.textContent=snippet(row.body);
        button.append(title,text); button.onclick=()=>act(()=>window.SeaNotebook.open(row.id)); root.append(button);
      });
    } catch (error) { if (id === searchTimer) root.textContent='手记搜索暂不可用'; }
  }
  window.SeaNotebook = {load:() => act(load),flush,search,open:async id => {showView('notes'); await open(id);}};
  act(async () => { await load(); const last = localStorage.getItem('sea-last-note'); if (last && notes.some(row => row.id === last)) await open(last); });
})();
