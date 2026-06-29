// 记忆演化 插件页面逻辑

// ── API Client ──────────────────────────

class ApiClient {
  constructor() {
    this.bridge = window.AstrBotPluginPage;
  }

  async ready() {
    if (!this.bridge) {
      throw new Error("Bridge 不可用");
    }
    if (this.bridge.ready) {
      try {
        await this.bridge.ready();
      } catch (e) {
        console.warn("Bridge ready 警告:", e);
      }
    }
  }

  async get(endpoint, params = {}) {
    if (!this.bridge || !this.bridge.apiGet) {
      throw new Error("Bridge apiGet 不可用");
    }
    const path = endpoint.startsWith("page/") ? endpoint : `page/${endpoint}`;
    return await this.bridge.apiGet(path, params);
  }

  async post(endpoint, body = {}) {
    if (!this.bridge || !this.bridge.apiPost) {
      throw new Error("Bridge apiPost 不可用");
    }
    const path = endpoint.startsWith("page/") ? endpoint : `page/${endpoint}`;
    return await this.bridge.apiPost(path, body);
  }
}

const api = new ApiClient();

// ── State ───────────────────────────────

const state = {
  proposals: [],
  currentProposalId: null,
  proposalFilter: "",
  snapshots: [],
  logs: [],
  status: null,
};

// ── Utils ───────────────────────────────

function $(id) {
  return document.getElementById(id);
}

function esc(text) {
  if (text == null) return "";
  const div = document.createElement("div");
  div.textContent = String(text);
  return div.innerHTML;
}

function toast(message, type = "info") {
  const container = $("toast-container");
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.textContent = message;
  container.appendChild(el);
  setTimeout(() => {
    el.style.opacity = "0";
    el.style.transition = "opacity 0.3s";
    setTimeout(() => el.remove(), 300);
  }, 3000);
}

function showLoading(containerId) {
  $(containerId).innerHTML =
    '<div class="loading"><span class="spinner"></span>加载中...</div>';
}

function statusBadge(status) {
  const labels = {
    pending: "待审",
    approved: "已通过",
    rejected: "已拒绝",
    stalled: "无法收敛",
  };
  const text = labels[status] || status;
  return `<span class="badge badge-${status || "dim"}">${esc(text)}</span>`;
}

function showModal(title, bodyHtml, actionsHtml = "") {
  const container = $("modal-container");
  container.innerHTML = `
    <div class="modal-overlay" id="modal-overlay">
      <div class="modal">
        <div class="modal-title">${esc(title)}</div>
        <div class="modal-body">${bodyHtml}</div>
        <div class="modal-actions">
          <button class="btn" id="modal-close-btn">关闭</button>
          ${actionsHtml}
        </div>
      </div>
    </div>
  `;
  // 绑定关闭按钮
  $("modal-close-btn").addEventListener("click", () => {
    container.innerHTML = "";
  });
  // 点击遮罩关闭
  $("modal-overlay").addEventListener("click", (e) => {
    if (e.target.id === "modal-overlay") {
      container.innerHTML = "";
    }
  });
}

// sandbox iframe 禁用了 confirm()/prompt()，用自定义模态框替代
function customConfirm(message) {
  return new Promise((resolve) => {
    const container = $("modal-container");
    container.innerHTML = `
      <div class="modal-overlay" id="modal-overlay">
        <div class="modal">
          <div class="modal-title">确认操作</div>
          <div class="modal-body">${esc(message)}</div>
          <div class="modal-actions">
            <button class="btn btn-secondary" id="modal-cancel-btn">取消</button>
            <button class="btn btn-primary" id="modal-confirm-btn">确认</button>
          </div>
        </div>
      </div>
    `;
    const close = (result) => {
      container.innerHTML = "";
      resolve(result);
    };
    $("modal-cancel-btn").addEventListener("click", () => close(false));
    $("modal-confirm-btn").addEventListener("click", () => close(true));
    $("modal-overlay").addEventListener("click", (e) => {
      if (e.target.id === "modal-overlay") close(false);
    });
  });
}

