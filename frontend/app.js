const $ = (selector, root = document) => root.querySelector(selector);
const el = (tag, className = "", text = "") => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
};
const markdownRenderer = window.markdownit?.({ html: false, linkify: true, breaks: true });
const markdownJobs = new WeakMap();
const state = {
  conversationId: null,
  conversations: [],
  snapshot: null,
  source: null,
  cursor: 0,
  clockOffsetMs: 0,
  refreshing: false,
  refreshingConversationId: null,
  queuedEvents: [],
  needsRefresh: false,
  drafts: new Map(),
  attachmentSelections: new Map(),
  selectedAttachments: new Set(),
  collapsedFolders: new Set(),
  selectedArtifactId: null,
  drawerOpen: false,
  busy: false,
  pendingSend: null,
  outlineDrafts: new Map(),
  editingOutlines: new Set(),
  expandedDeliveries: new Set(),
  contextPinned: false,
  previewRequest: 0,
  previewOpener: null,
  refreshOwner: null,
  viewVersion: 0,
  statusSyncError: null,
  cancelSyncTurnId: null,
  cancelSyncTimer: null,
  cancelSyncPolls: 0,
};

function renderMarkdown(target, source, evaluationId = null) {
  const body = String(source ?? "");
  if (!markdownRenderer || !window.DOMPurify) {
    target.textContent = body;
    return;
  }
  target.innerHTML = window.DOMPurify.sanitize(markdownRenderer.render(body), {
    USE_PROFILES: { html: true },
  });
  for (const link of target.querySelectorAll("a[href]")) {
    let filename = "";
    try { filename = decodeURIComponent(link.getAttribute("href").split(/[?#]/)[0]); } catch { /* Invalid URL. */ }
    const alias = state.snapshot?.artifact_aliases?.find(item => item.evaluation_id === evaluationId && item.filename === filename);
    const artifact = evaluationId && state.snapshot?.artifacts.find(item => item.evaluation_id === evaluationId &&
      (item.filename === filename || item.artifact_id === alias?.artifact_id));
    if (artifact && !/[\\/]/.test(filename)) {
      link.href = `#artifact-${artifact.artifact_id}`;
      link.addEventListener("click", event => { event.preventDefault(); void openPreview(artifact.artifact_id); });
      continue;
    }
    link.target = "_blank";
    link.rel = "noopener noreferrer";
  }
}

function queueMarkdownRender(target, source) {
  const job = markdownJobs.get(target) || { scheduled: false, source: "" };
  job.source = source;
  markdownJobs.set(target, job);
  if (job.scheduled) return;
  job.scheduled = true;
  requestAnimationFrame(() => {
    job.scheduled = false;
    if (!target.isConnected) return;
    const scroll = $("#chatScroll");
    const atBottom = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 120;
    renderMarkdown(target, job.source);
    if (atBottom) scroll.scrollTop = scroll.scrollHeight;
  });
}

function parseCsv(source) {
  const rows = [];
  let row = [];
  let cell = "";
  let quoted = false;
  const text = String(source ?? "").replace(/^\uFEFF/, "");
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    if (quoted) {
      if (char === '"' && text[index + 1] === '"') { cell += '"'; index += 1; }
      else if (char === '"') quoted = false;
      else cell += char;
    } else if (char === '"' && !cell) quoted = true;
    else if (char === ",") { row.push(cell); cell = ""; }
    else if (char === "\n" || char === "\r") {
      row.push(cell);
      rows.push(row);
      row = [];
      cell = "";
      if (char === "\r" && text[index + 1] === "\n") index += 1;
    } else cell += char;
  }
  if (row.length || cell) { row.push(cell); rows.push(row); }
  return rows;
}

function renderCsvPreview(source) {
  const wrapper = el("div", "csv-preview");
  const rows = parseCsv(source);
  if (!rows.length) return el("p", "preview-empty", "这个 CSV 文件没有内容。");
  const header = rows[0];
  const totalColumns = rows.reduce((max, row) => Math.max(max, row.length), 0);
  const columnCount = Math.min(totalColumns, 30);
  const visible = rows.slice(1, 101);
  const note = el("div", "csv-summary", `共 ${Math.max(0, rows.length - 1)} 行数据 · ${totalColumns} 列`);
  if (rows.length > 101 || totalColumns > 30) note.textContent += " · 仅预览前 100 行、30 列";
  const scroll = el("div", "csv-scroll");
  const table = el("table", "csv-table");
  const head = el("thead");
  const heading = el("tr");
  for (let index = 0; index < columnCount; index += 1) {
    const cell = el("th", "", header[index] || `第 ${index + 1} 列`);
    cell.scope = "col";
    heading.append(cell);
  }
  head.append(heading);
  const body = el("tbody");
  for (const values of visible) {
    const line = el("tr");
    for (let index = 0; index < columnCount; index += 1) line.append(el("td", "", values[index] || ""));
    body.append(line);
  }
  table.append(head, body);
  scroll.append(table);
  wrapper.append(note, scroll);
  return wrapper;
}

async function api(path, options = {}) {
  const response = await fetch(path, { signal: AbortSignal.timeout(15000), ...options });
  if (!response.ok) {
    let detail = `请求失败（${response.status}）`;
    try {
      const body = await response.json();
      detail = typeof body.detail === "string" ? body.detail : detail;
    } catch { /* Non-JSON server error. */ }
    throw new Error(detail);
  }
  const type = response.headers.get("content-type") || "";
  return type.includes("application/json") ? response.json() : response.text();
}

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.hidden = false;
  clearTimeout(toast.timeout);
  toast.timeout = setTimeout(() => { node.hidden = true; }, 4500);
}

function formatElapsed(milliseconds) {
  const total = Math.max(0, Math.floor(milliseconds / 1000));
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return minutes ? `${minutes}分钟${seconds}秒` : `${seconds}秒`;
}

function elapsedFor(turn) {
  if (!turn) return 0;
  if (turn.duration_ms !== null && turn.duration_ms !== undefined) return turn.duration_ms;
  return Math.max(0, Date.now() + state.clockOffsetMs - Date.parse(turn.started_at));
}

function activeTurn() {
  return state.snapshot?.turns.find(turn => ["running", "cancel_requested"].includes(turn.status));
}

function processingText(turn) {
  if (state.statusSyncError) return "状态同步失败，请重试";
  if (turn.status === "cancel_requested") return "正在终止任务";
  const compression = state.snapshot?.context?.compression;
  if (compression?.turn_id === turn.turn_id && compression.status === "started") return "正在整理对话上下文";
  if (state.snapshot?.progress?.turn_id === turn.turn_id) return state.snapshot.progress.message;
  const phases = ["稍等一下", "努力规划中", "马上出炉"];
  return phases[Math.floor(elapsedFor(turn) / 3500) % phases.length];
}

function renderTimers() {
  for (const label of document.querySelectorAll("[data-elapsed-turn]")) {
    const turn = state.snapshot?.turns.find(item => item.turn_id === label.dataset.elapsedTurn);
    if (turn) label.textContent = `已处理 ${formatElapsed(elapsedFor(turn))}`;
  }
  for (const label of document.querySelectorAll("[data-processing-turn]")) {
    const turn = state.snapshot?.turns.find(item => item.turn_id === label.dataset.processingTurn);
    if (turn) label.textContent = processingText(turn);
  }
}

function renderHistory() {
  const history = $("#history");
  history.replaceChildren();
  for (const conversation of state.conversations) {
    const button = el("button", conversation.conversation_id === state.conversationId ? "active" : "");
    button.type = "button";
    button.append(el("span", "name", conversation.title || "新对话"));
    button.append(el("span", "time", new Date(conversation.updated_at).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })));
    button.addEventListener("click", () => switchConversation(conversation.conversation_id));
    history.append(button);
  }
}

