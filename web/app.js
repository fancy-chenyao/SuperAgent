const statusIndicator = document.getElementById("statusIndicator");
const tabs = document.querySelectorAll(".tab");
const panels = document.querySelectorAll(".panel");

const userIdInput = document.getElementById("userId");
const langInput = document.getElementById("lang");
const taskTypeInput = document.getElementById("taskType");
const workModeInput = document.getElementById("workMode");
const deepThinkingInput = document.getElementById("deepThinking");
const searchBeforeInput = document.getElementById("searchBefore");
const debugInput = document.getElementById("debugMode");
const coorAgentsInput = document.getElementById("coorAgents");
const workflowIdInput = document.getElementById("workflowId");
const messageInput = document.getElementById("message");

const runBtn = document.getElementById("runBtn");
const stopBtn = document.getElementById("stopBtn");
const clearOutputBtn = document.getElementById("clearOutput");
const autoScrollBtn = document.getElementById("autoScrollBtn");
const pauseBtn = document.getElementById("pauseBtn");
const copyOutputBtn = document.getElementById("copyOutputBtn");

const streamOutput = document.getElementById("streamOutput");
const summaryFlow = document.getElementById("summaryFlow");
const summaryHint = document.getElementById("summaryHint");

const refreshAgentsBtn = document.getElementById("refreshAgents");
const refreshToolsBtn = document.getElementById("refreshTools");
const refreshWorkflowsBtn = document.getElementById("refreshWorkflows");

const agentsList = document.getElementById("agentsList");
const toolsList = document.getElementById("toolsList");
const workflowsList = document.getElementById("workflowsList");
const workflowDetail = document.getElementById("workflowDetail");
const mermaidContainer = document.getElementById("mermaidContainer");

let currentAbortController = null;
let outputBlocks = new Map();
let flowNodeMap = new Map();
let autoScrollEnabled = true;
let isPaused = false;
let pendingOutputQueue = [];
let selectedWorkflowId = null;

mermaid.initialize({ startOnLoad: false, theme: "default" });

const setStatus = (text, active = true) => {
  statusIndicator.querySelector(".label").textContent = text;
  statusIndicator.querySelector(".dot").style.background = active ? "#4be3ac" : "#ff6a6a";
  statusIndicator.querySelector(".dot").style.boxShadow = active
    ? "0 0 12px rgba(75, 227, 172, 0.7)"
    : "0 0 12px rgba(255, 106, 106, 0.7)";
};

const resetSummary = () => {
  summaryFlow.innerHTML = "";
  summaryHint.classList.add("hidden");
  summaryHint.classList.remove("error");
  summaryHint.textContent = "";
  flowNodeMap = new Map();
};

const showSummaryHint = (text, isError = false) => {
  summaryHint.textContent = text;
  summaryHint.classList.remove("hidden");
  if (isError) {
    summaryHint.classList.add("error");
  } else {
    summaryHint.classList.remove("error");
  }
};

const updateAutoScrollBtn = () => {
  autoScrollBtn.textContent = autoScrollEnabled ? "Auto-scroll: On" : "Auto-scroll: Off";
  autoScrollBtn.classList.toggle("active", autoScrollEnabled);
};

const updatePauseBtn = () => {
  pauseBtn.textContent = isPaused ? "Resume" : "Pause";
  pauseBtn.classList.toggle("active", isPaused);
};

const flashButton = (btn, text) => {
  const prevText = btn.textContent;
  btn.textContent = text;
  btn.disabled = true;
  setTimeout(() => {
    btn.textContent = prevText;
    btn.disabled = false;
  }, 1200);
};

