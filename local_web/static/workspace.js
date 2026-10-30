/* Independent, persistent workspace panels. All handles also work by keyboard. */
(() => {
  const key = 'sea-workspace-panels';
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(key)) || {}; } catch (_) {}
  if (typeof saved !== 'object' || Array.isArray(saved)) saved = {};
  const panels = [];
  const persist = () => { try { localStorage.setItem(key, JSON.stringify(saved)); } catch (_) {} };
  function panel(name, root, toggle, width, min, max, side = 'left', host = root) {
    if (!root || !toggle) return;
    const state = saved[name] && typeof saved[name] === 'object' ? saved[name] : {};
    state.width = Number.isFinite(state.width) ? Math.max(min, Math.min(max, state.width)) : width;
    state.closed = typeof state.closed === 'boolean' ? state.closed : name === 'outline' && innerWidth < 1250;
    saved[name] = state;
    const label = root.getAttribute('aria-label') || {notes:'笔记列表', course:'课程分类', provider:'模型服务商'}[name];
    toggle.setAttribute('aria-controls', root.id);
    const grip = document.createElement('div');
    grip.className = `panel-resizer panel-resizer-${name}`;
    grip.tabIndex = 0; grip.setAttribute('role', 'separator');
    grip.setAttribute('aria-orientation', 'vertical'); grip.setAttribute('aria-label', `调整${label}宽度`);
    grip.setAttribute('aria-controls', root.id);
    grip.setAttribute('aria-valuemin', min); grip.setAttribute('aria-valuemax', max);
    const apply = () => {
      document.documentElement.style.setProperty(`--panel-${name}-width`, `${state.width}px`);
      document.documentElement.dataset[`panel${name[0].toUpperCase()}${name.slice(1)}`] = state.closed ? 'closed' : 'open';
      toggle.setAttribute('aria-expanded', String(!state.closed));
      toggle.setAttribute('aria-label', `${state.closed ? '展开' : '收起'}${label}`);
      toggle.title = `${state.closed ? '展开' : '收起'}${label}`;
      if (name !== 'nav' && name !== 'course') toggle.textContent = `${state.closed ? '展开' : '收起'}${label}`;
      grip.setAttribute('aria-valuenow', Math.round(state.width));
      window.dispatchEvent(new Event('sea-panel-resize'));
    };
    toggle.onclick = () => {
      if (name === 'outline' && innerWidth <= 700) {
        const open = document.body.classList.toggle('mobile-outline-open');
        toggle.setAttribute('aria-expanded', String(open));
        toggle.setAttribute('aria-label', `${open ? '收起' : '展开'}笔记大纲`);
        toggle.textContent = open ? '收起大纲' : '展开大纲';
      } else { state.closed = !state.closed; apply(); persist(); }
    };
    grip.ondblclick = () => { state.width = width; apply(); persist(); };
    grip.onkeydown = event => {
      if (!['ArrowLeft', 'ArrowRight', 'Home'].includes(event.key)) return;
      event.preventDefault();
      const direction = (event.key === 'ArrowRight' ? 1 : -1) * (side === 'right' ? -1 : 1);
      state.width = event.key === 'Home' ? width : Math.max(min, Math.min(max, state.width + direction * (event.shiftKey ? 32 : 8)));
      apply(); persist();
    };
    grip.onpointerdown = event => {
      if (event.button !== 0) return;
      event.preventDefault(); grip.focus(); grip.setPointerCapture(event.pointerId);
      const start = event.clientX, before = state.width;
      document.body.classList.add('panel-resizing');
      grip.onpointermove = move => {
        state.width = Math.max(min, Math.min(max, before + (move.clientX - start) * (side === 'right' ? -1 : 1)));
        apply();
      };
      const finish = cancel => {
        if (cancel) state.width = before;
        grip.onpointermove = null; document.body.classList.remove('panel-resizing'); apply(); persist();
      };
      grip.onpointerup = () => finish(false); grip.onpointercancel = () => finish(true);
      grip.onlostpointercapture = () => { grip.onpointermove = null; document.body.classList.remove('panel-resizing'); };
    };
    host.append(grip); apply(); panels.push({name, state, apply, min, max});
  }
  panel('nav', document.getElementById('app-sidebar'), document.getElementById('nav-panel-toggle'), 208, 180, 300);
  panel('notes', document.getElementById('notebook-index'), document.getElementById('notes-panel-toggle'), 264, 220, 400);
  panel('outline', document.getElementById('note-outline'), document.getElementById('outline-panel-toggle'), 208, 160, 300, 'right');
  const courseToggle = document.getElementById('course-sidebar-open');
  const drawer = courseToggle.onclick;
  panel('course', document.getElementById('course-sidebar'), courseToggle, 176, 150, 320, 'left', document.querySelector('.courses-layout'));
  const toggleCourse = courseToggle.onclick;
  courseToggle.onclick = () => { if (innerWidth <= 700) drawer(); else toggleCourse(); };
  const provider = document.querySelector('.provider-sidebar');
  if (provider) {
    provider.id = 'provider-sidebar';
    const toggle = document.createElement('button'); toggle.type = 'button'; toggle.className = 'text-button provider-panel-toggle';
    toggle.setAttribute('aria-controls', provider.id); provider.parentElement.before(toggle);
    panel('provider', provider, toggle, 240, 180, 320, 'left', provider.parentElement);
  }
  window.addEventListener('storage', event => {
    if (event.key !== key) return;
    try {
      const incoming = JSON.parse(event.newValue) || {};
      for (const {name, state, apply, min, max} of panels) {
        if (typeof incoming[name]?.closed === 'boolean') state.closed = incoming[name].closed;
        if (Number.isFinite(incoming[name]?.width)) state.width = Math.max(min, Math.min(max, incoming[name].width));
        apply();
      }
    } catch (_) {}
  });
  window.addEventListener('resize', () => {
    if (innerWidth <= 700) {
      const toggle = document.getElementById('outline-panel-toggle');
      const open = document.body.classList.contains('mobile-outline-open');
      toggle.textContent = open ? '收起大纲' : '展开大纲'; toggle.setAttribute('aria-expanded', String(open));
    } else panels.find(row => row.name === 'outline')?.apply();
  });
  if (innerWidth <= 700) window.dispatchEvent(new Event('resize'));
  const closeOutline = () => {
    document.body.classList.remove('mobile-outline-open');
    if (innerWidth <= 700) {
      const toggle = document.getElementById('outline-panel-toggle');
      toggle.textContent = '展开大纲'; toggle.setAttribute('aria-expanded', 'false'); toggle.setAttribute('aria-label', '展开笔记大纲');
    }
  };
  document.addEventListener('pointerdown', event => {
    if (!event.target.closest('#note-outline,#outline-panel-toggle')) closeOutline();
  });
  document.addEventListener('keydown', event => { if (event.key === 'Escape') closeOutline(); });
  window.addEventListener('sea-view', closeOutline);
})();