function makeElapsed(turn, processing = false) {
  const line = el("div", `elapsed${processing ? " is-live" : ""}`);
  const duration = el("span", "elapsed-value", turn ? `已处理 ${formatElapsed(elapsedFor(turn))}` : "已处理");
  if (turn) duration.dataset.elapsedTurn = turn.turn_id;
  line.append(duration);
  if (processing && turn) {
    const activity = el("span", "processing-activity");
    activity.setAttribute("aria-label", "智能体正在处理");
    activity.append(el("span", "processing-dot"));
    const label = el("span", "processing-label", processingText(turn));
    label.dataset.processingTurn = turn.turn_id;
    label.setAttribute("aria-hidden", "true");
    activity.append(label);
    line.append(activity);
  }
  return line;
}

function renderMessage(message) {
  const row = el("div", `message ${message.role === "user" ? "user" : "assistant"}`);
  row.dataset.messageId = message.message_id;
  const body = el("div", "message-body");
  if (message.role === "assistant") {
    row.append(el("span", "agent-avatar", "✦"));
    const turn = state.snapshot.turns.find(item => item.turn_id === message.turn_id);
    const lastInTurn = state.snapshot.messages.filter(item => item.role === "assistant" && item.turn_id === message.turn_id).at(-1);
    const processing = Boolean(turn && ["running", "cancel_requested"].includes(turn.status) && lastInTurn?.message_id === message.message_id);
    body.append(makeElapsed(turn, processing));
    body.append(el("div", "speaker", "评测智能体"));
  }
  if (message.role === "assistant") {
    const content = el("div", "text markdown-content");
    renderMarkdown(content, message.body);
    body.append(content);
  } else body.append(el("p", "text", message.body));
  if (message.role === "user") {
    for (const attachment of state.snapshot.attachments.filter(item => item.message_id === message.message_id)) {
      const chip = el("div", "attachment");
      chip.append(el("span", "file-icon", attachment.filename.toLowerCase().endsWith(".docx") ? "W" : "MD"));
      chip.append(el("span", "filename", attachment.filename));
      body.append(chip);
    }
  } else if (message.artifact_ids?.length) {
    const artifacts = message.artifact_ids.map(id => state.snapshot.artifacts.find(item => item.artifact_id === id)).filter(Boolean);
    if (artifacts.length) body.append(renderDeliverables(artifacts, message.message_id));
  }
  row.append(body);
  return row;
}

function renderDeliverables(artifacts, messageId) {
  const card = el("section", "deliverables");
  const bridge = el("div", "delivery-bridge");
  const copy = el("div", "delivery-copy");
  copy.append(el("strong", "", `本轮交付 ${artifacts.length} 份文件`),
    el("span", "", "文件已就绪，可在右侧预览或展开下方清单。"));
  const browse = el("button", "delivery-browse", "查看文件 →");
  browse.type = "button";
  browse.addEventListener("click", () => {
    for (const artifact of artifacts) state.collapsedFolders.delete(artifact.evaluation_id);
    renderFolders();
    showDrawer(true);
    const first = [...document.querySelectorAll(".artifact-row")]
      .find(row => row.dataset.artifactId === artifacts[0].artifact_id);
    first?.scrollIntoView({ block: "nearest" });
    first?.focus({ preventScroll: true });
  });
  bridge.append(el("span", "delivery-icon", "▤"), copy, browse);
  const details = el("details", "delivery-details");
  details.open = state.expandedDeliveries.has(messageId);
  const summary = el("summary", "", `本轮文件清单 · ${artifacts.length} 项`);
  details.append(summary);
  const list = el("div", "delivery-list");
  for (const artifact of artifacts) {
    const button = el("button", "deliverable-row");
    button.type = "button";
    button.dataset.artifactId = artifact.artifact_id;
    button.append(el("span", "file-icon", artifact.kind === "docx" ? "W" : artifact.kind === "csv" ? "CSV" : "MD"));
    const main = el("span", "deliverable-main");
    main.append(el("strong", "", artifact.filename), el("small", "", `第 ${artifact.content_revision} 版 · 可预览`));
    button.append(main, el("span", "preview-action", "预览 →"));
    button.addEventListener("click", () => openPreview(artifact.artifact_id));
    list.append(button);
  }
  details.append(list);
  details.addEventListener("toggle", () => {
    if (!details.isConnected) return;
    if (details.open) state.expandedDeliveries.add(messageId);
    else state.expandedDeliveries.delete(messageId);
  });
  card.append(bridge, details);
  return card;
}

