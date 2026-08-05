const $ = (selector) => document.querySelector(selector);
let statusState = null;
let modelProviders = [];
let obsidianPlan = null;
let messageTimer = null;
let activeView = "courses";
let currentCourse = null;
let courseLectures = [];
let currentLecture = null;
let detailTab = "summary";
let courseRows = [];
let searchTimer = null;
let subscribedCourseIds = [];
let subscriptionCourses = [];
let subscriptionTerms = [];
let subscriptionTimer = null;
let catalogRows = [];
let courseZones = {};
let lectureNames = {};
let activeCourseZone = "all";
let updatePollTimer = null;
let rerunModelOptions = [];
let selectedSummaryVersionKeys = new Set();
const courseZoneRequests = new Map();
let loadedLibraryIdentity = "";
const STARRED_KEY = "icourse-local-starred-courses";
const COURSE_ZONE_LABELS = {
  organize: "整理区",
  study: "学习区",
  reference: "查阅区",
  archive: "归档区",
};
const RUN_STATUS_LABELS = {
  success: "成功", failure: "失败", cancelled: "已取消", timed_out: "超时",
  in_progress: "进行中", queued: "排队中", requested: "已请求", waiting: "等待中",
  pending: "排队中", completed: "已完成", neutral: "已完成", skipped: "已跳过",
  stale: "已过期", action_required: "需要处理", startup_failure: "启动失败",
};

function loadStarred() {
  try { return new Set(JSON.parse(localStorage.getItem(STARRED_KEY)) || []); }
  catch (_) { return new Set(); }
}
const starredCourses = loadStarred();

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {"Content-Type": "application/json", ...(options.headers || {})},
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const payload = await response.json();
      detail = typeof payload.detail === "string"
        ? payload.detail
        : JSON.stringify(payload.detail || payload);
    } catch (_) {}
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

function message(value, error = false) {
  const node = $("#message");
  node.textContent = value;
  node.className = `message${error ? " error" : ""}`;
  // Restart the entrance animation for consecutive messages.
  node.classList.remove("pop");
  void node.offsetWidth;
  node.classList.add("pop");
  clearTimeout(messageTimer);
  messageTimer = setTimeout(() => node.classList.add("hidden"), 5000);
}

function text(value) {
  return value == null || value === "" ? "—" : String(value);
}

