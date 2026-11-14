const state = {
  book: null,
  bookData: null,
  stage: "idea",
  leftCollapsed: false,
  rightCollapsed: false,
  illustCollapsed: false,
  chapterId: null,
  tree: { nodes: [], has_forks: false },
  treeNodeId: null,
  styles: { shared: [], book: [] },
  selected: new Set(),
  saving: {},
  timers: {},
  abort: null,
  jobId: null,
  generating: false,
  generatingChapterId: null,
  genDraft: "",
  characters: [],
  characterId: null,
  illustrations: [],
  illustSession: false,
  illustSourceExcerpt: "",
};

const $ = (id) => document.getElementById(id);

function closeDrawers() {
  document.body.classList.remove("drawer-left-open", "drawer-right-open", "drawer-illust-open");
}

function toggleDrawer(side) {
  const cls = side === "left" ? "drawer-left-open" : side === "illust" ? "drawer-illust-open" : "drawer-right-open";
  const willOpen = !document.body.classList.contains(cls);
  document.body.classList.remove("drawer-left-open", "drawer-right-open", "drawer-illust-open");
  if (willOpen) document.body.classList.add(cls);
}

function isNarrowScreen() {
  return window.matchMedia("(max-width: 900px)").matches;
}

function loadPaneCollapse() {
  try {
    const raw = JSON.parse(localStorage.getItem("snts-panes") || "{}");
    state.leftCollapsed = !!raw.left;
    state.rightCollapsed = !!raw.right;
    state.illustCollapsed = !!raw.illust;
  } catch (err) {
    state.leftCollapsed = false;
    state.rightCollapsed = false;
    state.illustCollapsed = false;
  }
}

function applyPaneCollapse() {
  const layout = document.querySelector(".layout");
  const left = $("pane-left");
  const right = $("pane-right");
  const illust = $("pane-illust");
  const isBody = state.stage === "body";
  if (layout) layout.classList.toggle("is-body-stage", isBody);
  if (isNarrowScreen()) {
    if (layout) layout.classList.remove("is-left-collapsed", "is-right-collapsed", "is-illust-collapsed");
    if (left) left.classList.remove("collapsed");
    if (right) right.classList.remove("collapsed");
    if (illust) illust.classList.remove("collapsed");
    return;
  }
  const leftOn = !!state.leftCollapsed;
  const rightOn = !!state.rightCollapsed;
  const illustOn = !!state.illustCollapsed;
  if (layout) {
    layout.classList.toggle("is-left-collapsed", leftOn);
    layout.classList.toggle("is-right-collapsed", rightOn);
    layout.classList.toggle("is-illust-collapsed", isBody && illustOn);
  }
  if (left) left.classList.toggle("collapsed", leftOn);
  if (right) right.classList.toggle("collapsed", rightOn);
  if (illust) illust.classList.toggle("collapsed", illustOn);
  const expL = $("btn-expand-left");
  const expR = $("btn-expand-right");
  const expI = $("btn-expand-illust");
  if (expL) expL.setAttribute("aria-expanded", leftOn ? "false" : "true");
  if (expR) expR.setAttribute("aria-expanded", rightOn ? "false" : "true");
  if (expI) expI.setAttribute("aria-expanded", illustOn ? "false" : "true");
  requestAnimationFrame(() => renderPlotTrees());
}

function setPaneCollapsed(side, collapsed) {
  if (side === "left") state.leftCollapsed = !!collapsed;
  else if (side === "illust") state.illustCollapsed = !!collapsed;
  else state.rightCollapsed = !!collapsed;
  try {
    localStorage.setItem("snts-panes", JSON.stringify({
      left: !!state.leftCollapsed,
      right: !!state.rightCollapsed,
      illust: !!state.illustCollapsed,
    }));
  } catch (err) { /* ignore */ }
  applyPaneCollapse();
}

const EXTRAS_IDS = ["outline-extras", "chapter-extras", "synopsis-extras"];

function loadExtrasState() {
  try {
    return JSON.parse(localStorage.getItem("snts-extras") || "{}") || {};
  } catch (err) {
    return {};
  }
}

function setExtrasOpen(id, open, persist = true) {
  const box = $(id);
  if (!box) return;
  box.classList.toggle("is-open", !!open);
  const btn = box.querySelector(".extras-toggle");
  if (btn) btn.setAttribute("aria-expanded", open ? "true" : "false");
  if (persist) {
    const saved = loadExtrasState();
    saved[id] = !!open;
    try { localStorage.setItem("snts-extras", JSON.stringify(saved)); } catch (err) { /* ignore */ }
  }
}

function toggleExtras(id) {
  const box = $(id);
  if (!box) return;
  setExtrasOpen(id, !box.classList.contains("is-open"));
}

function applyExtrasState() {
  const saved = loadExtrasState();
  EXTRAS_IDS.forEach((id) => setExtrasOpen(id, !!saved[id], false));
}


const LEFT_SECTION_DEFAULTS = { books: true, styles: false, params: false, timeline: true };

function loadLeftSections() {
  try {
    return JSON.parse(localStorage.getItem("snts-left-sections") || "{}") || {};
  } catch (err) {
    return {};
  }
}

function setLeftSectionOpen(id, open, persist = true) {
  const box = document.querySelector(`[data-left-section="${id}"]`);
  if (!box) return;
  box.classList.toggle("is-collapsed", !open);
  const btn = box.querySelector(".section-toggle");
  if (btn) btn.setAttribute("aria-expanded", open ? "true" : "false");
  if (persist) {
    const saved = loadLeftSections();
    saved[id] = !!open;
    try { localStorage.setItem("snts-left-sections", JSON.stringify(saved)); } catch (err) { /* ignore */ }
  }
}

function toggleLeftSection(id) {
  const box = document.querySelector(`[data-left-section="${id}"]`);
  if (!box) return;
  setLeftSectionOpen(id, box.classList.contains("is-collapsed"));
}

function applyLeftSections() {
  const saved = loadLeftSections();
  document.querySelectorAll("[data-left-section]").forEach((box) => {
    const id = box.dataset.leftSection;
    const open = Object.prototype.hasOwnProperty.call(saved, id) ? !!saved[id] : LEFT_SECTION_DEFAULTS[id] !== false;
    setLeftSectionOpen(id, open, false);
  });
}

let timelinePreview = null;

function timelineScope() {
  if (!state.book) return { kind: "need-book" };
  if (state.stage === "outline") return { kind: "outline" };
  if (state.stage === "body") {
    if (!state.chapterId) return { kind: "need-chapter" };
    return { kind: "chapter", chapterId: state.chapterId };
  }
  return { kind: "idle" };
}

async function loadTimeline() {
  const list = $("timeline-list");
  const hint = $("timeline-hint");
  if (!list || !hint) return;
  const scope = timelineScope();
  list.innerHTML = "";
  if (scope.kind === "need-book") {
    hint.textContent = "请先选择作品";
    hint.classList.remove("hidden");
    return;
  }
  if (scope.kind === "idle") {
    hint.textContent = "打开正文或大纲后查看时间线";
    hint.classList.remove("hidden");
    return;
  }
  if (scope.kind === "need-chapter") {
    hint.textContent = "请先选择要查看时间线的章节";
    hint.classList.remove("hidden");
    return;
  }
  const book = encodeURIComponent(state.book.name);
  const url = scope.kind === "outline"
    ? `/api/books/${book}/outline/history`
    : `/api/books/${book}/chapters/${scope.chapterId}/history`;
  try {
    const data = await api(url);
    const items = data.items || [];
    if (!items.length) {
      hint.textContent = scope.kind === "outline"
        ? "这本的大纲还没有时间线记录。生成、按思路修改或点「保存大纲」后会出现。"
        : "这一章还没有时间线记录。生成、续写、扩写、按意见修改或点「保存本章」后会出现。";
      hint.classList.remove("hidden");
      return;
    }
    hint.classList.add("hidden");
    items.forEach((item) => {
      const li = document.createElement("li");
      li.className = "timeline-item";
      li.dataset.id = item.id;
      const time = document.createElement("div");
      time.className = "time";
      const when = document.createElement("span");
      when.textContent = item.time_label || item.time || "";
      const kind = document.createElement("span");
      kind.className = "kind";
      kind.textContent = item.kind_label || item.kind || "";
      time.append(when, kind);
      const meta = document.createElement("div");
      meta.className = "meta";
      meta.textContent = `${item.hanzi || 0} 字`;
      const preview = document.createElement("p");
      preview.className = "preview";
      preview.textContent = item.preview || item.title || "";
      li.append(time, meta, preview);
      li.addEventListener("click", () => previewTimelineEntry(item.id));
      list.appendChild(li);
    });
  } catch (err) {
    hint.textContent = err.message || "时间线加载失败";
    hint.classList.remove("hidden");
  }
}

async function previewTimelineEntry(entryId) {
  const scope = timelineScope();
  if (scope.kind !== "outline" && scope.kind !== "chapter") return;
  const book = encodeURIComponent(state.book.name);
  const url = scope.kind === "outline"
    ? `/api/books/${book}/outline/history/${encodeURIComponent(entryId)}`
    : `/api/books/${book}/chapters/${scope.chapterId}/history/${encodeURIComponent(entryId)}`;
  try {
    const item = await api(url);
    timelinePreview = { scope, item };
    $("timeline-dialog-title").textContent = scope.kind === "outline" ? "大纲时间线预览" : "正文时间线预览";
    $("timeline-dialog-meta").textContent = `${item.time_label || item.time || ""} · ${item.kind_label || ""} · ${item.hanzi || 0} 字`;
    $("timeline-dialog-text").value = item.text || "";
    $("timeline-dialog").classList.remove("hidden");
  } catch (err) {
    toast(err.message, true);
  }
}

function closeTimelineDialog() {
  const modal = $("timeline-dialog");
  if (modal) modal.classList.add("hidden");
  timelinePreview = null;
}

async function restoreTimelineEntry() {
  if (!timelinePreview) return;
  if (state.generating) return toast("请先等待当前生成结束", true);
  const ok = window.confirm("确认用这一版覆盖当前正文/大纲？当前这版会先存入时间线");
  if (!ok) return;
  const { scope, item } = timelinePreview;
  const book = encodeURIComponent(state.book.name);
  const url = scope.kind === "outline"
    ? `/api/books/${book}/outline/history/${encodeURIComponent(item.id)}/restore`
    : `/api/books/${book}/chapters/${scope.chapterId}/history/${encodeURIComponent(item.id)}/restore`;
  try {
    await flushSaves();
    const data = await api(url, { method: "POST" });
    if (scope.kind === "outline") {
      $("outline-editor").value = data.text || data.outline || "";
      if (data.tree) applyTree(data.tree);
      renderPlotTrees();
      toast(`已恢复这一版大纲，共 ${data.hanzi || hanzi($("outline-editor").value)} 字`);
    } else if (state.chapterId === scope.chapterId) {
      $("chapter-editor").value = data.text || "";
      toast(`已恢复这一版正文，共 ${data.hanzi || hanzi($("chapter-editor").value)} 字`);
      await reloadBookMeta();
    } else {
      toast("已恢复这一版正文");
    }
    updateCounts();
    closeTimelineDialog();
    await loadTimeline();
  } catch (err) {
    toast(err.message, true);
  }
}

async function saveCurrentOutline() {
  if (!state.book) return toast("请先选择或新建一本书", true);
  if (state.generating) return toast("请先等待当前生成结束", true);
  const current = $("outline-editor").value.trim();
  if (!current) return toast("当前大纲没有可保存的内容", true);
  await flushSaves();
  try {
    const data = await api(`/api/books/${encodeURIComponent(state.book.name)}/outline/save`, {
      method: "POST",
      body: JSON.stringify({ text: $("outline-editor").value }),
    });
    if (data.tree) applyTree(data.tree);
    toast(`大纲已保存到时间线，共 ${data.hanzi || hanzi(current)} 字`);
    await reloadBookMeta();
    await loadTimeline();
  } catch (err) {
    toast(err.message, true);
  }
}

const ILLUST_MARKER_RE = /【插图:[A-Za-z0-9_-]+】/g;
function stripIllustrationMarkers(text) {
  return (text || "").replace(ILLUST_MARKER_RE, "");
}
const hanzi = (text) => ((text || "").match(/[\u4E00-\u9FFF]/g) || []).length;
function chapterHanzi(text) {
  return hanzi(stripIllustrationMarkers(text));
}

function toast(message, isError) {
  const el = $("toast");
  el.textContent = message;
  el.classList.toggle("error", !!isError);
  el.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add("hidden"), isError ? 10000 : 4200);
}

function apiErrorMessage(data, status) {
  const detail = data && data.detail;
  if (typeof detail === "string" && detail.trim()) return detail;
  if (Array.isArray(detail)) {
    const parts = detail.map((item) => (item && (item.msg || item.message)) || "").filter(Boolean);
    if (parts.length) return parts.join("；");
  }
  if (detail && typeof detail === "object" && (detail.msg || detail.message)) return detail.msg || detail.message;
  return (data && data.message) || `请求失败 ${status}`;
}

function setIllustStatus(message, isError) {
  const el = $("illust-status");
  if (!el) return;
  el.textContent = message || "";
  el.classList.toggle("error", !!isError);
  el.classList.toggle("hidden", !message);
}

function applyIllustProgress(info) {
  const box = $("illust-progress");
  const fill = $("illust-progress-fill");
  const text = $("illust-progress-text");
  if (!box) return;
  const label = info && info.label;
  const phase = info && info.phase;
  const active = !!(label && phase && phase !== "idle");
  box.classList.toggle("hidden", !active);
  if (!active) return;
  const percent = typeof info.percent === "number" ? Math.max(0, Math.min(100, info.percent)) : null;
  let line = label;
  if (percent != null) line += `（${percent}%）`;
  else if (info.total) line += `（${info.current || 0}/${info.total}）`;
  if (text) text.textContent = line;
  if (fill) {
    if (percent != null) {
      fill.parentElement.classList.add("determinate");
      fill.style.width = `${Math.max(4, percent)}%`;
    } else {
      fill.parentElement.classList.remove("determinate");
      fill.style.width = "";
    }
  }
  if (!($("illust-status") && $("illust-status").classList.contains("error"))) {
    setIllustStatus(line);
  }
  if (state.generating && $("progress-text")) {
    $("progress-text").textContent = line;
    const mainFill = $("progress-fill");
    if (mainFill && percent != null) {
      mainFill.parentElement.classList.add("determinate");
      mainFill.style.width = `${Math.max(4, percent)}%`;
    } else if (mainFill && percent == null) {
      mainFill.parentElement.classList.remove("determinate");
      mainFill.style.width = "";
    }
  }
}

function stopIllustProgressPolling() {
  if (state.illustProgressTimer) {
    clearInterval(state.illustProgressTimer);
    state.illustProgressTimer = null;
  }
}