function renderWelcome() {
  const welcome = el("section", "empty-state");
  welcome.append(el("div", "welcome-eyebrow", "✦  评测方案设计"),
    el("h2", "", "把产品需求，变成可执行的评测方案"),
    el("p", "welcome-description", "先讨论目标、范围和交付内容。确认大纲后，智能体再逐份生成文件。"));
  const upload = el("button", "welcome-upload");
  upload.type = "button";
  upload.id = "welcomeUpload";
  upload.disabled = state.busy;
  const copy = el("span", "welcome-upload-copy");
  copy.append(el("strong", "", "上传需求文档"), el("small", "", "支持 Markdown、Word（.docx），可同时添加多份资料"));
  upload.append(el("span", "welcome-upload-icon", "↥"), copy, el("span", "welcome-arrow", "→"));
  upload.addEventListener("click", () => $("#fileInput").click());
  welcome.append(upload, el("p", "welcome-label", "也可以从一句话开始"));
  const prompts = el("div", "welcome-prompts");
  for (const prompt of ["为产品问答助手设计一套评测方案", "先梳理评测目标和指标"]) {
    const button = el("button", "welcome-prompt");
    button.type = "button";
    button.disabled = state.busy;
    button.append(el("span", "", prompt), el("span", "welcome-arrow", "↗"));
    button.addEventListener("click", () => {
      const input = $("#messageInput");
      if (input.disabled) return;
      input.value = prompt;
      state.drafts.set(state.conversationId, prompt);
      input.focus();
    });
    prompts.append(button);
  }
  welcome.append(prompts, el("p", "welcome-footnote", "没有完整文档也可以开始，你可以边聊边补充。"));
  return welcome;
}

function renderOutline(outline) {
  const confirmed = outline.status === "confirmed";
  const card = el("div", `outline-card${confirmed ? " is-confirmed" : ""}`);
  card.dataset.outlineId = outline.outline_id;
  const head = el("div", "outline-head");
  const title = el("div");
  const evaluation = state.snapshot.evaluations.find(item => item.evaluation_id === outline.evaluation_id);
  title.append(el("strong", "", `${evaluation?.name || "评测方案"} · 大纲`),
    el("small", "", confirmed ? "已确认的生成依据，可随时查看" : "确认前可预览和修改整体大纲"));
  const actions = el("div", "outline-actions");
  if (confirmed) {
    actions.append(el("span", "small-label", "已确认"));
    head.append(title, actions);
    const preview = el("div", "outline-preview markdown-content");
    renderMarkdown(preview, outline.body);
    card.append(head, preview);
    return card;
  }
  const edit = el("button", "outline-edit", state.editingOutlines.has(outline.outline_id) ? "预览大纲" : "编辑大纲");
  edit.type = "button";
  actions.append(edit, el("span", "small-label", "待确认"));
  head.append(title, actions);
  const draft = state.outlineDrafts.get(outline.outline_id) ?? outline.body;
  const preview = el("div", "outline-preview markdown-content");
  renderMarkdown(preview, draft);
  preview.hidden = state.editingOutlines.has(outline.outline_id);
  const editor = el("textarea", "outline-textarea");
  editor.setAttribute("aria-label", "可编辑的评测方案大纲");
  editor.value = draft;
  editor.hidden = !state.editingOutlines.has(outline.outline_id);
  editor.disabled = Boolean(activeTurn()) || state.busy;
  editor.addEventListener("input", () => state.outlineDrafts.set(outline.outline_id, editor.value));
  edit.disabled = editor.disabled;
  edit.addEventListener("click", () => {
    const editing = !editor.hidden;
    if (editing) {
      state.outlineDrafts.set(outline.outline_id, editor.value);
      renderMarkdown(preview, editor.value);
      state.editingOutlines.delete(outline.outline_id);
    } else {
      state.editingOutlines.add(outline.outline_id);
    }
    editor.hidden = editing;
    preview.hidden = !editing;
    edit.textContent = editing ? "编辑大纲" : "预览大纲";
    if (!editing) editor.focus();
  });
  const foot = el("div", "outline-foot");
  const note = el("span", "", "修改内容会作为本次生成依据");
  const confirm = el("button", "primary", "确认并开始 →");
  confirm.type = "button";
  confirm.disabled = Boolean(activeTurn()) || state.busy;
  confirm.addEventListener("click", async () => {
    if (state.busy || activeTurn()) return;
    const conversationId = state.conversationId;
    state.busy = true;
    confirm.disabled = true;
    note.textContent = "正在确认大纲…";
    renderComposer();
    let failureMessage = null;
    try {
      let revision = outline.revision;
      const currentBody = state.outlineDrafts.get(outline.outline_id) ?? editor.value;
      if (currentBody !== outline.body) {
        const edited = await api(`/api/conversations/${conversationId}/outlines/${outline.outline_id}`, {
          method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ expected_revision: revision, body: currentBody }),
        });
        revision = edited.revision;
      }
      await api(`/api/conversations/${conversationId}/outlines/${outline.outline_id}/confirm`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ expected_revision: revision, client_key: crypto.randomUUID() }),
      });
      state.outlineDrafts.delete(outline.outline_id);
      state.editingOutlines.delete(outline.outline_id);
      if (state.conversationId === conversationId) await refreshSnapshot();
    } catch (error) {
      if (state.conversationId === conversationId) {
        failureMessage = error.message;
        toast(error.message);
      }
    } finally {
      state.busy = false;
      render();
      if (failureMessage && state.conversationId === conversationId) {
        const current = [...document.querySelectorAll(".outline-card")]
          .find(item => item.dataset.outlineId === outline.outline_id);
        const currentNote = current?.querySelector(".outline-foot span");
        if (currentNote) {
          currentNote.textContent = failureMessage;
          currentNote.classList.add("outline-error");
        }
      }
    }
  });
  foot.append(note, confirm);
  card.append(head, preview, editor, foot);
  return card;
}

