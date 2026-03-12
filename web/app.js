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
const agentsSearchInput = document.getElementById("agentsSearch");
const agentsSearchBtn = document.getElementById("agentsSearchBtn");
const agentFilterButtons = document.querySelectorAll(".agent-filter");
const coorCount = document.getElementById("coorCount");
const clearCoorBtn = document.getElementById("clearCoorBtn");
const healthCheckSelectedBtn = document.getElementById("healthCheckSelected");
const agentDetail = document.getElementById("agentDetail");
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
let selectedAgentName = null;
let selectedCoorAgents = new Set();
let latestAgents = [];
let agentFilter = "all";
let agentHealth = {};
let agentStats = {};

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
    coor_agents: selectedCoorAgents.size ? Array.from(selectedCoorAgents) : null,
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

const updateCoorCount = () => {
  if (!coorCount) return;
  coorCount.textContent = `已选协同代理: ${selectedCoorAgents.size}`;
  if (clearCoorBtn) {
    clearCoorBtn.disabled = selectedCoorAgents.size === 0;
  }
  if (healthCheckSelectedBtn) {
    healthCheckSelectedBtn.disabled = selectedCoorAgents.size === 0;
  }
};

const getMatchValue = () => (agentsSearchInput ? agentsSearchInput.value.trim() : "");

const buildAgentsUrl = (userId, match) => {
  let url = `/api/agents?user_id=${encodeURIComponent(userId)}`;
  if (match) {
    url += `&match=${encodeURIComponent(match)}`;
  }
  return url;
};

const buildHealthUrl = (userId, agentNames = []) => {
  const params = new URLSearchParams();
  if (userId) params.set("user_id", userId);
  params.set("include_share", "true");
  if (agentNames.length) params.set("agent_names", agentNames.join(","));
  return `/api/agents/health?${params.toString()}`;
};

const buildStatsUrl = (userId) => {
  const params = new URLSearchParams();
  if (userId) params.set("user_id", userId);
  params.set("include_share", "true");
  return `/api/agents/stats?${params.toString()}`;
};

const formatDate = (iso) => {
  if (!iso) return "";
  if (iso.includes("T")) return iso.split("T")[0];
  return iso.slice(0, 10);
};

const createTag = (text, variant = "") => {
  const tag = document.createElement("span");
  tag.className = `tag ${variant}`.trim();
  tag.textContent = text;
  return tag;
};

const setAgentDetailEmpty = (text) => {
  if (!agentDetail) return;
  agentDetail.textContent = "";
  const empty = document.createElement("div");
  empty.className = "agent-detail-empty";
  empty.textContent = text;
  agentDetail.appendChild(empty);
};