function escapeHtml(value) {
  return String(value || "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);
}

function safeMarkdownHref(value) {
  try {
    const url = new URL(String(value).replace(/&amp;/g, "&"), window.location.origin);
    return ["http:", "https:", "mailto:"].includes(url.protocol) ? url.href : "";
  } catch (_) {
    return "";
  }
}

function markdownInline(value) {
  const tokens = [];
  let output = escapeHtml(value);
  output = output.replace(/`([^`\n]+)`/g, (_match, code) => {
    const token = `\u0000${tokens.length}\u0000`;
    tokens.push(`<code>${code}</code>`);
    return token;
  });
  output = output.replace(/\[([^\]]+)\]\(([^\s)]+)(?:\s+&quot;[^)]*&quot;)?\)/g, (_match, label, href) => {
    const safeHref = safeMarkdownHref(href);
    return safeHref ? `<a href="${escapeHtml(safeHref)}" target="_blank" rel="noreferrer">${label}</a>` : label;
  });
  output = output
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/__([^_]+)__/g, "<strong>$1</strong>")
    .replace(/~~([^~]+)~~/g, "<del>$1</del>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(/(^|[^_])_([^_\n]+)_/g, "$1<em>$2</em>");
  return output.replace(/\u0000(\d+)\u0000/g, (_match, index) => tokens[Number(index)] || "");
}

function _splitTableRow(line) {
  // GFM pipe-table row → cell array; escaped \| stays inside a cell.
  let text = line.trim();
  if (text.startsWith("|")) text = text.slice(1);
  if (text.endsWith("|") && !text.endsWith("\\|")) text = text.slice(0, -1);
  return text.split(/(?<!\\)\|/).map((cell) => cell.trim().replace(/\\\|/g, "|"));
}

function _isTableDelimiter(line) {
  // e.g. | --- | :---: | ---: |
  const cells = _splitTableRow(line);
  return cells.length > 0 && cells.every((cell) => /^:?-{1,}:?$/.test(cell));
}

function _tableAlignments(delimiterLine) {
  return _splitTableRow(delimiterLine).map((cell) => {
    const left = cell.startsWith(":");
    const right = cell.endsWith(":");
    if (left && right) return "center";
    if (right) return "right";
    if (left) return "left";
    return "";
  });
}

function renderMarkdown(value) {
  const lines = String(value || "").replace(/\r\n?/g, "\n").split("\n");
  const html = [];
  let paragraph = [];
  const flushParagraph = () => {
    if (paragraph.length) html.push(`<p>${paragraph.map(markdownInline).join("<br>")}</p>`);
    paragraph = [];
  };
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    const heading = /^(#{1,6})\s+(.+)$/.exec(line);
    const unordered = /^\s*[-*+]\s+(.+)$/.exec(line);
    const ordered = /^\s*\d+[.)]\s+(.+)$/.exec(line);
    const isTable = line.includes("|")
      && index + 1 < lines.length
      && _isTableDelimiter(lines[index + 1])
      && _splitTableRow(line).length === _splitTableRow(lines[index + 1]).length;
    if (/^\s*```/.test(line)) {
      flushParagraph();
      const language = line.trim().slice(3).trim();
      const code = [];
      index += 1;
      while (index < lines.length && !/^\s*```/.test(lines[index])) code.push(lines[index++]);
      html.push(`<pre${language ? ` data-language="${escapeHtml(language)}"` : ""}><code>${escapeHtml(code.join("\n"))}</code></pre>`);
    } else if (isTable) {
      flushParagraph();
      const aligns = _tableAlignments(lines[index + 1]);
      const alignAttr = (cellIndex) => aligns[cellIndex] ? ` style="text-align:${aligns[cellIndex]}"` : "";
      const headCells = _splitTableRow(line)
        .map((cell, cellIndex) => `<th${alignAttr(cellIndex)}>${markdownInline(cell)}</th>`)
        .join("");
      index += 2; // skip header + delimiter
      const bodyRows = [];
      while (index < lines.length && lines[index].includes("|") && lines[index].trim()) {
        const cells = _splitTableRow(lines[index])
          .map((cell, cellIndex) => `<td${alignAttr(cellIndex)}>${markdownInline(cell)}</td>`)
          .join("");
        bodyRows.push(`<tr>${cells}</tr>`);
        index += 1;
      }
      index -= 1; // while 停在下一条非表格行，交还给外层 for 处理
      html.push(
        `<div class="table-wrap"><table><thead><tr>${headCells}</tr></thead>`
        + `<tbody>${bodyRows.join("")}</tbody></table></div>`
      );
    } else if (heading) {
      flushParagraph();
      const level = heading[1].length;
      html.push(`<h${level}>${markdownInline(heading[2])}</h${level}>`);
    } else if (/^\s{0,3}([-*_])(?:\s*\1){2,}\s*$/.test(line)) {
      flushParagraph(); html.push("<hr>");
    } else if (/^\s*>\s?/.test(line)) {
      flushParagraph();
      const quote = [];
      while (index < lines.length && /^\s*>\s?/.test(lines[index])) quote.push(lines[index++].replace(/^\s*>\s?/, ""));
      index -= 1;
      html.push(`<blockquote>${quote.map(markdownInline).join("<br>")}</blockquote>`);
    } else if (unordered || ordered) {
      flushParagraph();
      const tag = unordered ? "ul" : "ol";
      const items = [];
      const pattern = unordered ? /^\s*[-*+]\s+(.+)$/ : /^\s*\d+[.)]\s+(.+)$/;
      while (index < lines.length) {
        const item = pattern.exec(lines[index]);
        if (!item) break;
        items.push(`<li>${markdownInline(item[1])}</li>`);
        index += 1;
      }
      index -= 1;
      html.push(`<${tag}>${items.join("")}</${tag}>`);
    } else if (!line.trim()) {
      flushParagraph();
    } else {
      paragraph.push(line);
    }
  }
  flushParagraph();
  return html.join("") || "<p>暂无摘要</p>";
}

function createButton(label, onClick, className = "") {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = label;
  button.className = className;
  button.addEventListener("click", onClick);
  return button;
}

function createField(labelText, input, hint = "", className = "") {
  const label = document.createElement("label");
  label.className = className;
  label.append(document.createTextNode(labelText), input);
  if (hint) {
    const note = document.createElement("span");
    note.className = "field-hint";
    note.textContent = hint;
    label.append(note);
  }
  return label;
}

async function refreshStatus() {
  statusState = await api("/api/local/status");
  const repo = statusState.repository;
  const updateState = statusState.update?.state || "idle";
  const connectionLabel = {
    checking: "正在检查更新",
    updated: "本地资料已更新",
    current: "本地资料已是最新",
    failed: statusState.database_ready ? "正在使用本地资料" : "更新失败",
  }[updateState] || (statusState.configured ? "本地会话已配置" : "等待配置");
  $("#connection").textContent = connectionLabel;
  $("#settings-repository").textContent = `${repo.owner}/${repo.repo} · ${repo.branch}`;
  const rememberField = $("#remember-field");
  const rememberInput = rememberField.querySelector("input");
  rememberField.classList.toggle("hidden", !statusState.keychain_available);
  rememberInput.disabled = !statusState.keychain_available;
  if (!statusState.keychain_available) rememberInput.checked = false;
  for (const name of ["owner", "repo", "branch"]) {
    $("#setup-form").elements[name].value = repo[name] || "";
  }
  $("#setup").classList.toggle("hidden", statusState.configured);
  $("#dashboard").classList.toggle("hidden", !statusState.configured);
  $("#app-header").classList.toggle("hidden", !statusState.configured);
  $("#mobile-nav").classList.toggle("hidden", !statusState.configured);
  renderStats(statusState.database);
  const libraryIdentity = statusState.database_ready
    ? `${repo.owner}/${repo.repo}/${repo.branch}:${statusState.database?.commit_sha || "local"}`
    : "";
  // While the background update is running this function polls every 1.2s.
  // Reloading courses on every poll destroys any open native <select> popup.
  // Rebuild the library only on first load or when the data commit changes.
  if (statusState.database_ready && loadedLibraryIdentity !== libraryIdentity) {
    await loadCourseZones();
    await loadLectureNames();
    await loadCourses();
    loadedLibraryIdentity = libraryIdentity;
  }
  if (statusState.configured) {
    showView(activeView);
    loadRuns().catch((error) => message(error.message, true));
  }
  clearTimeout(updatePollTimer);
  if (statusState.configured && updateState === "checking") {
    updatePollTimer = setTimeout(() => refreshStatus().catch((error) => message(error.message, true)), 1200);
  }
}

function renderStats(db) {
  const values = db ? [
    [db.courses, "课程"], [db.lectures, "课次"], [db.ready, "已生成笔记"], [db.failed, "失败/待重试"],
  ] : [["—", "课程"], ["—", "课次"], ["—", "已生成笔记"], ["—", "失败/待重试"]];
  const root = $("#stats");
  root.replaceChildren();
  values.forEach(([value, label]) => {
    const card = document.createElement("div");
    card.className = "stat";
    const strong = document.createElement("strong");
    strong.textContent = value;
    const span = document.createElement("span");
    span.textContent = label;
    card.append(strong, span);
    root.append(card);
  });
}

function courseZone(courseId) {
  return COURSE_ZONE_LABELS[courseZones[String(courseId)]] ? courseZones[String(courseId)] : "organize";
}

async function loadCourseZones() {
  const result = await api("/api/local/course-zones");
  courseZones = result.zones || {};
}

async function loadLectureNames() {
  const result = await api("/api/local/lecture-names");
  lectureNames = result.names || {};
}

// Display name for a lecture note: custom rename > auto-derived from the
// summary > the raw sub_title (date/period label).
function lectureDisplayName(lecture) {
  if (!lecture) return "";
  const custom = lectureNames[String(lecture.sub_id)];
  return custom || lecture.auto_title || lecture.sub_title || String(lecture.sub_id);
}

async function moveCourseToZone(courseId, zone, select) {
  const id = String(courseId);
  const previous = Object.prototype.hasOwnProperty.call(courseZones, id)
    ? courseZones[id]
    : null;
  if (courseZone(id) === zone) return;
  const requestId = (courseZoneRequests.get(id) || 0) + 1;
  courseZoneRequests.set(id, requestId);
  // Do not redraw the course list here. Replacing a <select> during its
  // change event makes Safari/Chrome dismiss (or fail to show) the native
  // picker, which was the source of the intermittent, jerky interaction.
  courseZones = {...courseZones, [id]: zone};
  if (select) select.disabled = true;
  try {
    await api("/api/local/course-zones", {
      method: "PUT",
      body: JSON.stringify({course_id: id, zone}),
    });
    // In "全部" the existing card remains correct. In a filtered zone the
    // card just moved away, so redraw only after the picker has closed.
    if (activeCourseZone !== "all" && activeCourseZone !== zone) {
      await loadCourses();
    }
  } catch (error) {
    if (courseZoneRequests.get(id) === requestId) {
      if (previous === null) {
        const restored = {...courseZones};
        delete restored[id];
        courseZones = restored;
      } else {
        courseZones = {...courseZones, [id]: previous};
      }
      if (select?.isConnected) select.value = previous || "organize";
    }
    message(error.message, true);
  } finally {
    if (select?.isConnected && courseZoneRequests.get(id) === requestId) {
      select.disabled = false;
    }
  }
}

async function loadCourses() {
  courseRows = await api("/api/local/courses");
  courseRows.sort((a, b) => Number(starredCourses.has(String(b.course_id))) - Number(starredCourses.has(String(a.course_id))));
  const list = $("#course-list");
  list.replaceChildren();
  list.classList.remove("empty");
  const visibleCourses = activeCourseZone === "all"
    ? courseRows
    : courseRows.filter((course) => courseZone(course.course_id) === activeCourseZone);
  if (!visibleCourses.length) {
    list.textContent = activeCourseZone === "all" ? "数据库中还没有课程。" : "这个分区暂时没有课程。";
    list.classList.add("empty");
    return;
  }
  visibleCourses.forEach((course) => {
    const card = document.createElement("article");
    card.className = "course-card";
    const row = document.createElement("div");
    row.className = "course-row";
    const titleRow = document.createElement("div");
    titleRow.className = "course-title-row";
    const open = document.createElement("button");
    open.type = "button";
    open.className = "course-open";
    open.onclick = () => openCourse(course);
    const star = document.createElement("button");
    star.type = "button";
    star.className = `star-button${starredCourses.has(String(course.course_id)) ? " starred" : ""}`;
    star.textContent = starredCourses.has(String(course.course_id)) ? "★" : "☆";
    star.title = starredCourses.has(String(course.course_id)) ? "取消置顶" : "置顶";
    star.onclick = (event) => {
      event.stopPropagation();
      const id = String(course.course_id);
      if (starredCourses.has(id)) starredCourses.delete(id); else starredCourses.add(id);
      localStorage.setItem(STARRED_KEY, JSON.stringify([...starredCourses]));
      loadCourses();
    };
    const textBlock = document.createElement("div");
    const title = document.createElement("strong");
    title.className = "course-title";
    title.textContent = course.title || course.course_id;
    const meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = text(course.teacher);
    textBlock.append(title, meta);
    open.append(textBlock);
    titleRow.append(open, star);
    const count = document.createElement("span");
    count.className = "count-badge";
    count.textContent = `笔记 ${course.summary_count}/${course.total_count}`;
    row.append(titleRow, count);
    const footer = document.createElement("div");
    footer.className = "course-card-footer";
    const zoneControl = document.createElement("label");
    zoneControl.className = "course-zone-select";
    const select = document.createElement("select");
    select.setAttribute("aria-label", `${course.title || course.course_id} 的分区`);
    Object.entries(COURSE_ZONE_LABELS).forEach(([value, label]) => {
      select.append(new Option(label, value, false, value === courseZone(course.course_id)));
    });
    select.onchange = () => moveCourseToZone(course.course_id, select.value, select);
    zoneControl.append(select);
    footer.append(zoneControl);
    card.append(row, footer);
    list.append(card);
  });
}

async function openCourse(course) {
  currentCourse = course;
  courseLectures = await api(`/api/local/courses/${encodeURIComponent(course.course_id)}/lectures`);
  $("#course-title").textContent = course.title || course.course_id;
  $("#course-teacher").textContent = text(course.teacher);
  renderLectureList();
  showView("lectures");
}

function renderLectureList() {
  const list = $("#lecture-list");
  list.replaceChildren();
  courseLectures.forEach((lecture) => {
    const card = document.createElement("article");
    card.className = "lecture-card";
    const row = document.createElement("div");
    row.className = "lecture-row";
    const left = document.createElement("div");
    const displayName = lectureDisplayName(lecture);
    const title = document.createElement("strong");
    title.textContent = displayName;
    left.append(title);
    // Original date/period label demoted to a small caption — shown only
    // when the display name differs (a real name was derived or renamed).
    const timeLabel = lecture.sub_title || String(lecture.sub_id);
    if (displayName !== timeLabel) {
      const time = document.createElement("p");
      time.textContent = timeLabel;
      left.append(time);
    }
    if (lecture.error_msg) {
      const error = document.createElement("p");
      error.textContent = lecture.error_msg;
      left.append(error);
    }
    const state = document.createElement("span");
    state.className = `state-badge ${lectureState(lecture)}`;
    state.textContent = lectureStateLabel(lecture);
    row.append(left, state);
    const open = document.createElement("button");
    open.type = "button";
    open.className = "lecture-open";
    open.append(row);
    open.onclick = () => openLecture(lecture.sub_id);
    card.append(open);
    list.append(card);
  });
}

async function openLecture(subId) {
  currentLecture = await api(`/api/local/lectures/${encodeURIComponent(subId)}`);
  if (!currentCourse || String(currentCourse.course_id) !== String(currentLecture.course_id)) {
    currentCourse = courseRows.find((course) => String(course.course_id) === String(currentLecture.course_id)) || {
      course_id: currentLecture.course_id,
      title: currentLecture.course_title,
      teacher: currentLecture.teacher,
    };
    courseLectures = await api(`/api/local/courses/${encodeURIComponent(currentLecture.course_id)}/lectures`);
  }
  $("#detail-title").textContent = lectureDisplayName(currentLecture);
  const detailSubtitle = currentLecture.sub_title || "";
  $("#detail-subtitle").textContent = detailSubtitle;
  $("#detail-subtitle").classList.toggle(
    "hidden", !detailSubtitle || lectureDisplayName(currentLecture) === detailSubtitle
  );
  $("#detail-course").textContent = currentLecture.course_title || "";
  detailTab = "summary";
  // Empty on purpose — renderSummaryVersions auto-selects the latest version.
  selectedSummaryVersionKeys = new Set();
  renderDetail();
  showView("detail");
}

function lectureState(lecture) {
  if (lecture.error_stage) return "failed";
  return lecture.has_summary ? "ready" : "waiting";
}

function lectureStateLabel(lecture) {
  if (lecture.error_stage) return `失败 · ${lecture.error_stage}`;
  return lecture.has_summary ? "已生成" : "等待处理";
}

function formatTimestamp(seconds) {
  const total = Math.max(0, Number(seconds) || 0);
  const minute = Math.floor(total / 60);
  const second = Math.floor(total % 60);
  return `${String(minute).padStart(2, "0")}:${String(second).padStart(2, "0")}`;
}

function renderDetail() {
  if (!currentLecture) return;
  const root = $("#detail-content");
  root.replaceChildren();
  if (detailTab === "summary") {
    renderSummaryVersions(root);
  } else if (detailTab === "transcript") {
    $("#summary-version-controls").classList.add("hidden");
    const transcript = document.createElement("div");
    transcript.className = "transcript";
    transcript.textContent = currentLecture.transcript || "暂无转录文本";
    root.append(transcript);
  } else {
    $("#summary-version-controls").classList.add("hidden");
    const pages = currentLecture.ppt_pages || [];
    if (!pages.length) root.textContent = "暂无 PPT OCR 数据。";
    pages.forEach((page) => {
      const item = document.createElement("div");
      item.className = "ppt-item";
      const time = document.createElement("span");
      time.className = "ppt-time";
      time.textContent = `[第 ${page.page_num} 页 · ${formatTimestamp(page.created_sec)}]`;
      const content = document.createElement("div");
      content.className = "ppt";
      content.textContent = page.text || "";
      item.append(time, content);
      root.append(item);
    });
  }
  document.querySelectorAll(".detail-tab").forEach((button) => {
    const active = button.dataset.detailTab === detailTab;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  const index = courseLectures.findIndex((lecture) => String(lecture.sub_id) === String(currentLecture.sub_id));
  $("#previous-lecture").disabled = index <= 0;
  $("#next-lecture").disabled = index < 0 || index >= courseLectures.length - 1;
  applyStoredHighlights(root);
}

/* ── 笔记选中高亮 ─────────────────────────────────────────
   选中正文 → 浮动“高亮”按钮 → 用 <mark> 包裹；高亮片段按
   sub_id 存入 localStorage，重新渲染后按原文找回并复原。
   点击已高亮文本会浮出“取消高亮”按钮，再点一次确认取消。 */
const HIGHLIGHTS_KEY = "icourse-local-highlights";

function loadHighlightStore() {
  try { return JSON.parse(localStorage.getItem(HIGHLIGHTS_KEY)) || {}; }
  catch (_) { return {}; }
}

function highlightsFor(subId) {
  return loadHighlightStore()[String(subId)] || [];
}

function storeHighlights(subId, list) {
  const store = loadHighlightStore();
  if (list.length) store[String(subId)] = list;
  else delete store[String(subId)];
  localStorage.setItem(HIGHLIGHTS_KEY, JSON.stringify(store));
}

// 收集 root 下的文本节点；skipHighlights 时跳过已高亮 mark 内部，
// 避免重复包裹同一片段。
function _textNodes(root, skipHighlights = false) {
  const walker = document.createTreeWalker(
    root,
    NodeFilter.SHOW_TEXT,
    skipHighlights
      ? {
        acceptNode(node) {
          return node.parentElement && node.parentElement.closest("mark.user-highlight")
            ? NodeFilter.FILTER_REJECT
            : NodeFilter.FILTER_ACCEPT;
        },
      }
      : null,
  );
  const nodes = [];
  let node;
  while ((node = walker.nextNode())) nodes.push(node);
  return nodes;
}

// 把跨节点的 [start, end) 区间（按文本节点拼接偏移）逐段包进 <mark>。
// 倒序处理：splitText 只影响当前节点之后的部分，前面节点的偏移保持有效。
function _wrapOffsets(nodes, start, end) {
  let offset = 0;
  const segments = [];
  nodes.forEach((node) => {
    const nodeStart = offset;
    offset += node.data.length;
    const overlapStart = Math.max(start, nodeStart);
    const overlapEnd = Math.min(end, offset);
    if (overlapStart < overlapEnd) {
      segments.push([node, overlapStart - nodeStart, overlapEnd - nodeStart]);
    }
  });
  for (let i = segments.length - 1; i >= 0; i -= 1) {
    const [node, segStart, segEnd] = segments[i];
    if (segEnd < node.data.length) node.splitText(segEnd);
    const mid = segStart > 0 ? node.splitText(segStart) : node;
    const mark = document.createElement("mark");
    mark.className = "user-highlight";
    mark.title = "点击后可取消高亮";
    mid.parentNode.replaceChild(mark, mid);
    mark.appendChild(mid);
  }
}

// 选区起止点换算为文本节点拼接偏移（与 _textNodes 的顺序一致）。
function _rangeOffsets(root, range) {
  const nodes = _textNodes(root);
  let offset = 0;
  let start = -1;
  let end = -1;
  for (const node of nodes) {
    if (start === -1 && node === range.startContainer) start = offset + range.startOffset;
    if (node === range.endContainer) { end = offset + range.endOffset; break; }
    offset += node.data.length;
  }
  if (start === -1 || end === -1 || end <= start) return null;
  return [start, end];
}

// 按原文找回一个已存片段并包裹所有尚未高亮的出现。
function _applyHighlightSnippet(root, snippet) {
  if (!snippet) return;
  for (let guard = 0; guard < 500; guard += 1) {
    const nodes = _textNodes(root, true);
    let full = "";
    nodes.forEach((node) => { full += node.data; });
    const index = full.indexOf(snippet);
    if (index === -1) return;
    _wrapOffsets(nodes, index, index + snippet.length);
  }
}

function applyStoredHighlights(root) {
  if (!currentLecture) return;
  highlightsFor(currentLecture.sub_id).forEach((snippet) => {
    _applyHighlightSnippet(root, snippet);
  });
}

const highlightToolbar = document.createElement("button");
highlightToolbar.type = "button";
highlightToolbar.className = "highlight-toolbar hidden";
highlightToolbar.textContent = "高亮";
document.body.append(highlightToolbar);

// 点击已高亮文本时不立即取消，先记录待取消片段并浮出“取消高亮”按钮，
// 再点一次该按钮才真正移除——避免阅读时误触。
let pendingUnhighlight = null;

function hideHighlightToolbar() {
  highlightToolbar.classList.add("hidden");
}

function clearPendingUnhighlight() {
  pendingUnhighlight = null;
  highlightToolbar.textContent = "高亮";
  hideHighlightToolbar();
}

function _positionToolbar(rect) {
  highlightToolbar.style.top = `${Math.max(8, rect.top - 40)}px`;
  highlightToolbar.style.left = `${Math.min(
    window.innerWidth - 70,
    Math.max(8, rect.left + rect.width / 2 - 28),
  )}px`;
}

function _detailSelectionRange() {
  const selection = window.getSelection();
  if (!selection.rangeCount || selection.isCollapsed) return null;
  const range = selection.getRangeAt(0);
  const root = $("#detail-content");
  if (!root.contains(range.commonAncestorContainer)) return null;
  if (!range.toString().trim()) return null;
  return range;
}

function updateHighlightToolbar() {
  const range = _detailSelectionRange();
  if (!range) {
    // 选区塌陷：若正处于“待确认取消”状态则保留工具条，否则隐藏。
    if (!pendingUnhighlight) hideHighlightToolbar();
    return;
  }
  // 新选区出现：回到“高亮”模式。
  pendingUnhighlight = null;
  highlightToolbar.textContent = "高亮";
  _positionToolbar(range.getBoundingClientRect());
  highlightToolbar.classList.remove("hidden");
}

document.addEventListener("selectionchange", () => {
  window.requestAnimationFrame(updateHighlightToolbar);
});
document.addEventListener("scroll", () => clearPendingUnhighlight(), true);

// 点击其他位置（非高亮文本、非工具条）时，放弃待确认的取消操作。
document.addEventListener("click", (event) => {
  if (!pendingUnhighlight) return;
  if (event.target.closest("mark.user-highlight") || event.target === highlightToolbar) return;
  clearPendingUnhighlight();
});

// mousedown 阻止默认行为，点击按钮时选区不会丢失。
highlightToolbar.addEventListener("mousedown", (event) => event.preventDefault());
highlightToolbar.addEventListener("click", () => {
  const root = $("#detail-content");
  // 二次确认分支：工具条处于“取消高亮”模式时执行移除。
  if (pendingUnhighlight) {
    const snippet = pendingUnhighlight;
    clearPendingUnhighlight();
    removeHighlightSnippet(snippet);
    return;
  }
  const range = _detailSelectionRange();
  hideHighlightToolbar();
  if (!range || !currentLecture) return;
  const offsets = _rangeOffsets(root, range);
  if (!offsets) {
    message("该选区无法高亮", true);
    return;
  }
  const [start, end] = offsets;
  let full = "";
  _textNodes(root).forEach((node) => { full += node.data; });
  const snippet = full.slice(start, end);
  if (!snippet.trim()) return;
  _wrapOffsets(_textNodes(root), start, end);
  const list = highlightsFor(currentLecture.sub_id);
  if (!list.includes(snippet)) list.push(snippet);
  storeHighlights(currentLecture.sub_id, list);
  window.getSelection().removeAllRanges();
  message("已高亮");
});

// 移除某一片段的全部高亮（二次确认后由工具条调用）。
function removeHighlightSnippet(snippet) {
  if (!currentLecture) return;
  const root = $("#detail-content");
  root.querySelectorAll("mark.user-highlight").forEach((item) => {
    if (item.textContent === snippet) {
      item.replaceWith(document.createTextNode(item.textContent));
    }
  });
  root.normalize();
  storeHighlights(
    currentLecture.sub_id,
    highlightsFor(currentLecture.sub_id).filter((item) => item !== snippet),
  );
  message("已取消高亮");
}

// 点击已高亮文本：不立即取消，浮出“取消高亮”按钮等待二次确认。
$("#detail-content").addEventListener("click", (event) => {
  const mark = event.target.closest("mark.user-highlight");
  if (!mark || !currentLecture) return;
  pendingUnhighlight = mark.textContent;
  highlightToolbar.textContent = "取消高亮";
  _positionToolbar(mark.getBoundingClientRect());
  highlightToolbar.classList.remove("hidden");
});

function summaryVersions() {
  // One entry per rerun — the backend keys versions by (model, generated_at),
  // so same-model reruns all survive.  Returned latest first.
  const versions = [];
  const seen = new Set();
  (currentLecture?.summary_versions || []).forEach((version) => {
    if (!String(version?.summary || "").trim()) return;
    const key = `${version.model || "unknown"}@${version.generated_at || ""}`;
    if (seen.has(key)) return;
    seen.add(key);
    versions.push({
      key,
      model: String(version.model || "unknown"),
      summary: version.summary,
      generated_at: version.generated_at || "",
    });
  });
  // The lecture row's active summary is authoritative — list it even when
  // the versions table predates the feature (legacy databases).
  if (String(currentLecture?.summary || "").trim()) {
    const activeModel = String(currentLecture.summary_model || "unknown");
    const listed = versions.some(
      (version) => version.model === activeModel && version.summary === currentLecture.summary
    );
    if (!listed) {
      versions.push({
        key: "__active__",
        model: activeModel,
        summary: currentLecture.summary,
        generated_at: currentLecture.processed_at || "",
      });
    }
  }
  versions.sort((a, b) => String(b.generated_at || "").localeCompare(String(a.generated_at || "")));
  return versions;
}

function formatVersionDate(value) {
  if (!value) return "生成时间未记录";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

function renderSummaryVersions(root) {
  const versions = summaryVersions();
  const controls = $("#summary-version-controls");
  controls.replaceChildren();
  if (!versions.length) {
    controls.classList.add("hidden");
    root.innerHTML = `<div class="summary">${renderMarkdown(currentLecture.summary)}</div>`;
    return;
  }
  if (!selectedSummaryVersionKeys.size) selectedSummaryVersionKeys.add(versions[0].key);
  controls.classList.remove("hidden");
  const title = document.createElement("strong");
  title.textContent = versions.length > 1 ? "版本对比（可多选）" : "当前笔记版本";
  controls.append(title);
  const choices = document.createElement("div");
  choices.className = "summary-version-choices";
  const activeModel = String(currentLecture.summary_model || "unknown");
  versions.forEach((version) => {
    const label = document.createElement("label");
    label.className = "summary-version-choice";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = selectedSummaryVersionKeys.has(version.key);
    input.onchange = () => {
      if (input.checked) selectedSummaryVersionKeys.add(version.key);
      else selectedSummaryVersionKeys.delete(version.key);
      renderDetail();
    };
    const name = document.createElement("span");
    name.textContent = version.model;
    const timestamp = document.createElement("small");
    timestamp.textContent = formatVersionDate(version.generated_at);
    label.append(input, name, timestamp);
    // Mark the version that matches the lecture's active summary.
    if (version.model === activeModel && version.summary === currentLecture.summary) {
      const current = document.createElement("small");
      current.className = "summary-version-current";
      current.textContent = "当前";
      label.append(current);
    }
    choices.append(label);
  });
  controls.append(choices);
  const selected = versions.filter((version) => selectedSummaryVersionKeys.has(version.key));
  if (!selected.length) {
    root.textContent = "请选择至少一个版本。";
    return;
  }
  const grid = document.createElement("div");
  grid.className = `summary-version-grid${selected.length > 1 ? " compare" : ""}`;
  selected.forEach((version) => {
    const panel = document.createElement("section");
    panel.className = "summary-version-panel";
    const model = document.createElement("div");
    model.className = "detail-model available";
    model.textContent = `生成模型 · ${version.model}`;
    const summary = document.createElement("div");
    summary.className = "summary";
    // Model output is escaped by renderMarkdown before it becomes HTML.
    summary.innerHTML = renderMarkdown(version.summary);
    panel.append(model, summary);
    grid.append(panel);
  });
  root.append(grid);
}

async function populateRerunModelSelect(selector, refresh = false) {
  if (refresh || !rerunModelOptions.length) {
    const result = await api("/api/local/model-providers");
    rerunModelOptions = [];
    (result.providers || []).forEach((provider) => {
      if (!provider.enabled || !provider.api_key_configured) return;
      (provider.models || []).forEach((model) => {
        rerunModelOptions.push({provider: String(provider.name), model: String(model)});
      });
    });
  }
  if (!rerunModelOptions.length) {
    throw new Error("没有可用于重跑的模型；请先在模型管理中启用模型并保存 API Key。");
  }
  const select = $(selector);
  select.replaceChildren();
  rerunModelOptions.forEach((option, index) => {
    select.append(new Option(`${option.provider} / ${option.model}`, String(index)));
  });
}

function selectedRerunModel(selector) {
  return rerunModelOptions[Number($(selector).value)];
}

async function submitBatchRerun(subIds, courseIds, option, button) {
  if (!option) {
    message("请先选择模型。", true);
    return null;
  }
  const selectionText = courseIds.length ? `${courseIds.length} 门课程` : `${subIds.length} 个课次`;
  if (!confirm(`使用 ${option.provider} / ${option.model} 重跑已选 ${selectionText}？\n\n每个模型的版本都会保留；实际可重跑课次最多 20 个，并会产生模型费用。`)) return null;
  button.disabled = true;
  const original = button.textContent;
  button.textContent = "已提交…";
  try {
    const result = await api("/api/local/summary-reruns", {
      method: "POST",
      body: JSON.stringify({...option, sub_ids: subIds, course_ids: courseIds}),
    });
    message(`已提交 ${result.count} 个课次重跑；完成后检查更新即可查看各模型版本。`);
    setTimeout(() => loadRuns().catch(() => {}), 1500);
    return result;
  } catch (error) {
    message(error.message, true);
    return null;
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

/* ── 重跑视图 ─────────────────────────────────────────────
   课程级勾选走 course_ids（该课全部有转录的课次）；展开课程
   可改选单个课次走 sub_ids。整课勾选时清空其课次选择。 */
const rerunSelectedCourseIds = new Set();
const rerunSelectedSubIds = new Set();
const rerunOpenCourseIds = new Set();
const rerunLectureCache = new Map();

function updateRerunSubmit() {
  const courses = rerunSelectedCourseIds.size;
  const subs = rerunSelectedSubIds.size;
  const button = $("#rerun-page-submit");
  button.disabled = !courses && !subs;
  const parts = [];
  if (courses) parts.push(`${courses} 门课程`);
  if (subs) parts.push(`${subs} 节课次`);
  button.textContent = parts.length ? `重跑已选（${parts.join(" + ")}）` : "重跑已选";
}

async function toggleRerunCourseExpand(courseId) {
  const cid = String(courseId);
  if (rerunOpenCourseIds.has(cid)) {
    rerunOpenCourseIds.delete(cid);
    renderRerunView();
    return;
  }
  rerunOpenCourseIds.add(cid);
  renderRerunView();
  if (!rerunLectureCache.has(cid)) {
    try {
      const lectures = await api(`/api/local/courses/${encodeURIComponent(cid)}/lectures`);
      rerunLectureCache.set(cid, lectures);
    } catch (error) {
      rerunOpenCourseIds.delete(cid);
      message(error.message, true);
    }
    renderRerunView();
  }
}

function renderRerunView() {
  const list = $("#rerun-course-list");
  list.replaceChildren();
  if (!courseRows.length) {
    list.textContent = "数据库中还没有课程。";
    list.classList.add("empty");
    return;
  }
  list.classList.remove("empty");
  courseRows.forEach((course) => {
    const cid = String(course.course_id);
    const courseChecked = rerunSelectedCourseIds.has(cid);
    const card = document.createElement("div");
    card.className = "rerun-course";

    const head = document.createElement("div");
    head.className = "rerun-course-head";
    const label = document.createElement("label");
    label.className = "rerun-course-check";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = courseChecked;
    checkbox.onchange = () => {
      if (checkbox.checked) {
        rerunSelectedCourseIds.add(cid);
        // 整课重跑已包含全部课次，清掉该课的单课次选择
        (rerunLectureCache.get(cid) || []).forEach((lec) =>
          rerunSelectedSubIds.delete(String(lec.sub_id)));
      } else {
        rerunSelectedCourseIds.delete(cid);
      }
      renderRerunView();
    };
    const title = document.createElement("span");
    title.className = "rerun-course-title";
    title.textContent = course.title || cid;
    label.append(checkbox, title);
    const meta = document.createElement("span");
    meta.className = "rerun-course-meta";
    meta.textContent = `笔记 ${course.summary_count}/${course.total_count}`;
    const expand = document.createElement("button");
    expand.type = "button";
    expand.className = "rerun-expand";
    expand.textContent = rerunOpenCourseIds.has(cid) ? "▾ 收起" : "▸ 选课次";
    expand.onclick = () => toggleRerunCourseExpand(cid);
    head.append(label, meta, expand);
    card.append(head);

    if (rerunOpenCourseIds.has(cid)) {
      const box = document.createElement("div");
      box.className = "rerun-lectures";
      const lectures = rerunLectureCache.get(cid);
      if (!lectures) {
        box.textContent = "正在读取课次…";
      } else if (!lectures.length) {
        box.textContent = "暂无课次。";
      } else {
        lectures.forEach((lecture) => {
          const sid = String(lecture.sub_id);
          const rerunnable = Boolean(Number(lecture.transcript_available));
          const row = document.createElement("label");
          row.className = `rerun-lecture${rerunnable ? "" : " disabled"}`;
          const input = document.createElement("input");
          input.type = "checkbox";
          input.disabled = !rerunnable || courseChecked;
          input.checked = courseChecked || rerunSelectedSubIds.has(sid);
          input.title = rerunnable ? "" : "没有可用转录，不能只重跑摘要";
          input.onchange = () => {
            if (input.checked) rerunSelectedSubIds.add(sid);
            else rerunSelectedSubIds.delete(sid);
            updateRerunSubmit();
          };
          const name = document.createElement("span");
          name.textContent = lectureDisplayName(lecture);
          row.append(input, name);
          box.append(row);
        });
      }
      card.append(box);
    }
    list.append(card);
  });
  updateRerunSubmit();
}

async function loadRerunView() {
  if (!courseRows.length) courseRows = await api("/api/local/courses");
  try {
    await populateRerunModelSelect("#rerun-page-model-select", true);
  } catch (error) {
    message(error.message, true);
  }
  renderRerunView();
}

$("#rerun-page-submit").onclick = async () => {
  const result = await submitBatchRerun(
    [...rerunSelectedSubIds], [...rerunSelectedCourseIds],
    selectedRerunModel("#rerun-page-model-select"), $("#rerun-page-submit"),
  );
  if (result) {
    rerunSelectedCourseIds.clear();
    rerunSelectedSubIds.clear();
    renderRerunView();
  }
};

function renderSubscriptionCourse(course, action, label) {
  const row = document.createElement("div");
  row.className = "subscription-row";
  const content = document.createElement("div");
  const title = document.createElement("strong");
  title.textContent = course.title || course.course_id;
  const meta = document.createElement("span");
  meta.className = "meta";
  meta.textContent = [course.teacher, course.dept, course.term, course.course_id].filter(Boolean).join(" · ");
  content.append(title, meta);
  const button = createButton(label, action, "subscription-action");
  button.setAttribute("aria-label", `${label}${course.title || course.course_id}`);
  row.append(content, button);
  return row;
}

// 已订阅排序：time = 订阅时间（course_ids 列表顺序，新订阅追加在末尾）；
// term = 课程学期。正/倒序与字段选择都持久化在本机 localStorage。
const SUBSCRIPTION_SORT_KEY = "icourse-local-subscription-sort";
const SUBSCRIPTION_SORT_DIR_KEY = "icourse-local-subscription-sort-dir";
let subscriptionSort = localStorage.getItem(SUBSCRIPTION_SORT_KEY) || "time";
let subscriptionSortDir = localStorage.getItem(SUBSCRIPTION_SORT_DIR_KEY) || "asc";

function orderedSubscriptionCourses() {
  const ordered = [...subscriptionCourses];
  if (subscriptionSort === "term") {
    ordered.sort((a, b) =>
      String(a.term || "").localeCompare(String(b.term || ""), "zh")
      || String(a.title || a.course_id).localeCompare(String(b.title || b.course_id), "zh")
    );
  } else {
    const position = new Map(subscribedCourseIds.map((id, index) => [String(id), index]));
    ordered.sort((a, b) =>
      (position.get(String(a.course_id)) ?? Number.MAX_SAFE_INTEGER)
      - (position.get(String(b.course_id)) ?? Number.MAX_SAFE_INTEGER)
    );
  }
  if (subscriptionSortDir === "desc") ordered.reverse();
  return ordered;
}

function renderSubscriptions() {
  const current = $("#subscription-list");
  const catalog = $("#subscription-catalog");
  current.replaceChildren();
  catalog.replaceChildren();
  $("#subscription-count").textContent = String(subscribedCourseIds.length);
  current.classList.toggle("empty", !subscriptionCourses.length);
  if (!subscriptionCourses.length) current.textContent = "尚未订阅课程。";
  orderedSubscriptionCourses().forEach((course) => {
    current.append(renderSubscriptionCourse(course, () => {
      subscribedCourseIds = subscribedCourseIds.filter((id) => String(id) !== String(course.course_id));
      subscriptionCourses = subscriptionCourses.filter((item) => String(item.course_id) !== String(course.course_id));
      renderSubscriptions();
      queueSubscriptionSave();
    }, "移除"));
  });
  catalog.classList.toggle("empty", !catalogRows.length);
  if (!catalogRows.length) catalog.textContent = "没有匹配的课程。";
  catalogRows.forEach((course) => {
    const subscribed = subscribedCourseIds.includes(String(course.course_id));
    catalog.append(renderSubscriptionCourse(course, () => {
      if (subscribed) {
        subscribedCourseIds = subscribedCourseIds.filter((id) => String(id) !== String(course.course_id));
        subscriptionCourses = subscriptionCourses.filter((item) => String(item.course_id) !== String(course.course_id));
      } else {
        subscribedCourseIds = [...subscribedCourseIds, String(course.course_id)];
        subscriptionCourses = [...subscriptionCourses, course];
      }
      renderSubscriptions();
      // 点击“订阅/移除”立即保存，无需再按保存按钮。
      queueSubscriptionSave();
    }, subscribed ? "移除" : "订阅"));
  });
}

async function loadSubscriptionCatalog() {
  const query = $("#subscription-query").value.trim();
  const term = $("#subscription-term").value;
  const result = await api(`/api/local/subscription-catalog?q=${encodeURIComponent(query)}&term=${encodeURIComponent(term)}`);
  catalogRows = result.courses || [];
  const select = $("#subscription-term");
  const selected = select.value;
  if (JSON.stringify(subscriptionTerms) !== JSON.stringify(result.terms || [])) {
    subscriptionTerms = result.terms || [];
    select.replaceChildren(new Option("全部学期", ""));
    subscriptionTerms.forEach((item) => select.append(new Option(item, item)));
    select.value = subscriptionTerms.includes(selected) ? selected : "";
  }
  renderSubscriptions();
}

async function loadSubscriptions() {
  const state = await api("/api/local/subscriptions");
  subscribedCourseIds = (state.course_ids || []).map(String);
  subscriptionCourses = state.courses || [];
  await loadSubscriptionCatalog();
}

let subscriptionSaving = false;
let subscriptionSaveQueued = false;

// 点击“订阅/移除”后立即落盘：快速连续点击时合并为一次最终保存。
function queueSubscriptionSave() {
  subscriptionSaveQueued = true;
  if (!subscriptionSaving) saveSubscriptions();
}

async function saveSubscriptions() {
  if (subscriptionSaving) return;
  subscriptionSaving = true;
  try {
    while (subscriptionSaveQueued) {
      subscriptionSaveQueued = false;
      const state = await api("/api/local/subscriptions", {
        method: "PUT",
        body: JSON.stringify({course_ids: subscribedCourseIds}),
      });
      subscribedCourseIds = (state.course_ids || []).map(String);
      subscriptionCourses = state.courses || [];
      renderSubscriptions();
      message(`已保存 ${subscribedCourseIds.length} 门课程的订阅`);
    }
  } catch (error) {
    message(error.message, true);
    // 保存失败时重新拉取远端状态，避免界面与 Secret 不一致。
    try { await loadSubscriptions(); } catch (_) {}
  } finally {
    subscriptionSaving = false;
  }
}

async function loadRuns() {
  const runs = await api("/api/local/workflows");
  const root = $("#runs");
  root.replaceChildren();
  root.classList.remove("empty");
  if (!runs.length) {
    root.textContent = "没有 workflow 运行记录。";
    return;
  }
  runs.forEach((run) => {
    const row = document.createElement("div");
    row.className = "run";
    const link = document.createElement("a");
    link.href = run.html_url;
    link.target = "_blank";
    link.rel = "noreferrer";
    link.textContent = run.name;
    const meta = document.createElement("div");
    const statusKey = run.conclusion || run.status;
    meta.className = `meta status-${statusKey}`;
    const stamp = new Date(run.created_at);
    const stampText = Number.isNaN(stamp.getTime())
      ? ""
      : stamp.toLocaleString("zh-CN", {month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit"});
    meta.textContent = `${RUN_STATUS_LABELS[statusKey] || statusKey}${stampText ? " · " + stampText : ""}`;
    row.append(link, meta);
    root.append(row);
  });
}

function setModelView(open) {
  if (open) showView("models"); else showView("settings", {keepScroll: true});
}

function setObsidianView(open) {
  if (open) showView("obsidian"); else showView("settings", {keepScroll: true});
}

function showView(view, options = {}) {
  const changed = view !== activeView;
  activeView = view;
  const paneIds = {
    courses: "view-courses", lectures: "view-lectures", detail: "view-detail",
    search: "view-search", subscriptions: "view-subscriptions", rerun: "view-rerun",
    automation: "view-automation", settings: "view-settings",
    models: "model-management", obsidian: "obsidian-sync",
  };
  Object.entries(paneIds).forEach(([name, id]) => $("#" + id).classList.toggle("hidden", name !== view));
  const title = {
    courses: "iCourse", lectures: currentCourse?.title || "课程", detail: currentLecture?.sub_title || "笔记",
    search: "搜索", subscriptions: "订阅", rerun: "重跑",
    automation: "自动化", settings: "设置", models: "模型与 API", obsidian: "同步到 Obsidian",
  }[view] || "iCourse";
  $("#page-title").textContent = title;
  $("#back-button").classList.toggle("hidden", !["lectures", "detail", "models", "obsidian"].includes(view));
  // Lectures/detail are depths of the courses path — keep 课程 highlighted.
  const navView = ["lectures", "detail"].includes(view) ? "courses" : view;
  document.querySelectorAll(".nav-button").forEach((button) => {
    const active = button.dataset.view === navView;
    button.classList.toggle("active", active);
    button.toggleAttribute("aria-current", active);
  });
  // Forward navigation starts at the top; explicit back actions keep the
  // previous scroll position (callers pass {keepScroll: true}).
  if (changed && !options.keepScroll) window.scrollTo(0, 0);
}

function normalizeProvider(item) {
  const apiKeyEnv = String(item.api_key_env || "");
  return {
    enabled: item.enabled !== false,
    name: String(item.name || ""),
    base_url: String(item.base_url || ""),
    api_key_env: apiKeyEnv,
    models: Array.isArray(item.models) ? item.models.map(String) : [],
    api_key: "",
    api_key_configured: Boolean(item.api_key_configured),
    configured_api_key_env: item.api_key_configured ? apiKeyEnv.toUpperCase() : "",
  };
}

function modelSourceLabel(source) {
  return {
    "github-variable": "配置来源：GitHub Actions Variable",
    "local-cache": "配置来源：本地缓存（GitHub 尚未保存）",
    defaults: "配置来源：项目默认值（保存后写入 GitHub）",
  }[source] || `配置来源：${source || "未知"}`;
}

async function loadModelProviders() {
  const list = $("#model-provider-list");
  list.replaceChildren();
  const loading = document.createElement("p");
  loading.className = "empty";
  loading.textContent = "正在读取 GitHub 中的模型配置…";
  list.append(loading);
  const result = await api("/api/local/model-providers");
  modelProviders = (result.providers || []).map(normalizeProvider);
  const source = $("#model-source");
  source.textContent = modelSourceLabel(result.source);
  source.classList.remove("hidden");
  renderModelProviders();
}

function moveProvider(index, offset) {
  const destination = index + offset;
  if (destination < 0 || destination >= modelProviders.length) return;
  const [provider] = modelProviders.splice(index, 1);
  modelProviders.splice(destination, 0, provider);
  renderModelProviders();
}

function secretStatus(provider, node) {
  const currentEnv = provider.api_key_env.trim().toUpperCase();
  node.className = "secret-status";
  if (provider.api_key.trim()) {
    node.classList.add("pending");
    node.textContent = "已输入新 Key，保存后将覆盖对应 Secret";
  } else if (
    provider.api_key_configured
    && currentEnv === provider.configured_api_key_env
  ) {
    node.classList.add("configured");
    node.textContent = "GitHub Secret 已配置；留空将保留";
  } else {
    node.textContent = "尚未配置；启用前需要输入 API Key";
  }
}

async function testModelProvider(index, button, resultNode) {
  const provider = modelProviders[index];
  const apiKey = provider.api_key.trim();
  const model = provider.models.map((item) => item.trim()).find(Boolean);
  if (!apiKey) {
    message("测试只能使用本次重新输入的 API Key；GitHub 不允许读取已有 Secret。", true);
    return;
  }
  if (!model) {
    message("请至少填写一个模型，测试会使用第一行。", true);
    return;
  }
  if (!confirm(`测试 ${provider.name || "该供应商"} 的首个模型 ${model}？这会消耗极少量 token。`)) return;
  button.disabled = true;
  resultNode.textContent = "正在连接…";
  try {
    const result = await api("/api/local/model-providers/test", {
      method: "POST",
      body: JSON.stringify({
        name: provider.name,
        base_url: provider.base_url,
        api_key_env: provider.api_key_env,
        model,
        api_key: apiKey,
      }),
    });
    resultNode.textContent = `连接成功 · ${text(result.model)} · ${result.latency_ms} ms`;
    resultNode.className = "meta status-success";
    message("模型 API 连接测试成功");
  } catch (error) {
    resultNode.textContent = `测试失败 · ${error.message}`;
    resultNode.className = "meta status-failure";
    message(error.message, true);
  } finally {
    button.disabled = false;
  }
}

function renderModelProviders() {
  const root = $("#model-provider-list");
  root.replaceChildren();
  if (!modelProviders.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "尚未添加供应商。";
    root.append(empty);
    return;
  }

  modelProviders.forEach((provider, index) => {
    const card = document.createElement("section");
    card.className = `provider-card${provider.enabled ? "" : " is-disabled"}`;

    const head = document.createElement("div");
    head.className = "provider-head";
    const titleGroup = document.createElement("div");
    titleGroup.className = "provider-title";
    const priority = document.createElement("span");
    priority.className = "priority";
    priority.textContent = `#${index + 1}`;
    const title = document.createElement("strong");
    title.textContent = provider.name || "未命名供应商";
    const toggle = document.createElement("label");
    toggle.className = "toggle";
    const enabled = document.createElement("input");
    enabled.type = "checkbox";
    enabled.checked = provider.enabled;
    enabled.addEventListener("change", () => {
      provider.enabled = enabled.checked;
      card.classList.toggle("is-disabled", !provider.enabled);
    });
    toggle.append(enabled, document.createTextNode("启用"));
    titleGroup.append(priority, title, toggle);

    const controls = document.createElement("div");
    controls.className = "provider-controls";
    const up = createButton("↑ 上移", () => moveProvider(index, -1));
    up.disabled = index === 0;
    const down = createButton("↓ 下移", () => moveProvider(index, 1));
    down.disabled = index === modelProviders.length - 1;
    const remove = createButton("删除", () => {
      if (modelProviders.length === 1) {
        message("至少需要保留一个模型供应商。", true);
        return;
      }
      if (!confirm(`删除供应商 ${provider.name || `#${index + 1}`}？已保存的 GitHub Secret 不会被删除。`)) return;
      modelProviders.splice(index, 1);
      renderModelProviders();
    }, "danger");
    controls.append(up, down, remove);
    head.append(titleGroup, controls);

    const fields = document.createElement("div");
    fields.className = "provider-fields";

    const nameInput = document.createElement("input");
    nameInput.value = provider.name;
    nameInput.required = true;
    nameInput.maxLength = 50;
    nameInput.autocomplete = "off";
    nameInput.spellcheck = false;
    nameInput.placeholder = "例如 deepseek";
    nameInput.addEventListener("input", () => {
      provider.name = nameInput.value;
      title.textContent = provider.name || "未命名供应商";
    });
    fields.append(createField("供应商名称", nameInput, "仅使用字母、数字、下划线或连字符"));

    const baseUrlInput = document.createElement("input");
    baseUrlInput.type = "url";
    baseUrlInput.value = provider.base_url;
    baseUrlInput.required = true;
    baseUrlInput.autocomplete = "off";
    baseUrlInput.spellcheck = false;
    baseUrlInput.placeholder = "https://api.example.com/v1";
    baseUrlInput.addEventListener("input", () => { provider.base_url = baseUrlInput.value; });
    fields.append(createField("Base URL", baseUrlInput, "必须是兼容 OpenAI Chat Completions 的 HTTPS 地址"));

    const secretInput = document.createElement("input");
    secretInput.value = provider.api_key_env;
    secretInput.required = true;
    secretInput.maxLength = 100;
    secretInput.autocomplete = "off";
    secretInput.spellcheck = false;
    secretInput.placeholder = "LLM_MY_PROVIDER_API_KEY";
    const status = document.createElement("span");
    secretInput.addEventListener("input", () => {
      provider.api_key_env = secretInput.value;
      secretStatus(provider, status);
    });
    const secretField = createField("API Key Secret 名称", secretInput, "自定义名称需符合 LLM_*_API_KEY");
    secretStatus(provider, status);
    secretField.append(status);
    fields.append(secretField);

    const apiKeyInput = document.createElement("input");
    apiKeyInput.type = "password";
    apiKeyInput.value = provider.api_key;
    apiKeyInput.autocomplete = "new-password";
    apiKeyInput.spellcheck = false;
    apiKeyInput.placeholder = provider.api_key_configured ? "已配置；留空不修改" : "输入 API Key";
    apiKeyInput.addEventListener("input", () => {
      provider.api_key = apiKeyInput.value;
      secretStatus(provider, status);
    });
    fields.append(createField("API Key", apiKeyInput, "仅发送给本地服务；保存时加密写入 GitHub Secret"));

    const models = document.createElement("textarea");
    models.value = provider.models.join("\n");
    models.rows = Math.max(4, Math.min(8, provider.models.length + 1));
    models.placeholder = "每行一个模型名称\n例如 deepseek-chat";
    models.spellcheck = false;
    models.addEventListener("input", () => {
      provider.models = models.value.split(/\r?\n/);
    });
    fields.append(createField("模型及其优先级", models, "每行一个；第一行优先，失败后自动尝试下一行", "wide models-field"));

    const testRow = document.createElement("div");
    testRow.className = "provider-test-row";
    const testResult = document.createElement("p");
    testResult.className = "meta";
    testResult.textContent = "测试使用第一行模型；需要重新输入 Key。";
    const testButton = createButton("测试首个模型", () => testModelProvider(index, testButton, testResult));
    testRow.append(testButton, testResult);

    card.append(head, fields, testRow);
    root.append(card);
  });
}

function addModelProvider() {
  const usedNames = new Set(modelProviders.map((item) => item.name));
  let suffix = 1;
  while (usedNames.has(`custom-${suffix}`)) suffix += 1;
  modelProviders.push({
    enabled: true,
    name: `custom-${suffix}`,
    base_url: "",
    api_key_env: `LLM_CUSTOM_${suffix}_API_KEY`,
    models: [],
    api_key: "",
    api_key_configured: false,
    configured_api_key_env: "",
  });
  renderModelProviders();
  const cards = $("#model-provider-list").querySelectorAll(".provider-card");
  cards[cards.length - 1]?.scrollIntoView({behavior: "smooth", block: "center"});
}

async function saveModelProviders() {
  if (!modelProviders.length) {
    message("至少需要一个模型供应商。", true);
    return;
  }
  if (!confirm("保存会更新 GitHub Actions 的模型配置，并写入本次填写的 API Key。继续吗？")) return;
  const button = $("#model-save-button");
  button.disabled = true;
  try {
    const providers = modelProviders.map((provider) => {
      const result = {
        enabled: provider.enabled,
        name: provider.name.trim(),
        base_url: provider.base_url.trim(),
        api_key_env: provider.api_key_env.trim().toUpperCase(),
        models: provider.models.map((item) => item.trim()).filter(Boolean),
      };
      if (provider.api_key.trim()) result.api_key = provider.api_key.trim();
      return result;
    });
    await api("/api/local/model-providers", {
      method: "PUT",
      body: JSON.stringify({providers}),
    });
    message("模型配置已保存到 GitHub；正在重新加载…");
    await loadModelProviders();
  } catch (error) {
    message(error.message, true);
  } finally {
    button.disabled = false;
  }
}

const OBSIDIAN_STATUS = {
  create: {label: "新建", className: "create"},
  update: {label: "更新", className: "update"},
  unchanged: {label: "无变化", className: "unchanged"},
  conflict: {label: "冲突（不覆盖）", className: "conflict"},
};

function obsidianSettingsFromForm() {
  return {
    vault_path: $("#obsidian-vault-path").value.trim(),
    include_transcript: $("#obsidian-include-transcript").checked,
    include_ocr: $("#obsidian-include-ocr").checked,
  };
}

function obsidianCount(value) {
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? number : 0;
}

function resetObsidianPreview() {
  obsidianPlan = null;
  $("#obsidian-preview").classList.add("hidden");
  $("#obsidian-sync-confirm-button").disabled = true;
  $("#obsidian-sync-confirm-button").textContent = "确认同步";
  $("#obsidian-preview-counts").replaceChildren();
  $("#obsidian-preview-list").replaceChildren();
}

function invalidateObsidianPreview() {
  if (!obsidianPlan && $("#obsidian-preview").classList.contains("hidden")) return;
  resetObsidianPreview();
}

function renderObsidianCounts(counts, completed = false) {
  const root = $("#obsidian-preview-counts");
  root.replaceChildren();
  const items = completed
    ? [
      ["created", "已新建", "create"],
      ["updated", "已更新", "update"],
      ["unchanged", "无变化", "unchanged"],
      ["conflict", "未覆盖冲突", "conflict"],
    ]
    : [
      ["create", "将新建", "create"],
      ["update", "将更新", "update"],
      ["unchanged", "无变化", "unchanged"],
      ["conflict", "冲突不覆盖", "conflict"],
    ];
  items.forEach(([key, label, className]) => {
    const item = document.createElement("div");
    item.className = `preview-count ${className}`;
    const count = document.createElement("strong");
    count.textContent = obsidianCount(counts?.[key]);
    const name = document.createElement("span");
    name.textContent = label;
    item.append(count, name);
    root.append(item);
  });
}

function renderObsidianPreviewItems(items) {
  const root = $("#obsidian-preview-list");
  root.replaceChildren();
  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "没有可导出的笔记。请等待自动更新完成，或确认课程已经生成摘要。";
    root.append(empty);
    return;
  }
  items.forEach((item) => {
    const status = OBSIDIAN_STATUS[item.status] || {label: "待确认", className: "unknown"};
    const row = document.createElement("article");
    row.className = `obsidian-preview-item ${status.className}`;
    const badge = document.createElement("span");
    badge.className = "obsidian-item-status";
    badge.textContent = status.label;
    const content = document.createElement("div");
    content.className = "obsidian-item-content";
    const title = document.createElement("strong");
    title.textContent = text(item.title);
    const path = document.createElement("span");
    path.className = "meta obsidian-item-path";
    path.textContent = text(item.path);
    content.append(title, path);
    if (item.reason) {
      const reason = document.createElement("p");
      reason.className = "obsidian-item-reason";
      reason.textContent = String(item.reason);
      content.append(reason);
    }
    row.append(badge, content);
    root.append(row);
  });
}