function renderPackageProgress(turn) {
  const deliverables = state.snapshot.deliverables.filter(item =>
    item.outline_id === turn.outline_id && item.outline_revision === turn.outline_revision);
  const ready = deliverables.filter(item => item.status === "ready").length;
  const card = el("section", "package-progress");
  card.dataset.progressTurn = turn.turn_id;
  const head = el("div", "package-progress-head");
  head.append(el("strong", "", "产物生成进度"),
    el("span", "package-progress-count", `已完成 ${ready} / ${deliverables.length}`));
  card.append(head);
  const track = el("div", "package-progress-track");
  track.setAttribute("role", "progressbar");
  track.setAttribute("aria-label", "产物生成进度");
  track.setAttribute("aria-valuemin", "0");
  track.setAttribute("aria-valuemax", String(deliverables.length));
  track.setAttribute("aria-valuenow", String(ready));
  const fill = el("div", "package-progress-fill");
  fill.style.width = `${deliverables.length ? ready / deliverables.length * 100 : 0}%`;
  track.append(fill);
  card.append(track);
  const list = el("div", "package-progress-list");
  for (const item of deliverables) {
    const artifact = item.artifact_id && state.snapshot.artifacts.find(row => row.artifact_id === item.artifact_id);
    const row = el(artifact ? "button" : "div", `package-progress-item ${artifact ? "is-ready" : "is-pending"}`);
    if (artifact) {
      row.dataset.artifactId = artifact.artifact_id;
      row.type = "button";
      row.addEventListener("click", () => openPreview(artifact.artifact_id));
    }
    row.append(el("span", "package-progress-mark", artifact ? "✓" : "·"),
      el("span", "package-progress-name", artifact?.filename || item.name),
      el("span", "package-progress-state", artifact ? "预览 →" : "待完成"));
    list.append(row);
  }
  card.append(list);
  return card;
}

function renderWaiting(turn) {
  const packageTurn = ["package_generate", "package_continue"].includes(turn.scope_kind);
  const row = el("div", `message assistant waiting${packageTurn ? " has-package-progress" : ""}`);
  row.append(el("span", "agent-avatar", "✦"));
  const body = el("div", "message-body");
  body.append(makeElapsed(turn, true));
  if (packageTurn) {
    body.append(renderPackageProgress(turn));
  }
  row.append(body);
  return row;
}

function renderThread() {
  const container = $("#thread");
  const scroll = $("#chatScroll");
  const atBottom = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 110;
  const empty = Boolean(state.snapshot && !state.snapshot.messages.length && !state.snapshot.outlines.length && !activeTurn());
  // Keep the welcome controls and their focus stable during snapshot/SSE refreshes.
  if (!empty || !container.classList.contains("is-empty")) container.replaceChildren();
  container.classList.toggle("is-empty", empty);
  $(".main-pane").classList.toggle("is-empty", empty);
  if (empty && !$(".empty-state", container)) container.append(renderWelcome());
  else if (!state.snapshot) container.append(el("p", "message-placeholder", "正在读取对话…"));
  const entries = [
    ...(state.snapshot?.messages || []).map((value, index) => ({ kind: "message", value, index, time: value.created_at })),
    ...(state.snapshot?.outlines || []).filter(item => ["pending", "confirmed"].includes(item.status))
      .map((value, index) => ({ kind: "outline", value, index, time: value.created_at })),
  ];
  entries.sort((a, b) => Date.parse(a.time) - Date.parse(b.time) ||
    (a.kind === b.kind ? a.index - b.index : a.kind === "message" ? -1 : 1));
  for (const entry of entries) container.append(entry.kind === "message" ? renderMessage(entry.value) : renderOutline(entry.value));
  const active = activeTurn();
  if (active) {
    if (!state.snapshot.messages.some(message => message.role === "assistant" && message.turn_id === active.turn_id)) {
      container.append(renderWaiting(active));
    }
  } else {
    const latest = state.snapshot?.turns.at(-1);
    if (latest && ["cancelled", "failed", "interrupted"].includes(latest.status)) {
      const notice = el("div", "notice");
      const labels = {
        cancelled: ["本轮已终止", "已完成文件仍可在产物抽屉中预览。你可以发消息让智能体接着完成剩余工作。"],
        failed: ["本轮未完成", "已完成文件已保留。你可以检查资料后重新提出需求或继续剩余工作。"],
        interrupted: ["本轮因服务中断而未完成", "已完成文件已保留。服务恢复后可以通过聊天接着完成剩余工作。"],
      };
      if (latest.scope_kind === "focused_rename") {
        const renamed = state.snapshot.progress?.turn_id === latest.turn_id;
        labels[latest.status] = ["本轮改名已结束", renamed ? "文件名已整批更新，正文和内容版本保持原样。" : "改名未提交，原文件名和正文保持原样。"];
      }
      if (latest.scope_kind === "focused_edit") {
        let partial = {};
        try { partial = JSON.parse(latest.scope_json).partial_edit || {}; } catch { /* Older turn. */ }
        const updated = partial.updated_artifact_ids || [];
        const remaining = partial.remaining_artifact_ids || [];
        const detail = partial.updated_artifact_ids ?
          `已更新 ${updated.length} 个文件，另有 ${remaining.length} 个目标尚未修改。已更新版本保留在产物抽屉中。` :
          "本轮修改未完成。已发布的文件版本仍保留在产物抽屉中。";
        labels[latest.status] = ["本轮修改未完成", detail];
      }
      notice.append(el("strong", "", labels[latest.status][0]), el("p", "", labels[latest.status][1]));
      container.append(notice);
    }
  }
  if (atBottom) scroll.scrollTop = scroll.scrollHeight;
}