const renderAgentDetail = (agent) => {
  if (!agentDetail) return;
  if (!agent) {
    setAgentDetailEmpty("选择一个 Agent 查看详情");
    return;
  }

  agentDetail.textContent = "";
  const title = document.createElement("h3");
  title.textContent = agent.agent_name || "agent";

  const sub = document.createElement("div");
  sub.className = "agent-sub";
  const userLabel = agent.user_id === "share" ? "default" : "user";
  const sourceLabel = agent.source ? ` · ${agent.source}` : "";
  const nick = agent.nick_name ? `${agent.nick_name} · ` : "";
  sub.textContent = `${nick}${userLabel}${sourceLabel}`;

  const tagRow = document.createElement("div");
  tagRow.className = "tag-row";
  if (agent.llm_type) {
    tagRow.appendChild(createTag(`llm: ${agent.llm_type}`, "accent"));
  }
  if (agent.source) {
    tagRow.appendChild(createTag(`source: ${agent.source}`, agent.source === "remote" ? "warn" : ""));
  }

  const descTitle = document.createElement("h4");
  descTitle.textContent = "描述";
  const desc = document.createElement("p");
  desc.textContent = agent.description || "暂无描述";

  const toolsTitle = document.createElement("h4");
  toolsTitle.textContent = "Tools";
  const toolsRow = document.createElement("div");
  toolsRow.className = "tag-row";
  const tools = Array.isArray(agent.selected_tools) ? agent.selected_tools : [];
  if (tools.length) {
    tools.forEach((tool) => {
      const name = tool?.name || "";
      if (name) toolsRow.appendChild(createTag(name, "accent"));
    });
  } else {
    const emptyTools = document.createElement("p");
    emptyTools.textContent = "无";
    toolsRow.appendChild(emptyTools);
  }

  const healthTitle = document.createElement("h4");
  healthTitle.textContent = "Health";
  const health = agentHealth[agent.agent_name] || {};
  const healthRow = document.createElement("p");
  const healthParts = [];
  if (health.status) healthParts.push(`status: ${health.status}`);
  if (health.latency_ms !== null && health.latency_ms !== undefined) {
    healthParts.push(`latency: ${health.latency_ms}ms`);
  }
  if (health.error) healthParts.push(`error: ${health.error}`);
  healthRow.textContent = healthParts.length ? healthParts.join(" · ") : "n/a";

  const statsTitle = document.createElement("h4");
  statsTitle.textContent = "Usage";
  const stats = agentStats[agent.agent_name] || {};
  const statsRow = document.createElement("p");
  const statsParts = [];
  if (stats.runs !== undefined) statsParts.push(`runs: ${stats.runs}`);
  if (stats.last_used) statsParts.push(`last: ${stats.last_used}`);
  statsRow.textContent = statsParts.length ? statsParts.join(" · ") : "n/a";

  const endpointTitle = document.createElement("h4");
  endpointTitle.textContent = "Endpoint";
  const endpoint = document.createElement("p");
  endpoint.textContent = agent.endpoint || "n/a";

  const mcpTitle = document.createElement("h4");
  mcpTitle.textContent = "MCP Config";
  const mcpPre = document.createElement("pre");
  mcpPre.className = "code-block compact";
  mcpPre.textContent = JSON.stringify(agent.mcp_config || agent.mcp_servers || null, null, 2);

  const promptTitle = document.createElement("h4");
  promptTitle.textContent = "Prompt";
  const promptPre = document.createElement("pre");
  promptPre.className = "code-block compact";
  promptPre.textContent = agent.prompt || "";

  agentDetail.appendChild(title);
  agentDetail.appendChild(sub);
  agentDetail.appendChild(tagRow);
  agentDetail.appendChild(descTitle);
  agentDetail.appendChild(desc);
  agentDetail.appendChild(toolsTitle);
  agentDetail.appendChild(toolsRow);
  agentDetail.appendChild(healthTitle);
  agentDetail.appendChild(healthRow);
  agentDetail.appendChild(statsTitle);
  agentDetail.appendChild(statsRow);
  agentDetail.appendChild(endpointTitle);
  agentDetail.appendChild(endpoint);
  agentDetail.appendChild(mcpTitle);
  agentDetail.appendChild(mcpPre);
  agentDetail.appendChild(promptTitle);
  agentDetail.appendChild(promptPre);
};

const applyAgentFilter = (agents) => {
  if (!Array.isArray(agents)) return [];
  if (agentFilter === "default") {
    return agents.filter((agent) => agent.user_id === "share");
  }
  if (agentFilter === "user") {
    return agents.filter((agent) => agent.user_id !== "share");
  }
  if (agentFilter === "remote") {
    return agents.filter((agent) => agent.source === "remote");
  }
  return agents;
};