const ensureFlowNode = (agentName) => {
  if (flowNodeMap.has(agentName)) return flowNodeMap.get(agentName);
  const node = document.createElement("span");
  node.className = "flow-node new";
  node.textContent = agentName;
  if (summaryFlow.children.length > 0) {
    const arrow = document.createElement("span");
    arrow.className = "flow-arrow";
    arrow.textContent = "->";
    summaryFlow.appendChild(arrow);
  }
  summaryFlow.appendChild(node);
  flowNodeMap.set(agentName, node);
  setTimeout(() => node.classList.remove("new"), 800);
  return node;
};

const setActiveFlowNode = (agentName) => {
  flowNodeMap.forEach((node) => node.classList.remove("active"));
  const node = ensureFlowNode(agentName);
  node.classList.add("active");
};

const markFlowNodeDone = (agentName) => {
  const node = flowNodeMap.get(agentName);
  if (node) {
    node.classList.remove("active");
    node.classList.add("done");
  }
};

const switchTab = (tabId) => {
  tabs.forEach((tab) => tab.classList.toggle("active", tab.dataset.tab === tabId));
  panels.forEach((panel) => panel.classList.toggle("active", panel.id === `panel-${tabId}`));
};

tabs.forEach((tab) => {
  tab.addEventListener("click", () => switchTab(tab.dataset.tab));
});

const ensureOutputBlock = (agentName) => {
  if (!outputBlocks.has(agentName)) {
    const block = document.createElement("div");
    block.className = "output-block";
    const title = document.createElement("h4");
    title.textContent = agentName;
    const pre = document.createElement("pre");
    pre.textContent = "";
    block.appendChild(title);
    block.appendChild(pre);
    streamOutput.appendChild(block);
    outputBlocks.set(agentName, pre);
  }
  return outputBlocks.get(agentName);
};

const appendOutputImmediate = (agentName, content) => {
  const target = ensureOutputBlock(agentName || "system");
  target.textContent += content;
  if (autoScrollEnabled) {
    streamOutput.scrollTop = streamOutput.scrollHeight;
  }
};

const appendOutput = (agentName, content) => {
  if (isPaused) {
    pendingOutputQueue.push({ agentName: agentName || "system", content });
    return;
  }
  appendOutputImmediate(agentName, content);
};

const flushPendingOutput = () => {
  if (!pendingOutputQueue.length) return;
  const items = pendingOutputQueue.slice();
  pendingOutputQueue = [];
  items.forEach((item) => appendOutputImmediate(item.agentName, item.content));
};

const clearOutput = () => {
  streamOutput.innerHTML = "";
  outputBlocks = new Map();
  pendingOutputQueue = [];
};

const parseSse = (buffer, onEvent) => {
  const chunks = buffer.split("\n\n");
  const remainder = chunks.pop();
  chunks.forEach((chunk) => {
    const lines = chunk.split("\n");
    let eventName = "message";
    let dataLines = [];
    lines.forEach((line) => {
      if (line.startsWith("event:")) {
        eventName = line.replace("event:", "").trim();
      } else if (line.startsWith("data:")) {
        dataLines.push(line.replace("data:", "").trim());
      }
    });
    const dataText = dataLines.join("\n");
    if (dataText) {
      try {
        onEvent(eventName, JSON.parse(dataText));
      } catch (err) {
        onEvent(eventName, { raw: dataText });
      }
    }
  });
  return remainder || "";
};