function renderComposer() {
  const turn = activeTurn();
  const ready = Boolean(state.conversationId && state.snapshot);
  const locked = !ready || Boolean(turn) || state.busy;
  $("#messageInput").disabled = locked;
  for (const button of document.querySelectorAll(".welcome-upload, .welcome-prompt")) button.disabled = locked;
  $("#messageInput").placeholder = $(".main-pane").classList.contains("is-empty") ?
    "描述产品和希望评测的能力…" : "继续讨论，或告诉智能体要修改哪个文件…";
  $("#attachButton").disabled = locked;
  const send = $("#sendButton");
  send.classList.toggle("is-stop", Boolean(turn));
  send.textContent = turn ? "" : "↑";
  send.title = turn ? "终止任务" : "发送消息";
  send.setAttribute("aria-label", turn ? "终止任务" : "发送消息");
  send.disabled = turn ? turn.status === "cancel_requested" : !ready || state.busy;
  $("#composerWrap").classList.toggle("is-disabled", locked);
  const selected = $("#selectedAttachments");
  selected.replaceChildren();
  for (const id of state.selectedAttachments) {
    const attachment = state.snapshot?.attachments.find(item => item.attachment_id === id);
    if (attachment && !attachment.message_id) selected.append(el("span", "attachment-chip", attachment.filename));
  }
  renderContext();
  $("#statusSync").hidden = !state.statusSyncError;
  $("#statusSyncMessage").textContent = state.statusSyncError || "";
}

function renderContext() {
  const context = state.snapshot?.context || {};
  const usage = context.usage;
  const windowTokens = usage?.window_tokens || context.window_tokens || 128000;
  const threshold = usage?.summarize_at_tokens || context.summarize_at_tokens || 102400;
  const known = Number.isFinite(usage?.used_tokens);
  const used = known ? Math.max(0, usage.used_tokens) : 0;
  const percent = Math.round(used / windowTokens * 100);
  const compression = context.compression;
  const turn = activeTurn();
  const compressing = Boolean(turn && turn.status === "running" && compression?.turn_id === turn.turn_id && compression.status === "started");
  const meter = $("#contextMeter");
  meter.classList.toggle("is-high", known && used >= threshold);
  meter.classList.toggle("is-compressing", compressing);
  $("#contextPercent").textContent = known ? `${percent}%` : "—";
  $("#contextFill").style.strokeDasharray = `${compressing ? 28 : Math.min(100, percent)} 100`;
  $("#contextButton").setAttribute("aria-label", compressing ? "正在整理对话上下文，查看占用详情" :
    known ? `上下文约 ${percent}% 已用，查看详情` : "上下文占用，尚未统计，查看详情");
  $("#contextHeadline").textContent = compressing ? "正在整理对话上下文" :
    known ? `约 ${percent}% 已用（剩余 ${Math.max(0, 100 - percent)}%）` : "上下文窗口 · 尚未统计";
  const tokens = value => new Intl.NumberFormat("zh-CN").format(value);
  $("#contextTokens").textContent = known ?
    `已用约 ${tokens(used)} / 共 ${tokens(windowTokens)} tokens` : `窗口共 ${tokens(windowTokens)} tokens`;
  const latest = state.snapshot?.turns.at(-1);
  const currentCompression = compression?.turn_id === latest?.turn_id;
  const notices = {
    started: "正在整理对话上下文，完成后继续处理。历史消息和已生成文件会保留。",
    completed: "对话上下文已整理，继续处理当前任务。",
    failed: "整理对话上下文失败，历史消息和已生成文件仍保留。可重新发送消息重试。",
  };
  const status = currentCompression ? compression?.status : null;
  const notice = $("#contextNotice");
  const visible = status === "failed" || (turn?.status === "running" && ["started", "completed"].includes(status));
  notice.hidden = !visible;
  notice.dataset.state = status || "";
  const text = visible ? notices[status] : "";
  if (notice.textContent !== text) notice.textContent = text;
}

function showContextDetails(open) {
  $("#contextTooltip").hidden = !open;
  $("#contextButton").setAttribute("aria-expanded", String(open));
}

function renderFolders() {
  const folders = $("#folders");
  folders.replaceChildren();
  const evaluations = state.snapshot?.evaluations || [];
  const artifacts = state.snapshot?.artifacts || [];
  $("#artifactCount").textContent = String(artifacts.length);
  if (!evaluations.length) folders.append(el("p", "folder-empty", "还没有产品产物。"));
  for (const evaluation of evaluations) {
    const files = artifacts.filter(item => item.evaluation_id === evaluation.evaluation_id);
    const latestEdit = state.snapshot?.turns.filter(item => item.evaluation_id === evaluation.evaluation_id && item.scope_kind === "focused_edit").at(-1);
    const unfinishedEdit = latestEdit && ["failed", "cancelled", "interrupted"].includes(latestEdit.status);
    const button = el("button", "folder");
    button.type = "button";
    button.append(el("span", "folder-icon", "▣"), el("strong", "", evaluation.name));
    if (evaluation.package_status === "incomplete") button.append(el("span", "folder-state", "未完成的任务产物"));
    else if (unfinishedEdit) button.append(el("span", "folder-state", "最近一次修改未完成"));
    else button.append(el("small", "", `${files.length} 个文件`));
    button.append(el("span", "chevron", state.collapsedFolders.has(evaluation.evaluation_id) ? "▸" : "▾"));
    button.setAttribute("aria-expanded", String(!state.collapsedFolders.has(evaluation.evaluation_id)));
    button.addEventListener("click", () => {
      if (state.collapsedFolders.has(evaluation.evaluation_id)) state.collapsedFolders.delete(evaluation.evaluation_id);
      else state.collapsedFolders.add(evaluation.evaluation_id);
      renderFolders();
    });
    folders.append(button);
    if (state.collapsedFolders.has(evaluation.evaluation_id)) continue;
    const body = el("div", "folder-body");
    if (!files.length) body.append(el("div", "folder-empty", "暂无已完成文件"));
    for (const [label, grouped] of [
      ["文档", files.filter(item => ["md", "docx"].includes(item.kind))],
      ["数据表", files.filter(item => item.kind === "csv")],
      ["其他文件", files.filter(item => !["md", "docx", "csv"].includes(item.kind))],
    ]) {
      if (!grouped.length) continue;
      const group = el("section", "file-group");
      const head = el("div", "file-group-head");
      head.append(el("strong", "", label), el("span", "", `${grouped.length} 份`));
      group.append(head);
      for (const artifact of grouped) {
        const file = el("button", `artifact-row${state.selectedArtifactId === artifact.artifact_id ? " active" : ""}`);
        file.type = "button";
        file.dataset.artifactId = artifact.artifact_id;
        file.title = artifact.filename;
        const info = el("span", "artifact-info");
        info.append(el("strong", "artifact-name", artifact.filename),
          el("small", "artifact-revision", `第 ${artifact.content_revision} 版 · 只读预览`));
        file.append(el("span", `tiny-file ${artifact.kind === "csv" ? "is-table" : ""}`, artifact.kind === "docx" ? "W" : artifact.kind === "csv" ? "CSV" : "MD"),
          info, el("span", "preview-action", "预览 ↗"));
        file.addEventListener("click", () => openPreview(artifact.artifact_id));
        group.append(file);
      }
      body.append(group);
    }
    folders.append(body);
  }
}