const renderAgents = (agents) => {
  const filtered = applyAgentFilter(agents);
  if (!filtered.length) {
    setListState(agentsList, "No agents match current filter.", "empty");
    return;
  }

  agentsList.textContent = "";
  filtered.forEach((agent) => {
    const card = document.createElement("div");
    card.className = "card agent-card";
    if (selectedCoorAgents.has(agent.agent_name)) {
      card.classList.add("selected");
    }

    const head = document.createElement("div");
    head.className = "agent-card-head";

    const titleWrap = document.createElement("div");
    const title = document.createElement("div");
    title.className = "agent-name";
    title.textContent = agent.agent_name || "agent";
    const sub = document.createElement("div");
    sub.className = "agent-sub";
    const userLabel = agent.user_id === "share" ? "default" : "user";
    const sourceLabel = agent.source ? ` · ${agent.source}` : "";
    const nick = agent.nick_name ? `${agent.nick_name} · ` : "";
    sub.textContent = `${nick}${userLabel}${sourceLabel}`;
    titleWrap.appendChild(title);
    titleWrap.appendChild(sub);

    const selectBtn = document.createElement("button");
    selectBtn.className = "select-toggle";
    const isSelected = selectedCoorAgents.has(agent.agent_name);
    selectBtn.textContent = isSelected ? "Selected" : "Select";
    if (isSelected) selectBtn.classList.add("active");
    selectBtn.addEventListener("click", (event) => {
      event.stopPropagation();
      if (selectedCoorAgents.has(agent.agent_name)) {
        selectedCoorAgents.delete(agent.agent_name);
      } else {
        selectedCoorAgents.add(agent.agent_name);
      }
      updateCoorCount();
      renderAgents(latestAgents);
      if (selectedAgentName) {
        const active = latestAgents.find((item) => item.agent_name === selectedAgentName);
        if (active) renderAgentDetail(active);
      }
    });

    head.appendChild(titleWrap);
    head.appendChild(selectBtn);

    const desc = document.createElement("p");
    desc.textContent = agent.description || "";

    const tags = document.createElement("div");
    tags.className = "tag-row";
    if (agent.llm_type) {
      tags.appendChild(createTag(`llm: ${agent.llm_type}`, "accent"));
    }
    if (agent.source) {
      tags.appendChild(createTag(`source: ${agent.source}`, agent.source === "remote" ? "warn" : ""));
    }

    const health = agentHealth[agent.agent_name];
    if (health && health.status) {
      const variant = health.status === "ok" ? "accent" : health.status === "fail" ? "warn" : "";
      tags.appendChild(createTag(`health: ${health.status}`, variant));
    }

    const stats = agentStats[agent.agent_name];
    if (stats) {
      if (stats.runs !== undefined) {
        tags.appendChild(createTag(`runs: ${stats.runs}`, "accent"));
      }
      if (stats.last_used) {
        tags.appendChild(createTag(`last: ${formatDate(stats.last_used)}`));
      }
    }

    const tools = Array.isArray(agent.selected_tools) ? agent.selected_tools : [];
    const toolNames = tools.map((tool) => tool?.name).filter(Boolean);
    toolNames.slice(0, 3).forEach((name) => tags.appendChild(createTag(name)));
    if (toolNames.length > 3) {
      tags.appendChild(createTag(`+${toolNames.length - 3}`));
    }

    card.appendChild(head);
    card.appendChild(desc);
    card.appendChild(tags);

    card.addEventListener("click", () => {
      selectedAgentName = agent.agent_name;
      renderAgentDetail(agent);
    });

    agentsList.appendChild(card);
  });
};