function renderObsidianPreview(preview) {
  const counts = preview.counts || {};
  const total = ["create", "update", "unchanged", "conflict"]
    .reduce((sum, key) => sum + obsidianCount(counts[key]), 0);
  $("#obsidian-preview-title").textContent = `${text(preview.vault_name)} 的变更预览`;
  $("#obsidian-preview-meta").textContent = `${total} 篇笔记 · 输出目录：${text(preview.output_folder)}`;
  renderObsidianCounts(counts);
  renderObsidianPreviewItems(Array.isArray(preview.items) ? preview.items : []);
  const syncButton = $("#obsidian-sync-confirm-button");
  syncButton.disabled = !obsidianPlan;
  const changed = obsidianCount(counts.create) + obsidianCount(counts.update);
  syncButton.textContent = changed ? `确认同步 ${changed} 篇笔记` : "确认同步（无写入）";
  $("#obsidian-preview").classList.remove("hidden");
}

async function loadObsidianSettings() {
  resetObsidianPreview();
  const settings = await api("/api/local/obsidian/settings");
  $("#obsidian-vault-path").value = String(settings.vault_path || "");
  $("#obsidian-include-transcript").checked = Boolean(settings.include_transcript);
  $("#obsidian-include-ocr").checked = Boolean(settings.include_ocr);
}