function renderStatus() {
  const active = activeTurn();
  const pending = state.snapshot?.outlines.some(item => item.status === "pending");
  const latest = state.snapshot?.turns.at(-1);
  $("#conversationTitle").textContent = state.snapshot?.conversation.title || "新对话";
  $("#conversationContext").textContent = state.snapshot && !state.snapshot.messages.length ? "开始新的评测任务" : "对话";
  const status = $("#topStatus");
  status.textContent = active ? (active.status === "cancel_requested" ? "终止中" : "运行中") :
    pending ? "等待确认" : latest?.status === "cancelled" ? "已终止" :
    ["failed", "interrupted"].includes(latest?.status) ? "未完成" : latest?.status === "completed" ? "本轮已完成" : "就绪";
  status.dataset.state = status.textContent;
  status.hidden = !active && !pending && !["cancelled", "failed", "interrupted"].includes(latest?.status);
}

function render() {
  renderHistory();
  renderStatus();
  renderThread();
  renderComposer();
  renderFolders();
}

async function refreshConversations() {
  state.conversations = await api("/api/conversations");
  renderHistory();
}

function applyEvent(event) {
  if (event.event_id <= state.cursor) return;
  state.cursor = event.event_id;
  if (event.type === "message.delta") {
    const message = state.snapshot?.messages.find(item => item.message_id === event.payload.message_id);
    const row = [...document.querySelectorAll("[data-message-id]")].find(item => item.dataset.messageId === event.payload.message_id);
    if (message && row) {
      message.body += event.payload.delta;
      const text = $(".text", row);
      queueMarkdownRender(text, message.body);
      return;
    }
  }
  state.needsRefresh = true;
  void refreshSnapshot();
}

async function refreshSnapshot() {
  if (!state.conversationId) return false;
  const conversationId = state.conversationId;
  if (state.refreshOwner?.conversationId === conversationId && state.refreshOwner.version === state.viewVersion) {
    state.needsRefresh = true;
    return state.refreshOwner.promise;
  }
  const owner = { conversationId, version: state.viewVersion, promise: null };
  state.refreshOwner = owner;
  state.refreshing = true;
  state.refreshingConversationId = conversationId;
  const current = () => state.refreshOwner === owner && state.conversationId === conversationId;
  owner.promise = (async () => {
    try {
      do {
        state.needsRefresh = false;
        const snapshot = await api(`/api/conversations/${conversationId}`);
        if (!current()) return false;
        state.snapshot = snapshot;
        state.statusSyncError = null;
        state.cursor = snapshot.last_event_id;
        state.clockOffsetMs = Date.parse(snapshot.server_now) - Date.now();
        const cancelled = snapshot.turns.find(turn => turn.turn_id === state.cancelSyncTurnId);
        if (cancelled && ["completed", "cancelled", "failed", "interrupted"].includes(cancelled.status)) {
          clearTimeout(state.cancelSyncTimer);
          state.cancelSyncTimer = null;
          state.cancelSyncTurnId = null;
        }
        if (!state.cancelSyncTurnId) {
          state.cancelSyncTurnId = snapshot.turns.find(turn => turn.status === "cancel_requested")?.turn_id || null;
          state.cancelSyncPolls = 0;
        }
        const pending = state.queuedEvents.splice(0);
        render();
        for (const event of pending) applyEvent(event);
        // Do not await sidebar loading while holding the event-consumption lock.
        // Drain events again before releasing it; no awaited work follows this check.
      } while (state.needsRefresh || state.queuedEvents.length);
      void refreshConversations().catch(error => toast(error.message));
      return true;
    } catch (error) {
      if (current()) {
        state.statusSyncError = "状态同步失败，请点击重试。确认服务端已停止后才能继续输入。";
        renderComposer();
      }
      return false;
    }
    finally {
      if (current()) {
        state.refreshOwner = null;
        state.refreshing = false;
        state.refreshingConversationId = null;
        scheduleCancelSync();
      }
    }
  })();
  return owner.promise;
}

function scheduleCancelSync() {
  if (!state.cancelSyncTurnId || state.statusSyncError || state.cancelSyncTimer) return;
  if (state.cancelSyncPolls >= 15) {
    state.statusSyncError = "仍在等待服务端确认终止，请重试同步。";
    renderComposer();
    return;
  }
  const version = state.viewVersion;
  state.cancelSyncTimer = setTimeout(() => {
    state.cancelSyncTimer = null;
    if (version !== state.viewVersion) return;
    state.cancelSyncPolls += 1;
    void refreshSnapshot();
  }, 1000);
}

