(() => {
  let current = null, position = null, timer;
  const storageKey = row => `sea-reading:${row.kind}:${row.id}`;
  function savePosition() {
    if (!current || !position) return;
    try { localStorage.setItem(storageKey(current),JSON.stringify(position)); } catch (_) {}
  }
  function lastReading() {
    try { return JSON.parse(localStorage.getItem('sea-last-reading')); } catch (_) { return null; }
  }
  function updateContinue() {
    const row = lastReading(); const button = $('#continue-reading');
    button.classList.toggle('hidden',!row || !statusState?.database_ready);
    if (row) { button.title=row.title; button.onclick=()=> (row.kind==='talk' ? openTalk(row.id) : openLecture(row.id)).catch(error=>message(error.message,true)); }
  }
  window.addEventListener('scroll', () => {
    if (!current) return;
    position = {y:scrollY,ratio:scrollY / Math.max(1,document.documentElement.scrollHeight-innerHeight)};
    clearTimeout(timer); timer=setTimeout(savePosition,250);
  },{passive:true});
  window.addEventListener('sea-view', event => {
    const view=event.detail.view;
    const row = view==='detail' && currentLecture ? {kind:'course',id:currentLecture.sub_id,title:lectureDisplayName(currentLecture)}
      : view==='talkDetail' && currentTalk ? {kind:'talk',id:currentTalk.id,title:talkDisplayName(currentTalk)} : null;
    if (current?.kind===row?.kind && current?.id===row?.id) { updateContinue(); return; }
    savePosition(); current=row; position=null;
    if (row) {
      try { localStorage.setItem('sea-last-reading',JSON.stringify(row)); } catch (_) {}
      let saved=null; try { saved=JSON.parse(localStorage.getItem(storageKey(row))); } catch (_) {}
      if (saved && event.detail.options?.restoreReading !== false) requestAnimationFrame(()=>requestAnimationFrame(()=> {
        if (current !== row) return;
        const height=document.documentElement.scrollHeight-innerHeight;
        window.scrollTo(0,Math.min(saved.y,height));
        position=saved;
      }));
    }
    updateContinue();
  });
  window.addEventListener('beforeunload',savePosition);
  window.addEventListener('sea-course-data-deleted', event => {
    const deleted = row => row?.kind === 'course' && event.detail.subIds.includes(String(row.id));
    if (deleted(current)) { clearTimeout(timer); current = null; position = null; }
    if (deleted(lastReading())) localStorage.removeItem('sea-last-reading');
    event.detail.subIds.forEach(id => localStorage.removeItem(storageKey({kind:'course',id})));
    updateContinue();
  });
  const fontKey = 'sea-lecture-font-size';
  const defaultSize = 17, minSize = 12, maxSize = 32;
  const smaller = $('#lecture-font-smaller'), larger = $('#lecture-font-larger'), reset = $('#lecture-font-reset');
  let fontSize = defaultSize;
  function applyFontSize(size) {
    fontSize = Math.max(minSize, Math.min(maxSize, size));
    $('#view-detail').style.setProperty('--reading-font-size', `${fontSize}px`);
    reset.textContent = `${fontSize} px`;
    reset.setAttribute('aria-label', `当前字号 ${fontSize} 像素，恢复默认字号`);
    smaller.disabled = fontSize === minSize;
    larger.disabled = fontSize === maxSize;
  }
  function setFontSize(size) {
    applyFontSize(size);
    try { localStorage.setItem(fontKey, String(fontSize)); } catch (_) {}
  }
  try {
    const stored = Number(localStorage.getItem(fontKey));
    if (Number.isInteger(stored) && stored >= minSize && stored <= maxSize) fontSize = stored;
  } catch (_) {}
  applyFontSize(fontSize);
  smaller.onclick = () => setFontSize(fontSize - 1);
  larger.onclick = () => setFontSize(fontSize + 1);
  reset.onclick = () => setFontSize(defaultSize);

  document.addEventListener('keydown', event => {
    if (activeView !== 'detail' || event.defaultPrevented || event.isComposing || event.keyCode === 229 ||
        event.ctrlKey || event.metaKey || event.altKey || document.querySelector('dialog[open]')) return;
    const target = event.target;
    if (target.isContentEditable || target.closest('input, textarea, select, [role="menu"], [role="textbox"]')) return;
    if (target !== document.body && !target.closest('#view-detail')) return;
    if (!event.shiftKey && (event.key === 'ArrowLeft' || event.key === 'ArrowRight')) {
      if (!window.getSelection()?.isCollapsed || target.closest('.table-wrap, pre, .katex-display')) return;
      event.preventDefault();
      if (!event.repeat) stepLecture(event.key === 'ArrowLeft' ? -1 : 1);
      return;
    }
    const control = {'+': larger, '=': larger, '-': smaller, '0': reset}[event.key];
    if (control instanceof HTMLButtonElement) {
      event.preventDefault();
      control.click();
    }
  });
  $('#course-query').oninput=()=>renderCourses({keepSidebar:true});
  $('#library-home').onclick=()=>showView('courses');
})();