function customPrompt(message, defaultValue = "") {
  return new Promise((resolve) => {
    const container = $("modal-container");
    container.innerHTML = `
      <div class="modal-overlay" id="modal-overlay">
        <div class="modal">
          <div class="modal-title">输入</div>
          <div class="modal-body">${esc(message)}</div>
          <div class="modal-actions">
            <input type="text" class="input" id="modal-prompt-input" value="${esc(defaultValue)}" style="width:100%;margin-bottom:12px" />
            <button class="btn btn-secondary" id="modal-cancel-btn">取消</button>
            <button class="btn btn-primary" id="modal-confirm-btn">确定</button>
          </div>
        </div>
      </div>
    `;
    const input = $("modal-prompt-input");
    input.focus();
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        container.innerHTML = "";
        resolve(input.value);
      }
    });
    const close = (result) => {
      container.innerHTML = "";
      resolve(result);
    };
    $("modal-cancel-btn").addEventListener("click", () => close(null));
    $("modal-confirm-btn").addEventListener("click", () => close(input.value));
    $("modal-overlay").addEventListener("click", (e) => {
      if (e.target.id === "modal-overlay") close(null);
    });
  });
}

// ── Theme ─────────────────────────────

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  const iconDark = $("theme-icon-dark");
  const iconLight = $("theme-icon-light");
  if (iconDark) iconDark.classList.toggle("hidden", theme === "dark");
  if (iconLight) iconLight.classList.toggle("hidden", theme !== "dark");
}

function initTheme() {
  // iframe 被 sandbox 时 localStorage 可能不可用，需要 try/catch 保护
  let saved = "light";
  try {
    saved = localStorage.getItem("theme") || "light";
  } catch (e) {
    console.warn("[LMPatch] 无法读取 localStorage:", e);
  }
  applyTheme(saved);
  $("theme-toggle")?.addEventListener("click", () => {
    const current = document.documentElement.dataset.theme || "light";
    const next = current === "dark" ? "light" : "dark";
    applyTheme(next);
    try {
      localStorage.setItem("theme", next);
    } catch (e) {
      console.warn("[LMPatch] 无法写入 localStorage:", e);
    }
  });
}

// ── Page switching ──────────────────────

function switchPage(pageName) {
  document.querySelectorAll(".nav-item").forEach((item) => {
    item.classList.toggle("active", item.dataset.page === pageName);
  });
  document.querySelectorAll(".page").forEach((page) => {
    page.classList.toggle("active", page.id === `page-${pageName}`);
  });

  // 按需加载数据
  if (pageName === "proposals") loadProposals();
  else if (pageName === "init") loadInitState();
  else if (pageName === "snapshots") loadSnapshots();
  else if (pageName === "logs") loadLogs();
  else if (pageName === "status") loadStatus();
}

// ── Proposals ───────────────────────────

async function loadProposals() {
  showLoading("proposal-list");
  try {
    const params = {};
    if (state.proposalFilter) params.status = state.proposalFilter;
    // Bridge 会自动解包标准响应：成功时 resp 即为 data 字段（数组），失败时抛出 Error
    const resp = await api.get("proposals", params);
    state.proposals = Array.isArray(resp) ? resp : [];
    renderProposalList();
    updatePendingBadge();
  } catch (e) {
    $("proposal-list").innerHTML = `<div class="empty-state">加载失败: ${esc(
      e.message
    )}</div>`;
  }
}

function updatePendingBadge() {
  const badge = $("badge-pending");
  const pendingCount = state.proposals.filter(
    (p) => p.status === "pending"
  ).length;
  if (pendingCount > 0) {
    badge.textContent = pendingCount;
    badge.style.display = "inline-block";
  } else {
    badge.style.display = "none";
  }
}