async function previewObsidianSync() {
  const settings = obsidianSettingsFromForm();
  if (!settings.vault_path) {
    message("请先填写 Obsidian Vault 路径。", true);
    $("#obsidian-vault-path").focus();
    return;
  }
  const button = $("#obsidian-preview-button");
  button.disabled = true;
  button.textContent = "正在生成预览…";
  resetObsidianPreview();
  try {
    const preview = await api("/api/local/obsidian/preview", {
      method: "POST",
      body: JSON.stringify(settings),
    });
    if (!preview.plan_id) throw new Error("本地服务未返回同步计划，请重新预览。");
    obsidianPlan = String(preview.plan_id);
    renderObsidianPreview(preview);
    const changed = obsidianCount(preview.counts?.create) + obsidianCount(preview.counts?.update);
    message(changed ? `预览完成：将写入 ${changed} 篇笔记。` : "预览完成：没有需要写入的笔记。");
  } catch (error) {
    message(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "预览变更";
  }
}

async function syncObsidianPlan() {
  if (!obsidianPlan) {
    message("请先生成同步预览。", true);
    return;
  }
  if (!confirm("确认将预览中的新建和更新笔记写入本机 Obsidian Vault？冲突文件仍会保留原样。")) return;
  const planId = obsidianPlan;
  const button = $("#obsidian-sync-confirm-button");
  button.disabled = true;
  button.textContent = "正在同步…";
  try {
    const result = await api("/api/local/obsidian/sync", {
      method: "POST",
      body: JSON.stringify({plan_id: planId}),
    });
    obsidianPlan = null;
    $("#obsidian-preview-title").textContent = "Obsidian 同步完成";
    $("#obsidian-preview-meta").textContent = `输出目录：${text(result.output_folder)}`;
    renderObsidianCounts(result.counts || {}, true);
    const list = $("#obsidian-preview-list");
    list.replaceChildren();
    const completed = document.createElement("p");
    completed.className = "empty";
    completed.textContent = "已完成本次同步。若课程数据之后有变化，请重新预览。";
    list.append(completed);
    button.textContent = "已同步";
    const created = obsidianCount(result.counts?.created);
    const updated = obsidianCount(result.counts?.updated);
    const conflict = obsidianCount(result.counts?.conflict);
    message(`Obsidian 同步完成：新建 ${created}，更新 ${updated}，保留冲突 ${conflict}。`);
  } catch (error) {
    obsidianPlan = null;
    button.textContent = "请重新预览";
    message(`同步未完成：${error.message}。请重新预览后再试。`, true);
  } finally {
    button.disabled = true;
  }
}

/* ── 日间/夜间模式 ──
   data-theme 已在 <head> 内联脚本中早于首屏设置，这里只负责按钮状态与切换。 */
const THEME_KEY = "icourse-local-theme";

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  const button = $("#theme-toggle");
  const dark = theme === "dark";
  button.textContent = dark ? "☀️" : "🌙";
  const label = dark ? "切换到日间模式" : "切换到夜间模式";
  button.setAttribute("aria-label", label);
  button.title = label;
}

