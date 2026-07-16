const statusIndicator = document.getElementById("statusIndicator");
const readinessBanner = document.getElementById("readinessBanner");
const readinessTitle = document.getElementById("readinessTitle");
const readinessComponents = document.getElementById("readinessComponents");
const readinessHint = document.getElementById("readinessHint");
const tabs = document.querySelectorAll(".tab");
const panels = document.querySelectorAll(".panel");

const userIdInput = document.getElementById("userId");
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

const planningOutput = document.getElementById("planningOutput");
const executionOutput = document.getElementById("executionOutput");
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
const agentsFilterSelect = document.getElementById("agentsFilter");
const agentsSortSelect = document.getElementById("agentsSort");
const coorCount = document.getElementById("coorCount");
const clearCoorBtn = document.getElementById("clearCoorBtn");
const healthCheckSelectedBtn = document.getElementById("healthCheckSelected");
const agentDetail = document.getElementById("agentDetail");
const toolsList = document.getElementById("toolsList");
const toolDetail = document.getElementById("toolDetail");
const mcpList = document.getElementById("mcpList");
const mcpSummary = document.getElementById("mcpSummary");
const mcpToggle = document.getElementById("mcpToggle");
const mcpContent = document.getElementById("mcpContent");
const workflowsList = document.getElementById("workflowsList");
const workflowDetail = document.getElementById("workflowDetail");
const mermaidContainer = document.getElementById("mermaidContainer");
const workflowsPrevPageBtn = document.getElementById("workflowsPrevPage");
const workflowsNextPageBtn = document.getElementById("workflowsNextPage");
const workflowsPageInfo = document.getElementById("workflowsPageInfo");
const workflowsPageSizeSelect = document.getElementById("workflowsPageSize");
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
const mainAgentDecisionCard = document.getElementById("mainAgentDecisionCard");
const routingDecisionBadge = document.getElementById("routingDecisionBadge");
const taskProfileView = document.getElementById("taskProfileView");
const routingCandidatesView = document.getElementById("routingCandidatesView");
const routingExcludedView = document.getElementById("routingExcludedView");

let currentAbortController = null;
let planningOutputBlocks = new Map();
let executionOutputBlocks = new Map();
let executionStepCards = [];       // Step cards for execution log: {id, agentName, displayName, status, content, startTime, endTime, summary}
let currentStepCard = null;        // Currently active (running) step card
let executionStepCount = 0;        // Monotonic step counter
let flowSteps = [];
let activeStepIndex = -1;
const MAX_FLOW_STEPS = 40;
let autoScrollEnabled = true;
let selectedWorkflowId = null;
let selectedAgentName = null;
let selectedCoorAgents = new Set();
let latestAgents = [];
let agentFilter = "all";
let agentSort = "name";
let agentSearchQuery = "";
let agentSearchTimeout = null;
let agentHealth = {};
let agentStats = {};
let latestTools = [];
let toolStats = {};
let selectedToolName = null;
let mcpConfig = null;
let plannerBuffer = "";
let plannerFinalMessageBuffer = "";
let plannerCollecting = false;
let planSteps = [];
let plannerOnlyMode = false;
let plannerOnlyController = null;
let plannerOnlyTimeoutId = null;
let plannerOnlyStepsUpdated = false;
let instructionHistory = [];
let originalUserQuery = "";
let currentRunContext = null;
let executionInProgress = false;
let runtimeCanRun = false;
let workflowsPage = 1;
let workflowsPageSize = 5;
let workflowsTotal = 0;
let workflowsTotalPages = 0;
const PLANNER_ONLY_TIMEOUT_MS = 50000;

mermaid.initialize({ startOnLoad: false, theme: "default" });

const updateWorkflowsPagination = () => {
  if (!workflowsPageInfo || !workflowsPrevPageBtn || !workflowsNextPageBtn) return;

  const hasPages = workflowsTotalPages > 0;
  const currentPage = hasPages ? Math.min(workflowsPage, workflowsTotalPages) : 0;
  workflowsPageInfo.textContent = workflowsTotal
    ? `Page ${currentPage} / ${workflowsTotalPages} | Total ${workflowsTotal}`
    : "";

  workflowsPrevPageBtn.disabled = !hasPages || workflowsPage <= 1;
  workflowsNextPageBtn.disabled = !hasPages || workflowsPage >= workflowsTotalPages;
};

const setStatus = (text, active = true) => {
  statusIndicator.querySelector(".label").textContent = text;
  statusIndicator.querySelector(".dot").style.background = active ? "#4be3ac" : "#ff6a6a";
  statusIndicator.querySelector(".dot").style.boxShadow = active
    ? "0 0 12px rgba(75, 227, 172, 0.7)"
    : "0 0 12px rgba(255, 106, 106, 0.7)";
};

const readinessChip = (label, ok) =>
  `<span class="readiness-chip${ok ? "" : " warn"}">${label}</span>`;

const loadReadiness = async () => {
  if (!readinessBanner) return;
  runtimeCanRun = false;
  runBtn.disabled = true;
  readinessBanner.className = "readiness-banner loading";
  try {
    const response = await fetch("/api/health/ready");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    const components = data.components || {};
    const modelsReady = Boolean(components.models?.configured);
    const agentsReady = Boolean(components.agents?.ready);
    const searchReady = Boolean(components.search?.configured);
    const mcpConfigured = Boolean(components.mcp?.configured);
    runtimeCanRun = modelsReady && agentsReady;

    readinessBanner.className = `readiness-banner ${data.ready ? "ready" : "degraded"}`;
    readinessTitle.textContent = data.ready ? "核心运行环境已就绪" : "运行环境部分可用";
    readinessComponents.innerHTML = [
      readinessChip("页面可用", true),
      readinessChip(modelsReady ? "模型已配置" : "模型未配置", modelsReady),
      readinessChip(
        agentsReady ? `Agent ${components.agents?.count || 0} 个` : "Agent 未就绪",
        agentsReady,
      ),
      readinessChip(searchReady ? "规划搜索可用" : "规划搜索未配置", searchReady),
      readinessChip(
        mcpConfigured ? `MCP 已配置 ${components.mcp?.server_count || 0} 个` : "MCP 未配置",
        mcpConfigured,
      ),
    ].join("");

    if (!modelsReady) {
      readinessHint.textContent = "缺少基础模型或推理模型配置：可以浏览页面，但暂不能运行任务。";
    } else if (!agentsReady) {
      readinessHint.textContent = components.agents?.error || "Agent 初始化失败，请查看服务端日志。";
    } else if (!searchReady) {
      readinessHint.textContent = "核心工作流可运行；未配置 Tavily，已自动关闭规划前搜索。";
    } else {
      readinessHint.textContent = "页面、模型和 Agent 已就绪；MCP 状态按具体服务配置独立判断。";
    }

    if (searchBeforeInput && !searchReady) {
      searchBeforeInput.checked = false;
      searchBeforeInput.disabled = true;
      searchBeforeInput.title = components.search?.reason || "规划前搜索未配置";
    }
    runBtn.disabled = !runtimeCanRun;
  } catch (error) {
    runtimeCanRun = false;
    readinessBanner.className = "readiness-banner error";
    readinessTitle.textContent = "运行环境检查失败";
    readinessComponents.innerHTML = readinessChip("就绪接口不可用", false);
    readinessHint.textContent = error.message || String(error);
    runBtn.disabled = true;
  }
};

const resetSummary = () => {
  summaryFlow.innerHTML = "";
  summaryHint.classList.add("hidden");
  summaryHint.classList.remove("error");
  summaryHint.textContent = "";
  flowSteps = [];
  activeStepIndex = -1;
  clearStepCards();
};

