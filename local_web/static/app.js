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
let activeCourseZone = "all";
let updatePollTimer = null;
let rerunModelOptions = [];
const courseZoneRequests = new Map();
let loadedLibraryIdentity = "";
const STARRED_KEY = "icourse-local-starred-courses";
const COURSE_ZONE_LABELS = {
  organize: "整理区",
  study: "学习区",
  reference: "查阅区",
  archive: "归档区",
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
    if (/^\s*```/.test(line)) {
      flushParagraph();
      const language = line.trim().slice(3).trim();
      const code = [];
      index += 1;
      while (index < lines.length && !/^\s*```/.test(lines[index])) code.push(lines[index++]);
      html.push(`<pre${language ? ` data-language="${escapeHtml(language)}"` : ""}><code>${escapeHtml(code.join("\n"))}</code></pre>`);
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
    titleRow.append(open);
    const count = document.createElement("span");
    count.className = "count-badge";
    count.textContent = `${course.summary_count}/${course.total_count} 篇笔记`;
    row.append(titleRow, count);
    const footer = document.createElement("div");
    footer.className = "course-card-footer";
    footer.append(star);
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
  const list = $("#lecture-list");
  list.replaceChildren();
  courseLectures.forEach((lecture) => {
    const button = document.createElement("button");
    button.className = "lecture-card";
    const row = document.createElement("div");
    row.className = "lecture-row";
    const left = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = lecture.sub_title || lecture.sub_id;
    left.append(title);
    if (lecture.error_msg) {
      const error = document.createElement("p");
      error.textContent = lecture.error_msg;
      left.append(error);
    }
    const state = document.createElement("span");
    state.className = `state-badge ${lectureState(lecture)}`;
    state.textContent = lectureStateLabel(lecture);
    row.append(left, state);
    button.append(row);
    button.onclick = () => openLecture(lecture.sub_id);
    list.append(button);
  });
  showView("lectures");
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
  $("#detail-title").textContent = currentLecture.sub_title || currentLecture.sub_id;
  $("#detail-course").textContent = currentLecture.course_title || "";
  detailTab = "summary";
  closeRerunSummary();
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
  const rerunButton = $("#rerun-summary-button");
  rerunButton.disabled = !String(currentLecture.transcript || "").trim();
  rerunButton.classList.toggle("hidden", detailTab !== "summary");
  const root = $("#detail-content");
  root.replaceChildren();
  const model = document.createElement("div");
  model.className = `detail-model${currentLecture.summary_model ? " available" : ""}`;
  model.textContent = currentLecture.summary_model
    ? `生成模型 · ${currentLecture.summary_model}`
    : "生成模型 · 未记录";
  if (detailTab === "summary") {
    const summary = document.createElement("div");
    summary.className = "summary";
    // Markdown is escaped before the small, local renderer creates HTML; raw
    // model output never receives direct HTML execution privileges.
    summary.innerHTML = renderMarkdown(currentLecture.summary);
    root.append(model, summary);
  } else if (detailTab === "transcript") {
    const transcript = document.createElement("div");
    transcript.className = "transcript";
    transcript.textContent = currentLecture.transcript || "暂无转录文本";
    root.append(transcript);
  } else {
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
    button.classList.toggle("active", button.dataset.detailTab === detailTab);
  });
  const index = courseLectures.findIndex((lecture) => String(lecture.sub_id) === String(currentLecture.sub_id));
  $("#previous-lecture").disabled = index <= 0;
  $("#next-lecture").disabled = index < 0 || index >= courseLectures.length - 1;
}

function closeRerunSummary() {
  $("#rerun-summary-panel").classList.add("hidden");
  rerunModelOptions = [];
  $("#rerun-model-select").replaceChildren();
}

