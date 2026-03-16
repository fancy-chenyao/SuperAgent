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
const toolsSearchInput = document.getElementById("toolsSearch");
const toolsSearchBtn = document.getElementById("toolsSearchBtn");
const toolsSourceFilter = document.getElementById("toolsSourceFilter");
const toolsScopeFilter = document.getElementById("toolsScopeFilter");
const toolsSortSelect = document.getElementById("toolsSort");
const toolsCountTotal = document.getElementById("toolsCountTotal");
const toolsCountBuiltin = document.getElementById("toolsCountBuiltin");
const toolsCountMcp = document.getElementById("toolsCountMcp");

const agentsList = document.getElementById("agentsList");
const agentsSearchInput = document.getElementById("agentsSearch");
const agentsSearchBtn = document.getElementById("agentsSearchBtn");
const agentFilterButtons = document.querySelectorAll(".agent-filter");
const coorCount = document.getElementById("coorCount");
const clearCoorBtn = document.getElementById("clearCoorBtn");
const healthCheckSelectedBtn = document.getElementById("healthCheckSelected");
const agentDetail = document.getElementById("agentDetail");
const toolsList = document.getElementById("toolsList");
const toolDetail = document.getElementById("toolDetail");
const mcpList = document.getElementById("mcpList");
const mcpSummary = document.getElementById("mcpSummary");
const workflowsList = document.getElementById("workflowsList");
const workflowDetail = document.getElementById("workflowDetail");
const mermaidContainer = document.getElementById("mermaidContainer");
const planSummary = document.getElementById("planSummary");
const planHint = document.getElementById("planHint");
const planEditorList = document.getElementById("planEditorList");
const planValidationHint = document.getElementById("planValidationHint");
const addPlanStepBtn = document.getElementById("addPlanStep");
const validatePlanBtn = document.getElementById("validatePlan");
const nlPlanEditBtn = document.getElementById("nlPlanEdit");
const confirmExecuteBtn = document.getElementById("confirmExecute");
const retryPlanBtn = document.getElementById("retryPlan");
const planModal = document.getElementById("planModal");
const closePlanModalBtn = document.getElementById("closePlanModal");
const cancelPlanNlBtn = document.getElementById("cancelPlanNl");
const applyPlanNlBtn = document.getElementById("applyPlanNl");
const planNlInput = document.getElementById("planNlInput");
const planNlHint = document.getElementById("planNlHint");

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
let latestTools = [];
let toolStats = {};
let selectedToolName = null;
let mcpConfig = null;
let plannerBuffer = "";
let plannerCollecting = false;
let planSteps = [];
let plannerOnlyMode = false;
let plannerOnlyController = null;
let plannerOnlyTimeoutId = null;
let plannerOnlyStepsUpdated = false;
let instructionHistory = [];
const PLANNER_ONLY_TIMEOUT_MS = 50000;

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