applyTheme(document.documentElement.dataset.theme === "dark" ? "dark" : "light");
$("#theme-toggle").onclick = () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  try { localStorage.setItem(THEME_KEY, next); } catch (_) {}
  applyTheme(next);
};

$("#setup-form").onsubmit = async (event) => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.currentTarget));
  const submit = event.currentTarget.querySelector("button[type=submit]");
  submit.disabled = true;
  submit.textContent = "正在连接…";
  try {
    await api("/api/local/configure", {method: "POST", body: JSON.stringify(data)});
    message("本地会话已配置");
    activeView = "courses";
    await refreshStatus();
  } catch (error) {
    message(error.message, true);
  } finally {
    submit.disabled = false;
    submit.textContent = "连接并打开本地资料库";
  }
};

$("#configure-button").onclick = () => {
  $("#dashboard").classList.add("hidden");
  $("#app-header").classList.add("hidden");
  $("#mobile-nav").classList.add("hidden");
  $("#setup").classList.remove("hidden");
  window.scrollTo({top: 0, behavior: "smooth"});
};

$("#forget-credentials-button").onclick = async () => {
  if (!confirm("忘记这台 Mac 上保存的登录信息？本地加密资料库会保留，但再次打开需要重新登录。")) return;
  try {
    await api("/api/local/credentials/forget", {method: "POST", body: "{}"});
    message("已忘记本机登录信息");
    activeView = "courses";
    await refreshStatus();
  } catch (error) {
    message(error.message, true);
  }
};