const handleEvent = (eventName, payload) => {
  if (eventName === "messages") {
    const agentName = payload.agent_name || payload.data?.agent_name || payload.data?.tool || "assistant";
    const content = payload.data?.delta?.content || payload.data?.message || payload.raw || "";
    appendOutput(agentName, content);
    return;
  }
  if (eventName === "start_of_workflow") {
    resetSummary();
    appendOutput("system", `\n[workflow] ${payload.data?.workflow_id || ""}\n`);
    return;
  }
  if (eventName === "start_of_agent") {
    const agentName = payload.data?.agent_name || payload.agent_name || "agent";
    setActiveFlowNode(agentName);
    appendOutput("system", `\n[start_of_agent] ${agentName}\n`);
    return;
  }
  if (eventName === "end_of_agent") {
    const agentName = payload.data?.agent_name || payload.agent_name || "agent";
    markFlowNodeDone(agentName);
    appendOutput("system", `\n[end_of_agent] ${agentName}\n`);
    return;
  }
  if (eventName === "end_of_workflow") {
    showSummaryHint("Workflow completed.");
    appendOutput("system", "\n[workflow] completed\n");
    return;
  }
  if (eventName === "new_agent_created") {
    appendOutput("system", `\n[new agent] ${payload.data?.new_agent_name || ""}\n`);
    return;
  }
  if (eventName === "error") {
    showSummaryHint("Workflow error.", true);
    appendOutput("system", `\n[error] ${payload.data?.error || payload.raw || "unknown error"}\n`);
    return;
  }
  appendOutput("system", `\n[${eventName}] ${JSON.stringify(payload)}\n`);
};

const runWorkflow = async () => {
  const userId = userIdInput.value.trim();
  if (!userId) {
    setStatus("User ID required", false);
    return;
  }
  const message = messageInput.value.trim();
  if (!message) {
    setStatus("Message required", false);
    return;
  }

  setStatus("Running", true);
  resetSummary();
  runBtn.disabled = true;
  stopBtn.disabled = false;

  const coorAgents = coorAgentsInput.value
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);

  const payload = {
    user_id: userId,
    lang: langInput.value,
    task_type: taskTypeInput.value,
    workmode: workModeInput.value,
    messages: [{ role: "user", content: message }],
    debug: debugInput.checked,
    deep_thinking_mode: deepThinkingInput.checked,
    search_before_planning: searchBeforeInput.checked,
    coor_agents: coorAgents.length ? coorAgents : null,
    workflow_id: workflowIdInput.value.trim() || null,
  };

  currentAbortController = new AbortController();
  try {
    const response = await fetch("/api/workflows/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: currentAbortController.signal,
    });

    if (!response.ok || !response.body) {
      throw new Error(`HTTP ${response.status}`);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      buffer = parseSse(buffer, handleEvent);
    }
  } catch (err) {
    appendOutput("system", `\n[error] ${err.message || err}\n`);
    setStatus("Error", false);
    showSummaryHint("Workflow error.", true);
  } finally {
    runBtn.disabled = false;
    stopBtn.disabled = true;
    currentAbortController = null;
  }
};

const stopWorkflow = () => {
  if (currentAbortController) {
    currentAbortController.abort();
    setStatus("Stopped", false);
    showSummaryHint("Workflow stopped.");
  }
};

const createStateCard = (text, variant = "info") => {
  const card = document.createElement("div");
  card.className = `card state ${variant}`;
  card.textContent = text;
  return card;
};

const setListState = (container, text, variant) => {
  container.textContent = "";
  container.appendChild(createStateCard(text, variant));
};

const fetchAgents = async () => {
  setListState(agentsList, "Loading...", "loading");
  try {
    const userId = userIdInput.value.trim();
    const [defaultRes, userRes] = await Promise.all([
      fetch("/api/agents/default"),
      userId ? fetch(`/api/agents?user_id=${encodeURIComponent(userId)}`) : Promise.resolve(null),
    ]);

    if (!defaultRes.ok || (userRes && !userRes.ok)) {
      throw new Error("request failed");
    }

    const defaults = await defaultRes.json();
    const users = userRes ? await userRes.json() : [];

    const combined = [...defaults, ...users];
    if (!combined.length) {
      setListState(agentsList, "No agents found.", "empty");
      return;
    }

    agentsList.textContent = "";
    combined.forEach((agent) => {
      const card = document.createElement("div");
      card.className = "card";
      const title = document.createElement("h4");
      title.textContent = agent.agent_name;
      const desc = document.createElement("p");
      desc.textContent = agent.description || "";
      card.appendChild(title);
      card.appendChild(desc);
      agentsList.appendChild(card);
    });
  } catch (err) {
    setListState(agentsList, "Failed to load agents.", "error");
  }
};