function connectEvents(conversationId) {
  state.source?.close();
  const source = new EventSource(`/api/conversations/${conversationId}/events?after=${state.cursor}`);
  state.source = source;
  source.onopen = () => { if (state.source === source) void refreshSnapshot(); };
  const types = ["turn.started", "turn.cancel_requested", "turn.completed", "turn.cancelled", "turn.failed", "turn.interrupted", "message.started", "message.delta", "message.completed", "outline.ready", "outline.updated", "artifact.ready", "artifact.updated", "artifact.renamed", "tool.progress", "context.usage", "context.compression.started", "context.compression.completed", "context.compression.failed", "context.compression.cancelled"];
  for (const type of types) source.addEventListener(type, message => {
    if (state.source !== source) return;
    const event = JSON.parse(message.data);
    if (state.refreshing) state.queuedEvents.push(event);
    else applyEvent(event);
  });
}

async function switchConversation(conversationId) {
  if (conversationId === state.conversationId && state.snapshot) return;
  const input = $("#messageInput");
  if (state.conversationId) {
    state.drafts.set(state.conversationId, input.value);
    state.attachmentSelections.set(state.conversationId, state.selectedAttachments);
  }
  state.source?.close();
  state.source = null;
  state.viewVersion += 1;
  const version = state.viewVersion;
  state.refreshOwner = null;
  state.refreshing = false;
  state.refreshingConversationId = null;
  clearTimeout(state.cancelSyncTimer);
  state.cancelSyncTimer = null;
  state.cancelSyncTurnId = null;
  state.cancelSyncPolls = 0;
  state.statusSyncError = null;
  state.conversationId = conversationId;
  state.snapshot = null;
  state.contextPinned = false;
  showContextDetails(false);
  state.cursor = 0;
  state.queuedEvents = [];
  state.needsRefresh = false;
  state.selectedAttachments = state.attachmentSelections.get(conversationId) || new Set();
  state.attachmentSelections.set(conversationId, state.selectedAttachments);
  input.value = state.drafts.get(conversationId) || "";
  state.selectedArtifactId = null;
  closePreview();
  history.replaceState(null, "", `?conversation=${encodeURIComponent(conversationId)}`);
  render();
  await refreshSnapshot();
  if (state.viewVersion === version) connectEvents(conversationId);
}

async function createConversation() {
  try {
    const result = await api("/api/conversations", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: "新对话" }),
    });
    await refreshConversations();
    showDrawer(false);
    await switchConversation(result.conversation_id);
  } catch (error) { toast(error.message); }
}

async function sendMessage() {
  if (!state.conversationId || !state.snapshot) return;
  const body = $("#messageInput").value.trim();
  const selection = state.selectedAttachments;
  const attachmentIds = [...selection];
  if ((!body && !attachmentIds.length) || activeTurn() || state.busy) return;
  const conversationId = state.conversationId;
  const signature = JSON.stringify([conversationId, body, attachmentIds]);
  if (state.pendingSend?.signature !== signature) {
    state.pendingSend = { signature, clientKey: crypto.randomUUID() };
  }
  const clientKey = state.pendingSend.clientKey;
  state.busy = true;
  renderComposer();
  let accepted = false;
  let failure = null;
  try {
    try {
      await api(`/api/conversations/${conversationId}/messages`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ body, attachment_ids: attachmentIds, client_key: clientKey }),
      });
      accepted = true;
    } catch (error) {
      failure = error;
      // A lost response does not mean the server rejected the message.
      try {
        const snapshot = await api(`/api/conversations/${conversationId}`, {
          signal: AbortSignal.timeout(5000),
        });
        accepted = snapshot.turns.some(turn => turn.client_key === clientKey);
      } catch { /* Keep the draft and its key for an idempotent retry. */ }
    }
    if (accepted) {
      state.pendingSend = null;
      state.drafts.delete(conversationId);
      selection.clear();
      if (conversationId === state.conversationId) {
        $("#messageInput").value = "";
        await refreshSnapshot();
      }
    } else {
      toast(failure?.name === "TimeoutError" ? "发送超时，草稿已保留，请重试" :
        `${failure?.message || "发送失败"}；草稿已保留，请重试`);
    }
  } finally { state.busy = false; render(); }
}

async function cancelActiveTurn() {
  const turn = activeTurn();
  if (!turn || turn.status === "cancel_requested") return;
  const button = $("#sendButton");
  button.disabled = true;
  const conversationId = state.conversationId;
  const version = state.viewVersion;
  state.cancelSyncTurnId = turn.turn_id;
  state.cancelSyncPolls = 0;
  let requestError = null;
  try {
    await api(`/api/conversations/${conversationId}/turns/${turn.turn_id}/cancel`, { method: "POST" });
    if (version !== state.viewVersion) return;
    turn.status = "cancel_requested";
    render();
  } catch (error) {
    requestError = error;
  }
  if (version !== state.viewVersion) return;
  const synced = await refreshSnapshot();
  if (synced && requestError && activeTurn()?.status === "running") {
    state.cancelSyncTurnId = null;
    clearTimeout(state.cancelSyncTimer);
    state.cancelSyncTimer = null;
    toast("终止请求尚未确认，请再次点击终止任务。");
  }
}

async function uploadFiles(files) {
  if (!state.conversationId || !state.snapshot || activeTurn() || state.busy || !files.length) return;
  const conversationId = state.conversationId;
  const selection = state.selectedAttachments;
  state.busy = true;
  renderComposer();
  try {
    for (const file of files) {
      const form = new FormData();
      form.append("upload_key", crypto.randomUUID());
      form.append("file", file);
      const uploaded = await api(`/api/conversations/${conversationId}/attachments`, { method: "POST", body: form });
      selection.add(uploaded.attachment_id);
    }
    if (conversationId === state.conversationId) await refreshSnapshot();
  } catch (error) { toast(error.message); }
  finally { state.busy = false; $("#fileInput").value = ""; render(); }
}