function renderProposalList() {
  const container = $("proposal-list");
  if (state.proposals.length === 0) {
    container.innerHTML =
      '<div class="empty-state"><svg class="empty-state-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg><div>暂无提案</div></div>';
    return;
  }

  container.innerHTML = state.proposals
    .map((p) => {
      const isActive = p.id === state.currentProposalId;
      const rerollInfo =
        p.reroll_count > 0
          ? `<span>· 重议 ${p.reroll_count} 次</span>`
          : "";
      const initBadge =
        p.is_init && p.init_batch > 0
          ? `<span class="badge badge-init">初始化·迭代 ${p.init_batch}</span>`
          : "";
      return `
      <div class="proposal-item ${isActive ? "active" : ""}" data-id="${p.id}">
        <div class="proposal-item-id">#${p.id}</div>
        <div class="proposal-item-persona">${esc(p.persona_name)}</div>
        <div class="proposal-item-desc">${esc(
          p.change_description || "(无变更说明)"
        )}</div>
        <div class="proposal-item-meta">
          ${statusBadge(p.status)}
          ${initBadge}
          <span>· ${esc(p.created_at)}</span>
          ${rerollInfo}
        </div>
      </div>
    `;
    })
    .join("");

  // 绑定点击事件
  container.querySelectorAll(".proposal-item").forEach((item) => {
    item.addEventListener("click", () => {
      const id = parseInt(item.dataset.id);
      selectProposal(id);
    });
  });
}

function selectProposal(id) {
  state.currentProposalId = id;
  renderProposalList();
  renderProposalDetail();
}

function renderProposalDetail() {
  const container = $("proposal-detail");
  const proposal = state.proposals.find(
    (p) => p.id === state.currentProposalId
  );

  if (!proposal) {
    container.innerHTML =
      '<div class="empty-state"><svg class="empty-state-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg><div>从左侧选择一个提案查看详情</div></div>';
    return;
  }

  const aspectsHtml =
    proposal.changed_aspects && proposal.changed_aspects.length > 0
      ? `<div class="detail-aspects">${proposal.changed_aspects
          .map((a) => `<span class="aspect-tag">${esc(a)}</span>`)
          .join("")}</div>`
      : "";

  const initInfo =
    proposal.is_init && proposal.init_batch > 0
      ? `<span class="badge badge-init">初始化·迭代 ${proposal.init_batch}</span>`
      : "";

  const rerollInfo =
    proposal.reroll_count > 0
      ? `<span class="badge badge-dim">已重议 ${proposal.reroll_count} 次</span>`
      : "";

  // 根据状态决定操作按钮
  let actionsHtml = "";
  if (proposal.status === "pending") {
    actionsHtml = `
      <div class="reroll-box">
        <textarea id="reroll-reason" placeholder="打回理由（填写后点击「打回重提议」，LLM 将结合理由重新提议）..."></textarea>
      </div>
      <div class="action-row">
        <button class="btn btn-success" id="btn-approve">通过并写回</button>
        <button class="btn btn-warning" id="btn-reroll">打回重提议</button>
        <button class="btn btn-danger" id="btn-reject">直接拒绝</button>
      </div>
    `;
  } else if (proposal.status === "stalled") {
    actionsHtml = `
      <div class="action-row">
        <button class="btn btn-primary" id="btn-restart">重启提案</button>
        <span style="color:var(--text-tertiary);font-size:12px;align-self:center">
          该提案已超过最大重议次数，重启后将清零计数并重新提议
        </span>
      </div>
    `;
  } else {
    actionsHtml = `<div style="color:var(--text-tertiary);font-size:13px">该提案状态为「${
      proposal.status
    }」，不可再操作</div>`;
  }

  const rejectionHtml = proposal.rejection_reason
    ? `<div style="margin-top:8px;font-size:12px;color:var(--warning)">打回理由: ${esc(
        proposal.rejection_reason
      )}</div>`
    : "";

  container.innerHTML = `
    <div class="detail-header">
      <div class="detail-persona">${esc(proposal.persona_name)}</div>
      <div class="detail-desc">${esc(
        proposal.change_description || "(无变更说明)"
      )}</div>
      ${aspectsHtml}
      <div style="margin-top:8px;display:flex;gap:8px;align-items:center;font-size:12px;color:var(--text-tertiary)">
        ${statusBadge(proposal.status)}
        ${initInfo}
        <span>· 创建于 ${esc(proposal.created_at)}</span>
        <span>· Persona ID: ${esc(proposal.persona_id)}</span>
        ${rerollInfo}
      </div>
      ${rejectionHtml}
    </div>
    <div class="compare-grid">
      <div class="compare-pane">
        <div class="compare-pane-header original">
          <span>原始人设</span>
          <button class="btn btn-sm" onclick="window._lmpatch.viewText('原始人设', ${proposal.id}, 'original')">查看</button>
        </div>
        <div class="compare-pane-body">${esc(proposal.original_persona)}</div>
      </div>
      <div class="compare-pane">
        <div class="compare-pane-header proposed">
          <span>提议人设</span>
          <button class="btn btn-sm" onclick="window._lmpatch.viewText('提议人设', ${proposal.id}, 'proposed')">查看</button>
        </div>
        <div class="compare-pane-body">${esc(proposal.proposed_persona)}</div>
      </div>
    </div>
    <div class="detail-actions">
      ${actionsHtml}
    </div>
  `;

  // 绑定操作按钮
  if (proposal.status === "pending") {
    $("btn-approve")?.addEventListener("click", () => approveProposal(proposal.id));
    $("btn-reroll")?.addEventListener("click", () => rerollProposal(proposal.id));
    $("btn-reject")?.addEventListener("click", () => rejectProposal(proposal.id));
  } else if (proposal.status === "stalled") {
    $("btn-restart")?.addEventListener("click", () => restartProposal(proposal.id));
  }
}