const resetPlan = () => {
  if (mainAgentDecisionCard) mainAgentDecisionCard.style.display = "none";
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
  plannerFinalMessageBuffer = "";
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
  if (confirmExecuteBtn) {
    const hasPlan = planSteps.length > 0;
    const hasWorkflowId = workflowIdInput && workflowIdInput.value.trim();
    confirmExecuteBtn.disabled = executionInProgress || !(hasPlan && hasWorkflowId);
    confirmExecuteBtn.textContent = executionInProgress ? "Executing..." : "Confirm execution";
  }
  if (nlPlanEditBtn) {
    nlPlanEditBtn.disabled = executionInProgress;
  }
  if (validatePlanBtn) {
    validatePlanBtn.disabled = executionInProgress;
  }
  const hasPlan = planSteps.length > 0;
  if (retryPlanBtn) {
    retryPlanBtn.disabled = executionInProgress || !instructionHistory.length;
  }
  if (addPlanStepBtn) {
    addPlanStepBtn.disabled = executionInProgress;
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
  showPlanNlHint("Please enter an instruction for updating the plan.");
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

const getOutputContainer = (phase = "planning") => (phase === "executing" ? executionOutput : planningOutput);

const getOutputBlocks = (phase = "planning") => (phase === "executing" ? executionOutputBlocks : planningOutputBlocks);

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
      arrow.textContent = "->";
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
    showPlanValidationHint("No plan steps yet.", true);
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
    titleLine.textContent = step.title || "Untitled step";
    const descLine = document.createElement("div");
    descLine.textContent = step.description || "No description";
    const metaLine = document.createElement("div");
    metaLine.className = "meta";
    const roleText = step.agent_name ? `Agent: ${step.agent_name}` : "Agent: auto";
    const noteText = step.note ? `Note: ${step.note}` : "Note: none";
    metaLine.textContent = `${roleText} | ${noteText}`;

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
    errors.push("Plan is empty. Please add at least one step.");
  }

  const agentNames = new Set(
    Array.isArray(latestAgents) ? latestAgents.map((agent) => agent.agent_name).filter(Boolean) : []
  );
  if (!agentNames.size) {
    errors.push("Agent list is not loaded, so execution roles cannot be validated.");
  }

  planSteps.forEach((step, idx) => {
    const stepErrors = [];
    if (!step.title || !step.title.trim()) {
      stepErrors.push("Missing title");
    }
    if (step.agent_name && agentNames.size && !agentNames.has(step.agent_name)) {
      stepErrors.push(`Unknown execution agent: ${step.agent_name}`);
    }
    if (stepErrors.length) {
      errorsByIndex[idx] = stepErrors;
      errors.push(`Step ${idx + 1}: ${stepErrors.join("; ")}`);
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
    showPlanNlHint("Please enter an instruction for updating the plan.", true);
    return;
  }

  if (appendHistory) {
    instructionHistory = [...instructionHistory, instruction];
  }

  plannerOnlyMode = true;
  plannerBuffer = "";
  plannerCollecting = false;
  plannerOnlyStepsUpdated = false;
  showPlanNlHint("Generating an updated plan...");

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
          "Please regenerate the plan from the full instruction history and return JSON steps only. Do not include extra explanation.\n\nLatest update: " +
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
      showPlanNlHint("Planner request timed out. Please refine the instruction and try again.", true);
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
    }
  } catch (err) {
    if (err?.name !== "AbortError") {
      showPlanNlHint(`Planner request failed: ${err.message || err}`, true);
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

// Step Card Functions (Execution Log)

const clearStepCards = () => {
  executionStepCards = [];
  currentStepCard = null;
  executionStepCount = 0;
  if (executionOutput) executionOutput.innerHTML = "";
};

const createStepCard = (displayName, subAgentName) => {
  const card = {
    id: ++executionStepCount,
    agentName: subAgentName || displayName,
    displayName: displayName,
    status: "running",
    content: "",
    startTime: Date.now(),
    endTime: null,
    summary: "",
  };
  executionStepCards.push(card);
  currentStepCard = card;
  renderAllStepCards();
  if (autoScrollEnabled && executionOutput) {
    executionOutput.scrollTop = executionOutput.scrollHeight;
  }
  return card;
};

const appendStepContent = (content) => {
  if (!currentStepCard) return;
  currentStepCard.content += content;
  const cardEl = executionOutput?.querySelector(`[data-step-id="${currentStepCard.id}"]`);
  if (cardEl) {
    const bodyEl = cardEl.querySelector(".step-card-body");
    if (bodyEl && !bodyEl.classList.contains("hidden")) {
      bodyEl.textContent = currentStepCard.content;
    }
  }
  if (autoScrollEnabled && executionOutput) {
    executionOutput.scrollTop = executionOutput.scrollHeight;
  }
};

const finalizeStepCard = () => {
  if (!currentStepCard) return;
  currentStepCard.status = "done";
  currentStepCard.endTime = Date.now();
  currentStepCard.summary = generateStepSummary(currentStepCard);
  currentStepCard = null;
  renderAllStepCards();
};

const errorStepCard = (errMsg) => {
  if (currentStepCard) {
    currentStepCard.status = "error";
    currentStepCard.endTime = Date.now();
    const plainText = errMsg.replace(/<[^>]*>/g, "").trim();
    currentStepCard.summary = plainText.substring(0, 80) || "Execution error";
    currentStepCard.content = errMsg;
    currentStepCard._isHtml = true;
    currentStepCard = null;
  }
  renderAllStepCards();
};

const generateStepSummary = (card) => {
  const duration = card.endTime ? `${Math.round((card.endTime - card.startTime) / 1000)}s` : "";
  const raw = (card.content || "").trim();
  if (!raw) return duration;

  let parsed = null;
  try { parsed = JSON.parse(raw); } catch (e) { /* not JSON */ }

  // Unwrap {tool, result} wrapper
  if (parsed && parsed.result !== undefined) {
    parsed = parsed.result;
  }

  // Array of records
  if (Array.isArray(parsed) && parsed.length > 0) {
    const first = parsed[0];
    if (first.adtEmpeNm || first.name) {
      const name = first.adtEmpeNm || first.name;
      const email = first.internalMaiBox || "";
      return `${name}${email ? " (" + email + ")" : ""} | ${parsed.length} record(s) | ${duration}`;
    }
    return `Returned ${parsed.length} record(s) | ${duration}`;
  }

  // Object result
  if (parsed && typeof parsed === "object") {
    if (parsed.status === "error") return `Error: ${parsed.message || ""} | ${duration}`;

    // EmailDispatch: {status, sent: {id, to, subject}}
    if (parsed.sent && typeof parsed.sent === "object") {
      const s = parsed.sent;
      return `Sent to ${s.to || "?"} | ${s.id || ""} | ${duration}`;
    }

    // ReportAgent: {status, markdown: "..."}
    if (parsed.markdown) {
      const firstLine = (parsed.markdown || "").split("\n")[0].replace(/^#+\s*/, "");
      return `Generated ${firstLine.substring(0, 40)} | ${duration}`;
    }

    // Records with matched_count
    if (parsed.matched_count) {
      return `Matched ${parsed.matched_count} record(s) | ${duration}`;
    }

    if (parsed.status === "success") return `Execution succeeded | ${duration}`;
    return `Returned data | ${duration}`;
  }

  // Fallback: first 80 chars
  const preview = raw.replace(/\s+/g, " ").substring(0, 80);
  return `${preview}${raw.length > 80 ? "..." : ""} | ${duration}`;
};

const renderAllStepCards = () => {
  if (!executionOutput) return;
  const frag = document.createDocumentFragment();
  executionStepCards.forEach((card) => {
    renderStepCardInto(card, frag);
  });
  executionOutput.innerHTML = "";
  executionOutput.appendChild(frag);
};

const renderStepCardInto = (card, parent) => {
  const total = planSteps.length > 0 ? planSteps.length : executionStepCards.length;
  const duration = card.endTime
    ? `${Math.round((card.endTime - card.startTime) / 1000)}s`
    : (card.status === "running" ? "..." : "");

  const iconMap = { running: "[...]", done: "[ok]", error: "[x]", pending: "[ ]" };
  const icon = iconMap[card.status] || "[ ]";

  const cardEl = document.createElement("div");
  cardEl.className = `step-card ${card.status}`;
  cardEl.dataset.stepId = card.id;

  // Header (clickable toggle)
  const header = document.createElement("div");
  header.className = "step-card-header";
  header.addEventListener("click", () => {
    const body = cardEl.querySelector(".step-card-body");
    const toggle = cardEl.querySelector(".step-toggle");
    if (body) body.classList.toggle("hidden");
    if (toggle) toggle.textContent = body?.classList.contains("hidden") ? ">" : "v";
  });

  header.innerHTML =
    `<span class="step-status-icon">${icon}</span>` +
    `<span class="step-index">${card.id}/${total}</span>` +
    `<span class="step-agent-name">${escapeHtml(card.agentName)}</span>` +
    `<span class="step-summary-text">${escapeHtml(card.summary || (card.status === "running" ? "Running..." : ""))}</span>` +
    `<span class="step-duration">${duration}</span>` +
    `<span class="step-toggle">></span>`;

  // Body (collapsed by default, but expanded for error cards)
  const isError = card.status === "error";
  const body = document.createElement("div");
  body.className = `step-card-body${isError ? "" : " hidden"}`;
  if (card.content) {
    if (card._isHtml) {
      const div = document.createElement("div");
      div.className = "step-result";
      div.innerHTML = card.content;
      body.appendChild(div);
    } else {
      body.appendChild(formatResult(card.content));
    }
  }

  cardEl.appendChild(header);
  cardEl.appendChild(body);
  parent.appendChild(cardEl);

  // Fix toggle icon for error cards (since header was rendered with innerHTML before body existed)
  if (isError) {
    const toggle = cardEl.querySelector(".step-toggle");
    if (toggle) toggle.textContent = "v";
  }
};

// Result Formatting

const formatResult = (rawContent) => {
  const wrap = document.createElement("div");
  wrap.className = "step-result";

  let parsed = null;
  try { parsed = JSON.parse(rawContent); } catch (e) { /* not JSON */ }

  // Unwrap {tool, result} wrapper
  if (parsed && parsed.result !== undefined) {
    parsed = parsed.result;
  }

  if (parsed && Array.isArray(parsed) && parsed.length > 0 && typeof parsed[0] === "object") {
    wrap.appendChild(buildResultTable(parsed));
    return wrap;
  }

  if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
    wrap.appendChild(buildKeyValueList(parsed, 0));
    return wrap;
  }

  const pre = document.createElement("pre");
  pre.className = "step-result-pre";
  pre.textContent = parsed ? JSON.stringify(parsed, null, 2) : rawContent;
  wrap.appendChild(pre);
  return wrap;
};

const buildResultTable = (records) => {
  const wrapper = document.createElement("div");
  wrapper.className = "step-result-table-wrapper";

  const MAX_ROWS = 10;
  const displayRecords = records.slice(0, MAX_ROWS);
  const hasMore = records.length > MAX_ROWS;
  const cols = Object.keys(displayRecords[0]);

  const table = document.createElement("table");
  table.className = "step-result-table";

  const thead = document.createElement("thead");
  const hr = document.createElement("tr");
  cols.forEach((c) => { const th = document.createElement("th"); th.textContent = c; hr.appendChild(th); });
  thead.appendChild(hr);
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  displayRecords.forEach((row) => {
    const tr = document.createElement("tr");
    cols.forEach((col) => {
      const td = document.createElement("td");
      const val = row[col];
      td.textContent = val === null || val === undefined ? "-" : maskSensitiveIfNeeded(col, String(val));
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  wrapper.appendChild(table);

  if (hasMore) {
    const hint = document.createElement("div");
    hint.className = "step-result-hint";
    hint.textContent = `Total ${records.length} record(s), showing first ${MAX_ROWS}.`;
    wrapper.appendChild(hint);
  }
  return wrapper;
};

const buildKeyValueList = (obj, depth) => {
  depth = depth || 0;
  const dl = document.createElement("dl");
  dl.className = depth === 0 ? "step-result-kv" : "step-result-kv step-result-kv-nested";

  const isTextField = (k, v) => {
    const textKeys = /markdown|body|content|description|summary|report|text/i;
    return typeof v === "string" && (textKeys.test(k) || v.length > 200);
  };

  const looksLikeMarkdown = (v) => {
    return typeof v === "string" && (/^#{1,4}\s/m.test(v) || /\*\*/.test(v) || /\n[-*]\s/m.test(v) || /\n\d+\.\s/m.test(v));
  };

  Object.entries(obj).forEach(([k, v]) => {
    const dt = document.createElement("dt");
    dt.textContent = k;
    const dd = document.createElement("dd");

    if (v === null || v === undefined) {
      dd.textContent = "-";
    } else if (isTextField(k, v)) {
      if (looksLikeMarkdown(v)) {
        const mdDiv = document.createElement("div");
        mdDiv.className = "step-result-md";
        mdDiv.innerHTML = renderMarkdown(v);
        dd.appendChild(mdDiv);
      } else {
        const pre = document.createElement("pre");
        pre.className = "step-result-pre";
        pre.textContent = v;
        dd.appendChild(pre);
      }
    } else if (Array.isArray(v) && v.length > 0 && typeof v[0] === "object" && depth === 0) {
      dd.appendChild(buildResultTable(v));
    } else if (typeof v === "object" && depth < 1) {
      dd.appendChild(buildKeyValueList(v, depth + 1));
    } else if (typeof v === "object") {
      const pre = document.createElement("pre");
      pre.className = "step-result-pre";
      pre.textContent = JSON.stringify(v, null, 2);
      dd.appendChild(pre);
    } else {
      dd.textContent = maskSensitiveIfNeeded(k, String(v));
    }

    dl.appendChild(dt);
    dl.appendChild(dd);
  });
  return dl;
};

const maskSensitiveIfNeeded = (fieldName, value) => {
  const sensitivePattern = /identity|id_card|idcard|ssn|password|secret|token|phone|mobile|^a\d{5,}$|officePhone|internalMaiBox|idvId/i;
  if (sensitivePattern.test(fieldName) && value.length > 4) {
    return value.substring(0, 2) + "****" + value.substring(value.length - 2);
  }
  return value;
};

const escapeHtml = (str) => {
  const div = document.createElement("div");
  div.textContent = str || "";
  return div.innerHTML;
};

const renderRoutingDecision = (eventData) => {
  if (!mainAgentDecisionCard) return;
  const profile = eventData?.task_profile || {};
  const route = eventData?.routing_decision || {};
  const candidates = Array.isArray(route.candidate_agents) ? route.candidate_agents : [];
  const excluded = Array.isArray(route.excluded_agents) ? route.excluded_agents : [];
  const decision = route.decision || "UNKNOWN";
  mainAgentDecisionCard.style.display = "";
  routingDecisionBadge.textContent = `${decision} · ${Math.round((route.confidence || 0) * 100)}%`;
  routingDecisionBadge.className = `tag ${decision === "DISPATCH" ? "accent" : "warn"}`;

  const profileItems = [
    ["意图", profile.intent || "-"],
    ["任务类型", profile.task_type || "-"],
    ["动作", profile.action || profile.operation_mode || "-"],
    ["风险", profile.risk_level || profile.risk_profile || "-"],
    ["数据范围", Array.isArray(profile.data_scope) ? profile.data_scope.join(", ") : profile.data_scope || "-"],
    ["期望能力", (profile.expected_capabilities || []).join(", ") || "-"],
    ["缺失字段", (profile.missing_fields || []).join(", ") || "无"],
    ["画像置信度", `${Math.round((profile.confidence || 0) * 100)}%`],
  ];
  taskProfileView.innerHTML = profileItems
    .map(([label, value]) => `<div class="decision-profile-item"><small>${escapeHtml(label)}</small>${escapeHtml(String(value))}</div>`)
    .join("");

  routingCandidatesView.innerHTML = candidates.length
    ? candidates.map((item) => `
        <div class="decision-item">
          <strong>${escapeHtml(item.agent_id || "-")}</strong>
          <span class="decision-score">${Math.round((item.score || 0) * 100)}%</span>
          <div class="decision-reason">${escapeHtml((item.reason_codes || []).join(" · "))}</div>
        </div>`).join("")
    : '<div class="decision-item decision-reason">没有合法候选 Agent</div>';

  const visibleExcluded = excluded.slice(0, 8);
  routingExcludedView.innerHTML = visibleExcluded.length
    ? visibleExcluded.map((item) => `
        <div class="decision-item">
          <strong>${escapeHtml(item.agent_id || "-")}</strong>
          <div class="decision-reason">${escapeHtml(item.reason_code || "")}：${escapeHtml(item.reason || "")}</div>
        </div>`).join("")
        + (excluded.length > visibleExcluded.length
          ? `<div class="decision-reason">另有 ${excluded.length - visibleExcluded.length} 个排除项</div>`
          : "")
    : '<div class="decision-item decision-reason">没有被排除的 Agent</div>';
};

// Lightweight Markdown -> HTML

const renderMarkdown = (md) => {
  if (!md) return "";
  const lines = md.split("\n");
  const out = [];
  let inTable = false;
  let tableRows = [];
  let inCodeBlock = false;
  let codeBuf = [];
  let inList = null;  // "ul" | "ol" | null

  const flushTable = () => {
    if (tableRows.length === 0) return;
    const tbl = ["<table class=\"md-table\">"];
    tableRows.forEach((row, i) => {
      const tag = i === 0 ? "th" : "td";
      tbl.push("<tr>");
      row.forEach((cell) => {
        tbl.push(`<${tag}>${inlineMarkdown(cell.trim())}</${tag}>`);
      });
      tbl.push("</tr>");
    });
    tbl.push("</table>");
    out.push(tbl.join(""));
    tableRows = [];
    inTable = false;
  };

  const flushList = () => {
    if (inList === "ul") out.push("</ul>");
    if (inList === "ol") out.push("</ol>");
    inList = null;
  };

  const flushCodeBlock = () => {
    if (!inCodeBlock) return;
    out.push("<pre class=\"md-code\"><code>" + escapeHtml(codeBuf.join("\n")) + "</code></pre>");
    codeBuf = [];
    inCodeBlock = false;
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];

    // Code block toggle
    if (line.startsWith("```")) {
      if (inCodeBlock) {
        flushCodeBlock();
        continue;
      } else {
        flushList();
        flushTable();
        inCodeBlock = true;
        continue;
      }
    }
    if (inCodeBlock) {
      codeBuf.push(line);
      continue;
    }

    // Empty line - close lists / tables
    if (line.trim() === "") {
      flushList();
      flushTable();
      continue;
    }

    // Table row
    if (line.includes("|")) {
      flushList();
      const cells = line.split("|").map((c) => c.trim()).filter((c) => c !== "");
      // Skip separator rows like |---|---|
      if (cells.every((c) => /^[-:]+$/.test(c))) {
        continue;
      }
      if (!inTable) {
        inTable = true;
        tableRows = [];
      }
      tableRows.push(cells);
      continue;
    } else {
      flushTable();
    }

    // Headings
    const hMatch = line.match(/^(#{1,4})\s+(.+)/);
    if (hMatch) {
      flushList();
      const level = hMatch[1].length;
      out.push(`<h${level} class=\"md-h${level}\">${inlineMarkdown(hMatch[2])}</h${level}>`);
      continue;
    }

    // Horizontal rule
    if (/^[-*_]{3,}$/.test(line.trim())) {
      flushList();
      out.push("<hr class=\"md-hr\">");
      continue;
    }

    // Unordered list
    const ulMatch = line.match(/^(\s*)[-*]\s+(.+)/);
    if (ulMatch) {
      if (inList !== "ul") { flushList(); out.push("<ul class=\"md-ul\">"); inList = "ul"; }
      out.push(`<li>${inlineMarkdown(ulMatch[2])}</li>`);
      continue;
    }

    // Ordered list
    const olMatch = line.match(/^(\s*)\d+\.\s+(.+)/);
    if (olMatch) {
      if (inList !== "ol") { flushList(); out.push("<ol class=\"md-ol\">"); inList = "ol"; }
      out.push(`<li>${inlineMarkdown(olMatch[2])}</li>`);
      continue;
    }

    // Paragraph
    flushList();
    out.push(`<p class=\"md-p\">${inlineMarkdown(line)}</p>`);
  }

  flushList();
  flushTable();
  flushCodeBlock();
  return out.join("");
};

const inlineMarkdown = (text) => {
  let t = escapeHtml(text);
  // Bold **text**
  t = t.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  // Italic *text* (but not inside words)
  t = t.replace(/(?<!\*)\*(?!\*)([^*]+?)\*(?!\*)/g, "<em>$1</em>");
  // Inline code `text`
  t = t.replace(/`([^`]+)`/g, "<code class=\"md-inline-code\">$1</code>");
  return t;
};

// Original Output Block (planning log)

const ensureOutputBlock = (agentName, phase = "planning") => {
  const outputContainer = getOutputContainer(phase);
  const outputBlocks = getOutputBlocks(phase);
  if (!outputContainer) return null;
  if (!outputBlocks.has(agentName)) {
    const block = document.createElement("div");
    block.className = "output-block";
    const title = document.createElement("h4");
    title.textContent = agentName;
    const pre = document.createElement("pre");
    pre.textContent = "";
    block.appendChild(title);
    block.appendChild(pre);
    outputContainer.appendChild(block);
    outputBlocks.set(agentName, pre);
  }
  return outputBlocks.get(agentName);
};

const appendOutputImmediate = (agentName, content, phase = "planning") => {
  const outputContainer = getOutputContainer(phase);
  const target = ensureOutputBlock(agentName || "system", phase);
  if (!target || !outputContainer) return;
  target.textContent += content;
  if (autoScrollEnabled) {
    outputContainer.scrollTop = outputContainer.scrollHeight;
  }
};

const appendOutput = (agentName, content, phase = currentRunContext === "executing" ? "executing" : "planning") => {
  appendOutputImmediate(agentName, content, phase);
};

const refreshPlannerTimeout = () => {
  if (!plannerOnlyMode || !plannerOnlyTimeoutId) return;
  clearTimeout(plannerOnlyTimeoutId);
  plannerOnlyTimeoutId = setTimeout(() => {
    if (plannerOnlyController) {
      plannerOnlyController.abort();
    }
    showPlanNlHint("Planner request timed out. Please refine the instruction and try again.", true);
    plannerOnlyMode = false;
    plannerOnlyController = null;
  }, PLANNER_ONLY_TIMEOUT_MS);
};

const applyPlannerStepsFromBuffer = (buffer, options = {}) => {
  const { finalize = false } = options;
  const parsed = extractJsonFromText(buffer);
  const steps = normalizePlanSteps(parsed);
  if (!steps.length) {
    if (finalize) {
      const emptyStepsMessage = "Planner returned valid JSON, but no executable steps were generated.";
      const invalidJsonMessage = "Planner output is not valid JSON steps.";
      showPlanHint(parsed ? emptyStepsMessage : invalidJsonMessage, true);
      if (plannerOnlyMode) {
        showPlanNlHint(
          parsed
            ? "Planner completed, but the steps list is empty. Please refine the instruction and try again."
            : "Unable to parse planner output. Please refine the instruction and try again.",
          true
        );
      }
    }
    return false;
  }

  planSteps = steps.map((step) => normalizeStep(step));
  plannerOnlyStepsUpdated = true;
  renderPlanSummary(planSteps);
  renderPlanEditor();
  showPlanValidationHint("Plan updated. You can continue refining it with natural language.");

  if (finalize && plannerOnlyMode && plannerOnlyController) {
    plannerOnlyController.abort();
    showPlanNlHint("A new plan has been generated from your instruction.");
    closePlanModal();
  }
  return true;
};

const clearOutput = () => {
  if (planningOutput) planningOutput.innerHTML = "";
  if (executionOutput) executionOutput.innerHTML = "";
  planningOutputBlocks = new Map();
  executionOutputBlocks = new Map();
  clearStepCards();
};

const clearOutputPhase = (phase = "planning") => {
  const outputContainer = getOutputContainer(phase);
  if (outputContainer) outputContainer.innerHTML = "";
  if (phase === "executing") {
    executionOutputBlocks = new Map();
    clearStepCards();
  } else {
    planningOutputBlocks = new Map();
  }
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
    if (typeof agentName === "string" && agentName.toLowerCase().includes("planner")) {
      refreshPlannerTimeout();
      plannerFinalMessageBuffer += content;
      return;
    }
    if (!plannerOnlyMode) {
      if (currentRunContext === "executing" && currentStepCard) {
        appendStepContent(content);
      } else if (currentRunContext !== "executing") {
        appendOutput(agentName, content);
      }
    }
    return;
  }
  if (eventName === "planner_delta") {
    const agentName = payload.agent_name || payload.data?.agent_name || "planner";
    const content = payload.data?.delta?.content || "";
    const fullContent = payload.data?.full_content || "";
    const isFinal = Boolean(payload.data?.is_final);
    refreshPlannerTimeout();
    plannerCollecting = !isFinal;
    if (fullContent) {
      plannerBuffer = fullContent;
    } else if (content) {
      plannerBuffer += content;
    }
    if (content && !plannerOnlyMode) {
      appendOutput(agentName, content);
    }
    applyPlannerStepsFromBuffer(plannerBuffer, { finalize: false });
    return;
  }
  if (eventName === "start_of_workflow") {
    if (!plannerOnlyMode) {
      resetSummary();
      if (currentRunContext !== "executing") {
        resetPlan();
        appendOutput("system", `\n[workflow] ${payload.data?.workflow_id || ""}\n`);
      }
    }
    const wfId = payload.data?.workflow_id;
    if (wfId && workflowIdInput) {
      workflowIdInput.value = wfId;
    }
    updateConfirmExecuteState();
    return;
  }
  if (eventName === "routing_decision") {
    renderRoutingDecision(payload.data || {});
    return;
  }
  if (eventName === "start_of_agent") {
    const agentName = payload.data?.agent_name || payload.agent_name || "agent";
    const subAgentName = payload.data?.sub_agent_name || null;
    if (!plannerOnlyMode) {
      pushFlowStep(agentName);
    }
    if (typeof agentName === "string" && agentName.toLowerCase().includes("planner")) {
      plannerCollecting = true;
      plannerBuffer = "";
      plannerFinalMessageBuffer = "";
      showPlanHint("Collecting plan output...");
    }
    if (!plannerOnlyMode) {
      const isExecAgent = currentRunContext === "executing" && agentName.includes("agent_proxy");
      if (isExecAgent) {
        createStepCard(agentName, subAgentName);
      } else if (currentRunContext !== "executing") {
        appendOutput("system", `\n[start_of_agent] ${agentName}\n`);
      }
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
      const fallbackBuffer = plannerFinalMessageBuffer || plannerBuffer;
      if (fallbackBuffer) {
        plannerBuffer = fallbackBuffer;
      }
      applyPlannerStepsFromBuffer(plannerBuffer, { finalize: true });
    }
    if (!plannerOnlyMode) {
      const isExecAgent = currentRunContext === "executing" && agentName.includes("agent_proxy");
      if (isExecAgent) {
        finalizeStepCard();
      } else if (currentRunContext !== "executing") {
        appendOutput("system", `\n[end_of_agent] ${agentName}\n`);
      }
    }
    return;
  }
  if (eventName === "permission_denied") {
    const d = payload.data || {};
    const policyResult = d.policy_result || {};
    const scenarioFit = d.scenario_fit_result || {};
    const subject = d.subject || {};
    const object = d.object || {};
    const action = d.action || {};
    const subjectName = subject.subject_name || subject.attributes?.display_name || "?";
    const subjectRole = subject.attributes?.role || "?";
    const objectName = object.object_name || "?";
    const objectSensitivity = object.attributes?.sensitivity || "?";
    const actionVerb = action.verb || "?";
    const deniedReason = policyResult.reason || d.error || "Insufficient permission";
    const scenarioFitLabelMap = {
      match: "Scenario matched",
      mismatch: "Scenario mismatch",
      uncertain: "Scenario needs review",
    };
    const scenarioFitLabel = scenarioFitLabelMap[scenarioFit.fit] || "Scenario needs review";
    const scenarioFitReason = scenarioFit.reason || "";

    if (window.SecurityModule && window.SecurityModule.displaySecurityEvent) {
      window.SecurityModule.displaySecurityEvent("permission_denied", d);
    }

    if (currentRunContext === "executing") {
      errorStepCard(
        `<strong>S-ABAC Permission Denied</strong><br>` +
        `<div style="margin-top:8px;font-size:13px;color:var(--muted)">` +
        `<div><strong>User:</strong> ${escapeHtml(subjectName)} <span class="tag accent">${escapeHtml(subjectRole)}</span></div>` +
        `<div style="margin-top:4px"><strong>Action:</strong> ${escapeHtml(actionVerb)} -> ${escapeHtml(objectName)} <span class="tag">${escapeHtml(objectSensitivity)}</span></div>` +
        `<div style="margin-top:4px;color:var(--danger)"><strong>Reason:</strong> ${escapeHtml(deniedReason)}</div>` +
        (scenarioFitReason
          ? `<div style="margin-top:4px;color:var(--warning)"><strong>${escapeHtml(scenarioFitLabel)}:</strong> ${escapeHtml(scenarioFitReason)}</div>`
          : ``) +
        `</div>`
      );
    }

    appendOutput("system", `\n[security] S-ABAC permission denied: ${subjectName}(${subjectRole}) tried ${actionVerb} ${objectName}(${objectSensitivity}) - ${deniedReason}${scenarioFitReason ? ` | ${scenarioFitLabel}: ${scenarioFitReason}` : ""}\n`);
    showSummaryHint(`S-ABAC: ${deniedReason}`, true);
    setStatus("Permission Denied", false);
    return;
  }
  if (eventName === "workflow_error") {
    const d = payload.data || {};
    const friendlyReason = d.reason || d.error || "Workflow could not continue.";
    const detail = d.error || friendlyReason;

    if (currentRunContext === "executing") {
      errorStepCard(
        `<strong>Workflow paused</strong><br>` +
        `<div style="margin-top:8px;font-size:13px;color:var(--muted)">` +
        `<div style="color:var(--warning)"><strong>Hint:</strong> The current plan left no executable steps. This can happen after permission checks or agent filtering.</div>` +
        `<div style="margin-top:6px;color:var(--danger)"><strong>Reason:</strong> ${escapeHtml(detail)}</div>` +
        `<div style="margin-top:6px">Try switching the user role, adjusting available agents, or regenerating the plan.</div>` +
        `</div>`
      );
    } else {
      appendOutput(
        "system",
        `\n[workflow_error] ${friendlyReason} | No executable steps remain. Check the plan and permission filtering.\n`
      );
    }

    showSummaryHint("Workflow paused: no executable steps.", true);
    setStatus("Workflow Blocked", false);
    if (currentRunContext === "executing") {
      showPlanValidationHint("The current plan has no executable steps, so the workflow is paused.", true);
    } else {
      showPlanNlHint("The current plan has no executable steps, so the workflow is paused.", true);
    }
    return;
  }
  if (eventName === "end_of_workflow") {
    if (!plannerOnlyMode) {
      showSummaryHint("Workflow completed.");
      if (currentRunContext === "executing") {
        finalizeStepCard();
        showPlanValidationHint("Execution completed. You can review the execution log.");
        showPlanHint("Plan execution completed.");
      } else {
        appendOutput("system", "\n[workflow] completed\n");
      }
    } else if (!plannerOnlyStepsUpdated) {
      showPlanNlHint("Planner completed, but no executable steps were generated. Please refine the instruction and try again.", true);
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
      if (currentRunContext === "executing") {
        errorStepCard(payload.data?.error || payload.raw || "unknown error");
      } else {
        appendOutput("system", `\n[error] ${payload.data?.error || payload.raw || "unknown error"}\n`);
      }
      showPlanValidationHint("Execution failed. You can recover from Task History.", true);
    } else {
      showPlanNlHint(payload.data?.error || payload.raw || "unknown error", true);
    }
    return;
  }
  if (!plannerOnlyMode && currentRunContext !== "executing") {
    appendOutput("system", `\n[${eventName}] ${JSON.stringify(payload)}\n`);
  }
};

const runWorkflow = async () => {
  if (!runtimeCanRun) {
    setStatus("Environment not ready", false);
    readinessBanner?.scrollIntoView({ behavior: "smooth", block: "center" });
    return;
  }
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
  clearOutputPhase("planning");
  clearOutputPhase("executing");
  resetSummary();
  resetPlan();
  instructionHistory = [message];
  originalUserQuery = message;
  currentRunContext = "planning";
  if (workflowIdInput) {
    workflowIdInput.value = "";
  }
  runBtn.disabled = true;
  stopBtn.disabled = false;
  if (confirmExecuteBtn) confirmExecuteBtn.disabled = true;

  const payload = {
    user_id: userId,
    lang: "zh",
    workmode: "launch",
    stop_after_planner: true,
    instruction: message,
    instruction_history: instructionHistory,
    original_user_query: originalUserQuery,
    messages: [{ role: "user", content: message }],
    debug: debugInput.checked,
    deep_thinking_mode: deepThinkingInput.checked,
    search_before_planning: searchBeforeInput.checked,
    coor_agents: selectedCoorAgents.size ? Array.from(selectedCoorAgents) : null,
    workflow_id: null,
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
    currentRunContext = null;
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
    showPlanValidationHint("Workflow ID is required before execution.", true);
    return;
  }
  if (!planSteps.length) {
    showPlanValidationHint("Plan is empty, so execution cannot start.", true);
    return;
  }

  setStatus("Executing", true);
  clearOutputPhase("executing");
  resetSummary();
  currentRunContext = "executing";
  executionInProgress = true;
  showPlanValidationHint("Execution is in progress. Please do not click repeatedly.");
  showPlanHint("Plan execution is in progress. Check the execution log for updates.");
  updateConfirmExecuteState();
  runBtn.disabled = true;
  stopBtn.disabled = false;

  const payload = {
    user_id: userId,
    lang: "zh",
    workmode: "production",
    stop_after_planner: false,
    instruction: null,
    instruction_history: instructionHistory,
    original_user_query: originalUserQuery || instructionHistory[0] || "",
    messages: [{ role: "user", content: "Confirm execution and proceed with the current plan." }],
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
    showPlanValidationHint("Execution failed. Check the execution log and try again.", true);
  } finally {
    currentRunContext = null;
    executionInProgress = false;
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
      showPlanValidationHint("There is no instruction available to retry.", true);
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
    executionInProgress = false;
    setStatus("Stopped", false);
    showSummaryHint("Workflow stopped.");
    showPlanValidationHint("Execution stopped. You can run it again.", true);
    updateConfirmExecuteState();
  }
};

const createStateCard = (text, variant = "info") => {
  const card = document.createElement("div");
  card.className = `card state ${variant}`;

  if (variant === "empty") {
    const icon = document.createElement("div");
    icon.className = "empty-state-icon";
    icon.textContent = "[list]";
    card.appendChild(icon);

    const textEl = document.createElement("div");
    textEl.className = "empty-state-text";
    textEl.textContent = text;
    card.appendChild(textEl);
  } else if (variant === "loading") {
    // Create skeleton loading
    const skeleton = document.createElement("div");
    skeleton.className = "loading-skeleton";
    for (let i = 0; i < 3; i++) {
      const skeletonCard = document.createElement("div");
      skeletonCard.className = "skeleton-card";
      const titleLine = document.createElement("div");
      titleLine.className = "skeleton-line title";
      const line1 = document.createElement("div");
      line1.className = "skeleton-line";
      const line2 = document.createElement("div");
      line2.className = "skeleton-line short";
      skeletonCard.appendChild(titleLine);
      skeletonCard.appendChild(line1);
      skeletonCard.appendChild(line2);
      skeleton.appendChild(skeletonCard);
    }
    card.appendChild(skeleton);
  } else {
    card.textContent = text;
  }

  return card;
};

const setListState = (container, text, variant) => {
  container.textContent = "";
  container.appendChild(createStateCard(text, variant));
};

const updateCoorCount = () => {
  if (!coorCount) return;
  coorCount.textContent = `Selected ${selectedCoorAgents.size}`;
  if (clearCoorBtn) {
    clearCoorBtn.disabled = selectedCoorAgents.size === 0;
  }
  if (healthCheckSelectedBtn) {
    healthCheckSelectedBtn.disabled = selectedCoorAgents.size === 0;
  }
};

const debounce = (func, wait) => {
  let timeout;
  return function executedFunction(...args) {
    const later = () => {
      clearTimeout(timeout);
      func(...args);
    };
    clearTimeout(timeout);
    timeout = setTimeout(later, wait);
  };
};

const getHealthStatus = (agentName) => {
  const health = agentHealth[agentName];
  if (!health) return { status: "unknown", color: "#999" };
  if (health.status === "healthy") return { status: "healthy", color: "#4be3ac" };
  if (health.status === "unhealthy") return { status: "unhealthy", color: "#ff6a6a" };
  return { status: "unknown", color: "#999" };
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
    setAgentDetailEmpty("Select an agent to view details.");
    return;
  }

  agentDetail.textContent = "";
  const title = document.createElement("h3");
  title.textContent = agent.agent_name || "agent";

  const sub = document.createElement("div");
  sub.className = "agent-sub";
  const userLabel = agent.user_id === "share" ? "default" : "user";
  const sourceLabel = agent.source ? ` | ${agent.source}` : "";
  const nick = agent.nick_name ? `${agent.nick_name} | ` : "";
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
  descTitle.textContent = "Description";
  const desc = document.createElement("p");
  desc.textContent = agent.description || "No description";

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
    emptyTools.textContent = "None";
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
  healthRow.textContent = healthParts.length ? healthParts.join(" | ") : "n/a";

  const statsTitle = document.createElement("h4");
  statsTitle.textContent = "Usage";
  const stats = agentStats[agent.agent_name] || {};
  const statsRow = document.createElement("p");
  const statsParts = [];
  if (stats.runs !== undefined) statsParts.push(`runs: ${stats.runs}`);
  if (stats.last_used) statsParts.push(`last: ${stats.last_used}`);
  statsRow.textContent = statsParts.length ? statsParts.join(" | ") : "n/a";

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
  ["Field", "Type", "Required", "Description"].forEach((label) => {
    const th = document.createElement("th");
    th.textContent = label;
    headRow.appendChild(th);
  });
  thead.appendChild(headRow);
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  const required = new Set(schema.required || []);

  const renderRow = (key, value, level = 0, parentPath = "") => {
    const row = document.createElement("tr");
    const isRequired = required.has(key);
    if (isRequired) {
      row.classList.add("required-param");
    }

    const nameCell = document.createElement("td");
    const indent = level > 0 ? `${"  ".repeat(level)}->` : "";

    // Check if this is a nested object
    const isObject = value?.type === "object" && value?.properties;
    if (isObject) {
      const expandBtn = document.createElement("span");
      expandBtn.className = "expand-btn";
      expandBtn.textContent = ">";
      expandBtn.style.cursor = "pointer";
      expandBtn.style.userSelect = "none";

      const keyText = document.createTextNode(indent + key);
      nameCell.appendChild(expandBtn);
      nameCell.appendChild(keyText);

      let isExpanded = false;
      expandBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        isExpanded = !isExpanded;
        expandBtn.textContent = isExpanded ? "v" : ">";

        // Toggle nested rows
        const nestedRows = tbody.querySelectorAll(`[data-parent="${parentPath}${key}"]`);
        nestedRows.forEach(nestedRow => {
          nestedRow.style.display = isExpanded ? "" : "none";
        });
      });
    } else {
      nameCell.textContent = indent + key;
    }

    if (level > 0) {
      row.dataset.parent = parentPath;
      row.style.display = "none";
    }

    const typeCell = document.createElement("td");
    typeCell.textContent = formatSchemaType(value);

    const reqCell = document.createElement("td");
    reqCell.textContent = isRequired ? "Yes" : "No";
    if (isRequired) {
      reqCell.style.fontWeight = "bold";
      reqCell.style.color = "#1b8f6b";
    }

    const descCell = document.createElement("td");
    descCell.textContent = value?.description || value?.title || "";

    row.appendChild(nameCell);
    row.appendChild(typeCell);
    row.appendChild(reqCell);
    row.appendChild(descCell);
    tbody.appendChild(row);

    // Recursively render nested properties
    if (isObject) {
      const nestedRequired = new Set(value.required || []);
      Object.entries(value.properties).forEach(([nestedKey, nestedValue]) => {
        if (nestedRequired.has(nestedKey)) {
          required.add(nestedKey);
        }
        renderRow(nestedKey, nestedValue, level + 1, `${parentPath}${key}.`);
        if (nestedRequired.has(nestedKey)) {
          required.delete(nestedKey);
        }
      });
    }
  };

  Object.entries(schema.properties).forEach(([key, value]) => {
    renderRow(key, value);
  });

  table.appendChild(tbody);
  return table;
};

const renderToolDetail = (tool) => {
  if (!toolDetail) return;
  if (!tool) {
    setToolDetailEmpty("Select a tool to view details.");
    return;
  }

  toolDetail.textContent = "";

  const header = document.createElement("div");
  header.className = "tool-detail-header";

  const icon = document.createElement("span");
  icon.className = "tool-detail-icon";
  icon.textContent = tool.is_mcp ? "[mcp]" : "[tool]";

  const title = document.createElement("h3");
  title.textContent = tool.name || "tool";

  header.appendChild(icon);
  header.appendChild(title);

  const sub = document.createElement("div");
  sub.className = "agent-sub";
  const scope = tool.scope || tool.identifier?.scope || "n/a";
  const server = tool.server || tool.identifier?.server || "n/a";
  sub.textContent = `scope: ${scope} | server: ${server}`;

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
  descTitle.textContent = "Description";
  const desc = document.createElement("p");
  desc.textContent = tool.description || "No description";

  const usageTitle = document.createElement("h4");
  usageTitle.textContent = "Usage";
  const usageRow = document.createElement("p");
  const stats = toolStats[tool.name] || {};
  const parts = [];
  if (stats.workflows !== undefined) parts.push(`workflows: ${stats.workflows}`);
  if (stats.last_used) parts.push(`last: ${stats.last_used}`);
  usageRow.textContent = parts.length ? parts.join(" | ") : "n/a";

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

  toolDetail.appendChild(header);
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
    meta.textContent = parts.join(" | ");
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

    const header = document.createElement("div");
    header.className = "tool-card-header";

    const icon = document.createElement("span");
    icon.className = "tool-icon";
    icon.textContent = tool.is_mcp ? "[mcp]" : "[tool]";

    const title = document.createElement("h4");
    title.textContent = tool.name;

    header.appendChild(icon);
    header.appendChild(title);

    const desc = document.createElement("p");
    desc.textContent = tool.description || "";

    const tagRow = document.createElement("div");
    tagRow.className = "tag-row";
    tagRow.appendChild(createTag(tool.scope || "global"));
    tagRow.appendChild(createTag(tool.server || "builtin", tool.is_mcp ? "warn" : "accent"));

    // Add params count info
    if (tool.params_count) {
      const paramsText = tool.params_count.required > 0
        ? `${tool.params_count.required}/${tool.params_count.total} params`
        : `${tool.params_count.total} params`;
      tagRow.appendChild(createTag(paramsText, ""));
    }

    const stats = toolStats[tool.name] || {};
    const meta = document.createElement("div");
    meta.className = "tool-meta";
    const metaParts = [];
    if (stats.workflows !== undefined) metaParts.push(`workflows: ${stats.workflows}`);
    if (stats.last_used) metaParts.push(`last: ${formatDate(stats.last_used)}`);
    meta.textContent = metaParts.length ? metaParts.join(" | ") : "workflows: 0";

    card.appendChild(header);
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
    setToolDetailEmpty("Failed to load tool detail.");
  }
};

const applyAgentFilter = (agents) => {
  if (!Array.isArray(agents)) return [];

  let filtered = agents;

  // Apply type filter
  if (agentFilter === "default") {
    filtered = filtered.filter((agent) => agent.user_id === "share");
  } else if (agentFilter === "user") {
    filtered = filtered.filter((agent) => agent.user_id !== "share");
  } else if (agentFilter === "remote") {
    filtered = filtered.filter((agent) => agent.source === "remote");
  }

  // Apply search filter
  if (agentSearchQuery) {
    const query = agentSearchQuery.toLowerCase();
    filtered = filtered.filter((agent) => {
      const name = (agent.agent_name || "").toLowerCase();
      const nick = (agent.nick_name || "").toLowerCase();
      const desc = (agent.description || "").toLowerCase();
      return name.includes(query) || nick.includes(query) || desc.includes(query);
    });
  }

  // Apply sorting
  if (agentSort === "name") {
    filtered.sort((a, b) => (a.agent_name || "").localeCompare(b.agent_name || ""));
  } else if (agentSort === "health") {
    filtered.sort((a, b) => {
      const healthA = getHealthStatus(a.agent_name).status;
      const healthB = getHealthStatus(b.agent_name).status;
      const order = { healthy: 0, unknown: 1, unhealthy: 2 };
      return (order[healthA] || 1) - (order[healthB] || 1);
    });
  }

  return filtered;
};

const renderAgents = (agents) => {
  const filtered = applyAgentFilter(agents);
  if (!filtered.length) {
    const emptyMsg = agentSearchQuery
      ? `No agents found for "${agentSearchQuery}"`
      : "No agents match current filter.";
    setListState(agentsList, emptyMsg, "empty");
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
    titleWrap.style.flex = "1";

    const title = document.createElement("div");
    title.className = "agent-name";
    title.textContent = agent.agent_name || "agent";

    const sub = document.createElement("div");
    sub.className = "agent-sub";
    const userLabel = agent.user_id === "share" ? "default" : "user";
  const sourceLabel = agent.source ? ` | ${agent.source}` : "";
  const nick = agent.nick_name ? `${agent.nick_name} | ` : "";
    const toolCount = Array.isArray(agent.selected_tools) ? agent.selected_tools.length : 0;
    const toolLabel = toolCount > 0 ? ` | ${toolCount} tools` : "";
    sub.textContent = `${nick}${userLabel}${sourceLabel}${toolLabel}`;

    titleWrap.appendChild(title);
    titleWrap.appendChild(sub);

    const selectBtn = document.createElement("button");
    selectBtn.className = "select-toggle";
    const isSelected = selectedCoorAgents.has(agent.agent_name);
    selectBtn.textContent = isSelected ? "[x]" : "+";
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

  updateCoorCount();
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
      setAgentDetailEmpty("No agent details available.");
      return;
    }

    renderAgents(combined);
  } catch (err) {
    setListState(agentsList, "Failed to load agents.", "error");
    setAgentDetailEmpty("Failed to load agents.");
  }
};

const fetchTools = async () => {
  setListState(toolsList, "Loading...", "loading");
  setToolDetailEmpty("Select a tool to view details.");
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
    setListState(workflowsList, "Please enter a user_id first.", "empty");
    workflowsTotal = 0;
    workflowsTotalPages = 0;
    updateWorkflowsPagination();
    return;
  }

  setListState(workflowsList, "Loading...", "loading");
  try {
    const params = new URLSearchParams();
    params.set("user_id", userId);
    params.set("page", String(workflowsPage));
    params.set("page_size", String(workflowsPageSize));
    const res = await fetch(`/api/workflows?${params.toString()}`);
    if (!res.ok) {
      throw new Error("request failed");
    }
    const workflows = await res.json();
    workflowsTotal = Number.parseInt(res.headers.get("X-Total-Count") || "0", 10) || 0;
    workflowsTotalPages = Number.parseInt(res.headers.get("X-Total-Pages") || "0", 10) || 0;
    updateWorkflowsPagination();
    if (!workflows.length) {
      setListState(workflowsList, "No workflows found.", "empty");
      return;
    }

    workflowsList.textContent = "";
    workflows.forEach((wf) => {
      const title = formatWorkflowTitle(wf);
      const item = document.createElement("div");
      item.className = "workflow-item";
      item.dataset.workflowId = wf.workflow_id;
      item.setAttribute("role", "button");
      item.tabIndex = 0;

      const titleEl = document.createElement("div");
      titleEl.className = "workflow-item-id";
      titleEl.textContent = title;

      const metaEl = document.createElement("div");
      metaEl.className = "workflow-item-meta";

      const idSpan = document.createElement("span");
      idSpan.textContent = `ID: ${wf.workflow_id}`;

      const versionSpan = document.createElement("span");
      versionSpan.textContent = `lap: ${wf.lap} | version: ${wf.version}`;

      metaEl.appendChild(idSpan);
      metaEl.appendChild(versionSpan);

      item.appendChild(titleEl);
      item.appendChild(metaEl);

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
    workflowsTotal = 0;
    workflowsTotalPages = 0;
    updateWorkflowsPagination();
  }
};

const formatWorkflowTitle = (workflow) => {
  const taskName = getWorkflowTaskName(workflow);
  const date = getWorkflowDate(workflow);
  if (date && taskName) return `${date} | ${taskName}`;
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
    workflowDetail.textContent = "Failed to load workflow detail.";
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
    mermaidContainer.textContent = "No graph available.";
  }
};

const toggleAutoScroll = () => {
  autoScrollEnabled = !autoScrollEnabled;
  updateAutoScrollBtn();
  if (autoScrollEnabled) {
    if (planningOutput) {
      planningOutput.scrollTop = planningOutput.scrollHeight;
    }
    if (executionOutput) {
      executionOutput.scrollTop = executionOutput.scrollHeight;
    }
  }
};

const exportOutputTxt = () => {
  if (!planningOutputBlocks.size && !executionOutputBlocks.size) {
    flashButton(exportTxtBtn, "Empty");
    return;
  }

  const parts = [];
  const appendSection = (title, blocks) => {
    if (!blocks.size) return;
    parts.push(`=== ${title} ===`);
    blocks.forEach((pre, agentName) => {
      parts.push(`[${agentName}]`);
      parts.push(pre.textContent.trimEnd());
      parts.push("");
    });
  };
  appendSection("Planning log", planningOutputBlocks);
  appendSection("Execution log", executionOutputBlocks);
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
    flashButton(healthCheckSelectedBtn, "Select agents first");
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
    healthCheckSelectedBtn.textContent = "[ok] Done";
    setTimeout(() => {
      healthCheckSelectedBtn.textContent = prevText;
      healthCheckSelectedBtn.disabled = false;
    }, 1500);
  } catch (err) {
    console.error("Health check failed:", err);
    healthCheckSelectedBtn.textContent = "[x] Failed";
    setTimeout(() => {
      healthCheckSelectedBtn.textContent = prevText;
      healthCheckSelectedBtn.disabled = false;
    }, 2000);
  }
};

runBtn.addEventListener("click", runWorkflow);
stopBtn.addEventListener("click", stopWorkflow);
clearOutputBtn.addEventListener("click", clearOutput);
autoScrollBtn.addEventListener("click", toggleAutoScroll);
exportTxtBtn.addEventListener("click", exportOutputTxt);

refreshAgentsBtn.addEventListener("click", fetchAgents);
refreshToolsBtn.addEventListener("click", fetchTools);
refreshWorkflowsBtn.addEventListener("click", () => {
  workflowsPage = 1;
  fetchWorkflows();
});

// MCP toggle functionality
if (mcpToggle && mcpContent) {
  mcpToggle.addEventListener("click", () => {
    const isCollapsed = mcpContent.classList.toggle("collapsed");
    mcpToggle.classList.toggle("collapsed", isCollapsed);
    mcpToggle.textContent = isCollapsed ? ">" : "v";
  });
}

if (workflowsPageSizeSelect) {
  workflowsPageSize = Number.parseInt(workflowsPageSizeSelect.value || "5", 10) || 5;
  workflowsPageSizeSelect.addEventListener("change", () => {
    workflowsPageSize = Number.parseInt(workflowsPageSizeSelect.value || "5", 10) || 5;
    workflowsPage = 1;
    fetchWorkflows();
  });
}

if (workflowsPrevPageBtn) {
  workflowsPrevPageBtn.addEventListener("click", () => {
    workflowsPage = Math.max(1, workflowsPage - 1);
    fetchWorkflows();
  });
}

if (workflowsNextPageBtn) {
  workflowsNextPageBtn.addEventListener("click", () => {
    if (workflowsTotalPages && workflowsPage < workflowsTotalPages) {
      workflowsPage += 1;
      fetchWorkflows();
    }
  });
}

if (agentsSearchInput) {
  const debouncedSearch = debounce(() => {
    agentSearchQuery = agentsSearchInput.value.trim();
    if (latestAgents.length) {
      renderAgents(latestAgents);
    }
  }, 300);

  agentsSearchInput.addEventListener("input", debouncedSearch);
  agentsSearchInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      agentSearchQuery = agentsSearchInput.value.trim();
      if (latestAgents.length) {
        renderAgents(latestAgents);
      }
    }
  });
}

if (agentsSortSelect) {
  agentsSortSelect.addEventListener("change", () => {
    agentSort = agentsSortSelect.value || "name";
    if (latestAgents.length) {
      renderAgents(latestAgents);
    }
  });
}

if (toolsSearchInput) {
  toolsSearchInput.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.target.value = "";
      toolsSearchQuery = "";
      renderTools();
    }
  });
  toolsSearchInput.addEventListener("input", () => {
    renderTools();
  });
}

// Global keyboard shortcut for tools search
document.addEventListener("keydown", (event) => {
  if (event.key === "/" && !event.ctrlKey && !event.metaKey && !event.altKey) {
    const activeTab = document.querySelector(".tab.active");
    if (activeTab && activeTab.dataset.tab === "tools") {
      const target = event.target;
      if (target.tagName !== "INPUT" && target.tagName !== "TEXTAREA") {
        event.preventDefault();
        toolsSearchInput?.focus();
      }
    }
  }
});
if (toolsSourceFilter) {
  toolsSourceFilter.addEventListener("change", renderTools);
}
if (toolsScopeFilter) {
  toolsScopeFilter.addEventListener("change", renderTools);
}
if (toolsSortSelect) {
  toolsSortSelect.addEventListener("change", renderTools);
}

if (agentsFilterSelect) {
  agentsFilterSelect.addEventListener("change", () => {
    agentFilter = agentsFilterSelect.value || "all";
    if (latestAgents.length) {
      renderAgents(latestAgents);
    }
  });
}

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
loadReadiness();
updateCoorCount();
setAgentDetailEmpty("Select an agent to view details.");

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

const executionPhaseLabel = (phase) => {
  const labels = {
    'initial_planning': 'Initial planning',
    're_planning': 'Re-planning',
    'execution': 'Execution'
  };
  return labels[phase] || phase;
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

      // Create badges container
      const badgesContainer = document.createElement("div");
      badgesContainer.className = "badges";

      // Execution phase badge
      const phaseBadge = document.createElement("span");
      phaseBadge.className = "phase-badge";
      phaseBadge.textContent = executionPhaseLabel(task.execution_phase);

      // Status badge (use task.status directly)
      const statusBadge = document.createElement("span");
      statusBadge.className = `status-badge ${statusBadgeClass(task.status)}`;
      statusBadge.textContent = task.status;

      badgesContainer.appendChild(phaseBadge);
      badgesContainer.appendChild(statusBadge);

      header.appendChild(titleEl);
      header.appendChild(badgesContainer);

      const meta = document.createElement("div");
      meta.className = "task-item-meta";
      meta.textContent = `${formatDateTime(task.created_at)} | ${task.step_count} steps`;

      const wfId = document.createElement("div");
      wfId.className = "task-item-wfid";
      wfId.textContent = `workflow: ${task.workflow_id || "-"}`;

      // Delete button on card (using trash icon)
      const deleteBtn = document.createElement("button");
      deleteBtn.className = "task-card-delete-btn";
      deleteBtn.title = "Delete task";
      deleteBtn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>`;

      item.appendChild(header);
      item.appendChild(meta);
      item.appendChild(wfId);
      item.appendChild(deleteBtn);

      // Delete button click handler
      deleteBtn.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        if (confirm(`Are you sure you want to delete task "${task.task_id}"?\nThis will delete the task log and all associated checkpoints.`)) {
          deleteTaskById(task.task_id);
        }
      });

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
      nextEl.textContent = cp.next_node ? `-> ${cp.next_node}` : "";

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
      cpCollapseToggle.innerHTML = '<span class="icon">></span> JSON';

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
      <div class="log-meta-item"><b>Execution phase</b><span class="phase-badge">${executionPhaseLabel(log.execution_phase)}</span></div>
      <div class="log-meta-item"><b>Task status</b><span class="status-badge ${statusBadgeClass(log.status)}">${log.status}</span></div>
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
      // Click the full header to toggle expand/collapse
      headerEl.addEventListener("click", () => {
        entryEl.classList.toggle("expanded");
      });

      // Collapse toggle icon
      const collapseIcon = document.createElement("span");
      collapseIcon.className = "collapse-icon";
      collapseIcon.textContent = ">";

      const stepSpan = document.createElement("span");
      stepSpan.className = "step-badge";
      stepSpan.textContent = `Step ${entry.step}`;

      const roleSpan = document.createElement("span");
      roleSpan.className = "log-role";
      // Display agent_proxy with sub_agent_name as: agent_proxy [researcher]
      if (entry.node_name === "agent_proxy" && entry.sub_agent_name) {
        roleSpan.textContent = `${entry.node_name} [${entry.sub_agent_name}]`;
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
      
      // Default to collapsed (do not add expanded class)
      // No extra action needed because CSS defaults to display:none
      
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

const deleteTaskById = async (taskId) => {
  try {
    const res = await fetch(`/api/tasks/${encodeURIComponent(taskId)}`, {
      method: "DELETE",
    });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || "Delete failed");
    }
    // Clear panels if deleted task was selected
    if (selectedTaskId === taskId) {
      checkpointPanel.style.display = "none";
      logPanel.style.display = "none";
      resumePanel.style.display = "none";
      selectedTaskId = null;
    }
    // Refresh task list
    await fetchTasks();
  } catch (err) {
    alert(`Failed to delete task: ${err.message}`);
  }
};

const deleteTask = async () => {
  if (!selectedTaskId) {
    alert("Please select a task first.");
    return;
  }
  if (!confirm(`Are you sure you want to delete task "${selectedTaskId}"?\nThis will delete the task log and all associated checkpoints.`)) {
    return;
  }
  await deleteTaskById(selectedTaskId);
};

copyLogBtn.addEventListener("click", copyTaskLog);
resumeBtn.addEventListener("click", resumeTask);
resumeStopBtn.addEventListener("click", stopResume);
clearResumeOutputBtn.addEventListener("click", () => {
  resumeOutput.textContent = "";
});

// S-ABAC Demo: User role selector sync
(function() {
  const demoRole = document.getElementById("demoUserRole");
  const userIdInput = document.getElementById("userId");
  if (demoRole && userIdInput && demoRole.value && (!userIdInput.value || userIdInput.value === "test")) {
    userIdInput.value = demoRole.value;
  }
  if (demoRole) {
    demoRole.addEventListener("change", function() {
      if (userIdInput && demoRole.value) {
        userIdInput.value = demoRole.value;
      }
      if (window.SecurityModule && window.SecurityModule.loadUserSecurityProfile && demoRole.value) {
        window.SecurityModule.loadUserSecurityProfile(demoRole.value);
      }
      loadPermissionSummary(demoRole.value);
    });
  }
  if (demoRole && demoRole.value) {
    loadPermissionSummary(demoRole.value);
  }
})();

// S-ABAC Permission Summary for Run Tab
let currentUserPrecheck = null;

async function loadPermissionSummary(userId) {
  if (!userId) return;
  try {
    const resp = await fetch(`/api/security/users/${encodeURIComponent(userId)}/precheck`);
    if (!resp.ok) return;
    currentUserPrecheck = await resp.json();
    renderPermissionSummary(currentUserPrecheck);
    if (latestAgents.length) {
      renderAgents(latestAgents);
    }
    if (latestTools.length) {
      renderTools();
    }
  } catch (e) {
    console.warn("Failed to load permission summary:", e);
  }
}

function renderPermissionSummary(precheck) {
  const card = document.getElementById("permissionSummaryCard");
  const summary = document.getElementById("permissionSummary");
  if (!card || !summary) return;

  card.style.display = "";
  const profile = precheck.profile || {};
  const tools = precheck.tool_access || {};
  const blocked = Object.entries(tools).filter(([, info]) => !info.can_access);
  const accessible = Object.entries(tools).filter(([, info]) => info.can_access);

  summary.innerHTML = `
    <div class="perm-summary-row">
      <span class="perm-summary-icon">${profile.icon || '[user]'}</span>
      <span class="perm-summary-name">${escapeHtml(profile.display_name || precheck.user_id)}</span>
      <span class="tag accent">${escapeHtml(profile.role || '?')}</span>
      <span class="tag">CL${profile.clearance_level || 0}</span>
    </div>
    <div class="perm-summary-stats">
      <div class="perm-stat green">
        <span class="perm-stat-num">${accessible.length}</span>
        <span class="perm-stat-label">Directly accessible</span>
      </div>
      <div class="perm-stat red">
        <span class="perm-stat-num">${blocked.length}</span>
        <span class="perm-stat-label">Blocked</span>
      </div>
    </div>
    ${blocked.length > 0 ? `
    <div class="perm-blocked-list">
      <div class="perm-blocked-title">Blocked tools:</div>
      ${blocked.map(([name, info]) => `
        <div class="perm-blocked-item">
          <span class="perm-blocked-name">${escapeHtml(name)}</span>
          <span class="perm-blocked-reason">${escapeHtml(info.blocked_reason || 'Insufficient permission')}</span>
        </div>
      `).join('')}
    </div>` : ''}
  `;
}

// Update Agents Panel: filter by current user permissions
const originalRenderAgents = renderAgents;
renderAgents = function(agents) {
  if (currentUserPrecheck && currentUserPrecheck.agent_access) {
    const agentAccess = currentUserPrecheck.agent_access;
    agents = agents.map(agent => {
      const access = agentAccess[agent.agent_name];
      if (access && !access.available_to_user) {
        return Object.assign({}, agent, { _unavailable_to_user: true });
      }
      return agent;
    });
  }
  return originalRenderAgents(agents);
};

// Override agent card rendering to show unavailable state
const _origCreateAgentCard = function(card, agent) {
  if (agent._unavailable_to_user) {
    card.style.opacity = "0.45";
    card.style.pointerEvents = "none";
    card.title = "This agent is unavailable for the current user.";
    const badge = document.createElement("span");
    badge.className = "tag warn";
    badge.style.cssText = "position:absolute;top:4px;right:4px;font-size:10px;";
    badge.textContent = "[no access]";
    card.style.position = "relative";
    card.appendChild(badge);
  }
};

// Hook into the existing renderAgents to add unavailable markers
(function() {
  const origRender = renderAgents;
  renderAgents = function(agents) {
    const result = origRender(agents);
    if (agentsList) {
      agentsList.querySelectorAll(".agent-card").forEach(card => {
        const agentName = card.querySelector(".agent-name")?.textContent;
        if (agentName && currentUserPrecheck && currentUserPrecheck.agent_access) {
          const access = currentUserPrecheck.agent_access[agentName];
          if (access && !access.available_to_user) {
            card.style.opacity = "0.45";
            card.style.pointerEvents = "none";
            card.title = "This agent is unavailable for the current user.";
          }
        }
      });
    }
    return result;
  };
})();

// Update Tools Panel: show per-user access status
(function() {
  const origRender = renderTools;
  renderTools = function() {
    origRender();
    if (toolsList && currentUserPrecheck && currentUserPrecheck.tool_access) {
      const toolAccess = currentUserPrecheck.tool_access;
      toolsList.querySelectorAll(".tool-card").forEach(card => {
        const toolName = card.dataset.toolName;
        const info = toolAccess[toolName];
        if (!info) return;
        if (!info.can_access) {
          card.style.opacity = "0.45";
          card.title = info.blocked_reason || "Insufficient permission";
          let badge = card.querySelector(".tool-perm-badge");
          if (!badge) {
            badge = document.createElement("span");
            badge.className = "tag warn tool-perm-badge";
            badge.style.cssText = "position:absolute;top:4px;right:4px;font-size:10px;";
            badge.textContent = "[blocked]";
            card.style.position = "relative";
            card.appendChild(badge);
          }
        } else {
          card.style.opacity = "1";
          const badge = card.querySelector(".tool-perm-badge");
          if (badge) badge.remove();
        }
      });
    }
  };
})();