function startIllustProgressPolling() {
  stopIllustProgressPolling();
  const tick = async () => {
    const url = $("param-url") ? $("param-url").value.trim() : "";
    const q = url ? `?llm_url=${encodeURIComponent(url)}` : "";
    try {
      const session = await api("/api/models/status" + q);
      if (session && typeof session.illustration_session === "boolean") {
        setIllustSession(!!session.illustration_session);
      }
      applyIllustProgress(session && session.progress);
    } catch (err) {
      /* keep the last known status */
    }
  };
  tick();
  state.illustProgressTimer = setInterval(tick, 800);
}

function revealIllustPane() {
  if (typeof isNarrowScreen === "function" && isNarrowScreen()) {
    if (typeof toggleDrawer === "function") toggleDrawer("illust");
  } else {
    setPaneCollapsed("illust", false);
  }
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(apiErrorMessage(data, res.status));
  return data;
}

async function refreshHealth() {
  const box = $("health");
  try {
    const url = $("param-url").value.trim();
    const q = url ? `?llm_url=${encodeURIComponent(url)}` : "";
    const data = await api(`/api/health${q}`);
    let session = null;
    try { session = await api("/api/models/status" + q); } catch (err) { session = null; }
    if (session && typeof session.illustration_session === "boolean") {
      setIllustSession(!!session.illustration_session);
    }
    const ok = data.llm && data.llm.ok;
    box.classList.toggle("ok", ok || !!(session && session.illustration_session && session.comfyui));
    box.classList.toggle("bad", !(ok || (session && session.illustration_session && session.comfyui)));
    if (session && session.illustration_session) {
      box.querySelector(".health-text").textContent = "出图中，写作模型已暂时卸下";
    } else {
      box.querySelector(".health-text").textContent = ok ? "写作模型已连接" : `写作模型未连接：${(data.llm && data.llm.detail) || "未知错误"}`;
    }
  } catch (err) {
    box.classList.remove("ok");
    box.classList.add("bad");
    box.querySelector(".health-text").textContent = "无法连接本机服务";
  }
}

async function loadBooks(selectName) {
  const data = await api("/api/books");
  const list = $("book-list");
  list.innerHTML = "";
  if (!data.books.length) {
    list.innerHTML = '<li class="empty">还没有作品，先新建一本</li>';
    return;
  }
  data.books.forEach((book) => {
    const li = document.createElement("li");
    li.dataset.name = book.name;
    if (book.name === (selectName || (state.book && state.book.name))) li.classList.add("active");
    li.innerHTML = `<span class="name"></span><span class="meta">${book.chapter_count} 章 · 大纲 ${book.outline_hanzi} 字</span>`;
    li.querySelector(".name").textContent = book.name;
    li.addEventListener("click", () => selectBook(book.name));
    list.appendChild(li);
  });
}

async function selectBook(name) {
  const data = await api(`/api/books/${encodeURIComponent(name)}`);
  state.book = { name };
  state.bookData = data;
  state.tree = data.tree || { nodes: [], has_forks: false };
  document.querySelectorAll(".book-list li").forEach((li) => li.classList.toggle("active", li.dataset.name === name));
  $("idea-editor").value = data.idea || "";
  $("outline-editor").value = data.outline || "";
  if ($("world-editor")) $("world-editor").value = data.world || "";
  state.characters = [];
  state.characterId = null;
  updateCounts();
  applySettings(data.settings || {});
  await loadStyles();
  renderChapters(data.chapters || []);
  renderPlotTrees();
  updateChapterPath();
  if ((data.chapters || []).length) {
    const current = data.chapters.find((c) => c.id === state.chapterId) || data.chapters[0];
    await openChapter(current.id);
  } else {
    state.chapterId = null;
    $("chapter-editor").value = "";
    $("chapter-title").textContent = "尚未选择章节";
    if ($("path-synopsis")) $("path-synopsis").value = "";
    hideConsistency();
    updateCounts();
  }
  await loadCharacters();
  closeDrawers();
  loadTimeline();
}

function applySettings(settings) {
  $("param-temp").value = settings.temperature ?? 0.8;
  $("param-temp-val").textContent = Number($("param-temp").value).toFixed(2);
  $("param-chars").value = settings.chapter_target_chars ?? 2000;
  $("param-thinking").checked = !!settings.enable_thinking;
  $("param-url").value = settings.llm_url || "http://127.0.0.1:8080";
  $("chapter-target").textContent = $("param-chars").value;
  state.selected = new Set(settings.selected_styles || []);
}

function collectSettings() {
  return {
    temperature: Number($("param-temp").value),
    chapter_target_chars: Number($("param-chars").value),
    enable_thinking: $("param-thinking").checked,
    llm_url: $("param-url").value.trim(),
    selected_styles: Array.from(state.selected),
  };
}

function scheduleSave(key, fn, delay = 600) {
  clearTimeout(state.timers[key]);
  state.pendingSaves = state.pendingSaves || {};
  state.pendingSaves[key] = fn;
  state.timers[key] = setTimeout(async () => {
    delete state.pendingSaves[key];
    try {
      await fn();
    } catch (err) {
      toast(err.message, true);
    }
  }, delay);
}

async function flushSaves() {
  const pending = { ...(state.pendingSaves || {}) };
  state.pendingSaves = {};
  Object.keys(state.timers || {}).forEach((key) => clearTimeout(state.timers[key]));
  for (const fn of Object.values(pending)) {
    try {
      await fn();
    } catch (err) {
      toast(err.message, true);
    }
  }
}

async function saveSettings() {
  if (!state.book) return;
  const saved = await api(`/api/books/${encodeURIComponent(state.book.name)}/settings`, {
    method: "PUT",
    body: JSON.stringify(collectSettings()),
  });
  applySettings(saved);
}

async function loadStyles() {
  if (!state.book) return;
  const data = await api(`/api/books/${encodeURIComponent(state.book.name)}/styles`);
  state.styles = data;
  const items = [...(data.shared || []), ...(data.book || [])];
  if (!state.selected.size) {
    items.filter((s) => s.enabled_default && s.allowed).forEach((s) => state.selected.add(s.id));
  }
  for (const id of Array.from(state.selected)) {
    const item = items.find((s) => s.id === id);
    if (!item || !item.allowed) state.selected.delete(id);
  }
  renderStyles();
}

async function refreshStyles() {
  if (!state.book) return toast("请先选择作品", true);
  $("style-status").textContent = "正在重新扫描文风…";
  $("style-status").classList.remove("error");
  try {
    await loadStyles();
    const items = [...(state.styles.shared || []), ...(state.styles.book || [])];
    const allowed = items.filter((s) => s.allowed).length;
    $("style-status").textContent = `已重新扫描，可勾选 ${allowed} 份`;
    $("style-status").classList.remove("error");
    scheduleSave("settings", saveSettings, 200);
  } catch (err) {
    $("style-status").textContent = err.message;
    $("style-status").classList.add("error");
  }
}

function renderStyles() {
  const box = $("style-list");
  box.innerHTML = "";
  const groups = [
    { title: "共用文风库", items: state.styles.shared || [] },
    { title: "本作品样本", items: state.styles.book || [] },
  ];
  groups.forEach((group) => {
    if (!group.items.length && group.title === "本作品样本") return;
    const head = document.createElement("div");
    head.className = "meta";
    head.textContent = group.title;
    head.style.margin = "6px 0 2px";
    box.appendChild(head);
    if (!group.items.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "暂无";
      box.appendChild(empty);
    }
    group.items.forEach((item) => {
      const row = document.createElement("label");
      row.className = "style-item" + (item.allowed ? "" : " disabled");
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.disabled = !item.allowed;
      cb.checked = item.allowed && state.selected.has(item.id);
      cb.addEventListener("change", () => {
        if (cb.checked) state.selected.add(item.id);
        else state.selected.delete(item.id);
        scheduleSave("settings", saveSettings);
      });
      const info = document.createElement("span");
      const title = document.createElement("span");
      title.className = "name";
      title.textContent = `${item.name}${item.allowed ? ` · ${item.hanzi} 字` : ""}`;
      info.appendChild(title);
      if (!item.allowed && item.reason) {
        const reason = document.createElement("span");
        reason.className = "reason";
        reason.textContent = item.reason;
        info.appendChild(document.createElement("br"));
        info.appendChild(reason);
      }
      row.appendChild(cb);
      row.appendChild(info);
      box.appendChild(row);
    });
  });
}

function applyTree(tree) {
  if (tree) state.tree = tree;
  renderPlotTrees();
  updateChapterPath();
}


function fillEditorFromServer(el, serverText, key) {
  if (!el) return false;
  const server = serverText == null ? "" : String(serverText);
  const local = el.value || "";
  state.synced = state.synced || {};
  const last = state.synced[key] == null ? "" : String(state.synced[key]);
  if (local === server) {
    state.synced[key] = server;
    return false;
  }
  const emptyLocal = !local.trim();
  const dirty = local !== last;
  const focused = document.activeElement === el;
  if (emptyLocal && server.trim()) {
    el.value = server;
    state.synced[key] = server;
    return true;
  }
  if (!focused && !dirty) {
    el.value = server;
    state.synced[key] = server;
    return true;
  }
  return false;
}

function applyBookTexts(book) {
  if (!book) return;
  const outlineChanged = fillEditorFromServer($("outline-editor"), book.outline, "outline");
  const worldChanged = $("world-editor") ? fillEditorFromServer($("world-editor"), book.world, "world") : false;
  const ideaChanged = fillEditorFromServer($("idea-editor"), book.idea, "idea");
  if (outlineChanged || worldChanged || ideaChanged) updateCounts();
  if (book.tree) applyTree(book.tree);
  else if (outlineChanged) renderPlotTrees({ force: true });
}

async function putOutlineSafely() {
  if (!state.book) return null;
  const text = $("outline-editor").value;
  const known = ((state.synced && state.synced.outline) || (state.bookData && state.bookData.outline) || "");
  if (!text.trim() && known.trim() && !(state.allowEmptySave && state.allowEmptySave.outline)) {
    return null;
  }
  const saved = await api(`/api/books/${encodeURIComponent(state.book.name)}/outline`, {
    method: "PUT",
    body: JSON.stringify({ text }),
  });
  state.synced = state.synced || {};
  state.synced.outline = text;
  if (saved && saved.tree) applyTree(saved.tree);
  return saved;
}

async function putWorldSafely() {
  if (!state.book || !$("world-editor")) return null;
  const text = $("world-editor").value;
  const known = ((state.synced && state.synced.world) || (state.bookData && state.bookData.world) || "");
  if (!text.trim() && known.trim() && !(state.allowEmptySave && state.allowEmptySave.world)) {
    return null;
  }
  try {
    await api(`/api/books/${encodeURIComponent(state.book.name)}/world`, {
      method: "PUT",
      body: JSON.stringify({ text }),
    });
    state.synced = state.synced || {};
    state.synced.world = text;
  } catch (err) { /* 世界观可选 */ }
  if (state.allowEmptySave) state.allowEmptySave.world = false;
  return true;
}

async function reloadBookMeta() {
  if (!state.book) return null;
  const book = await api(`/api/books/${encodeURIComponent(state.book.name)}`);
  state.bookData = book;
  applyBookTexts(book);
  renderChapters(book.chapters || []);
  return book;
}

function treeNodes() {
  return (state.tree && state.tree.nodes) || [];
}

function findTreeNode(id) {
  if (!id) return null;
  return treeNodes().find((node) => node.id === id || node.chapter_id === id) || null;
}

function sameChapter(a, b) {
  if (a == null || b == null) return false;
  return String(a) === String(b);
}

function chapterLabel(id) {
  const node = findTreeNode(id);
  if (node) return node.display_title || node.title || node.file_title || `第${node.num || id}章`;
  const chapters = (state.bookData && state.bookData.chapters) || [];
  const ch = chapters.find((c) => sameChapter(c.id, id));
  if (ch && (ch.title || ch.display_title)) return ch.title || ch.display_title;
  return `第${Number(id) || id}章`;
}

function viewingGenEditor(targetEditor, boundChapterId) {
  if (!targetEditor) return false;
  if (!boundChapterId) return true;
  return sameChapter(state.chapterId, boundChapterId) && targetEditor === $("chapter-editor");
}

function selectedTreeNode() {
  return findTreeNode(state.treeNodeId) || findTreeNode(state.chapterId);
}

function currentPathIds() {
  const nodes = treeNodes();
  const byId = Object.fromEntries(nodes.map((node) => [node.id, node]));
  const start = selectedTreeNode();
  const path = new Set();
  const seen = new Set();
  let cur = start;
  while (cur && !seen.has(cur.id)) {
    path.add(cur.id);
    seen.add(cur.id);
    cur = cur.parent_id ? byId[cur.parent_id] : null;
  }
  return path;
}

function treeIndex(nodes) {
  const list = nodes || treeNodes();
  const byId = Object.fromEntries(list.map((node) => [node.id, node]));
  const kids = {};
  list.forEach((node) => { kids[node.id] = []; });
  let usedExplicit = false;
  list.forEach((node) => {
    const ids = node.children || [];
    if (ids.length) {
      usedExplicit = true;
      kids[node.id] = ids.map((id) => byId[id]).filter(Boolean);
    }
  });
  if (!usedExplicit) {
    list.forEach((node) => {
      if (node.parent_id && byId[node.parent_id]) kids[node.parent_id].push(node);
    });
  }
  return { list, byId, kids };
}

function forkDepth(node, byId, kids) {
  // Only indent after a fork: a straight 1→2→3 chain stays in one column.
  let depth = 0;
  const seen = new Set();
  let cur = node;
  while (cur && cur.parent_id && !seen.has(cur.id)) {
    seen.add(cur.id);
    const parent = byId[cur.parent_id];
    if (!parent) break;
    if ((kids[parent.id] || []).length > 1) depth += 1;
    cur = parent;
    if (depth > 40) break;
  }
  return depth;
}

function depthOf(node, byId) {
  let depth = 0;
  const seen = new Set();
  let cur = node;
  while (cur && cur.parent_id && !seen.has(cur.id)) {
    seen.add(cur.id);
    cur = byId[cur.parent_id];
    depth += 1;
    if (depth > 40) break;
  }
  return depth;
}