$("#model-button").onclick = async () => {
  const button = $("#model-button");
  button.disabled = true;
  setModelView(true);
  try {
    await loadModelProviders();
  } catch (error) {
    const source = $("#model-source");
    source.textContent = "配置读取失败";
    source.classList.remove("hidden");
    const root = $("#model-provider-list");
    root.replaceChildren();
    const detail = document.createElement("p");
    detail.className = "empty status-failure";
    detail.textContent = `${error.message}。请确认 Token 已授予 Variables 与 Secrets 权限。`;
    root.append(detail);
    message(error.message, true);
  } finally {
    button.disabled = false;
  }
};

$("#model-close-button").onclick = () => setModelView(false);
$("#model-add-button").onclick = addModelProvider;
$("#model-save-button").onclick = saveModelProviders;

$("#obsidian-button").onclick = async () => {
  const button = $("#obsidian-button");
  button.disabled = true;
  setObsidianView(true);
  try {
    await loadObsidianSettings();
  } catch (error) {
    message(`无法读取 Obsidian 设置：${error.message}`, true);
  } finally {
    button.disabled = false;
  }
};

$("#obsidian-close-button").onclick = () => setObsidianView(false);
$("#obsidian-form").onsubmit = (event) => {
  event.preventDefault();
  previewObsidianSync();
};
$("#obsidian-sync-confirm-button").onclick = syncObsidianPlan;
["#obsidian-vault-path", "#obsidian-include-transcript", "#obsidian-include-ocr"].forEach((selector) => {
  $(selector).addEventListener("input", invalidateObsidianPreview);
  $(selector).addEventListener("change", invalidateObsidianPreview);
});

