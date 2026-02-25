import { formatTimestamp } from "./utils/time.js";
import { appendAndParseSseBuffer } from "./utils/sse.js";

const planForm = document.getElementById("planForm");
const submitBtn = document.getElementById("submitBtn");
const resetBtn = document.getElementById("resetBtn");
const apiTokenInput = document.getElementById("apiToken");
const documentInput = document.getElementById("documentUpload");
const alertBox = document.getElementById("formAlert");
const pauseBtn = document.getElementById("pauseBtn");

const historyPane = document.getElementById("historyPane");
const formPane = document.getElementById("formPane");
const detailPane = document.getElementById("detailPane");
const historyList = document.getElementById("historyList");
const historySearchInput = document.getElementById("historySearch");
const historyCollapseBtn = document.getElementById("historyCollapseBtn");
const newTaskBtn = document.getElementById("newTaskBtn");
const planList = document.getElementById("planList");
const planProgress = document.getElementById("planProgress");
const progressBar = document.getElementById("progressBar");
const progressCard = document.querySelector(".progress-card");
const progressBody = document.getElementById("progressBody");
const progressToggleBtn = document.getElementById("progressToggle");

const timelineList = document.getElementById("timelineList");
const timelineEmptyMessage = document.getElementById("timelineEmptyMessage");
const timelineMeta = document.getElementById("timelineMeta");
const timelineStatusLabel = document.getElementById("timelineStatus");
const timelineTicker = document.getElementById("timelineTicker");
const summaryPanel = document.getElementById("summaryPanel");
const summaryText = document.getElementById("summaryText");
const filesPanel = document.getElementById("filesPanel");
const fileList = document.getElementById("fileList");
const downloadAllBtn = document.getElementById("downloadAllBtn");

const STATUS_BADGE_CLASS = {
  pending: "",
  running: "status-running",
  done: "status-done",
  failed: "status-failed",
  retry: "status-retry",
};

const STEP_TITLE_MAP = {
  product_analysis: "产品解析",
  plan_design: "评测方案设计",
  data_delivery: "数据与样本准备",
  execution_manual: "测试执行与物料",
};

const state = {
  steps: [],
  stepById: new Map(),
  stepOrder: [],
};

const timelineState = {
  requestId: "----",
  status: "等待执行",
  steps: new Map(),
  order: [],
  summary: "",
  files: [],
  tickerMessages: [],
  tickerIndex: 0,
};

const TASK_STORAGE_KEY = "evaluation_tasks_v1";
const TASK_STATUS_TEXT = {
  running: "进行中",
  done: "已完成",
  failed: "失败",
};

const historyState = {
  tasks: [],
  filter: "",
  collapsed: false,
  selectedId: null,
};

let tickerTimer = null;

let currentRequestId = null;
let currentApiToken = "";
let eventStreamAbort = null;
let reconnectTimer = null;
let reconnectAttempts = 0;
let shuttingDownStream = false;
let sseBuffer = "";
let progressCollapsed = false;
let isPaused = false;
let pendingProductName = "";
let streamCompleted = false;

const buildAuthHeaders = () => (currentApiToken ? { "X-API-Key": currentApiToken } : {});

const showAlert = (message, type = "info") => {
  if (!alertBox) return;
  if (!message) {
    alertBox.className = "alert hidden";
    alertBox.textContent = "";
    return;
  }
  alertBox.textContent = message;
  alertBox.className = `alert ${type}`;
};

const clearAlert = () => showAlert("");