function renderChapters(chapters) {
  const list = $("chapter-list");
  list.innerHTML = "";
  const nodes = treeNodes();
  if (nodes.length) {
    const { byId, kids } = treeIndex(nodes);
    const path = currentPathIds();
    nodes.forEach((node) => {
      const li = document.createElement("li");
      const cid = node.chapter_id || node.id;
      li.dataset.id = cid;
      li.dataset.nodeId = node.id;
      if (cid === state.chapterId || node.id === state.treeNodeId) li.classList.add("active");
      if (path.has(node.id)) li.classList.add("on-path");
      const depth = forkDepth(node, byId, kids);
      if (depth > 0) li.style.paddingLeft = `${10 + depth * 16}px`;
      const name = document.createElement("span");
      name.className = "name";
      if (node.branch) {
        const tag = document.createElement("span");
        tag.className = "branch-tag";
        tag.textContent = node.branch;
        name.appendChild(tag);
      }
      name.appendChild(document.createTextNode(node.display_title || node.file_title || `第${node.num || cid}章`));
      const meta = document.createElement("span");
      meta.className = "meta";
      if (!node.has_file) meta.textContent = node.has_backup ? "已删除，可恢复" : "尚未撰写";
      else meta.textContent = node.empty ? (node.has_backup ? "已删除，可恢复" : "尚未撰写") : `${node.hanzi} 字`;
      li.appendChild(name);
      li.appendChild(meta);
      li.addEventListener("click", () => openTreeNode(node, { stage: "body" }));
      list.appendChild(li);
    });
    syncPlotTreeSelection();
    return;
  }
  if (!chapters.length) {
    list.innerHTML = '<li class="empty">还没有章节。确认大纲后可在此追加。</li>';
    syncPlotTreeSelection();
    return;
  }
  chapters.forEach((ch) => {
    const li = document.createElement("li");
    li.dataset.id = ch.id;
    if (ch.id === state.chapterId) li.classList.add("active");
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = ch.title || `第${Number(ch.id)}章`;
    const meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = ch.empty ? "尚未撰写" : `${ch.hanzi} 字`;
    li.appendChild(name);
    li.appendChild(meta);
    li.addEventListener("click", () => openChapter(ch.id));
    list.appendChild(li);
  });
  syncPlotTreeSelection();
}

function estimateNodeHeight(node, width) {
  const title = node.display_title || `第${node.num || ""}章`;
  const inner = Math.max(80, width - 24);
  const charsPerLine = Math.max(6, Math.floor(inner / 13));
  const lines = Math.max(1, Math.ceil(Array.from(title).length / charsPerLine));
  const tag = node.branch ? 16 : 0;
  return 18 + tag + lines * 19 + 18;
}

function layoutPlotTree(nodes, containerWidth) {
  const GAP_Y = 16;
  const INDENT = 36;
  const PAD = 20;
  const { byId, kids } = treeIndex(nodes);
  const available = Math.max(200, (containerWidth || 320) - PAD * 2);
  const positions = {};
  let y = PAD;
  nodes.forEach((node) => {
    const depth = forkDepth(node, byId, kids);
    const x = PAD + depth * INDENT;
    const w = Math.max(168, available - depth * INDENT);
    const h = estimateNodeHeight(node, w);
    positions[node.id] = { x, y, w, h, depth };
    y += h + GAP_Y;
  });
  let maxX = PAD, maxY = PAD;
  Object.values(positions).forEach((box) => {
    maxX = Math.max(maxX, box.x + box.w + PAD);
    maxY = Math.max(maxY, box.y + box.h + PAD + 16);
  });
  return { positions, width: maxX, height: maxY };
}

let treeRenderLock = false;

function scrollSelectedTreeIntoView() {
  const container = $("outline-tree");
  if (!container || container.clientWidth === 0) return;
  const selected = selectedTreeNode();
  if (!selected) return;
  const id = (window.CSS && CSS.escape) ? CSS.escape(selected.id) : selected.id;
  const btn = container.querySelector(`.plot-node[data-node-id="${id}"]`);
  if (btn) btn.scrollIntoView({ block: "nearest", inline: "nearest", behavior: "auto" });
}

function scrollActiveChapterIntoView() {
  const li = document.querySelector("#chapter-list li.active");
  if (li) li.scrollIntoView({ block: "nearest", behavior: "auto" });
}

function outlineOffsetForNode(node, text) {
  if (!node || text == null) return -1;
  const heading = (node.heading || "").trim();
  const bare = heading.replace(/^#+\s*/, "").trim();
  const candidates = [heading, bare, node.display_title, node.file_title].filter(Boolean);
  for (const item of candidates) {
    const i = text.indexOf(item);
    if (i >= 0) return i;
  }
  return -1;
}

function scrollTextareaToOffset(el, start) {
  const cs = getComputedStyle(el);
  const div = document.createElement("div");
  [
    "font", "fontSize", "fontFamily", "fontWeight", "fontStyle",
    "lineHeight", "letterSpacing", "wordSpacing", "textTransform", "textIndent",
    "padding", "paddingTop", "paddingRight", "paddingBottom", "paddingLeft",
    "border", "borderWidth", "boxSizing", "tabSize",
  ].forEach((prop) => { div.style[prop] = cs[prop]; });
  div.style.position = "absolute";
  div.style.visibility = "hidden";
  div.style.pointerEvents = "none";
  div.style.whiteSpace = "pre-wrap";
  div.style.wordWrap = "break-word";
  div.style.overflowWrap = "break-word";
  div.style.width = `${el.clientWidth}px`;
  div.style.left = "-99999px";
  div.style.top = "0";
  const before = document.createElement("span");
  before.textContent = el.value.slice(0, start);
  const marker = document.createElement("span");
  marker.textContent = el.value.slice(start, start + 1) || ".";
  div.appendChild(before);
  div.appendChild(marker);
  document.body.appendChild(div);
  const top = marker.offsetTop;
  div.remove();
  const margin = Math.max(16, el.clientHeight * 0.2);
  el.scrollTop = Math.max(0, top - margin);
}

function scrollOutlineToNode(node) {
  const el = $("outline-editor");
  if (!el || !node) return;
  const start = outlineOffsetForNode(node, el.value);
  if (start < 0) return;
  let end = el.value.indexOf("\n", start);
  if (end < 0) end = el.value.length;
  state._syncingOutline = true;
  el.focus({ preventScroll: true });
  try { el.setSelectionRange(start, end); } catch (err) { /* ignore */ }
  scrollTextareaToOffset(el, start);
  requestAnimationFrame(() => { state._syncingOutline = false; });
}

function nodeAtOutlineCursor() {
  const el = $("outline-editor");
  if (!el) return null;
  const pos = el.selectionStart || 0;
  const text = el.value;
  let best = null;
  let bestStart = -1;
  treeNodes().forEach((node) => {
    const off = outlineOffsetForNode(node, text);
    if (off >= 0 && off <= pos && off >= bestStart) {
      best = node;
      bestStart = off;
    }
  });
  return best;
}

function syncTreeFromOutlineCursor() {
  if (state._syncingOutline) return;
  if (state.stage && state.stage !== "outline") return;
  const node = nodeAtOutlineCursor();
  if (!node || state.treeNodeId === node.id) return;
  state.treeNodeId = node.id;
  syncPlotTreeSelection();
  scrollSelectedTreeIntoView();
}

function syncPlotTreeSelection() {
  const container = $("outline-tree");
  if (!container) return;
  const canvas = container.querySelector(".plot-tree-canvas");
  if (!canvas || !container.querySelector(".plot-node")) {
    if (container.clientWidth > 0) renderPlotTrees();
    return;
  }
  const path = currentPathIds();
  const selected = selectedTreeNode();
  canvas.querySelectorAll(".plot-node").forEach((btn) => {
    const id = btn.dataset.nodeId;
    btn.classList.toggle("selected", !!(selected && id === selected.id));
    btn.classList.toggle("on-path", path.has(id));
  });
  canvas.querySelectorAll("path[data-child]").forEach((line) => {
    const onPath = path.has(line.dataset.child) && path.has(line.dataset.parent);
    line.setAttribute("stroke", onPath ? "#8a3b2d" : "#d2c4ae");
    line.setAttribute("stroke-width", onPath ? "2" : "1.5");
  });
}

function renderPlotTrees(options = {}) {
  const container = $("outline-tree");
  if (!container) return;
  if (container.clientWidth === 0 && !options.force) return;
  renderPlotTreeInto(container, { openOnClick: true, ...options });
}

function renderPlotTreeInto(container, options = {}) {
  if (!container) return;
  const nodes = treeNodes();
  const scrollTop = container.scrollTop;
  const scrollLeft = container.scrollLeft;
  treeRenderLock = true;
  container.innerHTML = "";
  if (!nodes.length) {
    const empty = document.createElement("p");
    empty.className = "plot-tree-empty";
    empty.textContent = "填写或生成大纲后，会在这里自动画出情节脉络。";
    container.appendChild(empty);
    requestAnimationFrame(() => { treeRenderLock = false; });
    return;
  }
  const { positions, width, height } = layoutPlotTree(nodes, container.clientWidth);
  const canvas = document.createElement("div");
  canvas.className = "plot-tree-canvas";
  canvas.style.width = `${Math.max(width, container.clientWidth || 0)}px`;
  canvas.style.height = `${height}px`;
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "plot-tree-lines");
  svg.setAttribute("width", String(Math.max(width, container.clientWidth || 0)));
  svg.setAttribute("height", String(height));
  svg.setAttribute("viewBox", `0 0 ${Math.max(width, container.clientWidth || 0)} ${height}`);
  const pathIds = currentPathIds();
  nodes.forEach((node) => {
    if (!node.parent_id || !positions[node.id] || !positions[node.parent_id]) return;
    const parent = positions[node.parent_id];
    const child = positions[node.id];
    const x1 = parent.x + 16;
    const y1 = parent.y + parent.h;
    const x2 = child.x;
    const yMid = child.y + child.h / 2;
    const r = 8;
    const line = document.createElementNS("http://www.w3.org/2000/svg", "path");
    let d;
    if (child.depth === parent.depth || Math.abs(child.x - parent.x) < 2) {
      d = `M ${x1} ${y1} V ${child.y}`;
    } else {
      d = `M ${x1} ${y1} V ${yMid - r} Q ${x1} ${yMid}, ${x1 + r} ${yMid} H ${x2}`;
    }
    line.setAttribute("d", d);
    line.setAttribute("fill", "none");
    line.setAttribute("stroke-linecap", "round");
    line.setAttribute("stroke-linejoin", "round");
    line.setAttribute("data-parent", node.parent_id);
    line.setAttribute("data-child", node.id);
    const onPath = pathIds.has(node.id) && pathIds.has(node.parent_id);
    line.setAttribute("stroke", onPath ? "#8a3b2d" : "#d2c4ae");
    line.setAttribute("stroke-width", onPath ? "2" : "1.5");
    svg.appendChild(line);
  });
  canvas.appendChild(svg);
  nodes.forEach((node) => {
    const box = positions[node.id];
    if (!box) return;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "plot-node";
    btn.dataset.nodeId = node.id;
    if (pathIds.has(node.id)) btn.classList.add("on-path");
    if (node.id === (selectedTreeNode() && selectedTreeNode().id)) btn.classList.add("selected");
    if (node.empty || !node.has_file) btn.classList.add("empty");
    btn.style.left = `${box.x}px`;
    btn.style.top = `${box.y}px`;
    btn.style.width = `${box.w}px`;
    btn.style.height = `${box.h}px`;
    if (node.branch) {
      const tag = document.createElement("span");
      tag.className = "branch-tag";
      tag.textContent = node.branch;
      btn.appendChild(tag);
    }
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = node.display_title || node.title || `第${node.num || ""}章`;
    btn.appendChild(name);
    const meta = document.createElement("span");
    meta.className = "meta";
    if (!node.has_file) meta.textContent = node.has_backup ? "可恢复" : "待写";
    else meta.textContent = node.empty ? "待写" : `${node.hanzi} 字`;
    btn.appendChild(meta);
    btn.addEventListener("click", () => {
      state.treeNodeId = node.id;
      syncPlotTreeSelection();
      scrollOutlineToNode(node);
    });
    canvas.appendChild(btn);
  });
  container.appendChild(canvas);
  relayoutPlotTree(canvas, svg, nodes, positions);
  container.scrollTop = scrollTop;
  container.scrollLeft = scrollLeft;
  if (options.scrollToSelected) {
    requestAnimationFrame(() => scrollSelectedTreeIntoView());
  }
  requestAnimationFrame(() => { treeRenderLock = false; });
}

function relayoutPlotTree(canvas, svg, nodes, positions) {
  const GAP_Y = 16;
  const PAD = 20;
  let y = PAD;
  let maxY = PAD;
  let maxX = PAD;
  nodes.forEach((node) => {
    const box = positions[node.id];
    if (!box) return;
    const id = (window.CSS && CSS.escape) ? CSS.escape(node.id) : node.id;
    const btn = canvas.querySelector(`.plot-node[data-node-id="${id}"]`);
    if (!btn) return;
    btn.style.height = "auto";
    const h = Math.max(box.h, btn.offsetHeight || box.h);
    box.y = y;
    box.h = h;
    btn.style.top = `${y}px`;
    btn.style.height = `${h}px`;
    y += h + GAP_Y;
    maxY = Math.max(maxY, box.y + box.h + PAD + 16);
    maxX = Math.max(maxX, box.x + box.w + PAD);
  });
  const width = Math.max(maxX, canvas.clientWidth || 0, parseFloat(canvas.style.width) || 0);
  canvas.style.height = `${maxY}px`;
  canvas.style.width = `${width}px`;
  svg.setAttribute("width", String(width));
  svg.setAttribute("height", String(maxY));
  svg.setAttribute("viewBox", `0 0 ${width} ${maxY}`);
  svg.querySelectorAll("path[data-child]").forEach((line) => {
    const parent = positions[line.dataset.parent];
    const child = positions[line.dataset.child];
    if (!parent || !child) return;
    const x1 = parent.x + 16;
    const y1 = parent.y + parent.h;
    const x2 = child.x;
    const yMid = child.y + child.h / 2;
    const r = 8;
    let d;
    if (child.depth === parent.depth || Math.abs(child.x - parent.x) < 2) {
      d = `M ${x1} ${y1} V ${child.y}`;
    } else {
      d = `M ${x1} ${y1} V ${yMid - r} Q ${x1} ${yMid}, ${x1 + r} ${yMid} H ${x2}`;
    }
    line.setAttribute("d", d);
  });
}

function updateChapterPath() {
  const el = $("chapter-path");
  if (!el) return;
  const node = selectedTreeNode();
  if (!node) {
    el.textContent = "";
    return;
  }
  const byId = Object.fromEntries(treeNodes().map((item) => [item.id, item]));
  const parts = [];
  let cur = node;
  const seen = new Set();
  while (cur && !seen.has(cur.id)) {
    parts.unshift(cur.display_title || cur.file_title || `第${cur.num || ""}章`);
    seen.add(cur.id);
    cur = cur.parent_id ? byId[cur.parent_id] : null;
  }
  el.textContent = parts.join(" → ");
}

async function openChapter(id) {
  if (!state.book) return;
  await flushSaves();
  const node = findTreeNode(id);
  if (node) return openTreeNode(node);
  const data = await api(`/api/books/${encodeURIComponent(state.book.name)}/chapters/${id}`);
  applyOpenedChapter(data);
  document.querySelectorAll(".chapter-list li").forEach((li) => li.classList.toggle("active", li.dataset.id === data.id));
  updateCounts();
  updateChapterPath();
  syncPlotTreeSelection();
  scrollActiveChapterIntoView();
  if (state.stage === "outline") scrollSelectedTreeIntoView();
  hideConsistency();
  await loadSynopsis();
  closeDrawers();
}

