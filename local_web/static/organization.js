/* Shared organization rules for the local UI and the Pages transport. */
(() => {
  const MAX_DEPTH = 6;
  const aliases = {organize: 'organize', study: 'study', reference: 'reference', archive: 'archive',
    unassigned: 'unassigned', '整理区': 'organize', '学习区': 'study', '查阅区': 'reference',
    '资料库': 'reference', '待整理': 'unassigned', '归档区': 'archive'};
  function normalizeZone(value) {
    const text = String(value || '').trim();
    return /^section-[a-f0-9]{32}$/.test(text) ? text : aliases[text] || null;
  }
  function validateSections(sections) {
    if (!Array.isArray(sections) || sections.length > 100) throw new Error('最多创建 100 个分区');
    const ids = new Set(), names = new Set();
    const clean = sections.map(section => {
      if (!section || typeof section !== 'object') throw new Error('分区格式无效');
      const {id, parent_id: parent} = section, name = String(section.name ?? '').trim();
      if (typeof id !== 'string' || normalizeZone(id) !== id || ['archive', 'unassigned'].includes(id) || ids.has(id)) throw new Error('分区标识无效或重复');
      if (parent != null && (typeof parent !== 'string' || !parent)) throw new Error('父分区无效');
      const nameKey = JSON.stringify([parent || null, name.toLowerCase()]);
      if (!name || name.length > 40 || ['归档', '归档区', '未分区', '待整理'].includes(name) || names.has(nameKey)) throw new Error('分区名称应为 1–40 个字符，同层不能重复或使用保留名称');
      ids.add(id); names.add(nameKey);
      return {id, name, ...(parent ? {parent_id: parent} : {})};
    });
    const byId = new Map(clean.map(section => [section.id, section]));
    clean.forEach(section => {
      const seen = new Set();
      for (let current = section; current; current = byId.get(current.parent_id)) {
        if (seen.has(current.id)) throw new Error('分区不能移入自身或子分区');
        seen.add(current.id);
        if (seen.size > MAX_DEPTH) throw new Error('分类最多支持 6 层');
        if (current.parent_id && !byId.has(current.parent_id)) throw new Error('父分区不存在');
      }
    });
    return clean;
  }
  function migrateOrganization(state) {
    const sections = (state.sections || []).map(section => ({...section}));
    const inbox = new Set(sections.filter(s => s.id === 'organize' && ['整理区', '待整理'].includes(s.name)).map(s => s.id));
    const byId = new Map(sections.map(section => [section.id, section]));
    const migrated = validateSections(sections.filter(s => !inbox.has(s.id)).map(section => {
      if (section.id === 'reference' && section.name === '查阅区') {
        section.name = '资料库';
        const siblingNames = new Set(sections.filter(s => s.id !== section.id && (s.parent_id || null) === (section.parent_id || null)).map(s => s.name));
        for (let suffix = 2; siblingNames.has(section.name); suffix++) section.name = `资料库 ${suffix}`;
      }
      if (inbox.has(section.parent_id)) {
        const parent = byId.get(section.parent_id).parent_id;
        delete section.parent_id;
        if (parent) section.parent_id = parent;
      }
      return section;
    }));
    const allowed = new Set(['unassigned', 'archive', ...migrated.map(s => s.id)]);
    return {sections: migrated, default_zone: 'unassigned', revision: state.revision || 0,
      zones: Object.fromEntries(Object.entries(state.zones || {}).map(([id, value]) => {
        const zone = normalizeZone(value);
        return [id, allowed.has(zone) ? zone : 'unassigned'];
      }))};
  }
  function sectionTree(sections) {
    const children = new Map();
    sections.forEach(section => {
      const parent = section.parent_id || null;
      if (!children.has(parent)) children.set(parent, []);
      children.get(parent).push(section);
    });
    const rows = [];
    function visit(parent, path) {
      (children.get(parent) || []).forEach(section => {
        const names = [...path, section.name];
        rows.push({section, depth: path.length, path: names.join(' / ')});
        visit(section.id, names);
      });
    }
    visit(null, []);
    return rows;
  }
  function descendantIds(sections, id) {
    const ids = new Set([id]);
    for (let size = -1; size !== ids.size;) {
      size = ids.size;
      sections.forEach(s => { if (ids.has(s.parent_id)) ids.add(s.id); });
    }
    return ids;
  }
  function canParent(sections, id, parent) {
    if (!parent) return true;
    const descendants = descendantIds(sections, id);
    if (descendants.has(parent)) return false;
    const rows = sectionTree(sections), source = rows.find(row => row.section.id === id);
    const destination = rows.find(row => row.section.id === parent);
    if (!source || !destination) return false;
    const subtreeDepth = Math.max(...rows.filter(row => descendants.has(row.section.id)).map(row => row.depth)) - source.depth;
    return destination.depth + 2 + subtreeDepth <= MAX_DEPTH;
  }
  function reparentSections(sections, id, parent) {
    if (!canParent(sections, id, parent)) throw new Error('不能移入自身、子分区或超过 6 层');
    const changed = sections.map(s => {
      const copy = {...s};
      if (s.id === id) {
        delete copy.parent_id;
        if (parent) copy.parent_id = parent;
      }
      return copy;
    });
    return sectionTree(changed).map(row => row.section);
  }
  function reorderSections(sections, id, targetId, after = false) {
    const target = sections.find(s => s.id === targetId);
    if (!target || descendantIds(sections, id).has(targetId)) throw new Error('不能移入自身或子分区');
    const changed = reparentSections(sections, id, target.parent_id);
    const subtree = descendantIds(changed, id), targetTree = descendantIds(changed, targetId);
    const moving = changed.filter(s => subtree.has(s.id)), remaining = changed.filter(s => !subtree.has(s.id));
    const index = after ? Math.max(...remaining.map((s, i) => targetTree.has(s.id) ? i : -1)) + 1
      : remaining.findIndex(s => s.id === targetId);
    remaining.splice(index, 0, ...moving);
    return remaining;
  }
  window.ICS = window.ICS || {};
  window.ICS.organization = {MAX_DEPTH, normalizeZone, validateSections, migrateOrganization,
    sectionTree, descendantIds, canParent, reparentSections, reorderSections};
})();