const fetchTools = async () => {
  setListState(toolsList, "Loading...", "loading");
  try {
    const res = await fetch("/api/tools");
    if (!res.ok) {
      throw new Error("request failed");
    }
    const tools = await res.json();
    if (!tools.length) {
      setListState(toolsList, "No tools found.", "empty");
      return;
    }
    toolsList.textContent = "";
    tools.forEach((tool) => {
      const card = document.createElement("div");
      card.className = "card";
      const title = document.createElement("h4");
      title.textContent = tool.name;
      const desc = document.createElement("p");
      desc.textContent = tool.description || "";
      card.appendChild(title);
      card.appendChild(desc);
      toolsList.appendChild(card);
    });
  } catch (err) {
    setListState(toolsList, "Failed to load tools.", "error");
  }
};

const fetchWorkflows = async () => {
  const userId = userIdInput.value.trim();
  if (!userId) {
    setListState(workflowsList, "请先输入 user_id", "empty");
    return;
  }

  setListState(workflowsList, "Loading...", "loading");
  try {
    const res = await fetch(`/api/workflows?user_id=${encodeURIComponent(userId)}`);
    if (!res.ok) {
      throw new Error("request failed");
    }
    const workflows = await res.json();
    if (!workflows.length) {
      setListState(workflowsList, "No workflows found.", "empty");
      return;
    }

    const sortedWorkflows = [...workflows].sort((a, b) => {
      const aTs = getWorkflowTimestamp(a);
      const bTs = getWorkflowTimestamp(b);
      if (aTs === null && bTs === null) return 0;
      if (aTs === null) return 1;
      if (bTs === null) return -1;
      return bTs - aTs;
    });

    workflowsList.textContent = "";
    sortedWorkflows.forEach((wf) => {
      const title = formatWorkflowTitle(wf);
      const item = document.createElement("div");
      item.className = "workflow-item";
      item.dataset.workflowId = wf.workflow_id;
      item.setAttribute("role", "button");
      item.tabIndex = 0;

      const titleEl = document.createElement("strong");
      titleEl.textContent = title;

      const meta = document.createElement("span");
      meta.textContent = `ID: ${wf.workflow_id} · lap: ${wf.lap} · version: ${wf.version}`;

      item.appendChild(titleEl);
      item.appendChild(meta);

      item.addEventListener("click", () => selectWorkflow(wf.workflow_id));
      item.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          selectWorkflow(wf.workflow_id);
        }
      });

      if (wf.workflow_id === selectedWorkflowId) {
        item.classList.add("active");
      }

      workflowsList.appendChild(item);
    });
  } catch (err) {
    setListState(workflowsList, "Failed to load workflows.", "error");
  }
};

const formatWorkflowTitle = (workflow) => {
  const taskName = getWorkflowTaskName(workflow);
  const date = getWorkflowDate(workflow);
  if (date && taskName) return `${date} · ${taskName}`;
  if (date) return date;
  if (taskName) return taskName;
  return workflow.workflow_id || "workflow";
};

const getWorkflowTaskName = (workflow) => {
  const messages = Array.isArray(workflow.user_input_messages)
    ? workflow.user_input_messages
    : [];
  const userMessage = messages.find((msg) => msg && msg.role === "user" && msg.content);
  const content = userMessage ? String(userMessage.content).trim() : "";
  if (!content) return "";
  const maxLength = 48;
  if (content.length <= maxLength) return content;
  return `${content.slice(0, maxLength)}...`;
};

const getWorkflowDate = (workflow) => {
  const messages = Array.isArray(workflow.user_input_messages)
    ? workflow.user_input_messages
    : [];
  const withTimestamp = messages.find((msg) => msg && msg.timestamp);
  const timestamp = withTimestamp ? String(withTimestamp.timestamp) : "";
  if (!timestamp) return "";
  if (timestamp.includes("T")) {
    return timestamp.split("T")[0];
  }
  return timestamp.slice(0, 10);
};