async function approveProposal(id) {
  if (!(await customConfirm("确认通过该提案并写回人设？"))) return;
  try {
    // Bridge 自动解包：成功时 resp 即为 data 字段，失败时抛出 Error
    const resp = await api.post("proposal/approve", { id });
    toast(resp.message || "已通过并写回", "success");
    // 如果是初始化提案，显示下一批的信息
    if (resp.init_next) {
      if (resp.init_next.completed) {
        toast(resp.init_next.message || "初始化已完成", "success");
      } else if (resp.init_next.success) {
        toast(
          resp.init_next.message || `迭代 ${resp.init_next.batch} 已生成`,
          "success"
        );
      } else if (resp.init_next.error) {
        toast(`下一批生成失败: ${resp.init_next.error}`, "warning");
      }
    }
    await loadProposals();
    // 如果 init_next 生成了新提案，自动选中它
    if (resp.init_next && resp.init_next.proposal_id) {
      state.currentProposalId = resp.init_next.proposal_id;
      renderProposalDetail();
    } else {
      state.currentProposalId = null;
      renderProposalDetail();
    }
  } catch (e) {
    toast(`操作失败: ${e.message}`, "error");
  }
}

async function rejectProposal(id) {
  const reason = await customPrompt("拒绝理由（可选）:");
  if (reason === null) return;
  try {
    const resp = await api.post("proposal/reject", { id, reason: reason || "" });
    toast(resp.message || "已拒绝", "success");
    await loadProposals();
    state.currentProposalId = null;
    renderProposalDetail();
  } catch (e) {
    toast(`操作失败: ${e.message}`, "error");
  }
}

async function rerollProposal(id) {
  const reason = $("reroll-reason")?.value?.trim();
  if (!reason) {
    toast("请填写打回理由", "warning");
    return;
  }
  try {
    const btn = $("btn-reroll");
    if (btn) btn.disabled = true;
    // stalled 视为"软成功"：操作已完成但提案标记为 stalled，bridge 会将其作为 data 返回
    const resp = await api.post("proposal/reroll", { id, reason });
    if (resp.stalled) {
      toast(resp.error || "已超过最大重议次数", "warning");
    } else {
      toast(resp.message || "已重新提议", "success");
    }
    await loadProposals();
  } catch (e) {
    toast(`操作失败: ${e.message}`, "error");
  } finally {
    const btn = $("btn-reroll");
    if (btn) btn.disabled = false;
  }
}

async function restartProposal(id) {
  if (!(await customConfirm("确认重启该提案？将清零重议计数并重新提议。"))) return;
  try {
    const resp = await api.post("proposal/restart", { id });
    toast(resp.message || "已重启", "success");
    await loadProposals();
  } catch (e) {
    toast(`操作失败: ${e.message}`, "error");
  }
}