const fetchAgents = async () => {
  setListState(agentsList, "Loading...", "loading");
  try {
    const userId = userIdInput.value.trim();
    const match = getMatchValue();
    const [defaultRes, userRes, healthRes, statsRes] = await Promise.all([
      fetch(buildAgentsUrl("share", match)),
      userId ? fetch(buildAgentsUrl(userId, match)) : Promise.resolve(null),
      fetch(buildHealthUrl(userId)),
      fetch(buildStatsUrl(userId)),
    ]);

    if (!defaultRes.ok || (userRes && !userRes.ok)) {
      throw new Error("request failed");
    }

    const defaults = await defaultRes.json();
    const users = userRes ? await userRes.json() : [];
    try {
      const healthJson = healthRes.ok ? await healthRes.json() : null;
      agentHealth = healthJson?.agents || {};
    } catch (err) {
      agentHealth = {};
    }

    try {
      const statsJson = statsRes.ok ? await statsRes.json() : null;
      agentStats = statsJson?.agents || {};
    } catch (err) {
      agentStats = {};
    }

    const combined = [...defaults, ...users];
    latestAgents = combined;
    if (!combined.length) {
      setListState(agentsList, "No agents found.", "empty");
      setAgentDetailEmpty("暂无 Agent 详情");
      return;
    }

    renderAgents(combined);
  } catch (err) {
    setListState(agentsList, "Failed to load agents.", "error");
    setAgentDetailEmpty("加载失败");
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

const runSelectedHealthCheck = async () => {
  const names = Array.from(selectedCoorAgents);
  if (!names.length) {
    flashButton(healthCheckSelectedBtn, "Select agents");
    return;
  }
  const prevText = healthCheckSelectedBtn.textContent;
  healthCheckSelectedBtn.textContent = "Checking...";
  healthCheckSelectedBtn.disabled = true;
  const userId = userIdInput.value.trim();
  try {
    const res = await fetch(buildHealthUrl(userId, names));
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`);
    }
    const healthJson = await res.json();
    const updates = healthJson?.agents || {};
    Object.keys(updates).forEach((key) => {
      agentHealth[key] = updates[key];
    });
    if (latestAgents.length) {
      renderAgents(latestAgents);
      if (selectedAgentName) {
        const active = latestAgents.find((item) => item.agent_name === selectedAgentName);
        if (active) renderAgentDetail(active);
      }
    }
  } catch (err) {
    setStatus("Health check failed", false);
  } finally {
    healthCheckSelectedBtn.textContent = prevText;
    healthCheckSelectedBtn.disabled = false;
  }
};

runBtn.addEventListener("click", runWorkflow);
stopBtn.addEventListener("click", stopWorkflow);
clearOutputBtn.addEventListener("click", clearOutput);
autoScrollBtn.addEventListener("click", toggleAutoScroll);
exportTxtBtn.addEventListener("click", exportOutputTxt);

refreshAgentsBtn.addEventListener("click", fetchAgents);
refreshToolsBtn.addEventListener("click", fetchTools);
refreshWorkflowsBtn.addEventListener("click", fetchWorkflows);

if (agentsSearchBtn) {
  agentsSearchBtn.addEventListener("click", fetchAgents);
}
if (agentsSearchInput) {
  agentsSearchInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      fetchAgents();
    }
  });
}

agentFilterButtons.forEach((btn) => {
  btn.addEventListener("click", () => {
    agentFilter = btn.dataset.filter || "all";
    agentFilterButtons.forEach((item) =>
      item.classList.toggle("active", item.dataset.filter === agentFilter)
    );
    if (latestAgents.length) {
      renderAgents(latestAgents);
    }
  });
});

if (clearCoorBtn) {
  clearCoorBtn.addEventListener("click", () => {
    selectedCoorAgents.clear();
    updateCoorCount();
    if (latestAgents.length) {
      renderAgents(latestAgents);
    }
  });
}

if (healthCheckSelectedBtn) {
  healthCheckSelectedBtn.addEventListener("click", runSelectedHealthCheck);
}

updateAutoScrollBtn();
setStatus("Ready", true);
updateCoorCount();
setAgentDetailEmpty("选择一个 Agent 查看详情");

// ============================================================
// Tasks panel
// ============================================================
const refreshTasksBtn = document.getElementById("refreshTasks");
const tasksList = document.getElementById("tasksList");
const checkpointPanel = document.getElementById("checkpointPanel");
const checkpointTaskIdBadge = document.getElementById("checkpointTaskId");
const checkpointsList = document.getElementById("checkpointsList");
const logPanel = document.getElementById("logPanel");
const logMeta = document.getElementById("logMeta");
const logHistory = document.getElementById("logHistory");
const copyLogBtn = document.getElementById("copyLogBtn");
const resumePanel = document.getElementById("resumePanel");
const resumeTaskIdInput = document.getElementById("resumeTaskId");
const resumeWorkflowIdInput = document.getElementById("resumeWorkflowId");
const resumeStepInput = document.getElementById("resumeStep");
const resumeUserIdInput = document.getElementById("resumeUserId");
const resumeBtn = document.getElementById("resumeBtn");
const resumeStopBtn = document.getElementById("resumeStopBtn");
const resumeOutput = document.getElementById("resumeOutput");
const clearResumeOutputBtn = document.getElementById("clearResumeOutput");

let selectedTaskId = null;
let resumeAbortController = null;

const formatDateTime = (isoStr) => {
  if (!isoStr) return "";
  try {
    const d = new Date(isoStr);
    return d.toLocaleString();
  } catch (_) {
    return isoStr;
  }
};

const statusBadgeClass = (status) => {
  if (status === "completed") return "badge-success";
  if (status === "failed") return "badge-error";
  if (status === "running") return "badge-info";
  return "badge-muted";
};

const fetchTasks = async () => {
  setListState(tasksList, "Loading...", "loading");
  try {
    const res = await fetch("/api/tasks");
    if (!res.ok) throw new Error("request failed");
    const tasks = await res.json();
    if (!tasks.length) {
      setListState(tasksList, "No tasks found.", "empty");
      return;
    }
    tasksList.textContent = "";
    tasks.forEach((task) => {
      const item = document.createElement("div");
      item.className = "task-item";
      if (task.task_id === selectedTaskId) item.classList.add("active");
      item.dataset.taskId = task.task_id;
      item.setAttribute("role", "button");
      item.tabIndex = 0;

      const header = document.createElement("div");
      header.className = "task-item-header";

      const titleEl = document.createElement("strong");
      titleEl.textContent = task.user_query
        ? task.user_query.slice(0, 60) + (task.user_query.length > 60 ? "..." : "")
        : task.task_id;

      const badge = document.createElement("span");
      badge.className = `status-badge ${statusBadgeClass(task.status)}`;
      badge.textContent = task.status;

      header.appendChild(titleEl);
      header.appendChild(badge);

      const meta = document.createElement("div");
      meta.className = "task-item-meta";
      meta.textContent = `${formatDateTime(task.created_at)} · ${task.step_count} steps`;

      const wfId = document.createElement("div");
      wfId.className = "task-item-wfid";
      wfId.textContent = `workflow: ${task.workflow_id || "-"}`;

      item.appendChild(header);
      item.appendChild(meta);
      item.appendChild(wfId);

      item.addEventListener("click", () => selectTask(task));
      item.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          selectTask(task);
        }
      });
      tasksList.appendChild(item);
    });
  } catch (err) {
    setListState(tasksList, "Failed to load tasks.", "error");
  }
};

const selectTask = async (task) => {
  selectedTaskId = task.task_id;
  document.querySelectorAll(".task-item").forEach((el) => {
    el.classList.toggle("active", el.dataset.taskId === task.task_id);
  });

  // Populate resume panel
  resumePanel.style.display = "";
  resumeTaskIdInput.value = task.task_id;
  resumeWorkflowIdInput.value = task.workflow_id || "";
  resumeUserIdInput.value = userIdInput.value || "test";

  // Load checkpoints
  await loadTaskCheckpoints(task.task_id);
  // Load log
  await loadTaskLog(task.task_id);
};

const loadTaskCheckpoints = async (taskId) => {
  checkpointPanel.style.display = "";
  checkpointTaskIdBadge.textContent = taskId;
  checkpointsList.textContent = "Loading...";
  try {
    const res = await fetch(`/api/tasks/${encodeURIComponent(taskId)}/checkpoints`);
    if (!res.ok) throw new Error("request failed");
    const checkpoints = await res.json();
    if (!checkpoints.length) {
      checkpointsList.textContent = "No checkpoints found.";
      return;
    }
    checkpointsList.textContent = "";
    checkpoints.forEach((cp) => {
      const row = document.createElement("div");
      row.className = "checkpoint-row";
      row.dataset.checkpointId = cp.checkpoint_id;
      row.dataset.taskId = selectedTaskId;
      row.dataset.step = cp.step;

      const stepBadge = document.createElement("span");
      stepBadge.className = "step-badge";
      stepBadge.textContent = `Step ${cp.step}`;

      const nodeEl = document.createElement("span");
      nodeEl.className = "checkpoint-node";
      nodeEl.textContent = cp.node_name;

      const nextEl = document.createElement("span");
      nextEl.className = "checkpoint-next";
      nextEl.textContent = cp.next_node ? `→ ${cp.next_node}` : "";

      const tsEl = document.createElement("span");
      tsEl.className = "checkpoint-ts";
      tsEl.textContent = formatDateTime(cp.timestamp);

      const resumeFromBtn = document.createElement("button");
      resumeFromBtn.className = "ghost small";
      resumeFromBtn.textContent = "Resume from here";
      resumeFromBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        resumeStepInput.value = cp.step;
        resumePanel.scrollIntoView({ behavior: "smooth" });
      });

      // Collapse toggle
      const cpCollapseToggle = document.createElement("span");
      cpCollapseToggle.className = "cp-collapse-toggle";
      cpCollapseToggle.innerHTML = '<span class="icon">▼</span> JSON';

      // Details panel - initially empty, will load on expand
      const detailsDiv = document.createElement("div");
      detailsDiv.className = "checkpoint-details";
      detailsDiv.innerHTML = '<div style="color:var(--muted);font-size:0.8rem">Click to load JSON...</div>';

      row.appendChild(stepBadge);
      row.appendChild(nodeEl);
      row.appendChild(nextEl);
      row.appendChild(tsEl);
      row.appendChild(resumeFromBtn);
      row.appendChild(cpCollapseToggle);
      row.appendChild(detailsDiv);

      // Click to toggle expand/collapse and load JSON
      let loaded = false;
      row.addEventListener("click", async () => {
        const isExpanding = !row.classList.contains("expanded");
        row.classList.toggle("expanded");

        if (isExpanding && !loaded) {
          // Load full checkpoint JSON
          try {
            const res = await fetch(`/api/tasks/${encodeURIComponent(selectedTaskId)}/checkpoints/${cp.step}`);
            if (res.ok) {
              const data = await res.json();
              detailsDiv.innerHTML = `<pre class="checkpoint-json">${JSON.stringify(data, null, 2)}</pre>`;
              loaded = true;
            } else {
              detailsDiv.innerHTML = '<div style="color:var(--danger)">Failed to load checkpoint data</div>';
            }
          } catch (err) {
            detailsDiv.innerHTML = `<div style="color:var(--danger)">Error: ${err.message}</div>`;
          }
        }
      });

      checkpointsList.appendChild(row);
    });
  } catch (err) {
    checkpointsList.textContent = "Failed to load checkpoints.";
  }
};

const loadTaskLog = async (taskId) => {
  logPanel.style.display = "";
  logMeta.textContent = "Loading...";
  logHistory.textContent = "";
  try {
    const res = await fetch(`/api/tasks/${encodeURIComponent(taskId)}/log`);
    if (!res.ok) throw new Error("request failed");
    const log = await res.json();

    logMeta.innerHTML = `
      <div class="log-meta-item"><b>Task ID</b><span>${log.task_id}</span></div>
      <div class="log-meta-item"><b>Status</b><span class="status-badge ${statusBadgeClass(log.status)}">${log.status}</span></div>
      <div class="log-meta-item"><b>Created</b><span>${formatDateTime(log.created_at)}</span></div>
      <div class="log-meta-item"><b>Finished</b><span>${formatDateTime(log.finished_at) || "-"}</span></div>
      ${log.error ? `<div class="log-meta-item error-text"><b>Error</b><span>${log.error}</span></div>` : ""}
    `;

    if (!log.history || !log.history.length) {
      logHistory.textContent = "No log entries.";
      return;
    }

    logHistory.textContent = "";
    log.history.forEach((entry, index) => {
      const entryEl = document.createElement("div");
      entryEl.className = `log-entry log-event-${entry.event || "message"}`;
      entryEl.dataset.agent = entry.role || entry.node_name || "";

      const headerEl = document.createElement("div");
      headerEl.className = "log-entry-header";
      // 点击整个 header 切换展开/折叠
      headerEl.addEventListener("click", () => {
        entryEl.classList.toggle("expanded");
      });

      // Collapse toggle icon
      const collapseIcon = document.createElement("span");
      collapseIcon.className = "collapse-icon";
      collapseIcon.textContent = "▼";

      const stepSpan = document.createElement("span");
      stepSpan.className = "step-badge";
      stepSpan.textContent = `Step ${entry.step}`;

      const roleSpan = document.createElement("span");
      roleSpan.className = "log-role";
      // Display agent_proxy with sub_agent_name as: agent_proxy【researcher】
      if (entry.node_name === "agent_proxy" && entry.sub_agent_name) {
        roleSpan.textContent = `${entry.node_name}【${entry.sub_agent_name}】`;
      } else {
        roleSpan.textContent = entry.role || entry.node_name;
      }

      const eventSpan = document.createElement("span");
      eventSpan.className = "log-event-tag";
      eventSpan.textContent = entry.event || "";

      const tsSpan = document.createElement("span");
      tsSpan.className = "log-ts";
      tsSpan.textContent = formatDateTime(entry.timestamp);

      headerEl.appendChild(collapseIcon);
      headerEl.appendChild(stepSpan);
      headerEl.appendChild(roleSpan);
      headerEl.appendChild(eventSpan);
      headerEl.appendChild(tsSpan);

      const contentEl = document.createElement("pre");
      contentEl.className = "log-content";
      contentEl.textContent = entry.content || "";

      entryEl.appendChild(headerEl);
      entryEl.appendChild(contentEl);
      
      // 默认全部折叠（不添加 expanded 类）
      // 不需要任何额外操作，CSS 默认 display:none
      
      logHistory.appendChild(entryEl);
    });
  } catch (err) {
    logMeta.textContent = "Failed to load log.";
  }
};

const copyTaskLog = async () => {
  const text = logHistory.innerText || logHistory.textContent || "";
  if (!text) {
    flashButton(copyLogBtn, "Empty");
    return;
  }
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
    } else {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "absolute";
      ta.style.left = "-9999px";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
    }
    flashButton(copyLogBtn, "Copied");
  } catch (_) {
    flashButton(copyLogBtn, "Failed");
  }
};

const resumeTask = async () => {
  const taskId = resumeTaskIdInput.value.trim();
  const workflowId = resumeWorkflowIdInput.value.trim();
  const resumeStep = parseInt(resumeStepInput.value, 10);
  const userId = resumeUserIdInput.value.trim() || "test";

  if (!taskId) {
    alert("Please select a task first.");
    return;
  }

  resumeOutput.textContent = "";
  resumeBtn.disabled = true;
  resumeStopBtn.disabled = false;

  const payload = {
    task_id: taskId,
    resume_step: isNaN(resumeStep) ? 0 : resumeStep,
    workflow_id: workflowId || null,
    user_id: userId,
    task_type: "agent_workflow",
    workmode: "launch",
    debug: false,
    deep_thinking_mode: true,
    search_before_planning: false,
    coor_agents: null,
  };

  resumeAbortController = new AbortController();
  try {
    const response = await fetch("/api/tasks/resume", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: resumeAbortController.signal,
    });
    if (!response.ok || !response.body) throw new Error(`HTTP ${response.status}`);

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    const appendResume = (text) => {
      resumeOutput.textContent += text;
      resumeOutput.scrollTop = resumeOutput.scrollHeight;
    };

    const handleResumeEvent = (eventName, payload) => {
      if (eventName === "messages") {
        const content = payload.data?.delta?.content || payload.data?.message || "";
        appendResume(content);
        return;
      }
      if (eventName === "start_of_agent") {
        appendResume(`\n[start] ${payload.data?.agent_name || ""}\n`);
        return;
      }
      if (eventName === "end_of_agent") {
        appendResume(`\n[end] ${payload.data?.agent_name || ""}\n`);
        return;
      }
      if (eventName === "end_of_workflow") {
        appendResume("\n[workflow completed]\n");
        return;
      }
      if (eventName === "error") {
        appendResume(`\n[error] ${payload.data?.error || "unknown error"}\n`);
        return;
      }
      appendResume(`\n[${eventName}] ${JSON.stringify(payload)}\n`);
    };

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      buffer = parseSse(buffer, handleResumeEvent);
    }
  } catch (err) {
    resumeOutput.textContent += `\n[error] ${err.message || err}\n`;
  } finally {
    resumeBtn.disabled = false;
    resumeStopBtn.disabled = true;
    resumeAbortController = null;
  }
};

const stopResume = () => {
  if (resumeAbortController) {
    resumeAbortController.abort();
  }
};

refreshTasksBtn.addEventListener("click", fetchTasks);
copyLogBtn.addEventListener("click", copyTaskLog);
resumeBtn.addEventListener("click", resumeTask);
resumeStopBtn.addEventListener("click", stopResume);
clearResumeOutputBtn.addEventListener("click", () => {
  resumeOutput.textContent = "";
});