function applyOpenedChapter(data, node) {
  state.chapterId = data.id;
  state.treeNodeId = (data.node && data.node.id) || (node && node.id) || data.id;
  const live = sameChapter(state.generatingChapterId, data.id) || (node && sameChapter(state.generatingChapterId, node.id));
  $("chapter-editor").value = live ? (state.genDraft || "") : (data.text || "");
  $("chapter-title").textContent = data.title || (node && (node.display_title || node.file_title)) || `第${Number(data.id)}章`;
  loadTimeline();
  loadIllustrations();
}

async function openTreeNode(node, options = {}) {
  if (!state.book || !node) return;
  await flushSaves();
  try {
    const data = await api(`/api/books/${encodeURIComponent(state.book.name)}/tree/nodes/${encodeURIComponent(node.id)}/open`, {
      method: "POST",
    });
    if (data.tree) applyTree(data.tree);
    applyOpenedChapter(data, node);
    renderChapters((state.bookData && state.bookData.chapters) || []);
    updateCounts();
    updateChapterPath();
    if (options.stage) setStage(options.stage);
    scrollActiveChapterIntoView();
    if (state.stage === "outline") scrollSelectedTreeIntoView();
    hideConsistency();
    await loadSynopsis();
    closeDrawers();
  } catch (err) {
    toast(err.message, true);
  }
}

function openDialog({ title, lead = "", fields = [], ok = "确定", danger = false }) {
  return new Promise((resolve) => {
    const modal = $("dialog");
    const form = $("dialog-form");
    $("dialog-title").textContent = title;
    $("dialog-lead").textContent = lead;
    $("dialog-ok").textContent = ok;
    $("dialog-ok").classList.toggle("danger", !!danger);
    $("dialog-ok").classList.toggle("primary", !danger);
    form.innerHTML = "";
    fields.forEach((field) => {
      const label = document.createElement("label");
      label.className = "field";
      label.appendChild(document.createTextNode(field.label || ""));
      const input = document.createElement(field.type === "textarea" ? "textarea" : "input");
      if (field.type !== "textarea") input.type = "text";
      else input.rows = 3;
      input.name = field.name;
      input.value = field.value || "";
      input.placeholder = field.placeholder || "";
      if (field.type === "textarea") input.style.fontFamily = "var(--font-ui)";
      label.appendChild(input);
      form.appendChild(label);
    });
    const cleanup = () => {
      $("dialog-ok").onclick = null;
      $("dialog-cancel").onclick = null;
      form.onsubmit = null;
      document.removeEventListener("keydown", onKey);
    };
    const finish = (value) => {
      modal.classList.add("hidden");
      cleanup();
      resolve(value);
    };
    const submit = () => {
      if (!fields.length) return finish(true);
      const values = {};
      fields.forEach((field) => {
        const input = form.elements[field.name];
        values[field.name] = input ? input.value.trim() : "";
      });
      finish(values);
    };
    const onKey = (ev) => {
      if (ev.key === "Escape") finish(null);
    };
    $("dialog-ok").onclick = submit;
    $("dialog-cancel").onclick = () => finish(null);
    form.onsubmit = (ev) => { ev.preventDefault(); submit(); };
    document.addEventListener("keydown", onKey);
    modal.classList.remove("hidden");
    const first = form.querySelector("input, textarea");
    if (first) first.focus();
  });
}

async function addBranchFromSelected() {
  if (!state.book) return toast("请先选择作品", true);
  if (state.generating) return toast("请先等待当前生成结束", true);
  const node = selectedTreeNode();
  if (!node) return toast("请先在脉络图中点选一个节点", true);
  const plan = node.ending_plan || {};
  const suggested = plan.suggested_branch || node.suggested_branch || "二结局";
  const forkTitle = plan.from_title || node.display_title || "当前节点";
  const start = plan.start_label || "";
  const siblings = (plan.sibling_branches || []).join("、");
  let lead = `从「${forkTitle}」分出「${suggested}」`;
  if (start) lead += `，新章从「${start}」开始`;
  if (siblings) lead += `，与${siblings}对齐`;
  lead += "。原来的走向会保留；之后写这条线时，模型只会看见分叉点之前的内容，不会看到其他结局。";
  const values = await openDialog({
    title: "添加结局分支",
    lead,
    fields: [
      { name: "branch_name", label: "分支名称", value: suggested, placeholder: "例如：三结局" },
      { name: "idea", label: "走向（选填）", type: "textarea", placeholder: "可以先写一句这条线想怎么走" },
    ],
    ok: "添加分支",
  });
  if (!values) return;
  if (!values.branch_name) return toast("请填写分支名称", true);
  await flushSaves();
  try {
    const result = await api(`/api/books/${encodeURIComponent(state.book.name)}/tree/branches`, {
      method: "POST",
      body: JSON.stringify({
        from_chapter: node.chapter_id || node.id,
        branch_name: values.branch_name,
        idea: values.idea || "",
        count: 1,
      }),
    });
    if (result.outline) $("outline-editor").value = result.outline;
    if (result.tree) applyTree(result.tree);
    state.bookData = Object.assign({}, state.bookData || {}, { chapters: result.chapters, outline: result.outline, tree: result.tree });
    renderChapters(result.chapters || []);
    updateCounts();
    const first = (result.chapter_ids || [])[0];
    if (first) {
      const created = findTreeNode(first);
      if (created) {
        state.treeNodeId = created.id;
        renderPlotTrees();
      }
    }
    toast(`已添加「${values.branch_name}」。点开新节点即可开始写`);
  } catch (err) {
    toast(err.message, true);
  }
}

async function deleteSelectedChapter() {
  if (!state.book) return toast("请先选择作品", true);
  if (state.generating) return toast("本章正在生成，请等生成结束后再删除", true);
  const node = selectedTreeNode();
  const chapterId = (node && node.chapter_id) || state.chapterId;
  if (!chapterId) return toast("请先选择要删除的章节", true);
  if (node && !node.has_file && !state.chapterId) return toast("这一章还没有正文文件", true);
  const title = (node && node.display_title) || $("chapter-title").textContent || `第${Number(chapterId)}章`;
  const ok = await openDialog({
    title: "删除本章正文",
    lead: `将删除「${title}」的正文文件，大纲节点会保留，章节编号也不会改动。删除前会自动备份，之后可用「恢复已保存的本章」或「恢复上一版正文」取回。`,
    ok: "确认删除",
    danger: true,
  });
  if (!ok) return;
  await flushSaves();
  try {
    const result = await api(`/api/books/${encodeURIComponent(state.book.name)}/chapters/${chapterId}`, { method: "DELETE" });
    if (result.tree) applyTree(result.tree);
    state.bookData = Object.assign({}, state.bookData || {}, { chapters: result.chapters, tree: result.tree });
    if (state.chapterId === chapterId) {
      $("chapter-editor").value = "";
      $("chapter-title").textContent = title;
      updateCounts();
    }
    renderChapters(result.chapters || []);
    updateChapterPath();
    toast(result.backed_up ? "本章正文已删除，备份已留下，可以随时恢复" : "本章正文已删除");
  } catch (err) {
    toast(err.message, true);
  }
}

async function openSelectedTreeChapter() {
  const node = selectedTreeNode();
  if (!node) return toast("请先在脉络图中点选一个节点", true);
  await openTreeNode(node, { stage: "body" });
}

function setStage(stage) {
  state.stage = stage;
  document.querySelectorAll(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.stage === stage));
  document.querySelectorAll("[data-stage-panel]").forEach((panel) => {
    panel.classList.toggle("hidden", panel.dataset.stagePanel !== stage);
  });
  if (stage === "outline") {
    requestAnimationFrame(() => renderPlotTrees({ scrollToSelected: true, force: true }));
  }
  if (stage === "characters") {
    loadCharacters();
  }
  const openIllust = $("btn-open-illust");
  if (openIllust) openIllust.classList.toggle("hidden", stage !== "body");
  applyPaneCollapse();
  if (stage === "body") loadIllustrations();
  loadTimeline();
}

function updateCounts() {
  $("idea-count").textContent = hanzi($("idea-editor").value);
  $("outline-count").textContent = hanzi($("outline-editor").value);
  if ($("world-count") && $("world-editor")) $("world-count").textContent = hanzi($("world-editor").value);
  $("chapter-count").textContent = chapterHanzi($("chapter-editor").value);
  $("chapter-target").textContent = $("param-chars").value;
}

async function readSSE(response, handlers) {
  if (!response.ok) {
    let detail = `请求失败 ${response.status}`;
    try {
      const data = await response.json();
      detail = data.detail || detail;
    } catch (_) {}
    throw new Error(detail);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const parts = buf.split("\n\n");
    buf = parts.pop() || "";
    for (const block of parts) {
      let event = "message";
      const dataLines = [];
      block.split("\n").forEach((line) => {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
      });
      if (!dataLines.length) continue;
      let payload = {};
      try { payload = JSON.parse(dataLines.join("\n")); } catch (_) { payload = { text: dataLines.join("\n") }; }
      if (handlers[event]) handlers[event](payload);
    }
  }
}

function formatDuration(seconds) {
  const sec = Math.max(0, Math.round(Number(seconds) || 0));
  if (sec < 60) return `${sec} 秒`;
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  if (m < 60) return s ? `${m} 分 ${s} 秒` : `${m} 分钟`;
  const h = Math.floor(m / 60);
  const mm = m % 60;
  return mm ? `${h} 小时 ${mm} 分` : `${h} 小时`;
}

function formatEta(seconds) {
  const sec = Math.max(0, Math.round(Number(seconds) || 0));
  if (sec < 15) return "即将完成";
  if (sec < 90) return `约 ${Math.round(sec / 5) * 5} 秒`;
  const m = Math.max(1, Math.round(sec / 60));
  if (m < 60) return `约 ${m} 分钟`;
  const h = Math.floor(m / 60);
  const mm = m % 60;
  return mm ? `约 ${h} 小时 ${mm} 分` : `约 ${h} 小时`;
}

function stopProgressTicker() {
  if (state.progressTimer) {
    clearInterval(state.progressTimer);
    state.progressTimer = null;
  }
  state.progress = null;
  const fill = $("progress-fill");
  if (fill) {
    fill.style.width = "";
    if (fill.parentElement) fill.parentElement.classList.remove("determinate");
  }
}

function renderProgress() {
  const p = state.progress;
  if (!p || !$("progress-text")) return;
  const now = Date.now();
  const elapsed = (now - p.startedAt) / 1000;
  const elapsedText = formatDuration(elapsed);
  let line;
  if (p.written > 0) {
    line = p.target > 0 ? `已写出 ${p.written} / ${p.target} 字` : `已写出 ${p.written} 字`;
    line += ` · 已用 ${elapsedText}`;
    const writeElapsed = Math.max(0.5, (now - (p.firstTextAt || p.startedAt)) / 1000);
    const speed = p.written / writeElapsed;
    if (p.target > 0 && p.written >= p.target) {
      line += " · 即将完成";
    } else if (p.target > 0 && p.written >= 80 && writeElapsed >= 8 && speed > 0) {
      line += ` · 预计还需 ${formatEta((p.target - p.written) / speed)}`;
    } else if (p.target > 0) {
      line += " · 正在估算剩余时间";
    }
    const fill = $("progress-fill");
    if (fill && p.target > 0) {
      const ratio = Math.max(0.04, Math.min(0.99, p.written / p.target));
      fill.parentElement.classList.add("determinate");
      fill.style.width = `${Math.round(ratio * 100)}%`;
    }
  } else {
    line = `${p.label || "正在生成…"} · 已用 ${elapsedText}`;
    if (p.thinking) line += "。思考阶段常常要几分钟，出字后会估算还要多久";
    else line += "。出字后会估算还要多久";
  }
  $("progress-text").textContent = line;
}

function setGenerating(on, label) {
  state.generating = on;
  $("progress").classList.toggle("hidden", !on);
  $("btn-stop").classList.toggle("hidden", !on);
  applyActionLocks();
  if (!on) {
    stopProgressTicker();
    $("progress-text").textContent = label || "正在生成…";
  } else if (label) {
    $("progress-text").textContent = label;
  }
}

function applyActionLocks() {
  const writingLocked = !!(state.generating || state.illustSession);
  ["btn-gen-outline", "btn-regen-outline", "btn-revise-outline", "btn-restore-outline", "btn-save-outline", "btn-fill-outline", "btn-next-outline", "btn-draft-world", "btn-gen-world", "btn-gen-characters", "btn-gen-chapter", "btn-continue-chapter", "btn-expand-chapter", "btn-revise-chapter", "btn-restore-chapter", "btn-save-chapter", "btn-check-consistency", "btn-restore-saved-chapter", "btn-delete-chapter", "btn-tree-open", "btn-tree-branch", "btn-tree-delete", "btn-list-branch", "btn-add-character"].forEach((id) => {
    if ($(id)) $(id).disabled = writingLocked;
  });
  if ($("btn-illust-passage")) $("btn-illust-passage").disabled = !!state.generating;
  if ($("btn-illust-chapter")) $("btn-illust-chapter").disabled = !!state.generating;
  if ($("btn-illust-generate")) $("btn-illust-generate").disabled = !!state.generating;
  if ($("btn-illust-finish")) $("btn-illust-finish").disabled = !state.illustSession || !!state.generating;
  const hint = $("illust-session-hint");
  if (hint) hint.classList.toggle("hidden", !state.illustSession);
}

function setIllustSession(on) {
  state.illustSession = !!on;
  applyActionLocks();
}