// ── Snapshots ───────────────────────────

async function loadSnapshots() {
  try {
    const resp = await api.get("snapshots", {});
    state.snapshots = Array.isArray(resp) ? resp : [];
    renderSnapshots();
  } catch (e) {
    toast(`加载快照失败: ${e.message}`, "error");
  }
}

function renderSnapshots() {
  const tbody = $("snapshot-tbody");
  if (state.snapshots.length === 0) {
    tbody.innerHTML =
      '<tr><td colspan="5" style="text-align:center;color:var(--text-tertiary);padding:40px">暂无快照</td></tr>';
    return;
  }

  tbody.innerHTML = state.snapshots
    .map((s) => {
      const desc = s.change_description || "(无说明)";
      return `
      <tr>
        <td class="col-id">#${s.id}</td>
        <td>${esc(s.persona_name)}</td>
        <td style="color:var(--text-secondary)">${esc(desc)}</td>
        <td class="col-time">${esc(s.created_at)}</td>
        <td class="col-action">
          <button class="btn btn-sm" onclick="window._lmpatch.viewSnapshot(${s.id})">查看</button>
          <button class="btn btn-danger btn-sm" onclick="window._lmpatch.rollback(${s.id})">回滚</button>
        </td>
      </tr>
    `;
    })
    .join("");
}

function viewSnapshot(id) {
  const snapshot = state.snapshots.find((s) => s.id === id);
  if (!snapshot) return;
  showModal(
    `快照 #${snapshot.id} - ${snapshot.persona_name}`,
    esc(snapshot.snapshot_text)
  );
}

async function rollbackSnapshot(id) {
  if (!(await customConfirm(`确认回滚到快照 #${id}？当前人设将保存为新快照以便撤销。`))) return;
  try {
    const resp = await api.post("snapshot/rollback", { id });
    toast(resp.message || "已回滚", "success");
    await loadSnapshots();
  } catch (e) {
    toast(`回滚失败: ${e.message}`, "error");
  }
}

// ── Compact logs ────────────────────────

async function loadLogs() {
  try {
    const resp = await api.get("compact-log", {});
    state.logs = Array.isArray(resp) ? resp : [];
    renderLogs();
  } catch (e) {
    toast(`加载日志失败: ${e.message}`, "error");
  }
}

function renderLogs() {
  const tbody = $("log-tbody");
  if (state.logs.length === 0) {
    tbody.innerHTML =
      '<tr><td colspan="5" style="text-align:center;color:var(--text-tertiary);padding:40px">暂无压缩日志</td></tr>';
    return;
  }

  tbody.innerHTML = state.logs
    .map((log) => {
      return `
      <tr>
        <td class="col-id">#${log.id}</td>
        <td>${esc(log.persona_id)}</td>
        <td style="color:var(--danger)">${log.deleted_count} 条</td>
        <td style="color:var(--success)">${log.created_count} 条</td>
        <td class="col-time">${esc(log.created_at)}</td>
      </tr>
    `;
    })
    .join("");
}

// ── Status ──────────────────────────────

async function loadStatus() {
  const grid = $("status-grid");
  grid.innerHTML =
    '<div class="loading"><span class="spinner"></span>加载中...</div>';
  try {
    const resp = await api.get("status", {});
    state.status = resp;
    renderStatus();
  } catch (e) {
    grid.innerHTML = `<div class="empty-state">加载失败: ${esc(
      e.message
    )}</div>`;
  }
}

