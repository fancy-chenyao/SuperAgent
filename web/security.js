/**
 * S-ABAC Security Module for CoorAgent Web Studio
 * Handles security dashboard, user switching, and permission visualization.
 */

// Security State
let secUsers = [];
let secCurrentUser = null;
let secPolicies = [];
let secSystemStatus = null;
let secToolAccess = {};
let secAgentAttributes = {};
let secLastDeniedEvents = [];
let secApprovals = [];
let secReconciliations = [];
const SEC_MAX_DENIED_HISTORY = 5;

function formatScenarioFitSummary(fitResult) {
    if (!fitResult || typeof fitResult !== "object") return "";

    const fit = fitResult.fit || "uncertain";
    const confidence = typeof fitResult.confidence === "number"
        ? ` (confidence ${Math.round(fitResult.confidence * 100)}%)`
        : "";
    const reason = fitResult.reason
        ? escapeHtml(fitResult.reason)
        : "No scenario-fit explanation provided.";
    const labelMap = {
        match: "Scenario match",
        mismatch: "Scenario mismatch",
        uncertain: "Scenario uncertain",
    };
    const label = labelMap[fit] || "Scenario uncertain";

    return `<div style="margin-top:8px"><strong>${label}</strong>${confidence}<br><span style="color:var(--muted)">${reason}</span></div>`;
}

// Security API Helpers

async function secFetch(url) {
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return resp.json();
}

async function loadSecurityStatus() {
    try {
        secSystemStatus = await secFetch("/api/security/status");
        renderSecurityStatus();
    } catch (e) {
        document.getElementById("securityStatus").innerHTML =
            `<div class="sec-error">S-ABAC: ${escapeHtml(e.message)}</div>`;
    }
}

async function loadSecurityUsers() {
    try {
        secUsers = await secFetch("/api/security/users");
        populateUserSelects();
    } catch (e) {
        console.warn("Failed to load security users:", e);
    }
}

async function loadSecurityPolicies() {
    try {
        secPolicies = await secFetch("/api/security/policies");
        renderPolicies();
    } catch (e) {
        console.warn("Failed to load policies:", e);
    }
}

async function loadSecurityApprovals() {
    const el = document.getElementById("securityApprovals");
    try {
        const requester = document.getElementById("userId")?.value || "admin";
        secApprovals = await secFetch(`/api/security/approvals?requester_id=${encodeURIComponent(requester)}`);
        renderSecurityApprovals();
    } catch (e) {
        if (el) {
            el.innerHTML = `<div class="sec-error">审批队列加载失败：${escapeHtml(e.message)}</div>`;
        }
    }
}

async function loadSecurityReconciliations() {
    const el = document.getElementById("securityReconciliations");
    try {
        const requester = document.getElementById("userId")?.value || "admin";
        secReconciliations = await secFetch(`/api/security/reconciliations?requester_id=${encodeURIComponent(requester)}`);
        renderSecurityReconciliations();
    } catch (e) {
        if (el) {
            el.innerHTML = `<div class="sec-error">人工核对队列加载失败：${escapeHtml(e.message)}</div>`;
        }
    }
}