async function runGeneration(url, body, targetEditor, afterDone, options = {}) {
  if (!state.book) return toast("请先选择或新建一本书", true);
  if (state.generating) return;
  const boundChapterId = options.chapterId || null;
  const startText = targetEditor ? targetEditor.value : "";
  const keepPrefix = !!(options.keepPrefix && startText.trim());
  const prefix = keepPrefix ? startText.replace(/\s+$/, "") + "\n\n" : "";
  let acc = "";
  let finishedText = "";
  state.abort = new AbortController();
  state.generatingChapterId = boundChapterId;
  state.genDraft = boundChapterId ? (prefix || "") : "";
  const writeGen = (text) => {
    if (boundChapterId) state.genDraft = text;
    if (viewingGenEditor(targetEditor, boundChapterId)) {
      targetEditor.value = text;
      updateCounts();
    }
  };
  stopProgressTicker();
  state.progress = {
    startedAt: Date.now(),
    firstTextAt: null,
    written: 0,
    target: Math.max(0, Number(options.targetChars) || 0),
    thinking: !!body.enable_thinking,
    label: options.label || "正在连接写作模型…",
  };
  setGenerating(true, options.label || "正在连接写作模型…");
  renderProgress();
  state.progressTimer = setInterval(renderProgress, 1000);
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: state.abort.signal,
    });
    await readSSE(res, {
      meta: (p) => {
        state.jobId = p.job_id;
        if (!state.progress) return;
        state.progress.label = ({
          outline: "正在生成大纲…",
          "outline-next": "正在写下一章大纲…",
          "outline-section": "正在写本章大纲…",
          world: "正在起草世界观…",
          consistency: "正在检查一致性…",
          synopsis: "正在更新本章摘要…",
        })[p.kind] || options.label || "正在生成正文…";
        renderProgress();
      },
      status: (p) => {
        if (!state.progress) return;
        const message = p.message || "正在生成…";
        state.progress.label = message;
        if (message.includes("思考")) state.progress.thinking = true;
        renderProgress();
      },
      text: (p) => {
        acc += p.text || "";
        writeGen(prefix + acc);
        if (!state.progress) return;
        if (!state.progress.firstTextAt) state.progress.firstTextAt = Date.now();
        state.progress.written = hanzi(acc);
        state.progress.thinking = false;
        renderProgress();
      },
      done: (p) => {
        let finalText = p.text || (prefix + acc);
        if (keepPrefix && startText.trim()) {
          const head = startText.trim().slice(0, 80);
          if (!finalText.trim().startsWith(head)) finalText = prefix + (p.text || acc);
        }
        finishedText = finalText;
        writeGen(finalText);
        if (afterDone) afterDone(p, { text: finalText, chapterId: boundChapterId });
      },
      error: (p) => { writeGen(startText); throw new Error(p.message || "生成失败"); },
      cancelled: (p) => { toast(p.message || "已停止生成"); writeGen(startText); },
    });
  } catch (err) {
    if (err.name !== "AbortError") toast(err.message, true);
  } finally {
    setGenerating(false);
    state.abort = null;
    state.jobId = null;
    state.generatingChapterId = null;
    state.genDraft = "";
  }
  return finishedText;
}

async function generateOutline() {
  if (!state.book) return toast("请先选择或新建一本书", true);
  if ($("outline-editor").value.trim()) {
    const ok = window.confirm("将按「思路」重新生成大纲，并覆盖当前内容。改进思路不会被使用。确定吗？");
    if (!ok) return;
  }
  await api(`/api/books/${encodeURIComponent(state.book.name)}/idea`, {
    method: "PUT",
    body: JSON.stringify({ text: $("idea-editor").value }),
  });
  await saveWorldNow();
  setStage("outline");
  $("outline-editor").value = "";
  await runGeneration(
    `/api/books/${encodeURIComponent(state.book.name)}/generate/outline`,
    {
      idea: $("idea-editor").value,
      style_ids: Array.from(state.selected),
      temperature: Number($("param-temp").value),
      enable_thinking: $("param-thinking").checked,
      llm_url: $("param-url").value.trim(),
    },
    $("outline-editor"),
    async (p) => { toast(`大纲已保存，共 ${p.hanzi || hanzi($("outline-editor").value)} 字`); await reloadBookMeta(); await loadTimeline(); },
    { targetChars: 2500 }
  );
}

async function reviseOutline() {
  if (!state.book) return toast("请先选择或新建一本书", true);
  const notes = $("outline-revise").value.trim();
  if (!notes) return toast("请先写下改进思路", true);
  const current = $("outline-editor").value.trim();
  if (!current) return toast("请先生成或填写大纲，再按改进思路修改", true);
  await api(`/api/books/${encodeURIComponent(state.book.name)}/idea`, {
    method: "PUT",
    body: JSON.stringify({ text: $("idea-editor").value }),
  });
  await putOutlineSafely();
  await saveWorldNow();
  await runGeneration(
    `/api/books/${encodeURIComponent(state.book.name)}/generate/outline`,
    {
      idea: $("idea-editor").value,
      revision: notes,
      outline: $("outline-editor").value,
      style_ids: Array.from(state.selected),
      temperature: Number($("param-temp").value),
      enable_thinking: $("param-thinking").checked,
      llm_url: $("param-url").value.trim(),
    },
    $("outline-editor"),
    async (p) => { toast(`大纲已按改进思路修改，共 ${p.hanzi || hanzi($("outline-editor").value)} 字`); await reloadBookMeta(); await loadTimeline(); },
    { targetChars: Math.max(2500, Math.min(hanzi($("outline-editor").value) + 800, 8000)), label: "正在按改进思路修改当前大纲…" }
  );
}

async function restorePreviousOutline() {
  if (!state.book) return toast("请先选择或新建一本书", true);
  if (state.generating) return toast("请先等待当前生成结束", true);
  try {
    const data = await api(`/api/books/${encodeURIComponent(state.book.name)}/outline/restore`, { method: "POST" });
    $("outline-editor").value = data.outline || "";
    if (data.tree) applyTree(data.tree);
    updateCounts();
    renderPlotTrees();
    toast(`已恢复上一版大纲，共 ${data.hanzi || hanzi($("outline-editor").value)} 字`);
    loadTimeline();
  } catch (err) {
    toast(err.message, true);
  }
}

async function generateChapter(mode) {
  if (!state.book) return toast("请先选择或新建一本书", true);
  if (mode === "generate" && $("chapter-editor").value.trim()) {
    const ok = window.confirm("生成本章会覆盖当前正文。若这版已经满意，请先点「保存本章」。确定要重新生成吗？");
    if (!ok) return;
  }
  await flushSaves();
  try {
    await api(`/api/books/${encodeURIComponent(state.book.name)}/idea`, {
      method: "PUT",
      body: JSON.stringify({ text: $("idea-editor").value }),
    });
    await saveWorldNow();
    await putOutlineSafely();
    if (state.chapterId && (mode === "continue" || $("chapter-editor").value.trim())) {
      await api(`/api/books/${encodeURIComponent(state.book.name)}/chapters/${state.chapterId}`, {
        method: "PUT",
        body: JSON.stringify({ text: $("chapter-editor").value }),
      });
    }
  } catch (err) {
    return toast(err.message, true);
  }
  if (!state.chapterId) {
    const added = await api(`/api/books/${encodeURIComponent(state.book.name)}/chapters`, {
      method: "POST",
      body: JSON.stringify({ title: "" }),
    });
    $("outline-editor").value = added.outline || $("outline-editor").value;
    renderChapters(added.chapters || []);
    await openChapter(added.id);
    updateCounts();
  }
  const targetChapterId = state.chapterId;
  const keepExisting = mode === "continue";
  if (!keepExisting) $("chapter-editor").value = "";
  const chapterTarget = Math.max(200, Number($("param-chars").value) || 2000);
  const already = keepExisting ? chapterHanzi($("chapter-editor").value) : 0;
  const payload = {
      chapter: targetChapterId,
      mode,
      style_ids: Array.from(state.selected),
      temperature: Number($("param-temp").value),
      enable_thinking: $("param-thinking").checked,
      target_chars: chapterTarget,
      llm_url: $("param-url").value.trim(),
      outline: $("outline-editor").value,
  };
  if (keepExisting) payload.text = $("chapter-editor").value;
  const title = chapterLabel(targetChapterId);
  const finished = await runGeneration(
    `/api/books/${encodeURIComponent(state.book.name)}/generate/chapter`,
    payload,
    $("chapter-editor"),
    async (p, meta) => {
      const n = p.hanzi || hanzi((meta && meta.text) || p.text || "");
      const viewing = sameChapter(state.chapterId, targetChapterId);
      const verb = mode === "continue" ? "已续写并保存" : "已生成并保存";
      toast(viewing ? `本章${verb}，共 ${n} 字` : `${title}${verb}，共 ${n} 字。你正在编辑的章节没有被改动`);
      await reloadBookMeta();
      await loadTimeline();
    },
    {
      targetChars: Math.max(200, chapterTarget - already),
      keepPrefix: mode === "continue",
      label: mode === "continue" ? `正在续写${title}…` : `正在生成${title}…`,
      chapterId: targetChapterId,
    }
  );
  maybeRefreshSynopsis(targetChapterId, finished);
}



async function saveCurrentChapter() {
  if (!state.book) return toast("请先选择或新建一本书", true);
  if (state.generating) return toast("请先等待当前生成结束", true);
  if (!state.chapterId) return toast("请先选择要保存的章节", true);
  const current = $("chapter-editor").value.trim();
  if (!current) return toast("当前章节没有可保存的正文", true);
  await flushSaves();
  try {
    const data = await api(`/api/books/${encodeURIComponent(state.book.name)}/chapters/${state.chapterId}/save`, {
      method: "POST",
      body: JSON.stringify({ text: $("chapter-editor").value }),
    });
    toast(`本章已存档，共 ${data.hanzi || hanzi(current)} 字。写下章或重新生成也不会盖掉这份保存`);
    maybeRefreshSynopsis();
    loadTimeline();
  } catch (err) {
    toast(err.message, true);
  }
}

async function restoreSavedChapter() {
  if (!state.book) return toast("请先选择或新建一本书", true);
  if (state.generating) return toast("请先等待当前生成结束", true);
  if (!state.chapterId) return toast("请先选择要恢复的章节", true);
  try {
    const data = await api(`/api/books/${encodeURIComponent(state.book.name)}/chapters/${state.chapterId}/restore-saved`, { method: "POST" });
    $("chapter-editor").value = data.text || "";
    updateCounts();
    toast(`已恢复上次保存的本章，共 ${data.hanzi || hanzi($("chapter-editor").value)} 字`);
    await reloadBookMeta();
    await loadTimeline();
  } catch (err) {
    toast(err.message, true);
  }
}


async function expandChapter() {
  if (!state.book) return toast("请先选择或新建一本书", true);
  if (state.generating) return toast("请先等待当前生成结束", true);
  await flushSaves();
  if (!state.chapterId) return toast("请先选择要扩写的章节", true);
  const current = $("chapter-editor").value.trim();
  if (!current) return toast("请先生成本章正文，再扩写", true);
  try {
    await api(`/api/books/${encodeURIComponent(state.book.name)}/idea`, {
      method: "PUT",
      body: JSON.stringify({ text: $("idea-editor").value }),
    });
    await saveWorldNow();
    await putOutlineSafely();
    await api(`/api/books/${encodeURIComponent(state.book.name)}/chapters/${state.chapterId}`, {
      method: "PUT",
      body: JSON.stringify({ text: $("chapter-editor").value }),
    });
  } catch (err) {
    return toast(err.message, true);
  }
  const targetChapterId = state.chapterId;
  const chapterTarget = Math.max(200, Number($("param-chars").value) || 2000);
  const currentHanzi = chapterHanzi(current);
  const title = chapterLabel(targetChapterId);
  const finished = await runGeneration(
    `/api/books/${encodeURIComponent(state.book.name)}/generate/chapter`,
    {
      chapter: targetChapterId,
      mode: "expand",
      text: $("chapter-editor").value,
      style_ids: Array.from(state.selected),
      temperature: Number($("param-temp").value),
      enable_thinking: $("param-thinking").checked,
      target_chars: chapterTarget,
      llm_url: $("param-url").value.trim(),
      outline: $("outline-editor").value,
    },
    $("chapter-editor"),
    async (p, meta) => {
      const n = p.hanzi || hanzi((meta && meta.text) || p.text || "");
      const viewing = sameChapter(state.chapterId, targetChapterId);
      toast(viewing ? `本章已扩写并保存，共 ${n} 字` : `${title}已扩写并保存，共 ${n} 字。你正在编辑的章节没有被改动`);
      await reloadBookMeta();
      await loadTimeline();
    },
    { targetChars: Math.max(chapterTarget, currentHanzi), label: `正在扩写${title}…`, chapterId: targetChapterId }
  );
  maybeRefreshSynopsis(targetChapterId, finished);
}

async function reviseChapter() {
  if (!state.book) return toast("请先选择或新建一本书", true);
  if (state.generating) return toast("请先等待当前生成结束", true);
  const notes = $("chapter-revise").value.trim();
  if (!notes) return toast("请先写下改进意见", true);
  await flushSaves();
  if (!state.chapterId) return toast("请先选择要修改的章节", true);
  const current = $("chapter-editor").value.trim();
  if (!current) return toast("请先生成本章正文，再按改进意见修改", true);
  try {
    await api(`/api/books/${encodeURIComponent(state.book.name)}/idea`, {
      method: "PUT",
      body: JSON.stringify({ text: $("idea-editor").value }),
    });
    await saveWorldNow();
    await putOutlineSafely();
    await api(`/api/books/${encodeURIComponent(state.book.name)}/chapters/${state.chapterId}`, {
      method: "PUT",
      body: JSON.stringify({ text: $("chapter-editor").value }),
    });
  } catch (err) {
    return toast(err.message, true);
  }
  const targetChapterId = state.chapterId;
  const currentHanzi = chapterHanzi(current);
  const title = chapterLabel(targetChapterId);
  const finished = await runGeneration(
    `/api/books/${encodeURIComponent(state.book.name)}/generate/chapter`,
    {
      chapter: targetChapterId,
      mode: "revise",
      revision: notes,
      text: $("chapter-editor").value,
      style_ids: Array.from(state.selected),
      temperature: Number($("param-temp").value),
      enable_thinking: $("param-thinking").checked,
      target_chars: Math.max(currentHanzi, Math.min(currentHanzi + 800, 20000)),
      llm_url: $("param-url").value.trim(),
      outline: $("outline-editor").value,
    },
    $("chapter-editor"),
    async (p, meta) => {
      const n = p.hanzi || hanzi((meta && meta.text) || p.text || "");
      const viewing = sameChapter(state.chapterId, targetChapterId);
      if (p.short_warning) {
        toast(p.message || `修改后共 ${n} 字，比原文短。若不是你想要的，请用「恢复上一版」。`);
      } else {
        toast(viewing ? `本章已按改进意见修改，共 ${n} 字` : `${title}已按改进意见修改并保存，共 ${n} 字。你正在编辑的章节没有被改动`);
      }
      await reloadBookMeta();
      await loadTimeline();
    },
    { targetChars: Math.max(currentHanzi, Math.min(currentHanzi + 800, 20000)), label: `正在按改进意见修改${title}…`, chapterId: targetChapterId }
  );
  maybeRefreshSynopsis(targetChapterId, finished);
}

async function restorePreviousChapter() {
  if (!state.book) return toast("请先选择或新建一本书", true);
  if (state.generating) return toast("请先等待当前生成结束", true);
  if (!state.chapterId) return toast("请先选择要恢复的章节", true);
  try {
    const data = await api(`/api/books/${encodeURIComponent(state.book.name)}/chapters/${state.chapterId}/restore`, { method: "POST" });
    $("chapter-editor").value = data.text || "";
    updateCounts();
    toast(`已恢复上一版正文，共 ${data.hanzi || hanzi($("chapter-editor").value)} 字`);
    loadTimeline();
    await reloadBookMeta();
  } catch (err) {
    toast(err.message, true);
  }
}