function renderStatus() {
  const s = state.status;
  if (!s) return;

  const lmDot = s.lm_available
    ? '<span class="status-dot on"></span>已连接'
    : '<span class="status-dot off"></span>未连接';
  const schedDot = s.scheduler_running
    ? '<span class="status-dot on"></span>运行中'
    : '<span class="status-dot off"></span>已停止';
  const patchDot = s.patch_enabled
    ? '<span class="status-dot on"></span>已启用'
    : '<span class="status-dot off"></span>已禁用';
  const compactDot = s.compact_enabled
    ? '<span class="status-dot on"></span>已启用'
    : '<span class="status-dot off"></span>已禁用';

  $("status-grid").innerHTML = `
    <div class="card status-item">
      <span class="status-label">LivingMemory 连接</span>
      <span class="status-value">${lmDot}</span>
    </div>
    <div class="card status-item">
      <span class="status-label">调度器</span>
      <span class="status-value">${schedDot}</span>
    </div>
    <div class="card status-item">
      <span class="status-label">人设补丁</span>
      <span class="status-value">${patchDot}</span>
    </div>
    <div class="card status-item">
      <span class="status-label">记忆压缩</span>
      <span class="status-value">${compactDot}</span>
    </div>
    <div class="card status-item">
      <span class="status-label">补丁间隔</span>
      <span class="status-value">${s.patch_interval_hours} 小时</span>
    </div>
    <div class="card status-item">
      <span class="status-label">压缩检查间隔</span>
      <span class="status-value">${s.compact_interval_hours} 小时</span>
    </div>
  `;

  // 更新 footer
  $("footer-info").textContent = s.lm_available
    ? "LivingMemory 已连接"
    : "LivingMemory 未连接";
}

// ── Trigger actions ─────────────────────

async function triggerPatch() {
  const btn = $("btn-trigger-patch");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "执行中...";
  }
  try {
    const resp = await api.post("trigger/patch", {});
    toast(resp.message || "已触发", "success");
    await loadProposals();
  } catch (e) {
    toast(`触发失败: ${e.message}`, "error");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "手动触发补丁";
    }
  }
}

async function triggerCompact() {
  const btn = $("btn-trigger-compact");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "执行中...";
  }
  try {
    const resp = await api.post("trigger/compact", {});
    toast(resp.message || "已触发", "success");
    await loadLogs();
  } catch (e) {
    toast(`触发失败: ${e.message}`, "error");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "手动触发压缩";
    }
  }
}

// ── 暴露给内联 onclick 的接口 ───────────

window._lmpatch = {
  viewText(title, proposalId, field) {
    const p = state.proposals.find((x) => x.id === proposalId);
    if (!p) return;
    const text = field === "original" ? p.original_persona : p.proposed_persona;
    showModal(title, esc(text));
  },
  viewSnapshot,
  rollback: rollbackSnapshot,
};

// ── Init (初始化) ───────────────────────

async function loadInitState() {
  const container = $("init-status");
  if (!container) return;
  container.innerHTML =
    '<div class="loading"><span class="spinner"></span>加载中...</div>';
  try {
    const resp = await api.get("init/state", {});
    renderInitState(resp);
  } catch (e) {
    container.innerHTML = `<div class="empty-state">加载失败: ${esc(
      e.message
    )}</div>`;
  }
}