const getWorkflowTimestamp = (workflow) => {
  const messages = Array.isArray(workflow.user_input_messages)
    ? workflow.user_input_messages
    : [];
  let latest = null;
  messages.forEach((msg) => {
    if (!msg || !msg.timestamp) return;
    const ts = Date.parse(String(msg.timestamp));
    if (Number.isNaN(ts)) return;
    if (latest === null || ts > latest) {
      latest = ts;
    }
  });
  return latest;
};

const selectWorkflow = async (workflowId) => {
  selectedWorkflowId = workflowId;
  document.querySelectorAll(".workflow-item").forEach((item) => {
    item.classList.toggle("active", item.dataset.workflowId === workflowId);
  });

  workflowDetail.textContent = "Loading...";
  mermaidContainer.textContent = "Loading...";

  const detailRes = await fetch(`/api/workflows/${encodeURIComponent(workflowId)}`);
  if (detailRes.ok) {
    const detail = await detailRes.json();
    workflowDetail.textContent = JSON.stringify(detail, null, 2);
  } else {
    workflowDetail.textContent = "加载失败";
  }

  const mermaidRes = await fetch(`/api/workflows/${encodeURIComponent(workflowId)}/mermaid`);
  if (mermaidRes.ok) {
    const code = await mermaidRes.text();
    mermaidContainer.textContent = "";
    const pre = document.createElement("pre");
    pre.className = "mermaid";
    pre.textContent = code;
    mermaidContainer.appendChild(pre);
    try {
      mermaid.run({ nodes: mermaidContainer.querySelectorAll(".mermaid") });
    } catch (err) {
      mermaidContainer.textContent = "Mermaid render failed.";
    }
  } else {
    mermaidContainer.textContent = "暂无可视化";
  }
};

const toggleAutoScroll = () => {
  autoScrollEnabled = !autoScrollEnabled;
  updateAutoScrollBtn();
  if (autoScrollEnabled) {
    streamOutput.scrollTop = streamOutput.scrollHeight;
  }
};

const togglePause = () => {
  isPaused = !isPaused;
  updatePauseBtn();
  streamOutput.classList.toggle("paused", isPaused);
  if (!isPaused) {
    flushPendingOutput();
  }
};

const copyAllOutput = async () => {
  if (!outputBlocks.size) {
    flashButton(copyOutputBtn, "Empty");
    return;
  }

  const parts = [];
  outputBlocks.forEach((pre, agentName) => {
    parts.push(`[${agentName}]`);
    parts.push(pre.textContent.trimEnd());
    parts.push("");
  });
  const text = parts.join("\n");

  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
    } else {
      const textarea = document.createElement("textarea");
      textarea.value = text;
      textarea.setAttribute("readonly", "");
      textarea.style.position = "absolute";
      textarea.style.left = "-9999px";
      document.body.appendChild(textarea);
      textarea.select();
      document.execCommand("copy");
      document.body.removeChild(textarea);
    }
    flashButton(copyOutputBtn, "Copied");
  } catch (err) {
    flashButton(copyOutputBtn, "Copy failed");
  }
};

runBtn.addEventListener("click", runWorkflow);
stopBtn.addEventListener("click", stopWorkflow);
clearOutputBtn.addEventListener("click", clearOutput);
autoScrollBtn.addEventListener("click", toggleAutoScroll);
pauseBtn.addEventListener("click", togglePause);
copyOutputBtn.addEventListener("click", copyAllOutput);

refreshAgentsBtn.addEventListener("click", fetchAgents);
refreshToolsBtn.addEventListener("click", fetchTools);
refreshWorkflowsBtn.addEventListener("click", fetchWorkflows);

updateAutoScrollBtn();
updatePauseBtn();
setStatus("Ready", true);