async function syncDatabase() {
  const button = $("#header-sync-button");
  try {
    button.disabled = true;
    button.classList.add("syncing");
    message("正在检查并更新本地资料库…");
    const result = await api("/api/local/sync", {method: "POST", body: "{}"});
    message(result.unchanged ? "本地资料已是最新" : `资料库已更新：${result.courses} 门课程`);
    await refreshStatus();
  } catch (error) {
    message(error.message, true);
  } finally {
    button.disabled = false;
    button.classList.remove("syncing");
  }
}

$("#header-sync-button").onclick = syncDatabase;

$("#run-button").onclick = async () => {
  if (!confirm("立即触发 iCourse Check workflow？")) return;
  try {
    await api("/api/local/workflows/check.yml/dispatch", {
      method: "POST",
      body: JSON.stringify({ref: "main", inputs: {}}),
    });
    message("已触发课程检查");
    setTimeout(loadRuns, 1500);
  } catch (error) {
    message(error.message, true);
  }
};

$("#backfill-titles-button").onclick = async () => {
  if (!confirm("为所有缺少标题的笔记生成 AI 标题？\n\n每篇笔记一次小调用（基于 PPT 与转录），不会重新生成摘要。")) return;
  try {
    await api("/api/local/workflows/single_run.yml/dispatch", {
      method: "POST",
      body: JSON.stringify({ref: "main", inputs: {backfill_titles: "true"}}),
    });
    message("已触发标题补齐，完成后检查更新即可看到新标题");
    setTimeout(loadRuns, 1500);
  } catch (error) {
    message(error.message, true);
  }
};