async function stopGeneration() {
  if (state.jobId) {
    try { await api(`/api/generate/cancel?job_id=${encodeURIComponent(state.jobId)}`, { method: "POST" }); } catch (_) {}
  }
  if (state.abort) state.abort.abort();
}


function clearEditor(id, label, shouldConfirm) {
  const el = $(id);
  if (!el) return;
  if (!el.value) return;
  if (state.generating) return toast("正在生成，请等结束后再清空", true);
  if (shouldConfirm && !confirm(`确定清空「${label}」吗？清空后会保存为空。`)) return;
  el.value = "";
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.focus();
}


async function saveWorldNow() {
  await putWorldSafely();
}

async function loadCharacters() {
  if (!state.book) {
    state.characters = [];
    renderCharacters();
    return;
  }
  const chapter = state.chapterId || (selectedTreeNode() && (selectedTreeNode().chapter_id || selectedTreeNode().id));
  const q = chapter ? `?chapter=${encodeURIComponent(chapter)}` : "";
  try {
    const data = await api(`/api/books/${encodeURIComponent(state.book.name)}/characters${q}`);
    state.characters = data.characters || [];
  } catch (err) {
    // 人物卡是可选项，接口不存在或还没有人物时不打扰
    state.characters = state.characters || [];
  }
  if (state.characterId && !state.characters.some((item) => item.id === state.characterId)) {
    state.characterId = null;
  }
  renderCharacters();
}

function renderCharacters() {
  const list = $("character-list");
  if (!list) return;
  list.innerHTML = "";
  if (!state.characters.length) {
    const empty = document.createElement("li");
    empty.className = "empty";
    empty.textContent = "还没有人物。先添加一张卡片。";
    list.appendChild(empty);
    renderCharacterDetail();
    return;
  }
  state.characters.forEach((item) => {
    const li = document.createElement("li");
    if (item.id === state.characterId) li.classList.add("active");
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = item.name;
    const meta = document.createElement("span");
    meta.className = "meta";
    const n = (item.path_patches || []).length;
    meta.textContent = n ? `本线 ${n} 条补丁` : "本线尚无补丁";
    li.appendChild(name);
    li.appendChild(meta);
    li.addEventListener("click", () => {
      state.characterId = item.id;
      renderCharacters();
    });
    list.appendChild(li);
  });
  renderCharacterDetail();
}

function selectedCharacter() {
  return (state.characters || []).find((item) => item.id === state.characterId) || null;
}

function renderCharacterDetail() {
  const empty = $("character-empty");
  const detail = $("character-detail");
  if (!empty || !detail) return;
  const item = selectedCharacter();
  if (!item) {
    empty.classList.remove("hidden");
    detail.classList.add("hidden");
    return;
  }
  empty.classList.add("hidden");
  detail.classList.remove("hidden");
  $("character-name").value = item.name || "";
  $("character-basics").value = item.basics || "";
  if ($("character-appearance")) $("character-appearance").value = item.appearance || "";
  $("character-state").textContent = item.current_state || "（沿本线尚无状态变化）";
  const hint = $("character-path-hint");
  if (hint) {
    const node = selectedTreeNode();
    hint.textContent = node
      ? `当前按「${node.display_title || "本章"}」所在的这条线累积补丁。写下一章时，模型会看到写到上一章为止的状态。`
      : "补丁会记在引起变化的那一章旁边，只沿当前情节线往下累积。点选一章后，这里会显示这条线上的状态。";
  }
  const list = $("patch-list");
  list.innerHTML = "";
  (item.path_patches || []).forEach((patch) => {
    const li = document.createElement("li");
    li.className = "patch-item";
    const head = document.createElement("div");
    head.className = "patch-head";
    const label = document.createElement("span");
    try { label.textContent = `第${Number(patch.chapter_id)}章`; }
    catch (_) { label.textContent = patch.chapter_id || ""; }
    const del = document.createElement("button");
    del.type = "button";
    del.className = "btn small";
    del.textContent = "删除";
    del.addEventListener("click", () => deletePatch(item.id, patch.id));
    head.appendChild(label);
    head.appendChild(del);
    const body = document.createElement("div");
    body.textContent = patch.text || "";
    li.appendChild(head);
    li.appendChild(body);
    list.appendChild(li);
  });
}

function scheduleCharacterSave() {
  if (!state.book || !state.characterId) return;
  const id = state.characterId;
  scheduleSave("character-" + id, async () => {
    await api(`/api/books/${encodeURIComponent(state.book.name)}/characters/${id}`, {
      method: "PUT",
      body: JSON.stringify({
        name: $("character-name").value,
        basics: $("character-basics").value,
        appearance: $("character-appearance") ? $("character-appearance").value : "",
      }),
    });
    await loadCharacters();
  });
}

async function addCharacter() {
  if (!state.book) return toast("请先选择或新建一本书", true);
  const values = await openDialog({
    title: "添加人物",
    lead: "先写一个名字，基础设定可以稍后在卡片里补充。",
    fields: [
      { name: "name", label: "名称", placeholder: "例如：林晚" },
      { name: "basics", label: "基础设定（选填）", type: "textarea", placeholder: "身份、性格、与本书相关的固定设定……" },
      { name: "appearance", label: "外形（选填）", type: "textarea", placeholder: "外貌、体态、常穿的衣服等可见特征" },
    ],
    ok: "添加",
  });
  if (!values) return;
  if (!values.name) return toast("请填写人物名称", true);
  try {
    const item = await api(`/api/books/${encodeURIComponent(state.book.name)}/characters`, {
      method: "POST",
      body: JSON.stringify({ name: values.name, basics: values.basics || "", appearance: values.appearance || "" }),
    });
    state.characterId = item.id;
    await loadCharacters();
    toast(`已添加「${item.name}」`);
  } catch (err) {
    toast(err.message, true);
  }
}

async function deleteSelectedCharacter() {
  if (!state.book || !state.characterId) return;
  const item = selectedCharacter();
  const title = item ? item.name : "该人物";
  if (!confirm(`确定删除「${title}」吗？基础设定和补丁都会一起删除。`)) return;
  try {
    await api(`/api/books/${encodeURIComponent(state.book.name)}/characters/${state.characterId}`, { method: "DELETE" });
    state.characterId = null;
    await loadCharacters();
    toast(`已删除「${title}」`);
  } catch (err) {
    toast(err.message, true);
  }
}

async function addPatch() {
  if (!state.book || !state.characterId) return toast("请先选择一个人物", true);
  const node = selectedTreeNode();
  const defaultChapter = (node && (node.chapter_id || node.id)) || state.chapterId || "";
  const values = await openDialog({
    title: "添加状态补丁",
    lead: "补丁记在引起变化的那一章。只有走这条线时才会累积到当前状态。",
    fields: [
      { name: "chapter_id", label: "章节编号", value: defaultChapter, placeholder: "例如：01" },
      { name: "text", label: "状态变化", type: "textarea", placeholder: "例如：得知潮汐电台的秘密，开始怀疑父亲" },
    ],
    ok: "添加补丁",
  });
  if (!values) return;
  if (!values.chapter_id) return toast("请填写章节编号", true);
  if (!values.text) return toast("请填写状态变化", true);
  try {
    await api(`/api/books/${encodeURIComponent(state.book.name)}/characters/${state.characterId}/patches`, {
      method: "POST",
      body: JSON.stringify({ chapter_id: values.chapter_id, text: values.text }),
    });
    await loadCharacters();
    toast("补丁已添加");
  } catch (err) {
    toast(err.message, true);
  }
}

async function deletePatch(characterId, patchId) {
  if (!state.book) return;
  if (!confirm("确定删除这条补丁吗？")) return;
  try {
    await api(`/api/books/${encodeURIComponent(state.book.name)}/characters/${characterId}/patches/${patchId}`, {
      method: "DELETE",
    });
    await loadCharacters();
  } catch (err) {
    toast(err.message, true);
  }
}

async function generateCharacters() {
  if (!state.book) return toast("请先选择或新建一本书", true);
  if (!$("idea-editor").value.trim()) return toast("请先写下思路，再生成人物", true);
  await flushSaves();
  try {
    await api(`/api/books/${encodeURIComponent(state.book.name)}/idea`, {
      method: "PUT",
      body: JSON.stringify({ text: $("idea-editor").value }),
    });
    await saveWorldNow();
  } catch (err) {
    return toast(err.message, true);
  }
  await runGeneration(
    `/api/books/${encodeURIComponent(state.book.name)}/generate/characters`,
    {
      idea: $("idea-editor").value,
      temperature: Number($("param-temp").value),
      enable_thinking: $("param-thinking").checked,
      llm_url: $("param-url").value.trim(),
    },
    null,
    async (p) => {
      state.characters = p.characters || state.characters;
      renderCharacters();
      setStage("characters");
      const n = (p.added && p.added.length) || p.count || 0;
      const skipped = (p.skipped && p.skipped.length) || 0;
      if (n) {
        toast(`已从思路生成 ${n} 个人物，可在人物页查看或修改` + (skipped ? `（${skipped} 个已有同名，已跳过）` : ""));
      } else if (skipped) {
        toast("这些人物已经有了，未再新增。可在人物页继续修改");
      } else {
        toast("没有新增人物，可在人物页手动添加");
      }
      await loadCharacters();
    },
    { targetChars: 1200, label: "正在从思路生成人物…" }
  );
}

async function generateWorldDraft() {
  if (!state.book) return toast("请先选择或新建一本书", true);
  if (!$("idea-editor").value.trim()) return toast("请先写下思路，再起草世界观", true);
  await flushSaves();
  await api(`/api/books/${encodeURIComponent(state.book.name)}/idea`, {
    method: "PUT",
    body: JSON.stringify({ text: $("idea-editor").value }),
  });
  setStage("world");
  await runGeneration(
    `/api/books/${encodeURIComponent(state.book.name)}/generate/world`,
    {
      idea: $("idea-editor").value,
      temperature: Number($("param-temp").value),
      enable_thinking: $("param-thinking").checked,
      llm_url: $("param-url").value.trim(),
    },
    $("world-editor"),
    async (p) => { toast(`世界观已保存，共 ${p.hanzi || hanzi($("world-editor").value)} 字`); },
    { targetChars: 1600, label: "正在从思路起草世界观…" }
  );
}

async function generateThisOutlineChapter() {
  if (!state.book) return toast("请先选择或新建一本书", true);
  const node = selectedTreeNode();
  if (!node) return toast("请先在情节脉络中点选要写大纲的那一章", true);
  const chapterId = node.chapter_id || node.id;
  await flushSaves();
  try {
    await api(`/api/books/${encodeURIComponent(state.book.name)}/idea`, {
      method: "PUT",
      body: JSON.stringify({ text: $("idea-editor").value }),
    });
    await saveWorldNow();
    await putOutlineSafely();
  } catch (err) {
    return toast(err.message, true);
  }
  setStage("outline");
  const notes = "";
  const existing = $("outline-editor").value;
  await runGeneration(
    `/api/books/${encodeURIComponent(state.book.name)}/generate/outline-section`,
    {
      idea: $("idea-editor").value,
      outline: existing,
      chapter_id: chapterId,
      notes,
      style_ids: Array.from(state.selected),
      temperature: Number($("param-temp").value),
      enable_thinking: $("param-thinking").checked,
      llm_url: $("param-url").value.trim(),
    },
    null,
    async (p) => {
      const full = p.outline || p.text || $("outline-editor").value;
      $("outline-editor").value = full;
      updateCounts();
      const fresh = findTreeNode(chapterId) || node;
      scrollOutlineToNode(fresh);
      toast(`已写好「${(fresh && fresh.display_title) || "本章"}」的大纲，共 ${p.hanzi || hanzi(full)} 字，可先审看`);
      await reloadBookMeta();
      await loadTimeline();
    },
    { targetChars: 800, label: "正在写本章大纲…" }
  );
}

async function generateNextOutlineChapter() {
  if (!state.book) return toast("请先选择或新建一本书", true);
  await flushSaves();
  try {
    await api(`/api/books/${encodeURIComponent(state.book.name)}/idea`, {
      method: "PUT",
      body: JSON.stringify({ text: $("idea-editor").value }),
    });
    await saveWorldNow();
    await putOutlineSafely();
  } catch (err) {
    return toast(err.message, true);
  }
  setStage("outline");
  const node = selectedTreeNode();
  const fromChapter = node ? (node.chapter_id || node.id) : null;
  const existing = $("outline-editor").value;
  await runGeneration(
    `/api/books/${encodeURIComponent(state.book.name)}/generate/outline-next`,
    {
      idea: $("idea-editor").value,
      outline: existing,
      from_chapter: fromChapter,
      style_ids: Array.from(state.selected),
      temperature: Number($("param-temp").value),
      enable_thinking: $("param-thinking").checked,
      llm_url: $("param-url").value.trim(),
    },
    $("outline-editor"),
    async (p) => {
      if (p.outline) $("outline-editor").value = p.outline;
      updateCounts();
      toast(`已追加下一章大纲，共 ${p.hanzi || hanzi($("outline-editor").value)} 字，可先审看再继续`);
      await reloadBookMeta();
      await loadTimeline();
    },
    { targetChars: 800, keepPrefix: !!existing.trim(), label: "正在写下一章大纲…" }
  );
}

async function loadSynopsis() {
  const box = $("path-synopsis");
  if (!box) return;
  if (!state.book || !state.chapterId) {
    box.value = "";
    return;
  }
  try {
    const data = await api(`/api/books/${encodeURIComponent(state.book.name)}/synopsis?chapter=${encodeURIComponent(state.chapterId)}`);
    box.value = data.text || "";
  } catch (err) {
    box.value = "";
  }
}

async function maybeRefreshSynopsis(chapterId, chapterText) {
  const id = chapterId || state.chapterId;
  if (!state.book || !id) return;
  if (state.generating) return;
  const editor = $("path-synopsis");
  const text = chapterText != null ? chapterText : (sameChapter(state.chapterId, id) ? $("chapter-editor").value : undefined);
  try {
    const res = await fetch(`/api/books/${encodeURIComponent(state.book.name)}/synopsis/${encodeURIComponent(id)}/refresh`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(text != null ? { text } : {}),
    });
    if (res.status === 200 && res.headers.get("content-type") && res.headers.get("content-type").includes("application/json")) {
      const data = await res.json();
      if (data.skipped) return;
    }
    let updated = "";
    await readSSE(res, {
      done: (p) => { updated = p.text || ""; },
      error: () => {},
      cancelled: () => {},
    });
    if (updated && editor && sameChapter(state.chapterId, id)) editor.value = updated;
  } catch (_) {
    // Keep the existing summary if the model is busy or the update fails.
  }
}

function hideConsistency() {
  const panel = $("consistency-panel");
  if (panel) {
    panel.classList.add("hidden");
    panel.innerHTML = "";
  }
}