function showDrawer(open) {
  state.drawerOpen = open;
  $("#drawer").hidden = !open;
  $("#toggleDrawer").setAttribute("aria-expanded", String(open));
}

async function openPreview(artifactId) {
  const conversationId = state.conversationId;
  const artifact = state.snapshot?.artifacts.find(item => item.artifact_id === artifactId);
  if (!artifact) return;
  const requestId = ++state.previewRequest;
  state.selectedArtifactId = artifactId;
  state.collapsedFolders.delete(artifact.evaluation_id);
  const dialog = $("#previewDialog");
  if (!dialog.open) {
    state.previewOpener = document.activeElement;
    dialog.showModal();
  }
  renderFolders();
  $("#previewTitle").textContent = artifact.filename;
  $("#previewRevision").textContent = `第 ${artifact.content_revision} 版`;
  $("#previewDownload").href = `/api/conversations/${conversationId}/artifacts/${artifactId}/download`;
  const preview = $("#preview");
  preview.setAttribute("aria-busy", "true");
  preview.scrollTop = 0;
  preview.replaceChildren(el("p", "preview-loading", "正在加载预览…"));
  const current = () => dialog.open && requestId === state.previewRequest &&
    conversationId === state.conversationId && artifactId === state.selectedArtifactId;
  try {
    const content = await api(artifact.preview_url);
    if (!current()) return;
    const page = el("div", "preview-page");
    if (artifact.kind === "csv") page.append(renderCsvPreview(content));
    else if (["md", "docx"].includes(artifact.kind)) {
      const markdown = el("div", "markdown-content");
      renderMarkdown(markdown, content, artifact.evaluation_id);
      page.append(markdown);
    } else page.append(el("pre", "", content));
    preview.replaceChildren(page);
  } catch (error) {
    if (!current()) return;
    const failure = el("div", "preview-error");
    const retry = el("button", "secondary", "重试预览");
    retry.type = "button";
    retry.addEventListener("click", () => void openPreview(artifactId));
    failure.append(el("p", "", error.message), retry);
    preview.replaceChildren(failure);
  } finally {
    if (current()) preview.setAttribute("aria-busy", "false");
  }
}

function closePreview() {
  state.previewRequest += 1;
  if ($("#previewDialog").open) $("#previewDialog").close();
}

$("#closePreview").addEventListener("click", closePreview);
$("#previewDialog").addEventListener("keydown", event => {
  if (event.key !== "Tab") return;
  const controls = [...$("#previewDialog").querySelectorAll("button:not([disabled]), a[href], [tabindex]:not([tabindex='-1'])")]
    .filter(control => control.getClientRects().length);
  const first = controls[0];
  const last = controls.at(-1);
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last?.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first?.focus();
  }
});
$("#previewDialog").addEventListener("close", () => {
  if ($("#previewDialog").open) return;
  state.previewRequest += 1;
  $("#preview").replaceChildren();
  $("#preview").setAttribute("aria-busy", "false");
  const opener = state.previewOpener;
  state.previewOpener = null;
  if (opener?.isConnected) opener.focus();
  else if (opener?.dataset.artifactId) {
    const kind = ["artifact-row", "deliverable-row", "package-progress-item"].find(name => opener.classList.contains(name));
    [...document.querySelectorAll(`.${kind}[data-artifact-id]`)]
      .find(button => button.dataset.artifactId === opener.dataset.artifactId)?.focus();
  }
});
$("#previewDialog").addEventListener("click", event => {
  const dialog = $("#previewDialog");
  const bounds = dialog.getBoundingClientRect();
  if (event.target === dialog && (event.clientX < bounds.left || event.clientX > bounds.right ||
      event.clientY < bounds.top || event.clientY > bounds.bottom)) closePreview();
});
$("#newChat").addEventListener("click", createConversation);
$("#retryStatusSync").addEventListener("click", () => {
  state.statusSyncError = null;
  state.cancelSyncPolls = 0;
  void refreshSnapshot();
});
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") {
    state.cancelSyncPolls = 0;
    void refreshSnapshot();
  }
});
$("#contextButton").addEventListener("click", () => {
  state.contextPinned = !state.contextPinned;
  showContextDetails(state.contextPinned);
});
$("#contextMeter").addEventListener("pointerenter", () => showContextDetails(true));
$("#contextMeter").addEventListener("pointerleave", () => {
  if (!state.contextPinned && !$("#contextMeter").contains(document.activeElement)) showContextDetails(false);
});
$("#contextButton").addEventListener("focus", () => showContextDetails(true));
$("#contextButton").addEventListener("blur", () => {
  state.contextPinned = false;
  showContextDetails(false);
});
document.addEventListener("pointerdown", event => {
  if (!$("#contextMeter").contains(event.target)) {
    state.contextPinned = false;
    showContextDetails(false);
  }
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape") {
    state.contextPinned = false;
    showContextDetails(false);
  }
});
$("#sendButton").addEventListener("click", () => {
  if (activeTurn()) void cancelActiveTurn();
  else void sendMessage();
});
$("#messageInput").addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    void sendMessage();
  }
});
$("#messageInput").addEventListener("input", event => {
  if (state.conversationId) state.drafts.set(state.conversationId, event.target.value);
});
$("#attachButton").addEventListener("click", () => $("#fileInput").click());
$("#fileInput").addEventListener("change", event => void uploadFiles([...event.target.files]));
$("#toggleDrawer").addEventListener("click", () => showDrawer(!state.drawerOpen));
$("#closeDrawer").addEventListener("click", () => showDrawer(false));
setInterval(renderTimers, 1000);

try {
  await refreshConversations();
  const requested = new URLSearchParams(location.search).get("conversation");
  const chosen = state.conversations.find(item => item.conversation_id === requested)?.conversation_id || state.conversations[0]?.conversation_id;
  if (chosen) await switchConversation(chosen);
  else await createConversation();
} catch (error) { toast(error.message); }