$("#course-back-button").onclick = () => showView("courses", {keepScroll: true});
$("#detail-back").onclick = () => showView("lectures", {keepScroll: true});
// 笔记改名：双击详情页标题触发，不再单设按钮。
async function renameCurrentLecture() {
  if (!currentLecture) return;
  const subId = String(currentLecture.sub_id);
  const input = prompt(
    "修改笔记名称（清空并确定则恢复自动命名）",
    lectureNames[subId] || lectureDisplayName(currentLecture),
  );
  if (input === null) return;
  const name = input.trim();
  try {
    const result = await api("/api/local/lecture-names", {
      method: "PUT",
      body: JSON.stringify({sub_id: subId, name}),
    });
    lectureNames = result.names || {};
    const displayName = lectureDisplayName(currentLecture);
    $("#detail-title").textContent = displayName;
    const subtitle = currentLecture.sub_title || "";
    $("#detail-subtitle").classList.toggle("hidden", !subtitle || displayName === subtitle);
    renderLectureList();
    message(name ? "已保存笔记名称" : "已恢复自动命名");
  } catch (error) {
    message(error.message, true);
  }
}
$("#detail-title").ondblclick = renameCurrentLecture;
$("#back-button").onclick = () => {
  if (activeView === "detail") showView("lectures", {keepScroll: true});
  else if (activeView === "lectures") showView("courses", {keepScroll: true});
  else showView("settings", {keepScroll: true});
};
$("#previous-lecture").onclick = async () => {
  const index = courseLectures.findIndex((lecture) => String(lecture.sub_id) === String(currentLecture?.sub_id));
  if (index > 0) {
    await openLecture(courseLectures[index - 1].sub_id);
    window.scrollTo(0, 0);
  }
};
$("#next-lecture").onclick = async () => {
  const index = courseLectures.findIndex((lecture) => String(lecture.sub_id) === String(currentLecture?.sub_id));
  if (index >= 0 && index < courseLectures.length - 1) {
    await openLecture(courseLectures[index + 1].sub_id);
    window.scrollTo(0, 0);
  }
};
document.querySelectorAll(".detail-tab").forEach((button) => {
  button.onclick = () => {
    detailTab = button.dataset.detailTab;
    renderDetail();
  };
});
document.querySelectorAll(".course-zone-button").forEach((button) => {
  button.onclick = async () => {
    activeCourseZone = button.dataset.courseZone;
    document.querySelectorAll(".course-zone-button").forEach((item) => {
      const active = item === button;
      item.classList.toggle("active", active);
      item.setAttribute("aria-selected", String(active));
    });
    try { await loadCourses(); }
    catch (error) { message(error.message, true); }
  };
});
document.querySelectorAll(".nav-button").forEach((button) => {
  button.onclick = async () => {
    const view = button.dataset.view;
    showView(view);
    if (view === "subscriptions") {
      try { await loadSubscriptions(); }
      catch (error) { message(error.message, true); }
    } else if (view === "rerun") {
      try { await loadRerunView(); }
      catch (error) { message(error.message, true); }
    }
  };
});
$("#subscription-sort").value = subscriptionSort;
$("#subscription-sort").onchange = () => {
  subscriptionSort = $("#subscription-sort").value;
  localStorage.setItem(SUBSCRIPTION_SORT_KEY, subscriptionSort);
  renderSubscriptions();
};

function renderSubscriptionSortDir() {
  $("#subscription-sort-dir").textContent = subscriptionSortDir === "asc" ? "↑" : "↓";
  $("#subscription-sort-dir").title = subscriptionSortDir === "asc" ? "当前正序，点击切换倒序" : "当前倒序，点击切换正序";
}
renderSubscriptionSortDir();
$("#subscription-sort-dir").onclick = () => {
  subscriptionSortDir = subscriptionSortDir === "asc" ? "desc" : "asc";
  localStorage.setItem(SUBSCRIPTION_SORT_DIR_KEY, subscriptionSortDir);
  renderSubscriptionSortDir();
  renderSubscriptions();
};
$("#subscription-term").onchange = () => loadSubscriptionCatalog().catch((error) => message(error.message, true));
$("#subscription-query").oninput = () => {
  clearTimeout(subscriptionTimer);
  subscriptionTimer = setTimeout(() => loadSubscriptionCatalog().catch((error) => message(error.message, true)), 240);
};
$("#settings-obsidian-button").onclick = () => $("#obsidian-button").click();
$("#refresh-runs-button").onclick = () => loadRuns().catch((error) => message(error.message, true));

$("#search").oninput = (event) => {
  clearTimeout(searchTimer);
  const query = event.target.value.trim();
  if (!query) {
    const root = $("#search-results");
    root.textContent = "输入关键词后开始搜索。";
    root.className = "search-results empty";
    return;
  }
  searchTimer = setTimeout(async () => {
    try {
      const results = await api(`/api/local/search?q=${encodeURIComponent(query)}`);
      const root = $("#search-results");
      root.replaceChildren();
      root.classList.remove("empty");
      results.forEach((item) => {
        const button = document.createElement("button");
        button.className = "search-card";
        const row = document.createElement("div");
        row.className = "search-row";
        const left = document.createElement("div");
        const course = document.createElement("p");
        course.textContent = item.course_title || item.course_id;
        const title = document.createElement("h2");
        title.textContent = lectureNames[String(item.sub_id)] || item.sub_title || item.sub_id;
        const snippet = document.createElement("p");
        snippet.textContent = item.snippet || item.hit_field;
        left.append(course, title, snippet);
        const hit = document.createElement("span");
        hit.className = "count-badge";
        hit.textContent = item.hit_field === "ocr" ? "OCR" : item.hit_field === "transcript" ? "转录" : "摘要";
        row.append(left, hit);
        button.append(row);
        button.onclick = () => openLecture(item.sub_id);
        root.append(button);
      });
      if (!results.length) {
        root.textContent = "没有找到匹配内容。";
        root.classList.add("empty");
      }
    } catch (error) {
      message(error.message, true);
    }
  }, 300);
};

refreshStatus().catch((error) => message(`无法连接本地服务：${error.message}`, true));
