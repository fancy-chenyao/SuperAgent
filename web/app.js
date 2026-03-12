const statusIndicator = document.getElementById("statusIndicator");
const tabs = document.querySelectorAll(".tab");
const panels = document.querySelectorAll(".panel");

const userIdInput = document.getElementById("userId");
const workModeInput = document.getElementById("workMode");
const deepThinkingInput = document.getElementById("deepThinking");
const searchBeforeInput = document.getElementById("searchBefore");
const debugInput = document.getElementById("debugMode");
const workflowIdInput = document.getElementById("workflowId");
const messageInput = document.getElementById("message");

const runBtn = document.getElementById("runBtn");
const stopBtn = document.getElementById("stopBtn");
const clearOutputBtn = document.getElementById("clearOutput");
const autoScrollBtn = document.getElementById("autoScrollBtn");
const exportTxtBtn = document.getElementById("exportTxtBtn");

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
let flowSteps = [];
let activeStepIndex = -1;
const MAX_FLOW_STEPS = 40;
let autoScrollEnabled = true;
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
  flowSteps = [];
  activeStepIndex = -1;
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

const flashButton = (btn, text) => {
  const prevText = btn.textContent;
  btn.textContent = text;
  btn.disabled = true;
  setTimeout(() => {
    btn.textContent = prevText;
    btn.disabled = false;
  }, 1200);
};

const renderFlowSteps = () => {
  summaryFlow.innerHTML = "";
  const frag = document.createDocumentFragment();

  flowSteps.forEach((step, idx) => {
    if (idx > 0) {
      const arrow = document.createElement("span");
      arrow.className = "flow-arrow";
      arrow.textContent = "→";
      frag.appendChild(arrow);
    }
    const node = document.createElement("span");
    node.className = "flow-node";
    if (step.state === "active") node.classList.add("active");
    if (step.state === "done") node.classList.add("done");
    if (step.state === "new") node.classList.add("new");
    node.textContent = step.agent;
    frag.appendChild(node);
  });

  summaryFlow.appendChild(frag);
  summaryFlow.scrollLeft = summaryFlow.scrollWidth;
};

const finishActiveStep = () => {
  if (activeStepIndex < 0) return;
  if (flowSteps[activeStepIndex]) {
    flowSteps[activeStepIndex].state = "done";
  }
  activeStepIndex = -1;
};

const pushFlowStep = (agentName) => {
  finishActiveStep();
  flowSteps.push({ agent: agentName, state: "new" });
  activeStepIndex = flowSteps.length - 1;
  if (flowSteps.length > MAX_FLOW_STEPS) {
    const removeCount = flowSteps.length - MAX_FLOW_STEPS;
    flowSteps.splice(0, removeCount);
    activeStepIndex = activeStepIndex - removeCount;
    if (activeStepIndex < 0) activeStepIndex = -1;
  }
  renderFlowSteps();
  const current = flowSteps[activeStepIndex];
  if (current) {
    setTimeout(() => {
      if (flowSteps[activeStepIndex] === current && current.state === "new") {
        current.state = "active";
        renderFlowSteps();
      }
    }, 800);
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
  appendOutputImmediate(agentName, content);
};

const clearOutput = () => {
  streamOutput.innerHTML = "";
  outputBlocks = new Map();
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
    pushFlowStep(agentName);
    appendOutput("system", `\n[start_of_agent] ${agentName}\n`);
    return;
  }
  if (eventName === "end_of_agent") {
    const agentName = payload.data?.agent_name || payload.agent_name || "agent";
    finishActiveStep();
    renderFlowSteps();
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

  const payload = {
    user_id: userId,
    lang: "zh",
    workmode: workModeInput.value,
    messages: [{ role: "user", content: message }],
    debug: debugInput.checked,
    deep_thinking_mode: deepThinkingInput.checked,
    search_before_planning: searchBeforeInput.checked,
    coor_agents: null,
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

const exportOutputTxt = () => {
  if (!outputBlocks.size) {
    flashButton(exportTxtBtn, "Empty");
    return;
  }

  const parts = [];
  outputBlocks.forEach((pre, agentName) => {
    parts.push(`[${agentName}]`);
    parts.push(pre.textContent.trimEnd());
    parts.push("");
  });
  const text = parts.join("\n");
  const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
  link.href = url;
  link.download = `cooragent-output-${timestamp}.txt`;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
};

runBtn.addEventListener("click", runWorkflow);
stopBtn.addEventListener("click", stopWorkflow);
clearOutputBtn.addEventListener("click", clearOutput);
autoScrollBtn.addEventListener("click", toggleAutoScroll);
exportTxtBtn.addEventListener("click", exportOutputTxt);

refreshAgentsBtn.addEventListener("click", fetchAgents);
refreshToolsBtn.addEventListener("click", fetchTools);
refreshWorkflowsBtn.addEventListener("click", fetchWorkflows);

updateAutoScrollBtn();
setStatus("Ready", true);