const resetPlan = () => {
  if (planSummary) planSummary.innerHTML = "";
  if (planHint) {
    planHint.classList.add("hidden");
    planHint.classList.remove("error");
    planHint.textContent = "";
  }
  if (planEditorList) planEditorList.innerHTML = "";
  if (planValidationHint) {
    planValidationHint.classList.add("hidden");
    planValidationHint.classList.remove("error");
    planValidationHint.textContent = "";
  }
  planSteps = [];
  plannerBuffer = "";
  plannerCollecting = false;
  updateConfirmExecuteState();
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

const showPlanHint = (text, isError = false) => {
  if (!planHint) return;
  planHint.textContent = text;
  planHint.classList.remove("hidden");
  if (isError) {
    planHint.classList.add("error");
  } else {
    planHint.classList.remove("error");
  }
};

const showPlanValidationHint = (text, isError = false) => {
  if (!planValidationHint) return;
  planValidationHint.textContent = text;
  planValidationHint.classList.remove("hidden");
  if (isError) {
    planValidationHint.classList.add("error");
  } else {
    planValidationHint.classList.remove("error");
  }
};

const updateConfirmExecuteState = () => {
  if (!confirmExecuteBtn) return;
  const hasPlan = planSteps.length > 0;
  const hasWorkflowId = workflowIdInput && workflowIdInput.value.trim();
  confirmExecuteBtn.disabled = !(hasPlan && hasWorkflowId);
  if (retryPlanBtn) {
    retryPlanBtn.disabled = !instructionHistory.length;
  }
};

const showPlanNlHint = (text, isError = false) => {
  if (!planNlHint) return;
  planNlHint.textContent = text;
  planNlHint.classList.remove("hidden");
  if (isError) {
    planNlHint.classList.add("error");
  } else {
    planNlHint.classList.remove("error");
  }
};

const openPlanModal = () => {
  if (!planModal) return;
  planModal.classList.remove("hidden");
  if (planNlInput) planNlInput.value = "";
  showPlanNlHint("请输入修改指令。");
};

const closePlanModal = () => {
  if (!planModal) return;
  planModal.classList.add("hidden");
  if (planNlHint) {
    planNlHint.classList.add("hidden");
    planNlHint.classList.remove("error");
    planNlHint.textContent = "";
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

const extractJsonFromText = (text) => {
  const trimmed = (text || "").trim();
  if (!trimmed) return null;

  const tryParse = (value) => {
    try {
      return JSON.parse(value);
    } catch {
      return null;
    }
  };

  let parsed = tryParse(trimmed);
  if (parsed) return parsed;

  const firstObj = trimmed.indexOf("{");
  const lastObj = trimmed.lastIndexOf("}");
  if (firstObj >= 0 && lastObj > firstObj) {
    parsed = tryParse(trimmed.slice(firstObj, lastObj + 1));
    if (parsed) return parsed;
  }

  const firstArr = trimmed.indexOf("[");
  const lastArr = trimmed.lastIndexOf("]");
  if (firstArr >= 0 && lastArr > firstArr) {
    parsed = tryParse(trimmed.slice(firstArr, lastArr + 1));
    if (parsed) return parsed;
  }

  return null;
};

const normalizePlanSteps = (payload) => {
  if (!payload) return [];
  if (Array.isArray(payload)) return payload;
  if (Array.isArray(payload.steps)) return payload.steps;
  if (Array.isArray(payload.planning_steps)) return payload.planning_steps;
  return [];
};

const normalizeStep = (step = {}) => ({
  title: step.title || "",
  description: step.description || "",
  agent_name: step.agent_name || "",
  note: step.note || "",
});

const renderPlanSummary = (steps) => {
  if (!planSummary) return;
  planSummary.innerHTML = "";
  if (!steps || !steps.length) {
    showPlanHint("No plan steps detected.", true);
    return;
  }

  const frag = document.createDocumentFragment();
  steps.forEach((step, index) => {
    const card = document.createElement("div");
    card.className = "plan-card";

    const title = document.createElement("div");
    title.className = "plan-title";
    const rawTitle = step?.title || step?.agent_name || `Step ${index + 1}`;
    title.textContent = `${index + 1}. ${rawTitle}`;

    const chip = document.createElement("div");
    chip.className = "plan-chip";
    const agentName = step?.agent_name || "auto";
    chip.textContent = `role: ${agentName}`;

    const desc = document.createElement("div");
    desc.className = "plan-desc";
    desc.textContent = step?.description || "No description provided.";

    const meta = document.createElement("div");
    meta.className = "plan-meta";
    const note = step?.note || "";
    meta.textContent = note ? `note: ${note}` : "note: n/a";

    card.appendChild(title);
    card.appendChild(chip);
    card.appendChild(desc);
    card.appendChild(meta);
    frag.appendChild(card);
  });

  planSummary.appendChild(frag);
  showPlanHint(`Plan loaded: ${steps.length} step(s).`);
  updateConfirmExecuteState();
};

const renderPlanEditor = (errorsByIndex = {}) => {
  if (!planEditorList) return;
  planEditorList.innerHTML = "";

  if (!planSteps.length) {
    showPlanValidationHint("暂无计划步骤。", true);
    return;
  }

  const frag = document.createDocumentFragment();
  planSteps.forEach((step, index) => {
    const item = document.createElement("div");
    item.className = "plan-editor-item";
    if (errorsByIndex[index]) item.classList.add("invalid");

    const head = document.createElement("div");
    head.className = "plan-editor-head";
    const title = document.createElement("div");
    title.className = "plan-editor-title";
    title.textContent = `Step ${index + 1}`;
    head.appendChild(title);

    const content = document.createElement("div");
    content.className = "plan-editor-content";
    const titleLine = document.createElement("div");
    titleLine.textContent = step.title || "未命名步骤";
    const descLine = document.createElement("div");
    descLine.textContent = step.description || "无描述";
    const metaLine = document.createElement("div");
    metaLine.className = "meta";
    const roleText = step.agent_name ? `角色: ${step.agent_name}` : "角色: 自动";
    const noteText = step.note ? `备注: ${step.note}` : "备注: 无";
    metaLine.textContent = `${roleText} · ${noteText}`;

    content.appendChild(titleLine);
    content.appendChild(descLine);
    content.appendChild(metaLine);

    item.appendChild(head);
    item.appendChild(content);
    frag.appendChild(item);
  });

  planEditorList.appendChild(frag);
  updateConfirmExecuteState();
};

const movePlanStep = (from, to) => {
  if (to < 0 || to >= planSteps.length) return;
  const nextSteps = [...planSteps];
  const [moved] = nextSteps.splice(from, 1);
  nextSteps.splice(to, 0, moved);
  planSteps = nextSteps;
  renderPlanEditor();
  renderPlanSummary(planSteps);
};

const removePlanStep = (index) => {
  if (index < 0 || index >= planSteps.length) return;
  planSteps = planSteps.filter((_, i) => i !== index);
  renderPlanEditor();
  renderPlanSummary(planSteps);
};

const addPlanStep = () => {
  planSteps = [...planSteps, normalizeStep({ title: "", description: "", agent_name: "", note: "" })];
  renderPlanEditor();
  renderPlanSummary(planSteps);
};

const validatePlanSteps = () => {
  const errors = [];
  const errorsByIndex = {};
  if (!planSteps.length) {
    errors.push("计划为空，请先新增步骤。");
  }

  const agentNames = new Set(
    Array.isArray(latestAgents) ? latestAgents.map((agent) => agent.agent_name).filter(Boolean) : []
  );
  if (!agentNames.size) {
    errors.push("Agent 列表未加载，无法校验执行角色。");
  }

  planSteps.forEach((step, idx) => {
    const stepErrors = [];
    if (!step.title || !step.title.trim()) {
      stepErrors.push("缺少标题");
    }
    if (step.agent_name && agentNames.size && !agentNames.has(step.agent_name)) {
      stepErrors.push(`执行角色不存在：${step.agent_name}`);
    }
    if (stepErrors.length) {
      errorsByIndex[idx] = stepErrors;
      errors.push(`第 ${idx + 1} 步：${stepErrors.join("，")}`);
    }
  });

  renderPlanEditor(errorsByIndex);
  renderPlanSummary(planSteps);
  return errors;
};

const runPlannerUpdate = async (instruction, appendHistory = true) => {
  const userId = userIdInput.value.trim();
  if (!userId) {
    showPlanNlHint("User ID required.", true);
    return;
  }
  if (!instruction) {
    showPlanNlHint("请输入修改指令。", true);
    return;
  }

  if (appendHistory) {
    instructionHistory = [...instructionHistory, instruction];
  }

  plannerOnlyMode = true;
  plannerBuffer = "";
  plannerCollecting = false;
  plannerOnlyStepsUpdated = false;
  showPlanNlHint("正在生成新的计划...");

  const payload = {
    user_id: userId,
    lang: "zh",
    workmode: "launch",
    stop_after_planner: true,
    instruction: instruction,
    instruction_history: instructionHistory,
    messages: [
      {
        role: "user",
        content:
          "请基于全部指令历史重新规划，输出JSON格式的steps。要求仅输出JSON，不要解释。\\n\\n最新补充：" +
          instruction,
      },
    ],
    debug: debugInput.checked,
    deep_thinking_mode: deepThinkingInput.checked,
    search_before_planning: searchBeforeInput.checked,
    coor_agents: selectedCoorAgents.size ? Array.from(selectedCoorAgents) : null,
    workflow_id: workflowIdInput.value.trim() || null,
  };

  const schedulePlannerTimeout = () => {
    if (plannerOnlyTimeoutId) {
      clearTimeout(plannerOnlyTimeoutId);
    }
    plannerOnlyTimeoutId = setTimeout(() => {
      if (plannerOnlyController) {
        plannerOnlyController.abort();
      }
      showPlanNlHint("生成超时，请调整指令后重试。", true);
      plannerOnlyMode = false;
      plannerOnlyController = null;
    }, PLANNER_ONLY_TIMEOUT_MS);
  };

  plannerOnlyController = new AbortController();
  schedulePlannerTimeout();
  try {
    const response = await fetch("/api/workflows/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: plannerOnlyController.signal,
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
      schedulePlannerTimeout();
    }
  } catch (err) {
    if (err?.name !== "AbortError") {
      showPlanNlHint(`生成失败: ${err.message || err}`, true);
    }
  } finally {
    plannerOnlyMode = false;
    plannerOnlyController = null;
    if (plannerOnlyTimeoutId) {
      clearTimeout(plannerOnlyTimeoutId);
      plannerOnlyTimeoutId = null;
    }
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
    if (plannerOnlyMode && typeof agentName === "string" && agentName.toLowerCase().includes("planner")) {
      if (plannerOnlyTimeoutId) {
        clearTimeout(plannerOnlyTimeoutId);
        plannerOnlyTimeoutId = setTimeout(() => {
          if (plannerOnlyController) {
            plannerOnlyController.abort();
          }
          showPlanNlHint("生成超时，请调整指令后重试。", true);
          plannerOnlyMode = false;
          plannerOnlyController = null;
        }, PLANNER_ONLY_TIMEOUT_MS);
      }
    }
    if (typeof agentName === "string" && agentName.toLowerCase().includes("planner")) {
      plannerCollecting = true;
      plannerBuffer += content;
    }
    if (!plannerOnlyMode) {
      appendOutput(agentName, content);
    }
    return;
  }
  if (eventName === "start_of_workflow") {
    if (!plannerOnlyMode) {
      resetSummary();
      resetPlan();
      appendOutput("system", `\n[workflow] ${payload.data?.workflow_id || ""}\n`);
    }
    const wfId = payload.data?.workflow_id;
    if (wfId && workflowIdInput && !workflowIdInput.value.trim()) {
      workflowIdInput.value = wfId;
    }
    updateConfirmExecuteState();
    return;
  }
  if (eventName === "start_of_agent") {
    const agentName = payload.data?.agent_name || payload.agent_name || "agent";
    if (!plannerOnlyMode) {
      pushFlowStep(agentName);
    }
    if (typeof agentName === "string" && agentName.toLowerCase().includes("planner")) {
      plannerCollecting = true;
      plannerBuffer = "";
      showPlanHint("Collecting plan output...");
    }
    if (!plannerOnlyMode) {
      appendOutput("system", `\n[start_of_agent] ${agentName}\n`);
    }
    return;
  }
  if (eventName === "end_of_agent") {
    const agentName = payload.data?.agent_name || payload.agent_name || "agent";
    if (!plannerOnlyMode) {
      finishActiveStep();
      renderFlowSteps();
    }
    if (typeof agentName === "string" && agentName.toLowerCase().includes("planner")) {
      plannerCollecting = false;
      const parsed = extractJsonFromText(plannerBuffer);
      const steps = normalizePlanSteps(parsed);
      if (steps.length) {
        planSteps = steps.map((step) => normalizeStep(step));
        plannerOnlyStepsUpdated = true;
        renderPlanSummary(planSteps);
        renderPlanEditor();
        showPlanValidationHint("计划已更新，可继续用自然语言补充。");
        if (plannerOnlyMode && plannerOnlyController) {
          plannerOnlyController.abort();
          showPlanNlHint("已根据指令生成新计划。");
          closePlanModal();
        }
      } else {
        showPlanHint("Planner output is not valid JSON steps.", true);
        if (plannerOnlyMode) {
          showPlanNlHint("未能解析规划结果，请调整指令再试。", true);
        }
      }
    }
    if (!plannerOnlyMode) {
      appendOutput("system", `\n[end_of_agent] ${agentName}\n`);
    }
    return;
  }
  if (eventName === "end_of_workflow") {
    if (!plannerOnlyMode) {
      showSummaryHint("Workflow completed.");
      appendOutput("system", "\n[workflow] completed\n");
    } else if (!plannerOnlyStepsUpdated) {
      showPlanNlHint("规划完成，但未生成可用步骤。请调整指令后重试。", true);
    }
    return;
  }
  if (eventName === "new_agent_created") {
    if (!plannerOnlyMode) {
      appendOutput("system", `\n[new agent] ${payload.data?.new_agent_name || ""}\n`);
    }
    return;
  }
  if (eventName === "error") {
    if (!plannerOnlyMode) {
      showSummaryHint("Workflow error.", true);
      appendOutput("system", `\n[error] ${payload.data?.error || payload.raw || "unknown error"}\n`);
      showPlanValidationHint("执行失败，可在 Task History 中恢复。", true);
    } else {
      showPlanNlHint(payload.data?.error || payload.raw || "unknown error", true);
    }
    return;
  }
  if (!plannerOnlyMode) {
    appendOutput("system", `\n[${eventName}] ${JSON.stringify(payload)}\n`);
  }
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
  resetPlan();
  instructionHistory = [message];
  runBtn.disabled = true;
  stopBtn.disabled = false;
  if (confirmExecuteBtn) confirmExecuteBtn.disabled = true;

  const payload = {
    user_id: userId,
    lang: "zh",
    workmode: workModeInput.value,
    stop_after_planner: workModeInput.value === "launch",
    instruction: message,
    instruction_history: instructionHistory,
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
    updateConfirmExecuteState();
  }
};

const runExecution = async () => {
  const userId = userIdInput.value.trim();
  if (!userId) {
    setStatus("User ID required", false);
    return;
  }
  const workflowId = workflowIdInput.value.trim();
  if (!workflowId) {
    showPlanValidationHint("缺少 Workflow ID，无法执行。", true);
    return;
  }
  if (!planSteps.length) {
    showPlanValidationHint("计划为空，无法执行。", true);
    return;
  }

  setStatus("Executing", true);
  resetSummary();
  runBtn.disabled = true;
  stopBtn.disabled = false;
  if (confirmExecuteBtn) confirmExecuteBtn.disabled = true;

  const payload = {
    user_id: userId,
    lang: "zh",
    workmode: "production",
    stop_after_planner: false,
    instruction: null,
    instruction_history: instructionHistory,
    messages: [{ role: "user", content: "确认执行，按当前计划执行。" }],
    debug: debugInput.checked,
    deep_thinking_mode: deepThinkingInput.checked,
    search_before_planning: searchBeforeInput.checked,
    coor_agents: selectedCoorAgents.size ? Array.from(selectedCoorAgents) : null,
    workflow_id: workflowId,
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
    updateConfirmExecuteState();
  }
};

if (addPlanStepBtn) {
  addPlanStepBtn.addEventListener("click", () => {
    addPlanStep();
    showPlanValidationHint("Step added. Remember to validate.");
  });
}

if (nlPlanEditBtn) {
  nlPlanEditBtn.addEventListener("click", () => openPlanModal());
}

if (confirmExecuteBtn) {
  confirmExecuteBtn.addEventListener("click", () => runExecution());
}

if (retryPlanBtn) {
  retryPlanBtn.addEventListener("click", () => {
    if (!instructionHistory.length) {
      showPlanValidationHint("没有可重试的指令。", true);
      return;
    }
    const lastInstruction = instructionHistory[instructionHistory.length - 1];
    runPlannerUpdate(lastInstruction, false);
  });
}

if (closePlanModalBtn) {
  closePlanModalBtn.addEventListener("click", () => closePlanModal());
}

if (cancelPlanNlBtn) {
  cancelPlanNlBtn.addEventListener("click", () => closePlanModal());
}

if (applyPlanNlBtn) {
  applyPlanNlBtn.addEventListener("click", () => {
    const instruction = planNlInput ? planNlInput.value.trim() : "";
    runPlannerUpdate(instruction);
  });
}

if (validatePlanBtn) {
  validatePlanBtn.addEventListener("click", () => {
    const errors = validatePlanSteps();
    if (!errors.length) {
      showPlanValidationHint("Validation passed.");
      return;
    }
    showPlanValidationHint(errors.join(" | "), true);
  });
}

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

const buildToolsStatsUrl = (userId) => {
  const params = new URLSearchParams();
  if (userId) params.set("user_id", userId);
  params.set("include_share", "true");
  return `/api/tools/stats?${params.toString()}`;
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

const setToolDetailEmpty = (text) => {
  if (!toolDetail) return;
  toolDetail.textContent = "";
  const empty = document.createElement("div");
  empty.className = "tool-detail-empty";
  empty.textContent = text;
  toolDetail.appendChild(empty);
};

const formatSchemaType = (schema) => {
  if (!schema || typeof schema !== "object") return "unknown";
  if (schema.type) {
    if (schema.type === "array" && schema.items) {
      const itemType = formatSchemaType(schema.items);
      return `array<${itemType}>`;
    }
    return schema.type;
  }
  const variants = schema.anyOf || schema.oneOf || schema.allOf;
  if (Array.isArray(variants)) {
    return variants.map((item) => formatSchemaType(item)).join(" | ");
  }
  return schema.title || "unknown";
};

const renderSchemaTable = (schema) => {
  if (!schema || !schema.properties) return null;
  const table = document.createElement("table");
  table.className = "schema-table";

  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  ["字段", "类型", "必填", "描述"].forEach((label) => {
    const th = document.createElement("th");
    th.textContent = label;
    headRow.appendChild(th);
  });
  thead.appendChild(headRow);
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  const required = new Set(schema.required || []);
  Object.entries(schema.properties).forEach(([key, value]) => {
    const row = document.createElement("tr");
    const nameCell = document.createElement("td");
    nameCell.textContent = key;
    const typeCell = document.createElement("td");
    typeCell.textContent = formatSchemaType(value);
    const reqCell = document.createElement("td");
    reqCell.textContent = required.has(key) ? "Yes" : "No";
    const descCell = document.createElement("td");
    descCell.textContent = value?.description || value?.title || "";
    row.appendChild(nameCell);
    row.appendChild(typeCell);
    row.appendChild(reqCell);
    row.appendChild(descCell);
    tbody.appendChild(row);
  });
  table.appendChild(tbody);
  return table;
};

const renderToolDetail = (tool) => {
  if (!toolDetail) return;
  if (!tool) {
    setToolDetailEmpty("选择一个 Tool 查看详情");
    return;
  }

  toolDetail.textContent = "";
  const title = document.createElement("h3");
  title.textContent = tool.name || "tool";

  const sub = document.createElement("div");
  sub.className = "agent-sub";
  const scope = tool.scope || tool.identifier?.scope || "n/a";
  const server = tool.server || tool.identifier?.server || "n/a";
  sub.textContent = `scope: ${scope} · server: ${server}`;

  const tagRow = document.createElement("div");
  tagRow.className = "tag-row";
  if (tool.is_mcp) {
    tagRow.appendChild(createTag("mcp", "warn"));
  } else {
    tagRow.appendChild(createTag("builtin", "accent"));
  }
  if (tool.version) {
    tagRow.appendChild(createTag(`v${tool.version}`));
  }
  if (Array.isArray(tool.tags)) {
    tool.tags.forEach((tag) => {
      if (tag) tagRow.appendChild(createTag(tag));
    });
  }

  const descTitle = document.createElement("h4");
  descTitle.textContent = "描述";
  const desc = document.createElement("p");
  desc.textContent = tool.description || "暂无描述";

  const usageTitle = document.createElement("h4");
  usageTitle.textContent = "Usage";
  const usageRow = document.createElement("p");
  const stats = toolStats[tool.name] || {};
  const parts = [];
  if (stats.workflows !== undefined) parts.push(`workflows: ${stats.workflows}`);
  if (stats.last_used) parts.push(`last: ${stats.last_used}`);
  usageRow.textContent = parts.length ? parts.join(" · ") : "n/a";

  const schemaTitle = document.createElement("h4");
  schemaTitle.textContent = "Args Schema";
  const schemaActions = document.createElement("div");
  schemaActions.className = "panel-actions";
  const copyBtn = document.createElement("button");
  copyBtn.className = "ghost";
  copyBtn.textContent = "Copy schema";
  copyBtn.addEventListener("click", () => {
    if (!tool.args_schema) return;
    const text = JSON.stringify(tool.args_schema, null, 2);
    navigator.clipboard.writeText(text).then(() => flashButton(copyBtn, "Copied"));
  });
  schemaActions.appendChild(copyBtn);

  const schemaContent = tool.args_schema ? renderSchemaTable(tool.args_schema) : null;
  const schemaEmpty = document.createElement("p");
  schemaEmpty.textContent = tool.args_schema ? "" : "No schema available.";

  toolDetail.appendChild(title);
  toolDetail.appendChild(sub);
  toolDetail.appendChild(tagRow);
  toolDetail.appendChild(descTitle);
  toolDetail.appendChild(desc);
  toolDetail.appendChild(usageTitle);
  toolDetail.appendChild(usageRow);
  toolDetail.appendChild(schemaTitle);
  if (tool.args_schema) {
    toolDetail.appendChild(schemaActions);
    if (schemaContent) {
      toolDetail.appendChild(schemaContent);
    } else {
      schemaEmpty.textContent = "Schema format unsupported.";
      toolDetail.appendChild(schemaEmpty);
    }
  } else {
    toolDetail.appendChild(schemaEmpty);
  }
};

const renderMcpConfig = () => {
  if (!mcpList || !mcpSummary) return;
  mcpList.textContent = "";
  mcpSummary.textContent = "";
  if (!mcpConfig) {
    mcpSummary.appendChild(createTag("MCP config unavailable", "warn"));
    return;
  }
  const servers = Array.isArray(mcpConfig.servers) ? mcpConfig.servers : [];
  const hash = mcpConfig.fingerprint?.hash ? mcpConfig.fingerprint.hash.slice(0, 8) : "";
  const mtime = mcpConfig.fingerprint?.mtime ? new Date(mcpConfig.fingerprint.mtime * 1000).toLocaleString() : "";

  mcpSummary.appendChild(createTag(`servers: ${servers.length}`, "accent"));
  if (hash) mcpSummary.appendChild(createTag(`hash: ${hash}`));
  if (mtime) mcpSummary.appendChild(createTag(`mtime: ${mtime}`));

  if (!servers.length) {
    mcpList.appendChild(createStateCard("No MCP servers configured.", "empty"));
    return;
  }

  servers.forEach((server) => {
    const item = document.createElement("div");
    item.className = "list-item";
    const title = document.createElement("strong");
    title.textContent = server.name || "mcp";
    const meta = document.createElement("div");
    meta.className = "agent-sub";
    const parts = [];
    if (server.transport) parts.push(`transport: ${server.transport}`);
    if (server.url) parts.push(`url: ${server.url}`);
    if (server.command) parts.push(`command: ${server.command}`);
    meta.textContent = parts.join(" · ");
    item.appendChild(title);
    item.appendChild(meta);
    mcpList.appendChild(item);
  });

  const mcpCount = latestTools.filter((tool) => tool.is_mcp).length;
  if (servers.length && mcpCount === 0) {
    mcpList.appendChild(createStateCard("MCP servers configured, but no MCP tools loaded.", "error"));
  }
};

const updateToolsCounts = (tools) => {
  if (!toolsCountTotal || !toolsCountBuiltin || !toolsCountMcp) return;
  const total = tools.length;
  const builtin = tools.filter((tool) => tool.server === "builtin").length;
  const mcp = tools.filter((tool) => tool.is_mcp).length;
  toolsCountTotal.textContent = `Total: ${total}`;
  toolsCountBuiltin.textContent = `Builtin: ${builtin}`;
  toolsCountMcp.textContent = `MCP: ${mcp}`;
};

const applyToolsFilters = (tools) => {
  const search = toolsSearchInput ? toolsSearchInput.value.trim().toLowerCase() : "";
  const sourceFilter = toolsSourceFilter ? toolsSourceFilter.value : "all";
  const scopeFilter = toolsScopeFilter ? toolsScopeFilter.value : "all";
  const filtered = tools.filter((tool) => {
    if (sourceFilter === "builtin" && tool.server !== "builtin") return false;
    if (sourceFilter === "mcp" && !tool.is_mcp) return false;
    if (scopeFilter !== "all" && tool.scope !== scopeFilter) return false;
    if (search) {
      const hay = `${tool.name} ${tool.description || ""}`.toLowerCase();
      if (!hay.includes(search)) return false;
    }
    return true;
  });

  const sortKey = toolsSortSelect ? toolsSortSelect.value : "name";
  if (sortKey === "last_used") {
    filtered.sort((a, b) => {
      const aTs = Date.parse(toolStats[a.name]?.last_used || "");
      const bTs = Date.parse(toolStats[b.name]?.last_used || "");
      if (Number.isNaN(aTs) && Number.isNaN(bTs)) return 0;
      if (Number.isNaN(aTs)) return 1;
      if (Number.isNaN(bTs)) return -1;
      return bTs - aTs;
    });
  } else {
    filtered.sort((a, b) => a.name.localeCompare(b.name));
  }
  return filtered;
};

const renderTools = () => {
  if (!toolsList) return;
  if (!latestTools.length) {
    setListState(toolsList, "No tools found.", "empty");
    return;
  }
  const filtered = applyToolsFilters(latestTools);
  if (!filtered.length) {
    setListState(toolsList, "No tools match current filter.", "empty");
    return;
  }

  toolsList.textContent = "";
  filtered.forEach((tool) => {
    const card = document.createElement("div");
    card.className = "card tool-card";
    card.dataset.toolName = tool.name;
    if (tool.name === selectedToolName) {
      card.classList.add("active");
    }

    const title = document.createElement("h4");
    title.textContent = tool.name;
    const desc = document.createElement("p");
    desc.textContent = tool.description || "";

    const tagRow = document.createElement("div");
    tagRow.className = "tag-row";
    tagRow.appendChild(createTag(tool.scope || "global"));
    tagRow.appendChild(createTag(tool.server || "builtin", tool.is_mcp ? "warn" : "accent"));

    const stats = toolStats[tool.name] || {};
    const meta = document.createElement("div");
    meta.className = "tool-meta";
    const metaParts = [];
    if (stats.workflows !== undefined) metaParts.push(`workflows: ${stats.workflows}`);
    if (stats.last_used) metaParts.push(`last: ${formatDate(stats.last_used)}`);
    meta.textContent = metaParts.length ? metaParts.join(" · ") : "workflows: 0";

    card.appendChild(title);
    card.appendChild(desc);
    card.appendChild(tagRow);
    card.appendChild(meta);

    card.addEventListener("click", () => selectTool(tool));
    toolsList.appendChild(card);
  });
};

const selectTool = async (tool) => {
  selectedToolName = tool.name;
  document.querySelectorAll(".tool-card").forEach((card) => {
    card.classList.toggle("active", card.dataset.toolName === tool.name);
  });
  setToolDetailEmpty("Loading...");
  try {
    const res = await fetch(`/api/tools/${encodeURIComponent(tool.name)}`);
    if (!res.ok) {
      throw new Error("request failed");
    }
    const detail = await res.json();
    renderToolDetail(detail);
  } catch (err) {
    setToolDetailEmpty("加载失败");
  }
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
  setToolDetailEmpty("选择一个 Tool 查看详情");
  try {
    const userId = userIdInput ? userIdInput.value.trim() : "";
    const statsUrl = buildToolsStatsUrl(userId);
    const results = await Promise.allSettled([
      fetch("/api/tools"),
      fetch(statsUrl),
      fetch("/api/tools/mcp"),
    ]);

    const toolsRes = results[0].status === "fulfilled" ? results[0].value : null;
    const statsRes = results[1].status === "fulfilled" ? results[1].value : null;
    const mcpRes = results[2].status === "fulfilled" ? results[2].value : null;

    if (!toolsRes || !toolsRes.ok) {
      throw new Error("request failed");
    }
    latestTools = await toolsRes.json();
    if (!Array.isArray(latestTools) || !latestTools.length) {
      setListState(toolsList, "No tools found.", "empty");
      return;
    }

    if (statsRes && statsRes.ok) {
      const statsJson = await statsRes.json();
      toolStats = statsJson?.tools || {};
    } else {
      toolStats = {};
    }

    if (mcpRes && mcpRes.ok) {
      mcpConfig = await mcpRes.json();
    } else {
      mcpConfig = null;
    }

    updateToolsCounts(latestTools);
    renderTools();
    renderMcpConfig();
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

if (toolsSearchBtn) {
  toolsSearchBtn.addEventListener("click", renderTools);
}
if (toolsSearchInput) {
  toolsSearchInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      renderTools();
    }
  });
  toolsSearchInput.addEventListener("input", () => {
    renderTools();
  });
}
if (toolsSourceFilter) {
  toolsSourceFilter.addEventListener("change", renderTools);
}
if (toolsScopeFilter) {
  toolsScopeFilter.addEventListener("change", renderTools);
}
if (toolsSortSelect) {
  toolsSortSelect.addEventListener("change", renderTools);
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
