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
        const height=document.documentElement.scrollHeight-innerHeight;
        window.scrollTo(0,Math.min(saved.y,height));
        position=saved;
      }));
    }
    updateContinue();
  });
  window.addEventListener('beforeunload',savePosition);
  $('#course-query').oninput=()=>renderCourses({keepSidebar:true});
  $('#library-home').onclick=()=>showView('courses');
})();