function renderInitState(s) {
  const container = $("init-status");
  if (!s || !container) return;

  const isRunning = s.status === "running";
  const typeLabel =
    s.type === "persona" ? "人设迭代初始化" : s.type === "compact" ? "记忆压缩初始化" : "";
  const statusLabel = {
    idle: "未开始",
    running: "进行中",
    completed: "已完成",
    cancelled: "已取消",
  }[s.status] || s.status;

  // 控制按钮显隐
  $("btn-start-persona-init").style.display = isRunning ? "none" : "";
  $("btn-start-compact-init").style.display = isRunning ? "none" : "";
  $("btn-cancel-init").style.display = isRunning ? "" : "none";

  // 导航栏徽标
  const badge = $("badge-init");
  if (badge) {
    if (isRunning) {
      badge.textContent = "!";
      badge.style.display = "inline-block";
    } else {
      badge.style.display = "none";
    }
  }

  if (s.status === "idle") {
    container.innerHTML = '<div class="empty-state">点击上方按钮开始初始化</div>';
    return;
  }

  const progressHtml = isRunning
    ? s.type === "persona"
      ? `<div class="init-progress">
           <div class="init-progress-item"><span class="init-label">当前 Persona</span><span class="init-value">${esc(s.current_persona_id || "准备中...")}</span></div>
           <div class="init-progress-item"><span class="init-label">当前迭代</span><span class="init-value">${s.current_batch > 0 ? "第 " + s.current_batch + " 批" : "准备中..."}</span></div>
           <div class="init-progress-item"><span class="init-label">Persona 进度</span><span class="init-value">${s.current_persona_idx + 1} / ${s.total_personas}</span></div>
           <div class="init-progress-item"><span class="init-label">已处理记忆</span><span class="init-value">${s.total_processed} 条</span></div>
           ${!s.current_persona_id ? '<div class="init-summary">⏳ 正在分析第一批记忆，请稍候...</div>' : ""}
         </div>`
      : `<div class="init-progress">
           <div class="init-progress-item"><span class="init-label">当前 Persona</span><span class="init-value">${esc(s.current_persona_id || "准备中...")}</span></div>
           <div class="init-progress-item"><span class="init-label">Persona 进度</span><span class="init-value">${s.current_persona_idx + 1} / ${s.total_personas}</span></div>
           <div class="init-progress-item"><span class="init-label">已压缩记忆</span><span class="init-value">${s.total_compacted} 条</span></div>
           ${!s.current_persona_id ? '<div class="init-summary">⏳ 正在启动压缩任务...</div>' : ""}
         </div>`
    : "";

  const timeHtml = s.started_at
    ? `<div class="init-progress-item"><span class="init-label">开始时间</span><span class="init-value">${esc(s.started_at)}</span></div>`
    : "";
  const finishedHtml = s.finished_at
    ? `<div class="init-progress-item"><span class="init-label">完成时间</span><span class="init-value">${esc(s.finished_at)}</span></div>`
    : "";

  const summaryHtml =
    s.status === "completed"
      ? s.type === "persona"
        ? `<div class="init-summary">✅ 人设迭代初始化已完成，共处理 ${s.total_processed} 条历史记忆</div>`
        : `<div class="init-summary">✅ 记忆压缩初始化已完成，共压缩 ${s.total_compacted} 条记忆</div>`
      : "";

  const errorHtml = s.error
    ? `<div class="init-error">❌ ${esc(s.error)}</div>`
    : "";

  container.innerHTML = `
    <div class="init-state-card ${isRunning ? "running" : s.status}">
      <div class="init-state-header">
        <span class="init-state-type">${esc(typeLabel)}</span>
        <span class="badge badge-${s.status === "running" ? "pending" : s.status === "completed" ? "approved" : s.status === "cancelled" ? "rejected" : "dim"}">${statusLabel}</span>
      </div>
      ${progressHtml}
      ${timeHtml}
      ${finishedHtml}
      ${summaryHtml}
      ${errorHtml}
    </div>
  `;
}

async function startPersonaInit() {
  if (!(await customConfirm("确认开始人设迭代初始化？\n\n将按历史记忆顺序，每批 20 条生成提案，你审批通过后自动进入下一批，直到处理完所有历史记忆。"))) return;
  const btn = $("btn-start-persona-init");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "启动中...";
  }
  // 启动期间轮询 init/state，让用户看到实时进度（LLM 分析第一批记忆可能需要 10-30 秒）
  pollInitState();
  try {
    const resp = await api.post("init/persona/start", {});
    // POST 完成（第一个提案已创建或无需初始化），停止轮询
    if (_initPollTimer) {
      clearInterval(_initPollTimer);
      _initPollTimer = null;
    }
    if (resp.completed) {
      toast(resp.message || "无需初始化，已全部处理", "success");
    } else {
      toast(resp.message || "已生成第一个提案，等待审批", "success");
      // 跳转到提案页面查看第一个提案
      switchPage("proposals");
    }
    await loadInitState();
  } catch (e) {
    // 出错时也停止轮询
    if (_initPollTimer) {
      clearInterval(_initPollTimer);
      _initPollTimer = null;
    }
    toast(`启动失败: ${e.message}`, "error");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "开始人设迭代初始化";
    }
  }
}