const withApiToken = (url) => {
  if (!currentApiToken) return url;
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}api_key=${encodeURIComponent(currentApiToken)}`;
};

const loadHistoryFromStorage = () => {
  try {
    const raw = localStorage.getItem(TASK_STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch (error) {
    console.warn("Failed to parse stored tasks", error);
    return [];
  }
};

const saveHistoryToStorage = () => {
  try {
    localStorage.setItem(TASK_STORAGE_KEY, JSON.stringify(historyState.tasks));
  } catch (error) {
    console.warn("Failed to persist tasks", error);
  }
};

const findTaskById = (requestId) => historyState.tasks.find((task) => task.requestId === requestId);

const registerTaskEntry = (requestId, productName) => {
  if (!requestId) return;
  const now = Date.now();
  const existing = findTaskById(requestId);
  if (existing) {
    existing.productName = productName || existing.productName;
    existing.createdAt = existing.createdAt || now;
    existing.status = existing.status || "running";
    existing.lastUpdated = now;
  } else {
    historyState.tasks.push({
      requestId,
      productName,
      status: "running",
      createdAt: now,
      lastUpdated: now,
      events: [],
      files: [],
      summary: "",
    });
  }
  historyState.selectedId = requestId;
  saveHistoryToStorage();
  renderHistoryList();
};

const updateTaskStatusValue = (requestId, status) => {
  if (!requestId) return;
  const task = findTaskById(requestId);
  if (!task) return;
  task.status = status;
  task.lastUpdated = Date.now();
  saveHistoryToStorage();
  renderHistoryList();
};

const recordTaskEvent = (requestId, payload) => {
  const task = findTaskById(requestId);
  if (!task) return;
  task.events = task.events || [];
  task.events.push(payload);
  if (task.events.length > 400) {
    task.events = task.events.slice(-400);
  }
  task.lastUpdated = Date.now();
  saveHistoryToStorage();
};

const updateTaskFilesData = (requestId, files = []) => {
  const task = findTaskById(requestId);
  if (!task) return;
  task.files = files;
  task.lastUpdated = Date.now();
  saveHistoryToStorage();
  renderHistoryList();
};

const updateTaskSummaryData = (requestId, summary) => {
  const task = findTaskById(requestId);
  if (!task) return;
  task.summary = summary;
  task.lastUpdated = Date.now();
  saveHistoryToStorage();
};

const removeHistoryTask = (requestId) => {
  historyState.tasks = historyState.tasks.filter((task) => task.requestId !== requestId);
  if (historyState.selectedId === requestId) {
    historyState.selectedId = null;
    resetState();
    stopEventStream();
    currentRequestId = null;
    updateTimelineMeta();
  }
  saveHistoryToStorage();
  renderHistoryList();
};

const toLocalTime = (value) => {
  if (value === undefined || value === null) return new Date();
  if (value instanceof Date) return value;
  if (typeof value === "number" && Number.isFinite(value)) return new Date(value);
  if (typeof value === "string" && value.trim()) {
    const trimmed = value.trim();
    const hasTimezone = /([zZ]|[+-]\d{2}:?\d{2})$/.test(trimmed);
    const target = hasTimezone ? trimmed : `${trimmed}Z`;
    const parsed = Date.parse(target);
    if (!Number.isNaN(parsed)) return new Date(parsed);
  }
  const fallback = Date.parse(value);
  return Number.isNaN(fallback) ? new Date() : new Date(fallback);
};

const formatTaskTime = (timestamp) => formatTimestamp(toLocalTime(timestamp));

const renderHistoryList = () => {
  if (!historyList) return;
  const term = historyState.filter.trim().toLowerCase();
  const tasks = [...historyState.tasks]
    .sort((a, b) => (b.createdAt || 0) - (a.createdAt || 0))
    .filter((task) =>
      !term ? true : (task.productName || "").toLowerCase().includes(term)
    );

  historyList.innerHTML = "";
  if (!tasks.length) {
    const empty = document.createElement("div");
    empty.className = "history-empty";
    empty.textContent = "暂无历史任务";
    historyList.appendChild(empty);
    return;
  }

  tasks.forEach((task) => {
    const item = document.createElement("div");
    item.className = "history-item";
    item.dataset.id = task.requestId;
    if (historyState.selectedId === task.requestId) {
      item.classList.add("active");
    }

    const info = document.createElement("div");
    info.className = "history-info";
    const name = document.createElement("strong");
    name.textContent = task.productName || task.requestId;
    const time = document.createElement("span");
    time.textContent = formatTaskTime(task.createdAt);
    info.append(name, time);

    const actions = document.createElement("div");
    actions.className = "history-actions";
    const status = document.createElement("span");
    status.className = "history-status";
    if (task.status === "done") status.classList.add("done");
    if (task.status === "failed") status.classList.add("failed");
    status.textContent = TASK_STATUS_TEXT[task.status] || TASK_STATUS_TEXT.running;
    const delBtn = document.createElement("button");
    delBtn.className = "delete-task";
    delBtn.dataset.deleteId = task.requestId;
    delBtn.type = "button";
    delBtn.textContent = "删除";
    actions.append(status, delBtn);

    item.append(info, actions);
    historyList.appendChild(item);
  });

  if (historyPane) {
    historyPane.classList.toggle("collapsed", historyState.collapsed);
  }
  if (historyCollapseBtn) {
    historyCollapseBtn.textContent = historyState.collapsed ? "展开" : "收起";
  }
};

const toggleHistoryCollapse = () => {
  historyState.collapsed = !historyState.collapsed;
  renderHistoryList();
  updateLayoutCollapse();
};

const hydrateHistoryPane = () => {
  historyState.tasks = loadHistoryFromStorage();
  renderHistoryList();
  updateLayoutCollapse();
};

const updateLayoutCollapse = () => {
  const container = historyPane?.parentElement;
  if (!container) return;
  container.classList.toggle("history-collapsed", historyState.collapsed);
  if (historyState.collapsed) {
    historyPane?.classList.add("collapsed");
  } else {
    historyPane?.classList.remove("collapsed");
  }
};

const loadTaskFromHistory = (requestId) => {
  const task = findTaskById(requestId);
  if (!task) return;
  stopEventStream();
  currentRequestId = task.requestId;
  historyState.selectedId = task.requestId;
  renderHistoryList();
  resetState();
  setTimelineStatus(TASK_STATUS_TEXT[task.status] || "历史任务");
  if (Array.isArray(task.events)) {
    task.events.forEach((event) => processEvent(event, true));
  }
  if (task.summary) {
    timelineState.summary = task.summary;
    renderSummary();
  }
  if (Array.isArray(task.files)) {
    setTimelineFiles(task.files);
  } else {
    updateDownloadAllButton();
  }
};

const updateDownloadAllButton = () => {
  if (!downloadAllBtn) return;
  const hasFiles = Array.isArray(timelineState.files) && timelineState.files.length > 0;
  downloadAllBtn.disabled = !(currentRequestId && hasFiles);
};

const collectFormData = () => {
  const formData = new FormData(planForm);
  const payload = {
    product_name: formData.get("product_name")?.trim(),
    application_scenario: null,
    target_users: null,
    business_goals: null,
    constraints: formData.get("constraints")?.trim() || null,
    core_features: [],
    workflow: [],
  };

  return payload;
};

const resetState = () => {
  state.steps = [];
  state.stepById = new Map();
  state.stepOrder = [];

  planList.innerHTML = "";
  planProgress.textContent = "0 / 0";
  progressBar.style.width = "0%";
  resetTimelineState();
  setProgressCollapsed(false);
  resetPauseState();
  streamCompleted = false;
};

const setProgressCollapsed = (collapsed) => {
  progressCollapsed = collapsed;
  if (!progressBody || !progressToggleBtn || !progressCard) return;
  progressBody.hidden = collapsed;
  progressCard.classList.toggle("collapsed", collapsed);
  progressToggleBtn.textContent = collapsed ? "展开" : "收起";
  progressToggleBtn.setAttribute("aria-expanded", String(!collapsed));
};

const updateTimelineMeta = () => {
  if (timelineMeta) {
    timelineMeta.textContent = `Request ID · ${timelineState.requestId || "----"}`;
  }
};

const setTimelineStatus = (text) => {
  timelineState.status = text;
  if (timelineStatusLabel) {
    timelineStatusLabel.textContent = text;
  }
};

const updateTickerDisplay = (timeText, message) => {
  if (!timelineTicker) return;
  const [timeNode, textNode] = timelineTicker.children;
  if (timeNode) timeNode.textContent = timeText;
  if (textNode) textNode.textContent = message;
};

const renderTicker = (data) => {
  if (!data) return;
  updateTickerDisplay(data.timestamp || "--:--", data.message || "执行动态更新");
};

const startTickerRotation = () => {
  if (tickerTimer) clearInterval(tickerTimer);
  if (!timelineState.tickerMessages.length) return;
  tickerTimer = setInterval(() => {
    if (!timelineState.tickerMessages.length) return;
    timelineState.tickerIndex =
      (timelineState.tickerIndex + 1) % timelineState.tickerMessages.length;
    renderTicker(timelineState.tickerMessages[timelineState.tickerIndex]);
  }, 4000);
};

const resetTicker = () => {
  if (tickerTimer) {
    clearInterval(tickerTimer);
    tickerTimer = null;
  }
  timelineState.tickerMessages = [];
  timelineState.tickerIndex = 0;
  updateTickerDisplay(
    "--:--",
    "当前等待步骤规划，提交后将展示实时执行动态。"
  );
};

const pushTicker = (message, timestamp) => {
  const entry = {
    message: message || "执行动态更新",
    timestamp: formatTimestamp(toLocalTime(timestamp || Date.now())),
  };
  timelineState.tickerMessages.unshift(entry);
  timelineState.tickerIndex = 0;
  renderTicker(entry);
  startTickerRotation();
};

const resetTimelineState = () => {
  resetTicker();
  timelineState.steps = new Map();
  timelineState.order = [];
  timelineState.summary = "";
  timelineState.files = [];
  timelineState.requestId = currentRequestId || "----";
  setTimelineStatus("等待执行");
  updateTimelineMeta();
  if (timelineList) {
    timelineList.innerHTML = "";
    if (timelineEmptyMessage) {
      timelineEmptyMessage.classList.remove("hidden");
      timelineList.appendChild(timelineEmptyMessage);
    }
  }
  if (summaryPanel) {
    summaryPanel.hidden = true;
    summaryText.innerHTML = "";
  }
  if (filesPanel) {
    filesPanel.hidden = true;
    fileList.innerHTML = "";
  }
  updateDownloadAllButton();
};

const resetPauseState = () => {
  isPaused = false;
  if (pauseBtn) {
    pauseBtn.disabled = true;
    pauseBtn.textContent = "暂停任务";
  }
};

const enablePauseButton = () => {
  if (pauseBtn) {
    pauseBtn.disabled = false;
    pauseBtn.textContent = isPaused ? "继续任务" : "暂停任务";
  }
};

const initializeTimelineForRequest = (requestId) => {
  resetTimelineState();
  timelineState.requestId = requestId || "----";
  updateTimelineMeta();
  setTimelineStatus("任务执行中");
};

const ensureTimelineStep = (step) => {
  if (!step?.id) return;
  const existing = timelineState.steps.get(step.id);
  if (existing) {
    if (step.title) existing.title = step.title;
    return;
  }
  timelineState.steps.set(step.id, {
    id: step.id,
    title: step.title || STEP_TITLE_MAP[step.id] || step.id,
    entries: [],
    lastTimestamp: "--:--",
  });
  timelineState.order.push(step.id);
};

const addTimelineEntry = (stepId, entry) => {
  const step = timelineState.steps.get(stepId);
  if (!step) return;
  step.entries.push(entry);
  if (entry.timestamp) step.lastTimestamp = entry.timestamp;
  renderTimelineList();
};

const renderTimelineList = () => {
  if (!timelineList) return;
  timelineList.innerHTML = "";
  let hasContent = false;

  timelineState.order.forEach((stepId, index) => {
    const step = timelineState.steps.get(stepId);
    if (!step || !step.entries.length) return;
    hasContent = true;

    const item = document.createElement("div");
    item.className = "timeline-item";

    const marker = document.createElement("div");
    marker.className = "timeline-marker";
    const dot = document.createElement("div");
    dot.className = "timeline-dot";
    const line = document.createElement("div");
    line.className = "timeline-line";
    if (index === timelineState.order.length - 1) line.style.visibility = "hidden";
    marker.append(dot, line);

    const panel = document.createElement("details");
    panel.className = "timeline-panel";
    panel.open = true;

    const summary = document.createElement("summary");
    const summaryMain = document.createElement("div");
    summaryMain.className = "summary-main";
    const title = document.createElement("div");
    title.className = "event-title";
    title.textContent = step.title;
    const time = document.createElement("span");
    time.className = "summary-time";
    time.textContent = step.lastTimestamp || "--:--";
    summaryMain.append(title, time);
    const arrow = document.createElement("span");
    arrow.className = "summary-arrow";
    arrow.textContent = "▶";
    summary.append(summaryMain, arrow);

    const body = document.createElement("div");
    body.className = "event-body";
    step.entries.forEach((entry) => {
      const chunk = document.createElement("div");
      chunk.style.marginBottom = "5px";
      if (entry.inline) {
        chunk.classList.add("inline-entry");
        chunk.innerHTML = `<span class="block-title">${entry.label}</span><span>${entry.content}</span>`;
        chunk.style.display = "flex";
        chunk.style.alignItems = "baseline";
        chunk.style.flexWrap = "wrap";
      } else {
        chunk.innerHTML = `<span class="block-title">${entry.label}</span><br/>${entry.content}`;
      }
      if (entry.annotation) {
        const note = document.createElement("div");
        note.className = "event-annotation";
        note.textContent = entry.annotation;
        chunk.appendChild(note);
      }
      if (entry.knowledge?.length) {
        const knowledgeBox = document.createElement("div");
        knowledgeBox.className = "knowledge-panel";
        knowledgeBox.innerHTML = `<strong>知识命中</strong>${entry.knowledge
          .map((item) => `<div>${item}</div>`)
          .join("")}`;
        chunk.appendChild(knowledgeBox);
      }
      if (entry.tags?.length) {
        const tags = document.createElement("div");
        tags.className = "event-tags";
        entry.tags.forEach((tag) => {
          const pill = document.createElement("span");
          pill.className = "tag";
          pill.textContent = tag;
          tags.appendChild(pill);
        });
        chunk.appendChild(tags);
      }
      body.appendChild(chunk);
    });

    panel.append(summary, body);
    item.append(marker, panel);
    timelineList.appendChild(item);
  });

  if (!hasContent) {
    timelineEmptyMessage?.classList.remove("hidden");
    if (timelineEmptyMessage) {
      timelineList.appendChild(timelineEmptyMessage);
    }
  } else {
    timelineEmptyMessage?.classList.add("hidden");
  }
};

const renderSummary = () => {
  if (!summaryPanel || !summaryText) return;
  if (!timelineState.summary) {
    summaryPanel.hidden = true;
    summaryText.innerHTML = "";
    return;
  }
  summaryPanel.hidden = false;
  if (window.marked) {
    summaryText.innerHTML = window.marked.parse(timelineState.summary);
  } else {
    summaryText.textContent = timelineState.summary;
  }
};

const renderFiles = () => {
  if (!filesPanel || !fileList) return;
  if (!timelineState.files.length) {
    filesPanel.hidden = true;
    fileList.innerHTML = "";
    return;
  }
  filesPanel.hidden = false;
  fileList.innerHTML = "";
  timelineState.files.forEach((file) => {
    const row = document.createElement("div");
    row.className = "file-item";
    const name = document.createElement("span");
    name.textContent = file.filename;
    row.appendChild(name);

    const actions = document.createElement("div");
    actions.className = "file-actions";
    const previewBtn = document.createElement("button");
    previewBtn.className = "ghost";
    previewBtn.type = "button";
    previewBtn.textContent = "预览";
    previewBtn.addEventListener("click", () => {
      const target = withApiToken(file.preview_url || `/preview/${timelineState.requestId}/${file.filename}`);
      window.open(target, "_blank", "noopener");
    });
    const downloadBtn = document.createElement("button");
    downloadBtn.className = "solid";
    downloadBtn.type = "button";
    downloadBtn.textContent = "下载";
    downloadBtn.addEventListener("click", () => {
      const target = withApiToken(file.download_url || `/download/${timelineState.requestId}/${file.filename}`);
      window.open(target, "_blank", "noopener");
    });
    actions.append(previewBtn, downloadBtn);
    row.appendChild(actions);
    fileList.appendChild(row);
  });
};

const setTimelineFiles = (files = []) => {
  timelineState.files = files;
  renderFiles();
  if (currentRequestId) {
    updateTaskFilesData(currentRequestId, files);
  }
  updateDownloadAllButton();
};

resetTimelineState();
hydrateHistoryPane();
updateDownloadAllButton();

const buildTags = (metadata) => {
  if (!metadata?.files?.length) return [];
  return metadata.files.map((file) => `输出文件 · ${file}`);
};

const formatToolInvocations = (tools = [], files = []) => {
  if ((!Array.isArray(tools) || !tools.length) && (!Array.isArray(files) || !files.length)) {
    return "";
  }
  const items = tools
    .map((tool) => {
      const name = tool?.name || "工具";
      const target = tool?.target ? ` · ${tool.target}` : "";
      const details = tool?.details ? `（${tool.details}）` : "";
      return `<li>${name}${target}${details}</li>`;
    })
    .join("");
  const fileLabels = (files || [])
    .map((file) => `<li class="file-chip">输出文件 · ${file}</li>`)
    .join("");
  return `<ul class="tool-list">${items}${fileLabels}</ul>`;
};

const buildKnowledgeList = (metadata) => {
  if (Array.isArray(metadata?.vector_hits) && metadata.vector_hits.length) {
    return metadata.vector_hits;
  }
  if (typeof metadata?.vector_notes === "string" && metadata.vector_notes.trim()) {
    return metadata.vector_notes
      .split("\n")
      .map((line) => line.replace(/^-\\s*/, "").trim())
      .filter(Boolean);
  }
  return [];
};

const formatAnnotation = (metadata, label) => {
  if (label === "模型思考" && metadata?.action_items?.length) {
    return metadata.action_items.join("；");
  }
  if (metadata?.notes?.length) {
    return metadata.notes.join("；");
  }
  if (metadata?.error) {
    return metadata.error;
  }
  return "";
};

const resolveLabel = (event) => {
  if (event.step.endsWith("_thinking")) return "模型思考";
  if (event.step.endsWith("_reflection")) return "下一步计划";
  if (event.step.endsWith("_redo")) return "重试提醒";
  if (event.status === "started") return "步骤开始";
  if (event.status === "succeeded") return "步骤完成";
  if (event.status === "failed") return "步骤失败";
  return "执行记录";
};

const stopEventStream = () => {
  shuttingDownStream = true;
  if (eventStreamAbort) {
    eventStreamAbort.abort();
    eventStreamAbort = null;
  }
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  reconnectAttempts = 0;
  sseBuffer = "";
};

const resetUI = () => {
  resetState();
  stopEventStream();
  currentRequestId = null;
  clearAlert();
};

const ensureStep = (step, appendToEnd = true) => {
  if (!step?.id) return;
  if (state.stepById.has(step.id)) {
    const stored = state.stepById.get(step.id);
    stored.title = step.title || stored.title;
    return;
  }

  const resolvedTitle = step.title || STEP_TITLE_MAP[step.id] || step.id;
  const stepEntry = {
    id: step.id,
    title: resolvedTitle,
    status: "pending",
  };
  state.steps.push(stepEntry);
  state.stepById.set(step.id, stepEntry);
  if (appendToEnd) {
    state.stepOrder.push(step.id);
  } else {
    state.stepOrder.unshift(step.id);
  }
};

const renderSteps = () => {
  planList.innerHTML = "";
  const total = state.steps.length;
  const completed = state.steps.filter((step) => step.status === "done").length;
  planProgress.textContent = `${completed} / ${total || 0}`;
  const percent = total ? Math.min(100, Math.max(0, (completed / total) * 100)) : 0;
  progressBar.style.width = `${percent}%`;

  state.stepOrder.forEach((stepId, index) => {
      const step = state.stepById.get(stepId);
    if (!step) return;
    const item = document.createElement("li");
    item.className = "step-item";

    const indexSpan = document.createElement("span");
    indexSpan.className = "step-index";
    indexSpan.textContent = index + 1;

    const title = document.createElement("strong");
    title.textContent = step.title;

    item.append(indexSpan, title);

    const badge = document.createElement("span");
    badge.classList.add("status-badge");
    if (STATUS_BADGE_CLASS[step.status]) {
      badge.classList.add(STATUS_BADGE_CLASS[step.status]);
    }
    badge.textContent =
      step.status === "done"
        ? "已完成"
        : step.status === "running"
        ? "进行中"
        : step.status === "failed"
        ? "失败"
        : step.status === "retry"
        ? "重试中"
        : "待执行";
    item.appendChild(badge);
    planList.appendChild(item);
  });
};

const updateStepStatus = (stepId, status) => {
  const step = state.stepById.get(stepId);
  if (!step) return;
  step.status = status;
  renderSteps();
};

const handleStepExecutionEvent = (payload) => {
  const { step_id: stepId, step, status, message, metadata = {}, timestamp } = payload;
  if (!stepId) return;

  const incomingTitle = typeof metadata?.title === "string" && metadata.title.trim() ? metadata.title : undefined;
  ensureStep({ id: stepId, title: incomingTitle });
  ensureTimelineStep({ id: stepId, title: incomingTitle });

  if (step.endsWith("_redo")) {
    if (status === "started") {
      updateStepStatus(stepId, "retry");
    } else if (status === "failed") {
      updateStepStatus(stepId, "failed");
    }
  } else if (!step.endsWith("_thinking") && !step.endsWith("_reflection")) {
    if (status === "started") {
      updateStepStatus(stepId, "running");
      setTimelineStatus("任务执行中");
    } else if (status === "failed") {
      updateStepStatus(stepId, "failed");
    } else if (status === "succeeded") {
      updateStepStatus(stepId, "done");
    }
  }

  const label = resolveLabel(payload);
  let labelOverride = null;
  let content = "";
  if (step.endsWith("_thinking") || payload.phase === "thinking") {
    content = message || metadata.outline || "模型正在思考下一步。";
  } else if (metadata.insight) {
    content = metadata.insight;
  } else if (metadata.files?.length) {
    const quoted = metadata.files.map((file) => `《${file}》`);
    content = `生成${quoted.join("、")}。`;
  } else if (metadata.error) {
    content = metadata.error;
  } else {
    content = message || "";
  }

  const shouldSkipMainEntry = !labelOverride && label === "步骤完成";
  if (!shouldSkipMainEntry) {
    const entry = {
      label: labelOverride || label,
      content: content || "",
      annotation: formatAnnotation(metadata, label),
      knowledge: buildKnowledgeList(metadata),
      tags: buildTags(metadata),
      timestamp: formatTimestamp(toLocalTime(timestamp)),
      inline: label === "步骤开始",
    };
    addTimelineEntry(stepId, entry);
  }

  if (Array.isArray(metadata.tools_used) && metadata.tools_used.length) {
    const fileLabels = Array.isArray(metadata.files) ? metadata.files : [];
    const toolContent = formatToolInvocations(metadata.tools_used, fileLabels);
    if (toolContent) {
      addTimelineEntry(stepId, {
        label: "工具调用",
        content: toolContent,
        timestamp: formatTimestamp(toLocalTime(timestamp)),
      });
    }
  }
};

const handlePlanGeneration = (payload) => {
  const { metadata, message, timestamp } = payload;
  if (Array.isArray(metadata?.plan)) {
    metadata.plan.forEach((step) => {
      ensureStep(step);
      ensureTimelineStep(step);
    });
    renderSteps();
  }
  setTimelineStatus("规划完成");
  pushTicker(message || "已生成行动计划。", timestamp);
};

const handlePlanAdjustment = (payload) => {
  const steps = payload.metadata?.added_steps;
  if (Array.isArray(steps)) {
    steps.forEach((step) => {
      ensureStep(step);
      ensureTimelineStep(step);
    });
    renderSteps();
  }
  pushTicker(payload.message || "行动计划已调整。", payload.timestamp);
};

const handleContextDecision = (payload) => {
  const rationale = payload.metadata?.rationale || payload.message;
  if (rationale) {
    pushTicker(`策略评估：${rationale}`, payload.timestamp);
  }
};

const handleStepQaEvent = (payload) => {
  const stepId = payload.step_id;
  if (!stepId) return;
  ensureTimelineStep({ id: stepId });
  const metadata = payload.metadata || {};
  const decision = metadata.decision || "continue";
  const lines = [];
  if (metadata.notes) {
    lines.push(metadata.notes);
  }
  if (Array.isArray(metadata.missing_sections) && metadata.missing_sections.length) {
    lines.push(`缺失章节：${metadata.missing_sections.join("；")}`);
  }
  if (Array.isArray(metadata.redo_adjustments) && metadata.redo_adjustments.length) {
    lines.push(`补充事项：${metadata.redo_adjustments.join("；")}`);
  }
  if (Array.isArray(metadata.additional_steps) && metadata.additional_steps.length) {
    lines.push(`新增步骤：${metadata.additional_steps.map((step) => step.title || step.id).join("、")}`);
  }
  const entry = {
    label: "模型反思",
    content: lines.length ? lines.join("<br/>") : payload.message || "已完成质检。",
    tags: ["QA", decision].filter(Boolean),
    timestamp: formatTimestamp(toLocalTime(payload.timestamp)),
  };
  addTimelineEntry(stepId, entry);
  pushTicker(`质检(${stepId})：${decision}`, payload.timestamp);
};

const handleSummaryEvent = (payload) => {
  if (payload.message) {
    timelineState.summary = payload.message;
  } else if (payload.metadata?.summary) {
    timelineState.summary = payload.metadata.summary;
  }
  renderSummary();
  if (currentRequestId) {
    updateTaskSummaryData(currentRequestId, timelineState.summary);
  }
};

const fetchManifest = async (requestId, attempt = 0) => {
  try {
    const response = await fetch(`/manifest/${requestId}`, {
      headers: buildAuthHeaders(),
    });
    if (!response.ok) {
      if (response.status === 202 && attempt < 20) {
        setTimeout(() => fetchManifest(requestId, attempt + 1), 1500);
        return;
      }
      throw new Error(`获取文件清单失败：${response.status}`);
    }
    const manifest = await response.json();
    setTimelineFiles(manifest.files || []);
  } catch (error) {
    pushTicker(error.message, new Date().toISOString());
    showAlert(error.message, "error");
  } finally {
    submitBtn.disabled = false;
  }
};

const processEvent = (payload, isReplay = false) => {
  const { step, step_id: stepId } = payload;
  const requestId = payload.request_id;

  if (!isReplay && requestId) {
    recordTaskEvent(requestId, payload);
  }

  if (step === "warning") {
    const warningMessage = payload.message || "系统提醒";
    showAlert(warningMessage, "warning");
    pushTicker(`⚠️ ${warningMessage}`, payload.timestamp);
    return;
  }

  if (requestId && !isReplay) {
    if (step === "accepted") {
      updateTaskStatusValue(requestId, "running");
    } else if (step === "finalize") {
      updateTaskStatusValue(requestId, "done");
    } else if (step === "error") {
      updateTaskStatusValue(requestId, "failed");
    }
  }

  if (step === "plan_generation") {
    handlePlanGeneration(payload);
    return;
  }

  if (step === "plan_adjusted") {
    handlePlanAdjustment(payload);
    return;
  }

  if (step === "context_decision") {
    handleContextDecision(payload);
    return;
  }

  if (step?.endsWith("_qa")) {
    handleStepQaEvent(payload);
    return;
  }

  if (step === "safety_guard" || step === "completed") {
    pushTicker(payload.message || "执行状态更新", payload.timestamp);
    return;
  }

  if (step === "finalize") {
    streamCompleted = true;
    stopEventStream();
    setTimelineStatus("执行完成");
    pushTicker(payload.message || "结果已写入输出目录。", payload.timestamp);
    if (!isReplay) {
      fetchManifest(currentRequestId);
    } else if (currentRequestId) {
      const storedTask = findTaskById(currentRequestId);
      if (storedTask?.files) {
        setTimelineFiles(storedTask.files);
      }
    }
    clearAlert();
    resetPauseState();
    return;
  }

  if (step === "final_summary") {
    handleSummaryEvent(payload);
    return;
  }

  if (step === "error") {
    setTimelineStatus("执行失败");
    pushTicker(payload.message || "执行失败。", payload.timestamp);
    submitBtn.disabled = false;
    streamCompleted = true;
    stopEventStream();
    showAlert(payload.message || "执行失败，请检查输入后重试。", "error");
    resetPauseState();
    return;
  }

  if (stepId) {
    handleStepExecutionEvent(payload);
  } else if (payload.message) {
    pushTicker(payload.message, payload.timestamp);
  }
};

const scheduleReconnect = (reason = "事件流中断，准备重连") => {
  if (!currentRequestId || isPaused || streamCompleted) return;
  if (reconnectAttempts >= 5) {
    showAlert("事件流多次中断，请刷新页面或重新提交任务。", "error");
    return;
  }
  const delay = Math.min(15000, 1000 * 2 ** reconnectAttempts);
  reconnectAttempts += 1;
  pushTicker(`${reason}（第${reconnectAttempts}次）`, new Date().toISOString());
  reconnectTimer = setTimeout(() => startEventStream(currentRequestId), delay);
};

const startEventStream = async (requestId) => {
  if (streamCompleted) return;
  shuttingDownStream = false;
  streamCompleted = false;
  eventStreamAbort = new AbortController();
  try {
    const response = await fetch(`/events/${requestId}`, {
      headers: buildAuthHeaders(),
      signal: eventStreamAbort.signal,
    });
    if (!response.ok) {
      throw new Error(`事件流建立失败：${response.status}`);
    }
    reconnectAttempts = 0;
    const reader = response.body.getReader();
    const decoder = new TextDecoder("utf-8");
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      const textChunk = decoder.decode(value, { stream: true });
      const parsed = appendAndParseSseBuffer(sseBuffer, textChunk);
      sseBuffer = parsed.remainingBuffer;
      parsed.events.forEach((rawEvent) => {
        if (!rawEvent) return;
        try {
          const payload = JSON.parse(rawEvent);
          processEvent(payload);
        } catch (error) {
          pushTicker(`解析事件失败：${error.message}`, new Date().toISOString());
        }
      });
    }
    if (!shuttingDownStream) {
      scheduleReconnect("事件流已结束");
    }
  } catch (error) {
    if (eventStreamAbort?.signal?.aborted) return;
    pushTicker(`事件流异常：${error.message}`, new Date().toISOString());
    scheduleReconnect("事件流异常");
  } finally {
    eventStreamAbort = null;
  }
};

const handleEventStream = (requestId) => {
  stopEventStream();
  if (!requestId) return;
  startEventStream(requestId);
};

const submitRequest = async (payload) => {
  const body = new FormData();
  body.append("payload", JSON.stringify(payload));
  const file = documentInput.files?.[0];
  if (file) {
    body.append("document", file);
  }

  const headers = {};
  if (currentApiToken) headers["X-API-Key"] = currentApiToken;

  const response = await fetch("/generate-plan", {
    method: "POST",
    body,
    headers,
  });
  if (!response.ok) {
    throw new Error(`请求失败：${response.status}`);
  }
  return response.json();
};

planForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = collectFormData();
  if (!payload.product_name) {
    pushTicker("请填写产品名称。", new Date().toISOString());
    return;
  }

  pendingProductName = payload.product_name;
  resetUI();
  currentApiToken = apiTokenInput?.value?.trim() || "";
  submitBtn.disabled = true;

  try {
    const data = await submitRequest(payload);
    currentRequestId = data.request_id;
    registerTaskEntry(currentRequestId, pendingProductName || payload.product_name);
    pendingProductName = "";
    initializeTimelineForRequest(currentRequestId);
    showAlert("任务已受理，正在规划执行。", "success");
    pushTicker("任务已受理，开始执行智能规划。", new Date().toISOString());
    enablePauseButton();
    handleEventStream(currentRequestId);
  } catch (error) {
    submitBtn.disabled = false;
    pushTicker(error.message, new Date().toISOString());
    showAlert(error.message, "error");
  }
});

progressToggleBtn?.addEventListener("click", () => {
  setProgressCollapsed(!progressCollapsed);
});

pauseBtn?.addEventListener("click", () => {
  if (!currentRequestId) return;
  if (!isPaused) {
    isPaused = true;
    stopEventStream();
    setTimelineStatus("任务已暂停");
    pushTicker("任务已暂停，可稍后继续。", new Date().toISOString());
    pauseBtn.textContent = "继续任务";
  } else {
    isPaused = false;
    handleEventStream(currentRequestId);
    setTimelineStatus("任务执行中");
    pushTicker("已继续任务执行。", new Date().toISOString());
    pauseBtn.textContent = "暂停任务";
  }
});

resetBtn?.addEventListener("click", () => {
  planForm.reset();
  if (documentInput) documentInput.value = "";
  showAlert("表单已重置。", "info");
});

historyCollapseBtn?.addEventListener("click", () => {
  toggleHistoryCollapse();
});

historySearchInput?.addEventListener("input", (event) => {
  historyState.filter = event.target.value || "";
  renderHistoryList();
});

historyList?.addEventListener("click", (event) => {
  const deleteId = event.target?.dataset?.deleteId;
  if (deleteId) {
    event.stopPropagation();
    const task = findTaskById(deleteId);
    const label = task?.productName || deleteId;
    if (window.confirm(`确认删除任务「${label}」吗？`)) {
      removeHistoryTask(deleteId);
    }
    return;
  }
  const item = event.target.closest(".history-item");
  if (!item) return;
  const { id } = item.dataset;
  if (!id) return;
  loadTaskFromHistory(id);
});

newTaskBtn?.addEventListener("click", () => {
  window.open(window.location.href, "_blank", "noopener");
});

downloadAllBtn?.addEventListener("click", () => {
  if (!currentRequestId) return;
  const target = withApiToken(`/download-all/${currentRequestId}`);
  window.open(target, "_blank", "noopener");
});
