/**
 * S-ABAC Security Module for CoorAgent Web Studio
 * Handles security dashboard, user switching, and permission visualization.
 */

// ─── Security State ────────────────────────────────────────────────
let secUsers = [];
let secCurrentUser = null;
let secPolicies = [];
let secSystemStatus = null;
let secToolAccess = {};
let secAgentAttributes = {};

// ─── Security API Helpers ──────────────────────────────────────────

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

// ─── Render Functions ──────────────────────────────────────────────

function renderSecurityStatus() {
    const el = document.getElementById("securityStatus");
    if (!el || !secSystemStatus) return;

    const enabled = secSystemStatus.s_abac_enabled;
    el.innerHTML = `
        <div class="sec-status-row">
            <span class="sec-status-dot ${enabled ? 'on' : 'off'}"></span>
            <span class="sec-status-label">S-ABAC: <strong>${enabled ? 'ENABLED' : 'DISABLED'}</strong></span>
        </div>
        <div class="sec-stats">
            <div class="sec-stat"><span>${secSystemStatus.policies_count}</span><small>Policies</small></div>
            <div class="sec-stat"><span>${secSystemStatus.agent_attributes_count}</span><small>Agent Attrs</small></div>
            <div class="sec-stat"><span>${secSystemStatus.resource_attributes_count}</span><small>Resource Attrs</small></div>
        </div>
    `;
}

function populateUserSelects() {
    const sel = document.getElementById("securityUserSelect");
    if (!sel) return;
    sel.innerHTML = '<option value="">-- 选择模拟用户 --</option>';
    secUsers.forEach(u => {
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
            <div class="sec-profile-icon">${p.icon || '👤'}</div>
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
        bar += `<span class="clearance-seg ${i <= level ? 'filled' : ''}" style="--lvl:${i}"></span>`;
    }
    bar += `</span><span class="clearance-text">L${level}</span>`;
    return bar;
}

function renderAgentsList() {
    const el = document.getElementById("userAgentsList");
    if (!el || !secCurrentUser) return;

    const agents = secCurrentUser.available_agents || [];
    if (!agents.length) {
        el.innerHTML = '<div class="sec-empty">此用户没有可用 Agent</div>';
        return;
    }

    el.innerHTML = agents.map(a => `
        <div class="sec-agent-item">
            <span class="sec-agent-name">🤖 ${escapeHtml(a.agent_name)}</span>
            <span class="tag">${escapeHtml(a.role)}</span>
            <span class="tag accent">CL${a.clearance_level}</span>
        </div>
    `).join('');
}

function renderToolAccessGrid() {
    const el = document.getElementById("toolAccessGrid");
    if (!el || !secToolAccess) return;

    const tools = Object.entries(secToolAccess);
    if (!tools.length) {
        el.innerHTML = '<div class="sec-empty">无工具权限数据</div>';
        return;
    }

    el.innerHTML = tools.map(([name, info]) => {
        const icon = info.can_access ? '✅' : '❌';
        const cls = info.can_access ? 'allowed' : 'denied';
        return `
            <div class="sec-tool-row ${cls}">
                <span class="sec-tool-icon">${icon}</span>
                <span class="sec-tool-name">${escapeHtml(name)}</span>
                <span class="tag ${info.sensitivity === 'HIGH' || info.sensitivity === 'CRITICAL' ? 'warn' : ''}">${info.sensitivity}</span>
                <span class="sec-tool-roles">${(info.allowed_roles || []).join(', ') || 'any'}</span>
            </div>
        `;
    }).join('');
}

function renderPolicies() {
    const el = document.getElementById("policyList");
    if (!el || !secPolicies.length) {
        if (el) el.innerHTML = '<div class="sec-empty">无策略数据</div>';
        return;
    }

    el.innerHTML = secPolicies.map(p => `
        <div class="sec-policy-card">
            <div class="sec-policy-header">
                <strong>${escapeHtml(p.policy_id)}</strong>
                <span class="tag accent">${p.rules.length} rule(s)</span>
            </div>
            <div class="sec-policy-desc">${escapeHtml(p.description)}</div>
            ${p.rules.map(r => `
                <div class="sec-policy-rule">
                    <span class="tag ${r.effect === 'ALLOW' ? 'accent' : 'warn'}">${r.effect}</span>
                    ${renderRuleCondition(r.condition)}
                </div>
            `).join('')}
        </div>
    `).join('');
}

function renderRuleCondition(cond) {
    if (!cond || !cond.all) return '';
    return cond.all.map(c => {
        const entries = Object.entries(c);
        if (!entries.length) return '';
        const [key, val] = entries[0];
        const shortKey = key.replace('subject.attributes.', 'subj.').replace('object.attributes.', 'obj.').replace('action.', 'act.');
        return `<span class="sec-cond">${shortKey}=${JSON.stringify(val)}</span>`;
    }).join(' ');
}

// ─── Event Handlers ─────────────────────────────────────────────────

function initSecurityTab() {
    const sel = document.getElementById("securityUserSelect");
    if (sel) {
        sel.addEventListener("change", () => {
            if (sel.value) {
                loadUserSecurityProfile(sel.value);
            }
        });
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

// ─── Security Event Display (for Run output) ───────────────────────

function displaySecurityEvent(eventName, data) {
    const container = document.getElementById("executionOutput");
    if (!container) return;

    let html = '';
    if (eventName === "permission_denied") {
        const policy = data.policy_result || {};
        html = `
            <div class="step-card error" style="margin-top:8px">
                <div class="step-card-header" style="background:rgba(217,83,79,0.1)">
                    <span class="step-status-icon">🚫</span>
                    <span class="step-agent-name">S-ABAC Permission Denied</span>
                    <span class="step-summary-text">${escapeHtml(data.error || policy.reason || '权限不足')}</span>
                </div>
                <div class="step-card-body">
                    <div class="sec-event-detail">
                        <div style="color:var(--danger)"><strong>拒绝原因:</strong> ${escapeHtml(policy.reason || data.error || '权限不足')}</div>
                        <div style="margin-top:8px;color:var(--muted);font-size:12px">
                            请使用具有更高权限的用户角色重试，或联系管理员。
                        </div>
                    </div>
                </div>
            </div>`;
    }

    if (html) {
        const wrapper = document.createElement("div");
        wrapper.innerHTML = html;
        container.appendChild(wrapper.firstElementChild);
        if (typeof autoScrollEnabled !== 'undefined' && autoScrollEnabled) {
            container.scrollTop = container.scrollHeight;
        }
    }
}

// ─── Initialize ─────────────────────────────────────────────────────

function initSecurityModule() {
    initSecurityTab();
    loadSecurityStatus();
    loadSecurityUsers();
    loadSecurityPolicies();
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

// ─── Expose for integration with app.js ────────────────────────────

window.SecurityModule = {
    loadUserSecurityProfile,
    displaySecurityEvent,
    loadSecurityStatus,
};