async function checkConsistency() {
  if (!state.book) return toast("请先选择或新建一本书", true);
  if (!state.chapterId) return toast("请先选择要检查的章节", true);
  const current = $("chapter-editor").value.trim();
  if (!current) return toast("请先写下或生成本章正文，再检查一致性", true);
  await flushSaves();
  await saveWorldNow();
  hideConsistency();
  let result = null;
  await runGeneration(
    `/api/books/${encodeURIComponent(state.book.name)}/generate/consistency`,
    {
      chapter: state.chapterId,
      text: $("chapter-editor").value,
      temperature: Number($("param-temp").value),
      enable_thinking: $("param-thinking").checked,
      llm_url: $("param-url").value.trim(),
    },
    null,
    async (p) => { result = p; },
    { targetChars: 800, label: "正在检查一致性…" }
  );
  if (result) renderConsistency(result);
}

function renderConsistency(data) {
  const panel = $("consistency-panel");
  if (!panel) return;
  const conflicts = data.conflicts || [];
  const patches = data.proposed_patches || [];
  panel.classList.remove("hidden");
  panel.innerHTML = "";
  const title = document.createElement("h3");
  title.textContent = (conflicts.length || patches.length) ? "一致性检查" : "未发现明显冲突";
  panel.appendChild(title);
  if (conflicts.length) {
    const lead = document.createElement("p");
    lead.className = "hint";
    lead.textContent = `发现 ${conflicts.length} 处可能冲突。不会自动改正文。`;
    panel.appendChild(lead);
    const list = document.createElement("ol");
    conflicts.forEach((item) => {
      const li = document.createElement("li");
      const who = item.related_character ? `（${item.related_character}）` : "";
      li.textContent = `${item.text || ""}${who}`;
      list.appendChild(li);
    });
    panel.appendChild(list);
    const use = document.createElement("button");
    use.type = "button";
    use.className = "btn";
    use.textContent = "写入改进意见";
    use.addEventListener("click", () => {
      const notes = conflicts.map((item, i) => `${i + 1}. ${item.text || ""}`).join("\n");
      const box = $("chapter-revise");
      box.value = [box.value.trim(), notes].filter(Boolean).join("\n");
      setExtrasOpen("chapter-extras", true);
      toast("已写入改进意见，可用「按改进意见修改当前正文」");
    });
    panel.appendChild(use);
  }
  if (patches.length) {
    const lead = document.createElement("p");
    lead.className = "hint";
    lead.textContent = "建议更新人物状态。只有你确认后才会写入卡片。";
    panel.appendChild(lead);
    patches.forEach((item, index) => {
      const row = document.createElement("label");
      row.className = "consistency-item";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = true;
      cb.dataset.index = String(index);
      const span = document.createElement("span");
      span.textContent = `${item.character_name || item.character_id}：${item.text}`;
      row.appendChild(cb);
      row.appendChild(span);
      panel.appendChild(row);
    });
    const actions = document.createElement("div");
    actions.className = "row";
    const ok = document.createElement("button");
    ok.type = "button";
    ok.className = "btn primary";
    ok.textContent = "确认写入选中补丁";
    ok.addEventListener("click", () => confirmConsistencyPatches(panel, patches));
    const no = document.createElement("button");
    no.type = "button";
    no.className = "btn";
    no.textContent = "全部放弃";
    no.addEventListener("click", () => {
      hideConsistency();
      toast("已放弃建议补丁，人物卡未改动");
    });
    actions.appendChild(ok);
    actions.appendChild(no);
    panel.appendChild(actions);
  }
}

async function confirmConsistencyPatches(panel, patches) {
  const chosen = [];
  panel.querySelectorAll("input[type=checkbox]").forEach((cb) => {
    if (cb.checked) {
      const item = patches[Number(cb.dataset.index)];
      if (item) chosen.push(item);
    }
  });
  if (!chosen.length) {
    hideConsistency();
    toast("未选择补丁，人物卡未改动");
    return;
  }
  try {
    await api(`/api/books/${encodeURIComponent(state.book.name)}/characters/patches/confirm`, {
      method: "POST",
      body: JSON.stringify({ patches: chosen }),
    });
    await loadCharacters();
    hideConsistency();
    toast(`已将 ${chosen.length} 条补丁写入人物卡`);
  } catch (err) {
    toast(err.message, true);
  }
}



async function loadIllustrations() {
  const list = $("illust-list");
  if (!list) return;
  if (!state.book || !state.chapterId) {
    state.illustrations = [];
    list.innerHTML = "";
    const empty = document.createElement("li");
    empty.className = "illust-empty";
    empty.textContent = "选择一章后，这里会显示本章插图。";
    list.appendChild(empty);
    return;
  }
  try {
    const data = await api(`/api/illustrations/${encodeURIComponent(state.book.name)}?chapter=${encodeURIComponent(state.chapterId)}`);
    state.illustrations = data.illustrations || [];
  } catch (err) {
    state.illustrations = [];
  }
  renderIllustrations();
}

function renderIllustrations() {
  const list = $("illust-list");
  if (!list) return;
  list.innerHTML = "";
  if (!state.illustrations.length) {
    const empty = document.createElement("li");
    empty.className = "illust-empty";
    empty.textContent = state.chapterId ? "这一章还没有插图。" : "选择一章后，这里会显示本章插图。";
    list.appendChild(empty);
    return;
  }
  const bookName = state.book && state.book.name;
  state.illustrations.forEach((item) => {
    const li = document.createElement("li");
    li.className = "illust-card";
    const img = document.createElement("img");
    img.alt = "本章插图";
    img.src = `/api/illustrations/${encodeURIComponent(bookName)}/${encodeURIComponent(item.id)}/image?t=${encodeURIComponent(item.created_at || item.id)}`;
    img.addEventListener("click", () => openIllustLightbox(item, img.src));
    const prompt = document.createElement("p");
    prompt.className = "illust-prompt-preview";
    prompt.textContent = item.prompt || "";
    const meta = document.createElement("p");
    meta.className = "meta";
    meta.textContent = item.inserted ? "已插入正文" : "尚未插入正文";
    const row = document.createElement("div");
    row.className = "row";
    const insertBtn = document.createElement("button");
    insertBtn.type = "button";
    insertBtn.className = "btn small";
    insertBtn.textContent = item.inserted ? "再次插入" : "插入正文";
    insertBtn.addEventListener("click", () => insertIllustration(item.id));
    const dropBtn = document.createElement("button");
    dropBtn.type = "button";
    dropBtn.className = "btn small";
    dropBtn.textContent = "丢掉";
    dropBtn.addEventListener("click", () => discardIllustration(item.id));
    const saveBtn = document.createElement("button");
    saveBtn.type = "button";
    saveBtn.className = "btn small";
    saveBtn.textContent = "保存";
    saveBtn.addEventListener("click", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      saveIllustration(item);
    });
    row.appendChild(insertBtn);
    row.appendChild(dropBtn);
    row.appendChild(saveBtn);
    li.appendChild(img);
    if (item.prompt) li.appendChild(prompt);
    li.appendChild(meta);
    li.appendChild(row);
    list.appendChild(li);
  });
}


function illustrationFilename(item) {
  if (item && item.filename) return String(item.filename);
  return `${(item && item.id) || "illustration"}.png`;
}

function illustrationImageUrl(item) {
  const bookName = state.book && state.book.name;
  if (!bookName || !item || !item.id) return "";
  return `/api/illustrations/${encodeURIComponent(bookName)}/${encodeURIComponent(item.id)}/image?t=${encodeURIComponent(item.created_at || item.id)}`;
}

function openIllustLightbox(item, src) {
  const box = $("illust-lightbox");
  const img = $("illust-lightbox-image");
  if (!box || !img) return;
  state.lightboxItem = item || null;
  img.src = src || illustrationImageUrl(item);
  img.alt = "插图预览";
  box.classList.remove("hidden");
  document.body.classList.add("lightbox-open");
  const closeBtn = $("illust-lightbox-close");
  if (closeBtn && closeBtn.focus) closeBtn.focus();
}

function closeIllustLightbox() {
  const box = $("illust-lightbox");
  const img = $("illust-lightbox-image");
  if (box) box.classList.add("hidden");
  if (img) img.removeAttribute("src");
  state.lightboxItem = null;
  document.body.classList.remove("lightbox-open");
}

async function saveIllustration(item) {
  const target = item || state.lightboxItem;
  if (!target) return toast("没有可保存的插图", true);
  const url = illustrationImageUrl(target);
  if (!url) return toast("请先选择或新建一本书", true);
  try {
    const res = await fetch(url);
    if (!res.ok) throw new Error("无法下载该插图");
    const blob = await res.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = illustrationFilename(target);
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  } catch (err) {
    toast((err && err.message) || "保存插图失败", true);
  }
}

function parseDownloadName(header, fallback) {
  if (!header) return fallback;
  const star = /filename\*=(?:UTF-8'')?([^;]+)/i.exec(header);
  if (star && star[1]) {
    try { return decodeURIComponent(star[1].trim().replace(/^"+|"+$/g, "")); } catch (err) { /* use fallback below */ }
  }
  const plain = /filename=(?:"([^"]+)"|([^;]+))/i.exec(header);
  if (plain) return (plain[1] || plain[2] || "").trim() || fallback;
  return fallback;
}

async function exportBookPdf() {
  if (!state.book) return toast("请先选择或新建一本书", true);
  if (state.exportingPdf) return;
  try {
    state.exportingPdf = true;
    if ($("btn-export-pdf")) $("btn-export-pdf").disabled = true;
    await flushSaves();
    toast("正在导出全书 PDF…");
    const params = new URLSearchParams();
    if (state.chapterId) params.set("chapter", state.chapterId);
    const qs = params.toString();
    const url = `/api/books/${encodeURIComponent(state.book.name)}/export/pdf` + (qs ? `?${qs}` : "");
    const res = await fetch(url);
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(apiErrorMessage(data, res.status));
    }
    const blob = await res.blob();
    const filename = parseDownloadName(res.headers.get("Content-Disposition"), "全书.pdf");
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    toast("全书 PDF 已保存到该书文件夹，并已开始下载");
  } catch (err) {
    toast((err && err.message) || "导出 PDF 失败", true);
  } finally {
    state.exportingPdf = false;
    if ($("btn-export-pdf")) $("btn-export-pdf").disabled = false;
  }
}

function currentChapterSelection() {
  const editor = $("chapter-editor");
  if (!editor) return "";
  const start = editor.selectionStart || 0;
  const end = editor.selectionEnd || 0;
  if (start === end) return "";
  return editor.value.slice(start, end);
}

async function describeIllustration(scope) {
  if (!state.book) return toast("请先选择或新建一本书", true);
  if (!state.chapterId) return toast("请先选择章节", true);
  if (state.generating) return toast("请先等待当前生成结束", true);
  if (state.illustSession) {
    return toast("请先点「出图结束」，再让写作模型写新的画面说明。若只是改说明再出图，可以直接点「确认出图」。", true);
  }
  const editor = $("chapter-editor");
  let text = "";
  if (scope === "passage") {
    text = currentChapterSelection();
    if (!text.trim()) return toast("请先在正文里选中一段，再为这段配图", true);
  } else {
    text = editor ? editor.value : "";
    if (!stripIllustrationMarkers(text).trim()) return toast("本章还没有正文，无法配图", true);
  }
  try {
    revealIllustPane();
    setIllustStatus("正在写画面说明…");
    setGenerating(true, "正在写画面说明…");
    const data = await api(`/api/illustrations/${encodeURIComponent(state.book.name)}/describe`, {
      method: "POST",
      body: JSON.stringify({
        chapter: state.chapterId,
        text,
        llm_url: $("param-url").value.trim(),
        temperature: Number($("param-temp").value),
      }),
    });
    state.illustSourceExcerpt = data.source_excerpt || text;
    $("illust-prompt").value = data.prompt || "";
    $("illust-prompt-box").classList.remove("hidden");
    if ($("illust-prompt-box") && $("illust-prompt-box").scrollIntoView) {
      $("illust-prompt-box").scrollIntoView({ block: "nearest" });
    }
    setIllustStatus("画面说明已写好，可先改一改，再点「确认出图」。");
    toast("画面说明已写好，可先改一改再出图");
  } catch (err) {
    setIllustStatus(err.message || "写画面说明失败", true);
    toast(err.message, true);
  } finally {
    setGenerating(false);
  }
}

async function generateIllustration() {
  if (!state.book) return toast("请先选择或新建一本书", true);
  if (!state.chapterId) return toast("请先选择章节", true);
  if (state.generating) return toast("正在生成正文，请等结束后再出图", true);
  const prompt = ($("illust-prompt") && $("illust-prompt").value.trim()) || "";
  if (!prompt) return toast("请先确认画面说明，再出图", true);
  try {
    revealIllustPane();
    setIllustStatus(state.illustSession ? "正在出图…" : "正在切换到出图模型…");
    setGenerating(true, state.illustSession ? "正在出图…" : "正在切换到出图模型…");
    startIllustProgressPolling();
    const data = await api(`/api/illustrations/${encodeURIComponent(state.book.name)}/generate`, {
      method: "POST",
      body: JSON.stringify({
        chapter: state.chapterId,
        prompt,
        source_excerpt: state.illustSourceExcerpt || "",
        llm_url: $("param-url").value.trim(),
      }),
    });
    setIllustSession(true);
    $("illust-prompt-box").classList.remove("hidden");
    await loadIllustrations();
    setIllustStatus("出图完成，可先预览再决定是否插入正文。");
    toast("出图完成，可先预览再决定是否插入正文");
    return data;
  } catch (err) {
    setIllustStatus(err.message || "出图失败", true);
    toast(err.message, true);
  } finally {
    stopIllustProgressPolling();
    applyIllustProgress(null);
    setGenerating(false);
    refreshHealth();
  }
}

async function finishIllustrationSession() {
  if (!state.book) return;
  if (state.generating) return toast("正在出图，请等这张完成后再结束", true);
  if (!state.illustSession) return toast("当前没有进行中的出图");
  try {
    revealIllustPane();
    setGenerating(true, "正在重新加载写作模型…");
    startIllustProgressPolling();
    await api(`/api/illustrations/${encodeURIComponent(state.book.name)}/finish-session`, {
      method: "POST",
      body: JSON.stringify({ llm_url: $("param-url").value.trim() }),
    });
    setIllustSession(false);
    toast("出图已结束，写作模型已重新加载");
  } catch (err) {
    toast(err.message, true);
  } finally {
    stopIllustProgressPolling();
    applyIllustProgress(null);
    setGenerating(false);
    refreshHealth();
  }
}