function governanceMutationHeaders() {
    let token = window.sessionStorage.getItem("governanceAdminApiKey") || "";
    if (!token) {
        token = window.prompt("请输入治理管理员凭据：", "") || "";
        if (!token) throw new Error("未提供治理管理员凭据");
        window.sessionStorage.setItem("governanceAdminApiKey", token);
    }
    return {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${token}`,
    };
}

async function decideSecurityReconciliation(reconciliationId, decision) {
    let externalOperationId = "";
    if (decision === "succeeded") {
        externalOperationId = window.prompt(
            "请输入外部系统中的操作编号（例如邮件 ID、文档 ID、流水号）：",
            ""
        );
        if (externalOperationId === null) return null;
        if (!externalOperationId.trim()) {
            throw new Error("确认成功必须填写外部操作编号");
        }
    }
    const prompts = {
        retry: "请确认已在外部系统核对：操作没有发生。可填写核对依据：",
        succeeded: "可填写确认成功的核对依据：",
        freeze: "可填写继续冻结的原因：",
        terminate: "可填写终止原因：",
    };
    const comment = window.prompt(prompts[decision] || "处置说明：", "");
    if (comment === null) return null;
    const response = await fetch(
        `/api/security/reconciliations/${encodeURIComponent(reconciliationId)}/${decision}`,
        {
            method: "POST",
            headers: governanceMutationHeaders(),
            body: JSON.stringify({
                comment,
                external_operation_id: externalOperationId.trim(),
            }),
        }
    );
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
        if (response.status === 401) {
            window.sessionStorage.removeItem("governanceAdminApiKey");
        }
        throw new Error(data.detail || `HTTP ${response.status}`);
    }
    await loadSecurityReconciliations();
    return data;
}

function renderSecurityReconciliations() {
    const el = document.getElementById("securityReconciliations");
    if (!el) return;
    if (!secReconciliations.length) {
        el.innerHTML = '<div class="sec-empty">暂无人工核对记录</div>';
        return;
    }

    const labels = {
        pending: "待核对",
        frozen: "已冻结",
        retry_ready: "可安全重试",
        confirmed_succeeded: "已确认成功",
        terminated: "已终止",
    };
    el.innerHTML = secReconciliations.slice(0, 20).map((item) => {
        const status = String(item.status || "pending").toLowerCase();
        const isActionable = status === "pending" || status === "frozen";
        const canResume = status === "retry_ready" || status === "confirmed_succeeded";
        const externalId = item.external_operation_id
            ? `<div><strong>外部操作：</strong>${escapeHtml(item.external_operation_id)}</div>`
            : "";
        const actions = isActionable
            ? `<div class="sec-approval-actions sec-reconciliation-actions">
                 <button class="primary sec-reconciliation-decide" data-id="${escapeHtml(item.reconciliation_id)}" data-decision="retry" type="button">确认未执行，安全重试</button>
                 <button class="ghost sec-reconciliation-decide" data-id="${escapeHtml(item.reconciliation_id)}" data-decision="succeeded" type="button">确认已成功</button>
                 <button class="ghost sec-reconciliation-decide" data-id="${escapeHtml(item.reconciliation_id)}" data-decision="freeze" type="button">保持冻结</button>
                 <button class="ghost danger sec-reconciliation-decide" data-id="${escapeHtml(item.reconciliation_id)}" data-decision="terminate" type="button">人工终止</button>
               </div>`
            : "";
        const resume = canResume
            ? `<div class="sec-approval-actions">
                 <button class="primary sec-reconciliation-resume" data-task-id="${escapeHtml(item.task_id)}" data-workflow-id="${escapeHtml(item.workflow_id)}" data-resume-step="${Number(item.resume_step) || 1}" data-user-id="${escapeHtml(item.user_id)}" type="button">继续原任务</button>
               </div>`
            : "";
        return `
          <div class="sec-approval-item status-${escapeHtml(status)}">
            <div class="sec-approval-header">
              <strong>${escapeHtml(labels[status] || status.toUpperCase())}</strong>
              <span class="tag warn">${escapeHtml(item.step_id || "step")}</span>
            </div>
            <div class="sec-approval-id">${escapeHtml(item.reconciliation_id)}</div>
            <div class="sec-approval-body">
              <div><strong>任务：</strong>${escapeHtml(item.task_id)}</div>
              <div><strong>执行者：</strong>${escapeHtml(item.agent_name || "unknown")}</div>
              <div title="${escapeHtml(item.error || "")}"><strong>失败原因：</strong>${escapeHtml(item.error || "外部操作结果不确定")}</div>
              ${externalId}
            </div>
            ${actions}${resume}
          </div>`;
    }).join("");

    el.querySelectorAll(".sec-reconciliation-decide").forEach((button) => {
        button.addEventListener("click", async () => {
            if (button.dataset.decision === "terminate") {
                const confirmed = window.confirm(
                    "终止后任务不会继续执行，未确认回执仍会保留。确定终止吗？"
                );
                if (!confirmed) return;
            }
            button.disabled = true;
            try {
                await decideSecurityReconciliation(
                    button.dataset.id,
                    button.dataset.decision
                );
            } catch (e) {
                window.alert(`人工核对操作失败：${e.message}`);
                button.disabled = false;
            }
        });
    });
    el.querySelectorAll(".sec-reconciliation-resume").forEach((button) => {
        button.addEventListener("click", () => {
            if (typeof window.resumeApprovedTask === "function") {
                window.resumeApprovedTask({
                    task_id: button.dataset.taskId,
                    workflow_id: button.dataset.workflowId,
                    resume_step: Number(button.dataset.resumeStep) || 1,
                    user_id: button.dataset.userId || "test",
                });
            }
        });
    });
}

async function decideSecurityApproval(approvalId, decision) {
    const comment = window.prompt(
        decision === "approve" ? "审批意见（可选）" : "拒绝原因（可选）",
        ""
    );
    if (comment === null) return;
    const response = await fetch(
        `/api/security/approvals/${encodeURIComponent(approvalId)}/${decision}`,
        {
            method: "POST",
            headers: governanceMutationHeaders(),
            body: JSON.stringify({ comment }),
        }
    );
    if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        if (response.status === 401) {
            window.sessionStorage.removeItem("governanceAdminApiKey");
        }
        throw new Error(data.detail || `HTTP ${response.status}`);
    }
    await loadSecurityApprovals();
}

function renderSecurityApprovals() {
    const el = document.getElementById("securityApprovals");
    if (!el) return;
    if (!secApprovals.length) {
        el.innerHTML = '<div class="sec-empty">暂无审批记录</div>';
        return;
    }

    el.innerHTML = secApprovals.slice(0, 20).map((item) => {
        const status = String(item.status || "pending").toLowerCase();
        const reason = item.policy_result?.reason || "策略要求人工审批";
        const target = item.object?.id || item.node_name || "unknown";
        const action = item.action?.attributes?.action_type || item.action?.verb || "execute";
        const pendingActions = status === "pending"
            ? `<div class="sec-approval-actions">
                 <button class="primary sec-approval-decide" data-id="${escapeHtml(item.approval_id)}" data-decision="approve" type="button">批准</button>
                 <button class="ghost sec-approval-decide" data-id="${escapeHtml(item.approval_id)}" data-decision="reject" type="button">拒绝</button>
               </div>`
            : "";
        const resumeAction = status === "approved"
            ? `<div class="sec-approval-actions">
                 <button class="primary sec-approval-resume" data-task-id="${escapeHtml(item.task_id)}" data-workflow-id="${escapeHtml(item.workflow_id)}" data-resume-step="${Number(item.resume_step) || 1}" data-user-id="${escapeHtml(item.user_id)}" type="button">恢复任务</button>
               </div>`
            : "";
        return `
          <div class="sec-approval-item status-${escapeHtml(status)}">
            <div class="sec-approval-header">
              <strong>${escapeHtml(status.toUpperCase())}</strong>
              <span class="tag accent">${escapeHtml(item.step_id || item.node_name || "step")}</span>
            </div>
            <div class="sec-approval-id">${escapeHtml(item.approval_id)}</div>
            <div class="sec-approval-body">
              <div><strong>任务：</strong>${escapeHtml(item.task_id)}</div>
              <div><strong>操作：</strong>${escapeHtml(action)} → ${escapeHtml(target)}</div>
              <div><strong>原因：</strong>${escapeHtml(reason)}</div>
            </div>
            ${pendingActions}${resumeAction}
          </div>`;
    }).join("");

    el.querySelectorAll(".sec-approval-decide").forEach((button) => {
        button.addEventListener("click", async () => {
            button.disabled = true;
            try {
                await decideSecurityApproval(button.dataset.id, button.dataset.decision);
            } catch (e) {
                window.alert(`审批操作失败：${e.message}`);
                button.disabled = false;
            }
        });
    });
    el.querySelectorAll(".sec-approval-resume").forEach((button) => {
        button.addEventListener("click", () => {
            if (typeof window.resumeApprovedTask === "function") {
                window.resumeApprovedTask({
                    task_id: button.dataset.taskId,
                    workflow_id: button.dataset.workflowId,
                    resume_step: Number(button.dataset.resumeStep) || 1,
                    user_id: button.dataset.userId || "test",
                });
            }
        });
    });
}

async function loadUserSecurityProfile(userId) {
    try {
        const data = await secFetch(`/api/security/users/${userId}`);
        secCurrentUser = data;
        secToolAccess = data.tool_access || {};
        renderUserProfile();
        renderAgentsList();
        renderToolAccessGrid();

        const userIdInput = document.getElementById("userId");
        if (userIdInput && data.user_id) {
            userIdInput.value = data.user_id;
        }

        const demoRole = document.getElementById("demoUserRole");
        if (demoRole && data.user_id && demoRole.value !== data.user_id) {
            demoRole.value = data.user_id;
        }
    } catch (e) {
        console.warn("Failed to load user profile:", e);
    }
}

// Render Functions

function renderSecurityStatus() {
    const el = document.getElementById("securityStatus");
    if (!el || !secSystemStatus) return;

    const enabled = secSystemStatus.s_abac_enabled;
    const schedulerEnabled = secSystemStatus.orchestration_scheduler_enabled;
    const recoveryEnabled = secSystemStatus.auto_recovery_enabled;
    const recoveryAttempts = Number(
        secSystemStatus.auto_recovery_max_attempts || 0
    );
    el.innerHTML = `
        <div class="sec-status-row">
            <span class="sec-status-dot ${enabled ? "on" : "off"}"></span>
            <span class="sec-status-label">S-ABAC: <strong>${enabled ? "ENABLED" : "DISABLED"}</strong></span>
        </div>
        <div class="sec-status-row">
            <span class="sec-status-dot ${schedulerEnabled ? "on" : "off"}"></span>
            <span class="sec-status-label">TaskGraph Scheduler: <strong>${schedulerEnabled ? "ENABLED" : "DISABLED"}</strong></span>
        </div>
        <div class="sec-status-row">
            <span class="sec-status-dot ${recoveryEnabled ? "on" : "off"}"></span>
            <span class="sec-status-label">DAG Auto Recovery: <strong>${recoveryEnabled ? "ENABLED" : "DISABLED"}</strong>${recoveryEnabled ? ` · max ${recoveryAttempts}` : ""}</span>
        </div>
        <div class="sec-stats">
            <div class="sec-stat"><span>${secSystemStatus.policies_count}</span><small>Policies</small></div>
            <div class="sec-stat"><span>${secSystemStatus.agent_attributes_count}</span><small>Agent attrs</small></div>
            <div class="sec-stat"><span>${secSystemStatus.resource_attributes_count}</span><small>Resource attrs</small></div>
        </div>
    `;
}

function populateUserSelects() {
    const sel = document.getElementById("securityUserSelect");
    if (!sel) return;

    sel.innerHTML = '<option value="">-- Select demo user --</option>';
    secUsers.forEach((u) => {
        sel.innerHTML += `<option value="${u.user_id}">${u.icon} ${u.display_name}</option>`;
    });
}

function renderUserProfile() {
    const el = document.getElementById("userProfileDetail");
    if (!el || !secCurrentUser) return;

    const p = secCurrentUser.profile;
    const levelBar = getClearanceBar(p.clearance_level);
    el.innerHTML = `
        <div class="sec-profile-card">
            <div class="sec-profile-icon">${p.icon || "USER"}</div>
            <div class="sec-profile-info">
                <strong>${escapeHtml(p.display_name)}</strong>
                <div>Role: <span class="tag accent">${escapeHtml(p.role)}</span></div>
                <div>Dept: ${escapeHtml(p.department)} | Trust: ${escapeHtml(p.trust_level)}</div>
                <div class="sec-clearance">Clearance: ${levelBar}</div>
                <div class="sec-desc">${escapeHtml(p.description)}</div>
            </div>
        </div>
    `;
}

function getClearanceBar(level) {
    const max = 5;
    let bar = '<span class="clearance-bar">';
    for (let i = 1; i <= max; i++) {
        bar += `<span class="clearance-seg ${i <= level ? "filled" : ""}" style="--lvl:${i}"></span>`;
    }
    bar += `</span><span class="clearance-text">L${level}</span>`;
    return bar;
}

function renderAgentsList() {
    const el = document.getElementById("userAgentsList");
    if (!el || !secCurrentUser) return;

    const agents = secCurrentUser.available_agents || [];
    if (!agents.length) {
        el.innerHTML = '<div class="sec-empty">No available agents for this user.</div>';
        return;
    }

    el.innerHTML = agents.map((a) => `
        <div class="sec-agent-item">
            <span class="sec-agent-name">AGENT ${escapeHtml(a.agent_name)}</span>
            <span class="tag">${escapeHtml(a.role)}</span>
            <span class="tag accent">CL${a.clearance_level}</span>
        </div>
    `).join("");
}

function renderToolAccessGrid() {
    const el = document.getElementById("toolAccessGrid");
    if (!el || !secToolAccess) return;

    const tools = Object.entries(secToolAccess);
    if (!tools.length) {
        el.innerHTML = '<div class="sec-empty">No tool-access data.</div>';
        return;
    }

    el.innerHTML = tools.map(([name, info]) => {
        const icon = info.can_access ? "ALLOW" : "DENY";
        const cls = info.can_access ? "allowed" : "denied";
        return `
            <div class="sec-tool-row ${cls}">
                <span class="sec-tool-icon">${icon}</span>
                <span class="sec-tool-name">${escapeHtml(name)}</span>
                <span class="tag ${info.sensitivity === "HIGH" || info.sensitivity === "CRITICAL" ? "warn" : ""}">${info.sensitivity}</span>
                <span class="sec-tool-roles">${(info.allowed_roles || []).join(", ") || "any"}</span>
            </div>
        `;
    }).join("");
}

function renderPolicies() {
    const el = document.getElementById("policyList");
    if (!el || !secPolicies.length) {
        if (el) el.innerHTML = '<div class="sec-empty">No policy data.</div>';
        return;
    }

    el.innerHTML = secPolicies.map((p) => `
        <div class="sec-policy-card">
            <div class="sec-policy-header">
                <strong>${escapeHtml(p.policy_id)}</strong>
                <span class="tag accent">${p.rules.length} rule(s)</span>
            </div>
            <div class="sec-policy-desc">${escapeHtml(p.description)}</div>
            ${p.rules.map((r) => `
                <div class="sec-policy-rule">
                    <span class="tag ${r.effect === "ALLOW" ? "accent" : "warn"}">${r.effect}</span>
                    ${renderRuleCondition(r.condition)}
                </div>
            `).join("")}
        </div>
    `).join("");
}

function renderLastDeniedEvent() {
    const el = document.getElementById("securityLastDenied");
    if (!el) return;

    if (!secLastDeniedEvents.length) {
        el.innerHTML = '<div class="sec-empty">No permission-denied events yet.</div>';
        return;
    }

    el.innerHTML = secLastDeniedEvents.map((d, idx) => {
        const policy = d.policy_result || {};
        const subject = d.subject || {};
        const object = d.object || {};
        const action = d.action || {};
        const scenarioFit = d.scenario_fit_result || {};

        const subjectName = subject.subject_name || subject.attributes?.display_name || subject.id || "Unknown user";
        const subjectRole = subject.attributes?.job_role || subject.attributes?.role || "Unknown role";
        const objectName = object.object_name || object.id || "Unknown object";
        const objectSensitivity = object.attributes?.sensitivity || "UNKNOWN";
        const actionVerb = action.attributes?.action_type || action.verb || "unknown";
        const deniedReason = policy.reason || d.error || "Permission denied";

        return `
            <div class="sec-policy-card" style="border-left:3px solid var(--danger); margin-bottom:${idx === secLastDeniedEvents.length - 1 ? "0" : "10px"}">
                <div class="sec-policy-header">
                    <strong>${idx === 0 ? "Most recent denied event" : `Denied history #${idx + 1}`}</strong>
                    <span class="tag warn">${escapeHtml(objectSensitivity)}</span>
                </div>
                <div class="sec-policy-desc">
                    <div><strong>Subject:</strong> ${escapeHtml(subjectName)} <span class="tag accent">${escapeHtml(subjectRole)}</span></div>
                    <div style="margin-top:6px"><strong>Target:</strong> ${escapeHtml(actionVerb)} -> ${escapeHtml(objectName)}</div>
                    <div style="margin-top:6px;color:var(--danger)"><strong>Reason:</strong> ${escapeHtml(deniedReason)}</div>
                    ${formatScenarioFitSummary(scenarioFit)}
                </div>
            </div>
        `;
    }).join("");
}

function renderRuleCondition(cond) {
    if (!cond || !cond.all) return "";
    return cond.all.map((c) => {
        const entries = Object.entries(c);
        if (!entries.length) return "";
        const [key, val] = entries[0];
        const shortKey = key
            .replace("subject.attributes.", "subj.")
            .replace("object.attributes.", "obj.")
            .replace("action.", "act.");
        return `<span class="sec-cond">${shortKey}=${JSON.stringify(val)}</span>`;
    }).join(" ");
}

// Event Handlers

function initSecurityTab() {
    const sel = document.getElementById("securityUserSelect");
    if (sel) {
        sel.addEventListener("change", () => {
            if (sel.value) {
                loadUserSecurityProfile(sel.value);
            }
        });
    }

    const refreshApprovalsBtn = document.getElementById("refreshApprovalsBtn");
    if (refreshApprovalsBtn) {
        refreshApprovalsBtn.addEventListener("click", loadSecurityApprovals);
    }

    const refreshReconciliationsBtn = document.getElementById("refreshReconciliationsBtn");
    if (refreshReconciliationsBtn) {
        refreshReconciliationsBtn.addEventListener(
            "click",
            loadSecurityReconciliations
        );
    }

    const demoRole = document.getElementById("demoUserRole");
    if (demoRole) {
        demoRole.addEventListener("change", () => {
            const userIdInput = document.getElementById("userId");
            if (userIdInput) {
                userIdInput.value = demoRole.value;
            }
            if (demoRole.value) {
                loadUserSecurityProfile(demoRole.value);
                if (typeof loadPermissionSummary === "function") {
                    loadPermissionSummary(demoRole.value);
                }
            }
        });
    }
}

// Security Event Display (for Run output)

function displaySecurityEvent(eventName, data) {
    const container = document.getElementById("executionOutput");
    if (!container) return;

    let html = "";
    if (eventName === "permission_denied") {
        secLastDeniedEvents = [data, ...secLastDeniedEvents].slice(0, SEC_MAX_DENIED_HISTORY);
        renderLastDeniedEvent();

        const policy = data.policy_result || {};
        html = `
            <div class="step-card error" style="margin-top:8px">
                <div class="step-card-header" style="background:rgba(217,83,79,0.1)">
                    <span class="step-status-icon">DENY</span>
                    <span class="step-agent-name">S-ABAC Permission Denied</span>
                    <span class="step-summary-text">${escapeHtml(data.error || policy.reason || "Permission denied")}</span>
                </div>
                <div class="step-card-body">
                    <div class="sec-event-detail">
                        <div style="color:var(--danger)"><strong>Reason:</strong> ${escapeHtml(policy.reason || data.error || "Permission denied")}</div>
                        ${formatScenarioFitSummary(data.scenario_fit_result)}
                        <div style="margin-top:8px;color:var(--muted);font-size:12px">
                            Retry with a user that has higher privileges, or contact an administrator.
                        </div>
                    </div>
                </div>
            </div>`;
    }

    if (html) {
        const wrapper = document.createElement("div");
        wrapper.innerHTML = html;
        container.appendChild(wrapper.firstElementChild);
        if (typeof autoScrollEnabled !== "undefined" && autoScrollEnabled) {
            container.scrollTop = container.scrollHeight;
        }
    }
}

// Initialize

function initSecurityModule() {
    initSecurityTab();
    loadSecurityStatus();
    loadSecurityUsers();
    loadSecurityPolicies();
    loadSecurityApprovals();
    loadSecurityReconciliations();
    renderLastDeniedEvent();

    const demoRole = document.getElementById("demoUserRole");
    if (demoRole && demoRole.value) {
        if (typeof loadPermissionSummary === "function") {
            loadPermissionSummary(demoRole.value);
        }
    }
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => {
        setTimeout(initSecurityModule, 500);
    });
} else {
    setTimeout(initSecurityModule, 500);
}

// Expose for integration with app.js

window.SecurityModule = {
    loadUserSecurityProfile,
    displaySecurityEvent,
    loadSecurityStatus,
    loadSecurityApprovals,
    loadSecurityReconciliations,
    formatScenarioFitSummary,
};