async function openRerunSummary() {
  if (!currentLecture || !String(currentLecture.transcript || "").trim()) {
    message("这节课没有可用转录，暂时不能只重新生成笔记。", true);
    return;
  }
  const button = $("#rerun-summary-button");
  button.disabled = true;
  try {
    const result = await api("/api/local/model-providers");
    rerunModelOptions = [];
    (result.providers || []).forEach((provider) => {
      if (!provider.enabled || !provider.api_key_configured) return;
      (provider.models || []).forEach((model) => {
        rerunModelOptions.push({provider: String(provider.name), model: String(model)});
      });
    });
    if (!rerunModelOptions.length) {
      message("没有可用于重跑的模型；请先在模型管理中启用模型并保存 API Key。", true);
      return;
    }
    const select = $("#rerun-model-select");
    select.replaceChildren();
    rerunModelOptions.forEach((option, index) => {
      select.append(new Option(`${option.provider} / ${option.model}`, String(index)));
    });
    $("#rerun-summary-panel").classList.remove("hidden");
  } catch (error) {
    message(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function confirmRerunSummary() {
  if (!currentLecture) return;
  const option = rerunModelOptions[Number($("#rerun-model-select").value)];
  if (!option) {
    message("请先选择模型。", true);
    return;
  }
  if (!confirm(`使用 ${option.provider} / ${option.model} 重新生成这节课的笔记？\n\n将保留转录与 PPT OCR，并替换当前摘要。这会产生模型费用。`)) return;
  const button = $("#rerun-summary-confirm");
  button.disabled = true;
  button.textContent = "已提交…";
  try {
    await api(`/api/local/lectures/${encodeURIComponent(currentLecture.sub_id)}/rerun-summary`, {
      method: "POST",
      body: JSON.stringify(option),
    });
    closeRerunSummary();
    message(`已提交 ${option.provider} / ${option.model} 重跑；完成后检查更新即可看到新笔记。`);
    setTimeout(() => loadRuns().catch(() => {}), 1500);
  } catch (error) {
    message(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "开始重跑";
  }
}

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

function renderSubscriptions() {
  const current = $("#subscription-list");
  const catalog = $("#subscription-catalog");
  current.replaceChildren();
  catalog.replaceChildren();
  $("#subscription-count").textContent = String(subscribedCourseIds.length);
  current.classList.toggle("empty", !subscriptionCourses.length);
  if (!subscriptionCourses.length) current.textContent = "尚未订阅课程。";
  subscriptionCourses.forEach((course) => {
    current.append(renderSubscriptionCourse(course, () => {
      subscribedCourseIds = subscribedCourseIds.filter((id) => String(id) !== String(course.course_id));
      subscriptionCourses = subscriptionCourses.filter((item) => String(item.course_id) !== String(course.course_id));
      renderSubscriptions();
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

async function saveSubscriptions() {
  const button = $("#subscription-save-button");
  button.disabled = true;
  try {
    const state = await api("/api/local/subscriptions", {
      method: "PUT",
      body: JSON.stringify({course_ids: subscribedCourseIds}),
    });
    subscribedCourseIds = (state.course_ids || []).map(String);
    subscriptionCourses = state.courses || [];
    renderSubscriptions();
    message(`已保存 ${subscribedCourseIds.length} 门课程的订阅`);
  } catch (error) {
    message(error.message, true);
  } finally {
    button.disabled = false;
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
    meta.className = `meta status-${run.conclusion || run.status}`;
    meta.textContent = `${run.conclusion || run.status} · ${new Date(run.created_at).toLocaleString()}`;
    row.append(link, meta);
    root.append(row);
  });
}

function setModelView(open) {
  if (open) showView("models"); else showView("settings");
}

function setObsidianView(open) {
  if (open) showView("obsidian"); else showView("settings");
}

function showView(view) {
  activeView = view;
  const paneIds = {
    courses: "view-courses", lectures: "view-lectures", detail: "view-detail",
    search: "view-search", subscriptions: "view-subscriptions", automation: "view-automation", settings: "view-settings",
    models: "model-management", obsidian: "obsidian-sync",
  };
  Object.entries(paneIds).forEach(([name, id]) => $("#" + id).classList.toggle("hidden", name !== view));
  const title = {
    courses: "iCourse", lectures: currentCourse?.title || "课程", detail: currentLecture?.sub_title || "笔记",
    search: "搜索", subscriptions: "订阅", automation: "自动化", settings: "设置", models: "模型与 API", obsidian: "同步到 Obsidian",
  }[view] || "iCourse";
  $("#page-title").textContent = title;
  $("#back-button").classList.toggle("hidden", !["lectures", "detail", "models", "obsidian"].includes(view));
  $("#header-sync-button").classList.toggle("hidden", view !== "courses");
  document.querySelectorAll(".nav-button").forEach((button) => button.classList.toggle("active", button.dataset.view === view));
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
  $("#model-source").textContent = modelSourceLabel(result.source);
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

$("#setup-form").onsubmit = async (event) => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.currentTarget));
  try {
    await api("/api/local/configure", {method: "POST", body: JSON.stringify(data)});
    message("本地会话已配置");
    activeView = "courses";
    await refreshStatus();
  } catch (error) {
    message(error.message, true);
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
    $("#model-source").textContent = "配置读取失败";
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
  try {
    ["#sync-button", "#automation-sync-button", "#header-sync-button"].forEach((selector) => {
      const button = $(selector);
      if (button) button.disabled = true;
    });
    message("正在检查并更新本地资料库…");
    const result = await api("/api/local/sync", {method: "POST", body: "{}"});
    message(result.unchanged ? "本地资料已是最新" : `资料库已更新：${result.courses} 门课程`);
    await refreshStatus();
  } catch (error) {
    message(error.message, true);
  } finally {
    ["#sync-button", "#automation-sync-button", "#header-sync-button"].forEach((selector) => {
      const button = $(selector);
      if (button) button.disabled = false;
    });
  }
}

$("#sync-button").onclick = syncDatabase;
$("#automation-sync-button").onclick = syncDatabase;
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

$("#course-back-button").onclick = () => showView("courses");
$("#detail-back").onclick = () => showView("lectures");
$("#rerun-summary-button").onclick = openRerunSummary;
$("#rerun-summary-close").onclick = closeRerunSummary;
$("#rerun-summary-confirm").onclick = confirmRerunSummary;
$("#back-button").onclick = () => {
  if (activeView === "detail") showView("lectures");
  else if (activeView === "lectures") showView("courses");
  else showView("settings");
};
$("#previous-lecture").onclick = () => {
  const index = courseLectures.findIndex((lecture) => String(lecture.sub_id) === String(currentLecture?.sub_id));
  if (index > 0) openLecture(courseLectures[index - 1].sub_id);
};
$("#next-lecture").onclick = () => {
  const index = courseLectures.findIndex((lecture) => String(lecture.sub_id) === String(currentLecture?.sub_id));
  if (index >= 0 && index < courseLectures.length - 1) openLecture(courseLectures[index + 1].sub_id);
};
document.querySelectorAll(".detail-tab").forEach((button) => {
  button.onclick = () => {
    detailTab = button.dataset.detailTab;
    if (detailTab !== "summary") closeRerunSummary();
    renderDetail();
  };
});
document.querySelectorAll(".course-zone-button").forEach((button) => {
  button.onclick = async () => {
    activeCourseZone = button.dataset.courseZone;
    document.querySelectorAll(".course-zone-button").forEach((item) => {
      item.classList.toggle("active", item === button);
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
    }
  };
});
$("#subscription-save-button").onclick = saveSubscriptions;
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
        title.textContent = item.sub_title || item.sub_id;
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
        root.textContent = "没有搜索结果。";
        root.classList.add("empty");
      }
    } catch (error) {
      message(error.message, true);
    }
  }, 300);
};

refreshStatus().catch((error) => message(`无法连接本地服务：${error.message}`, true));