async function insertIllustration(id) {
  if (!state.book || !state.chapterId) return toast("请先选择章节", true);
  const editor = $("chapter-editor");
  try {
    const data = await api(`/api/illustrations/${encodeURIComponent(state.book.name)}/${encodeURIComponent(id)}/insert-marker`, {
      method: "POST",
    });
    const marker = data.marker;
    if (editor && marker) {
      const pos = typeof editor.selectionStart === "number" ? editor.selectionStart : editor.value.length;
      editor.value = editor.value.slice(0, pos) + marker + editor.value.slice(pos);
      editor.selectionStart = editor.selectionEnd = pos + marker.length;
      updateCounts();
      const chapterId = state.chapterId;
      const bookName = state.book.name;
      scheduleSave("chapter", async () => {
        await api(`/api/books/${encodeURIComponent(bookName)}/chapters/${chapterId}`, {
          method: "PUT",
          body: JSON.stringify({ text: editor.value }),
        });
      });
    }
    toast("已在光标处插入插图标记");
    await loadIllustrations();
  } catch (err) {
    toast(err.message, true);
  }
}

async function discardIllustration(id) {
  if (!state.book) return;
  if (!confirm("丢掉这张插图吗？如果正文里有对应标记，也会一并去掉。")) return;
  try {
    const data = await api(`/api/illustrations/${encodeURIComponent(state.book.name)}/${encodeURIComponent(id)}`, {
      method: "DELETE",
    });
    const editor = $("chapter-editor");
    if (editor && data && data.chapter_id && sameChapter(state.chapterId, data.chapter_id) && data.text != null) {
      editor.value = data.text;
      updateCounts();
    }
    toast("已丢掉这张插图，并去掉对应标记");
    await loadIllustrations();
  } catch (err) {
    toast(err.message, true);
  }
}

function bind() {
  $("btn-open-left").addEventListener("click", () => toggleDrawer("left"));
  $("btn-open-right").addEventListener("click", () => toggleDrawer("right"));
  $("btn-close-left").addEventListener("click", closeDrawers);
  $("btn-close-right").addEventListener("click", closeDrawers);
  $("drawer-backdrop").addEventListener("click", closeDrawers);
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") {
      if ($("illust-lightbox") && !$("illust-lightbox").classList.contains("hidden")) {
        closeIllustLightbox();
        return;
      }
      if ($("timeline-dialog") && !$("timeline-dialog").classList.contains("hidden")) {
        closeTimelineDialog();
        return;
      }
      closeDrawers();
    }
  });
  $("btn-toggle-outline-extras").addEventListener("click", () => toggleExtras("outline-extras"));
  $("btn-toggle-chapter-extras").addEventListener("click", () => toggleExtras("chapter-extras"));
  if ($("btn-toggle-synopsis")) $("btn-toggle-synopsis").addEventListener("click", () => toggleExtras("synopsis-extras"));
  applyExtrasState();
  applyLeftSections();
  document.querySelectorAll("[data-toggle-section]").forEach((btn) => {
    btn.addEventListener("click", () => toggleLeftSection(btn.dataset.toggleSection));
  });
  if ($("btn-refresh-timeline")) $("btn-refresh-timeline").addEventListener("click", () => loadTimeline());
  if ($("timeline-dialog-close")) $("timeline-dialog-close").addEventListener("click", closeTimelineDialog);
  if ($("timeline-dialog-cancel")) $("timeline-dialog-cancel").addEventListener("click", closeTimelineDialog);
  if ($("timeline-dialog-restore")) $("timeline-dialog-restore").addEventListener("click", restoreTimelineEntry);
  if ($("btn-save-outline")) $("btn-save-outline").addEventListener("click", saveCurrentOutline);

  $("create-book-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const name = $("new-book-name").value.trim();
    if (!name) return;
    try {
      await api("/api/books", { method: "POST", body: JSON.stringify({ name }) });
      $("new-book-name").value = "";
      await loadBooks(name);
      await selectBook(name);
    } catch (err) { toast(err.message, true); }
  });

  document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => setStage(tab.dataset.stage)));
  $("btn-gen-outline").addEventListener("click", generateOutline);
  $("btn-regen-outline").addEventListener("click", generateOutline);
  $("btn-fill-outline").addEventListener("click", generateThisOutlineChapter);
  $("btn-next-outline").addEventListener("click", generateNextOutlineChapter);
  $("btn-draft-world").addEventListener("click", generateWorldDraft);
  if ($("btn-gen-world")) $("btn-gen-world").addEventListener("click", generateWorldDraft);
  if ($("btn-gen-characters")) $("btn-gen-characters").addEventListener("click", generateCharacters);
  $("btn-revise-outline").addEventListener("click", reviseOutline);
  $("btn-restore-outline").addEventListener("click", restorePreviousOutline);
  $("btn-confirm-outline").addEventListener("click", async () => {
    if (!$("outline-editor").value.trim()) return toast("请先生成或填写大纲", true);
    if (!state.book) return toast("请先选择或新建一本书", true);
    await flushSaves();
    try {
      const saved = await putOutlineSafely();
      if (saved.tree) applyTree(saved.tree);
      await reloadBookMeta();
    } catch (err) {
      return toast(err.message, true);
    }
    setStage("body");
  });
  $("btn-gen-chapter").addEventListener("click", () => generateChapter("generate"));
  $("btn-continue-chapter").addEventListener("click", () => generateChapter("continue"));
  $("btn-expand-chapter").addEventListener("click", expandChapter);
  $("btn-save-chapter").addEventListener("click", saveCurrentChapter);
  if ($("btn-export-pdf")) $("btn-export-pdf").addEventListener("click", exportBookPdf);
  if ($("illust-lightbox-close")) $("illust-lightbox-close").addEventListener("click", closeIllustLightbox);
  if ($("illust-lightbox-backdrop")) $("illust-lightbox-backdrop").addEventListener("click", closeIllustLightbox);
  if ($("illust-lightbox-save")) $("illust-lightbox-save").addEventListener("click", () => saveIllustration(state.lightboxItem));
  $("btn-check-consistency").addEventListener("click", checkConsistency);
  $("btn-restore-saved-chapter").addEventListener("click", restoreSavedChapter);
  $("btn-delete-chapter").addEventListener("click", deleteSelectedChapter);
  $("btn-revise-chapter").addEventListener("click", reviseChapter);
  $("btn-restore-chapter").addEventListener("click", restorePreviousChapter);
  $("btn-tree-open").addEventListener("click", openSelectedTreeChapter);
  $("btn-tree-branch").addEventListener("click", addBranchFromSelected);
  $("btn-tree-delete").addEventListener("click", deleteSelectedChapter);
  $("btn-list-branch").addEventListener("click", addBranchFromSelected);
  $("btn-collapse-left").addEventListener("click", () => setPaneCollapsed("left", true));
  $("btn-collapse-right").addEventListener("click", () => setPaneCollapsed("right", true));
  $("btn-expand-left").addEventListener("click", () => setPaneCollapsed("left", false));
  $("btn-expand-right").addEventListener("click", () => setPaneCollapsed("right", false));
  if ($("btn-collapse-illust")) $("btn-collapse-illust").addEventListener("click", () => setPaneCollapsed("illust", true));
  if ($("btn-expand-illust")) $("btn-expand-illust").addEventListener("click", () => setPaneCollapsed("illust", false));
  if ($("btn-open-illust")) $("btn-open-illust").addEventListener("click", () => toggleDrawer("illust"));
  if ($("btn-close-illust")) $("btn-close-illust").addEventListener("click", closeDrawers);
  if ($("btn-illust-passage")) $("btn-illust-passage").addEventListener("click", () => describeIllustration("passage"));
  if ($("btn-illust-chapter")) $("btn-illust-chapter").addEventListener("click", () => describeIllustration("chapter"));
  if ($("btn-illust-generate")) $("btn-illust-generate").addEventListener("click", generateIllustration);
  if ($("btn-illust-finish")) $("btn-illust-finish").addEventListener("click", finishIllustrationSession);
  window.addEventListener("resize", () => {
    applyPaneCollapse();
    requestAnimationFrame(() => renderPlotTrees());
  });
  $("btn-stop").addEventListener("click", stopGeneration);

  $("btn-clear-idea").addEventListener("click", () => clearEditor("idea-editor", "思路", true));
  $("btn-clear-world").addEventListener("click", () => clearEditor("world-editor", "世界观", true));
  $("btn-clear-outline").addEventListener("click", () => clearEditor("outline-editor", "大纲", true));
  $("btn-clear-chapter").addEventListener("click", () => clearEditor("chapter-editor", "本章正文", true));
  $("btn-clear-outline-revise").addEventListener("click", () => clearEditor("outline-revise", "改进思路", false));
  $("btn-clear-chapter-revise").addEventListener("click", () => clearEditor("chapter-revise", "改进意见", false));

  $("idea-editor").addEventListener("input", () => {
    updateCounts();
    if (!state.book) return;
    scheduleSave("idea", () => api(`/api/books/${encodeURIComponent(state.book.name)}/idea`, {
      method: "PUT", body: JSON.stringify({ text: $("idea-editor").value }),
    }));
  });
  $("world-editor").addEventListener("input", () => {
    updateCounts();
    if (!state.book) return;
    scheduleSave("world", async () => {
      try {
        await putWorldSafely();
      } catch (err) { /* 世界观可选 */ }
    });
  });
  if ($("character-name")) {
    $("character-name").addEventListener("input", scheduleCharacterSave);
    $("character-basics").addEventListener("input", scheduleCharacterSave);
    if ($("character-appearance")) $("character-appearance").addEventListener("input", scheduleCharacterSave);
  }
  $("btn-add-character").addEventListener("click", addCharacter);
  $("btn-delete-character").addEventListener("click", deleteSelectedCharacter);
  $("btn-add-patch").addEventListener("click", addPatch);
  $("path-synopsis").addEventListener("input", () => {
    if (!state.book || !state.chapterId) return;
    const chapterId = state.chapterId;
    const bookName = state.book.name;
    const text = $("path-synopsis").value;
    scheduleSave("synopsis", async () => {
      try {
        await api(`/api/books/${encodeURIComponent(bookName)}/synopsis/${chapterId}`, {
          method: "PUT", body: JSON.stringify({ text }),
        });
      } catch (err) { /* 路径摘要可选 */ }
    });
  });
  ["click", "keyup", "mouseup"].forEach((ev) => {
    $("outline-editor").addEventListener(ev, () => syncTreeFromOutlineCursor());
  });
  $("outline-editor").addEventListener("input", () => {
    updateCounts();
    syncTreeFromOutlineCursor();
    if (!state.book) return;
    scheduleSave("outline", async () => {
      const saved = await putOutlineSafely();
      if (saved.tree) applyTree(saved.tree);
      renderChapters((state.bookData && state.bookData.chapters) || []);
    });
  });
  $("chapter-editor").addEventListener("input", () => {
    updateCounts();
    if (!state.book || !state.chapterId) return;
    const chapterId = state.chapterId;
    const bookName = state.book.name;
    const text = $("chapter-editor").value;
    scheduleSave("chapter", async () => {
      await api(`/api/books/${encodeURIComponent(bookName)}/chapters/${chapterId}`, {
        method: "PUT", body: JSON.stringify({ text }),
      });
      await reloadBookMeta();
    });
  });

  ["param-temp", "param-chars", "param-thinking", "param-url"].forEach((id) => {
    $(id).addEventListener("input", () => {
      $("param-temp-val").textContent = Number($("param-temp").value).toFixed(2);
      $("chapter-target").textContent = $("param-chars").value;
      scheduleSave("settings", saveSettings);
    });
    $(id).addEventListener("change", () => {
      if (id === "param-url") refreshHealth();
      scheduleSave("settings", saveSettings, 200);
    });
  });

  $("btn-refresh-styles").addEventListener("click", refreshStyles);

  $("btn-paste-style").addEventListener("click", async () => {
    if (!state.book) return toast("请先选择作品", true);
    try {
      await api(`/api/books/${encodeURIComponent(state.book.name)}/styles`, {
        method: "POST",
        body: JSON.stringify({ text: $("style-paste").value }),
      });
      $("style-paste").value = "";
      $("style-status").textContent = "已保存到本作品";
      $("style-status").classList.remove("error");
      await loadStyles();
    } catch (err) {
      $("style-status").textContent = err.message;
      $("style-status").classList.add("error");
    }
  });

  $("style-upload").addEventListener("change", async (ev) => {
    if (!state.book) return toast("请先选择作品", true);
    const file = ev.target.files && ev.target.files[0];
    ev.target.value = "";
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    try {
      const res = await fetch(`/api/books/${encodeURIComponent(state.book.name)}/styles/upload`, { method: "POST", body: form });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || "上传失败");
      state.selected.add(data.id);
      $("style-status").textContent = `已上传 ${data.name}`;
      $("style-status").classList.remove("error");
      await loadStyles();
      scheduleSave("settings", saveSettings, 200);
    } catch (err) {
      $("style-status").textContent = err.message;
      $("style-status").classList.add("error");
    }
  });


  $("btn-fetch-style").addEventListener("click", async () => {
    if (!state.book) return toast("请先选择作品", true);
    const url = $("style-url").value.trim();
    if (!url) return toast("请粘贴网页链接", true);
    $("style-status").textContent = "正在抓取网页…";
    $("style-status").classList.remove("error");
    try {
      const data = await api(`/api/books/${encodeURIComponent(state.book.name)}/styles/from-url`, {
        method: "POST",
        body: JSON.stringify({ url }),
      });
      $("style-url").value = "";
      state.selected.add(data.id);
      $("style-status").textContent = `已抓取 ${data.name}`;
      $("style-status").classList.remove("error");
      await loadStyles();
      scheduleSave("settings", saveSettings, 200);
    } catch (err) {
      $("style-status").textContent = err.message;
      $("style-status").classList.add("error");
    }
  });

  $("btn-add-chapter").addEventListener("click", async () => {
    if (!state.book) return toast("请先选择作品", true);
    try {
      const added = await api(`/api/books/${encodeURIComponent(state.book.name)}/chapters`, {
        method: "POST", body: JSON.stringify({ title: "" }),
      });
      $("outline-editor").value = added.outline || $("outline-editor").value;
      if (added.tree) applyTree(added.tree);
      else await reloadBookMeta();
      updateCounts();
      renderChapters(added.chapters || []);
      await openChapter(added.id);
      setStage("outline");
      toast("已在大纲末尾追加一章，可先改标题再写正文");
    } catch (err) { toast(err.message, true); }
  });
}

async function boot() {
  bind();
  loadPaneCollapse();
  applyPaneCollapse();
  if ($("outline-tree") && typeof ResizeObserver !== "undefined") {
    let lastTreeWidth = 0;
    const ro = new ResizeObserver(() => {
      if (treeRenderLock) return;
      const el = $("outline-tree");
      if (!el) return;
      const w = el.clientWidth;
      if (w === 0 || Math.abs(w - lastTreeWidth) < 2) return;
      lastTreeWidth = w;
      renderPlotTrees();
    });
    ro.observe($("outline-tree"));
  }
  await refreshHealth();
  setInterval(refreshHealth, 15000);
  try {
    await loadBooks();
    const first = document.querySelector(".book-list li[data-name]");
    if (first) await selectBook(first.dataset.name);
  } catch (err) {
    toast(err.message, true);
  }
}

boot();