async function startCompactInit() {
  if (!(await customConfirm("确认开始记忆压缩初始化？\n\n将从重要性最低的记忆开始，每批 10 条自动压缩，后台运行直到完成。"))) return;
  const btn = $("btn-start-compact-init");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "启动中...";
  }
  try {
    const resp = await api.post("init/compact/start", {});
    toast(resp.message || "初始化已启动，后台运行中", "success");
    await loadInitState();
    // 开始轮询状态
    pollInitState();
  } catch (e) {
    toast(`启动失败: ${e.message}`, "error");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "开始记忆压缩初始化";
    }
  }
}

async function cancelInit() {
  if (!(await customConfirm("确认取消初始化？正在处理的当前批次会完成后停止。"))) return;
  try {
    const resp = await api.post("init/cancel", {});
    toast(resp.message || "取消请求已发送", "success");
    await loadInitState();
  } catch (e) {
    toast(`取消失败: ${e.message}`, "error");
  }
}

// 压缩初始化后台任务轮询
let _initPollTimer = null;

function pollInitState() {
  if (_initPollTimer) clearInterval(_initPollTimer);
  _initPollTimer = setInterval(async () => {
    try {
      const resp = await api.get("init/state", {});
      renderInitState(resp);
      if (resp.status !== "running") {
        clearInterval(_initPollTimer);
        _initPollTimer = null;
        if (resp.status === "completed") {
          toast("记忆压缩初始化已完成", "success");
        } else if (resp.status === "cancelled") {
          toast("记忆压缩初始化已取消", "info");
        }
      }
    } catch (e) {
      // 轮询失败，静默忽略
    }
  }, 5000);
}

// ── Init (应用入口) ────────────────────

async function init() {
  // 初始化主题（localStorage 在 sandbox 中可能不可用，已在 initTheme 内部处理）
  try {
    initTheme();
  } catch (e) {
    console.warn("[LMPatch] 主题初始化失败:", e);
  }

  // 绑定导航
  document.querySelectorAll(".nav-item[data-page]").forEach((item) => {
    item.addEventListener("click", () => switchPage(item.dataset.page));
  });

  // 绑定过滤器
  document.querySelectorAll("#proposal-filters .filter-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document
        .querySelectorAll("#proposal-filters .filter-btn")
        .forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      state.proposalFilter = btn.dataset.status;
      loadProposals();
    });
  });

  // 绑定刷新按钮
  $("btn-refresh-proposals")?.addEventListener("click", loadProposals);
  $("btn-refresh-init")?.addEventListener("click", loadInitState);
  $("btn-refresh-snapshots")?.addEventListener("click", loadSnapshots);
  $("btn-refresh-logs")?.addEventListener("click", loadLogs);
  $("btn-refresh-status")?.addEventListener("click", loadStatus);

  // 绑定触发按钮
  $("btn-trigger-patch")?.addEventListener("click", triggerPatch);
  $("btn-trigger-compact")?.addEventListener("click", triggerCompact);

  // 绑定初始化按钮
  $("btn-start-persona-init")?.addEventListener("click", startPersonaInit);
  $("btn-start-compact-init")?.addEventListener("click", startCompactInit);
  $("btn-cancel-init")?.addEventListener("click", cancelInit);

  // 等待 Bridge 就绪
  try {
    await api.ready();
  } catch (e) {
    console.warn("[LMPatch] Bridge 初始化警告:", e);
  }

  // 加载首页数据
  try {
    await loadProposals();
  } catch (e) {
    console.error("[LMPatch] 首页数据加载失败:", e);
  }

  // 同时加载状态更新 footer
  try {
    const resp = await api.get("status", {});
    state.status = resp;
    $("footer-info").textContent = state.status.lm_available
      ? "LivingMemory 已连接"
      : "LivingMemory 未连接";
  } catch (e) {
    $("footer-info").textContent = "状态未知";
  }
}

// 捕获 init 的未处理异常，避免静默卡在"加载中"
init().catch((e) => {
  console.error("[LMPatch] 初始化失败:", e);
  const list = $("proposal-list");
  if (list) {
    list.innerHTML = `<div class="empty-state">初始化失败: ${esc(
      e.message || String(e)
    )}</div>`;
  }
});
