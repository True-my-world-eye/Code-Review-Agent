/* ============================================================
 * Code Review Agent · Web 前端逻辑（原生 JS，无构建依赖）
 * 职责：配置管理 / 发起审查 / 时间线轮询 / 报告与修复渲染
 * ============================================================ */

"use strict";

/* ---------------- 基础工具 ---------------- */

/** 统一 fetch 封装：自动解析 JSON，非 2xx 抛出含 detail 的 Error */
async function api(path, options = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  let data = null;
  try { data = await resp.json(); } catch (_) { /* 非 JSON 响应忽略 */ }
  if (!resp.ok) {
    const msg = (data && data.detail) ? data.detail : `请求失败（HTTP ${resp.status}）`;
    throw new Error(msg);
  }
  return data;
}

const $ = (id) => document.getElementById(id);

/** 轻提示 */
let toastTimer = null;
function toast(message, isError = false) {
  const el = $("toast");
  el.textContent = message;
  el.classList.toggle("err", isError);
  el.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 3200);
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text == null ? "" : String(text);
  return div.innerHTML;
}

/* ---------------- 全局状态 ---------------- */
const state = {
  mode: "path",        // path | code
  root: null,          // 审查根（null = 项目根目录；否则为选定的绝对路径）
  taskId: null,        // 当前轮询的任务
  pollTimer: null,     // 轮询定时器
  history: [],         // 本次运行内的审查历史
  providers: [],       // 服务商列表
};

/* ---------------- 目录选择器状态 ---------------- */
const dirState = { current: "", parent: null };

/* ============================================================
 * 配置：读取 / 保存 / 连通性自检
 * ============================================================ */

/** 顶栏状态药丸 */
function renderConnPill(settings) {
  const pill = $("connPill");
  const configured = settings.is_configured;
  pill.textContent = configured
    ? `${settings.provider} · ${settings.effective_model} ● 已配置`
    : `${settings.provider} · 未配置 Key`;
  pill.className = "pill " + (configured ? "pill-ok" : "pill-warn");
}

/** 拉取配置并填充顶栏 + 设置表单 */
async function loadConfig() {
  try {
    const data = await api("/api/config");
    state.providers = data.providers;
    renderConnPill(data.settings);
    fillSettingsForm(data.settings);
  } catch (err) {
    renderConnPill({ provider: "-", is_configured: false });
    toast(err.message, true);
  }
}

function fillSettingsForm(s) {
  const select = $("cfgProvider");
  select.innerHTML = state.providers
    .map((p) => `<option value="${escapeHtml(p.id)}">${escapeHtml(p.label)}</option>`)
    .join("");
  select.value = s.provider;
  $("cfgBaseUrl").value = s.base_url || "";
  $("cfgApiKey").value = "";          // 明文 Key 永不回显
  $("cfgApiKey").placeholder = s.api_key ? `当前：${s.api_key}` : "sk-...";
  $("cfgModel").value = s.model || "";
  $("cfgTemperature").value = s.temperature;
  $("cfgMaxIter").value = s.max_iterations;
  $("cfgAutoFix").checked = !!s.auto_fix_enabled;
}

function settingsMsg(text, ok) {
  const el = $("settingsMsg");
  if (!text) { el.classList.add("hidden"); return; }
  el.textContent = text;
  el.className = "dialog-msg " + (ok ? "ok" : "err");
}

async function saveSettings() {
  const body = {
    provider: $("cfgProvider").value,
    base_url: $("cfgBaseUrl").value.trim(),
    api_key: $("cfgApiKey").value,          // 空字符串 = 保持不变（服务端约定）
    model: $("cfgModel").value.trim(),
    temperature: parseFloat($("cfgTemperature").value),
    max_iterations: parseInt($("cfgMaxIter").value, 10),
    auto_fix_enabled: $("cfgAutoFix").checked,
  };
  try {
    const data = await api("/api/config", { method: "PUT", body: JSON.stringify(body) });
    renderConnPill(data.settings);
    fillSettingsForm(data.settings);
    settingsMsg("✓ 已保存", true);
    toast("配置已保存");
    setTimeout(() => $("settingsDialog").close(), 600);
  } catch (err) {
    settingsMsg(err.message, false);
  }
}

async function testConnection() {
  // 自检前先保存当前表单（用户改完直接测，少点一次）
  const body = {
    provider: $("cfgProvider").value,
    base_url: $("cfgBaseUrl").value.trim(),
    api_key: $("cfgApiKey").value,
    model: $("cfgModel").value.trim(),
  };
  try {
    await api("/api/config", { method: "PUT", body: JSON.stringify(body) });
  } catch (err) {
    settingsMsg(err.message, false);
    return;
  }
  settingsMsg("正在测试连接…", true);
  try {
    const data = await api("/api/config/test", { method: "POST", body: "{}" });
    settingsMsg((data.ok ? "✓ " : "✗ ") + data.message, data.ok);
    await loadConfig();
  } catch (err) {
    settingsMsg(err.message, false);
  }
}

/* ============================================================
 * 审查：发起 / 轮询 / 时间线渲染
 * ============================================================ */

function setMode(mode) {
  state.mode = mode;
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.mode === mode);
  });
  $("panePath").classList.toggle("hidden", mode !== "path");
  $("paneCode").classList.toggle("hidden", mode !== "code");
}

async function startReview() {
  const body = { ask: $("inputAsk").value.trim() || null };
  if (state.mode === "path") {
    body.path = $("inputPath").value.trim() || ".";
    if (state.root) body.root = state.root;   // 选定的任意本地目录
  } else {
    body.code = $("inputCode").value;
    body.file_name = $("inputFileName").value.trim() || "snippet.py";
    if (!body.code || !body.code.trim()) {
      toast("请先粘贴要审查的代码", true);
      return;
    }
  }

  $("btnReview").disabled = true;
  hideResults();
  try {
    const data = await api("/api/review", { method: "POST", body: JSON.stringify(body) });
    state.taskId = data.task_id;
    pushHistory(data.task_id, data.target, "running");
    $("timelineCard").classList.remove("hidden");
    $("timeline").innerHTML = "";
    setStatus("running", "运行中…");
    state.pollTimer = setInterval(pollTimeline, 600);
  } catch (err) {
    $("btnReview").disabled = false;
    toast(err.message, true);
  }
}

function hideResults() {
  $("reportCard").classList.add("hidden");
  $("rawCard").classList.add("hidden");
}

async function pollTimeline() {
  if (!state.taskId) return;
  try {
    const data = await api(`/api/review/${state.taskId}/timeline`);
    renderTimeline(data.events);
    if (data.status === "done" || data.status === "error") {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
      $("btnReview").disabled = false;
      setStatus(data.status, data.status === "done" ? "已完成" : "失败");
      updateHistory(data.task_id, data.status);
      if (data.status === "error") {
        toast(data.error || "审查失败", true);
        return;
      }
      renderReport(data);
    }
  } catch (err) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
    $("btnReview").disabled = false;
    setStatus("error", "失败");
    toast(err.message, true);
  }
}

function setStatus(status, text) {
  const el = $("timelineStatus");
  el.textContent = text;
  el.className = "status " + status;
}

const EVENT_PREFIX = {
  llm: "◉", tool: "⚙", tool_result: "", retry: "⚠", info: "ℹ",
  error: "✗", final: "✓",
};

function renderTimeline(events) {
  const ul = $("timeline");
  ul.innerHTML = events.map((ev) => {
    let cls = "t-" + ev.kind;
    if (ev.kind === "tool_result" && ev.data && ev.data.ok === false) {
      cls = "t-tool_result_fail";
    } else if (ev.kind === "tool_result") {
      cls = "t-tool_result_ok";
    }
    const prefix = EVENT_PREFIX[ev.kind] !== undefined ? EVENT_PREFIX[ev.kind] + " " : "";
    return `<li class="${cls}">${escapeHtml(prefix + ev.text)}</li>`;
  }).join("");
  ul.scrollTop = ul.scrollHeight;
}

/* ============================================================
 * 报告渲染与修复
 * ============================================================ */

const SEVERITY_META = {
  error: { label: "严重", cls: "badge-error", weight: 0 },
  warning: { label: "警告", cls: "badge-warning", weight: 1 },
  suggestion: { label: "建议", cls: "badge-suggestion", weight: 2 },
};

function renderReport(data) {
  const report = data.report || { parsed: false, summary: "", issues: [] };
  // 缓存最近一次报告与审查根：applyFix 拼接 /api/fix 的 path 用
  window.__lastReport = report;
  window.__lastRootRel = data.root_rel || ".";
  if (!report.parsed) {
    // JSON 解析失败：降级展示原文
    $("rawCard").classList.remove("hidden");
    $("rawContent").textContent = data.content || "(空)";
    return;
  }
  $("reportCard").classList.remove("hidden");
  $("reportSummary").textContent = report.summary || "未发现需要报告的问题";

  const issues = (report.issues || []).slice().sort(
    (a, b) => (SEVERITY_META[a.severity]?.weight ?? 3) - (SEVERITY_META[b.severity]?.weight ?? 3)
  );
  $("issuesList").innerHTML = issues.map((issue, idx) => renderIssue(issue, idx)).join("");

  const counts = { error: 0, warning: 0, suggestion: 0 };
  issues.forEach((i) => { counts[i.severity] = (counts[i.severity] || 0) + 1; });
  const s = data.stats || {};
  $("reportMeta").textContent =
    `共 ${issues.length} 个问题：${counts.error || 0} 严重 / ${counts.warning || 0} 警告 / ` +
    `${counts.suggestion || 0} 建议 · 推理 ${s.iterations ?? "-"} 轮 · 工具调用 ${s.tool_calls ?? "-"} 次` +
    (s.truncated ? " · 已达轮数上限（部分报告）" : "");
}

function renderIssue(issue, idx) {
  const meta = SEVERITY_META[issue.severity] || SEVERITY_META.suggestion;
  const loc = issue.file
    ? (issue.line ? `${issue.file}:${issue.line}` : issue.file)
    : "(未知文件)";
  const canFix = issue.fix && issue.file;
  return `
    <div class="issue" id="issue-${idx}">
      <div class="issue-head">
        <span class="badge ${meta.cls}">${meta.label}</span>
        <span class="issue-loc">${escapeHtml(loc)}</span>
      </div>
      <div class="issue-msg">${escapeHtml(issue.message)}</div>
      ${issue.suggestion ? `<div class="issue-sugg"><b>建议：</b>${escapeHtml(issue.suggestion)}</div>` : ""}
      ${canFix ? `
        <div class="issue-actions">
          <button class="btn btn-primary btn-sm" type="button"
                  onclick="applyFix(${idx})">应用修复</button>
        </div>` : ""}
    </div>`;
}

/** 点击即确认：弹二次确认后调用 /api/fix */
async function applyFix(idx) {
  const report = window.__lastReport;
  if (!report || !report.issues || !report.issues[idx]) return;
  const issue = report.issues[idx];
  if (!issue.fix) return;
  const ok = confirm(
    `确认修复 ${issue.file}:${issue.line}？\n\n` +
    `将替换为：\n${issue.fix.new_code.slice(0, 300)}\n\n` +
    `（执行前会自动备份为 .bak）`
  );
  if (!ok) return;
  // 报告中的 file 相对审查根，接口需要相对项目根的路径
  const rootRel = window.__lastRootRel || ".";
  const relPath = rootRel === "." ? issue.file : `${rootRel}/${issue.file}`;
  try {
    const data = await api("/api/fix", {
      method: "POST",
      body: JSON.stringify({
        path: relPath,
        old_code: issue.fix.old_code,
        new_code: issue.fix.new_code,
        task_id: state.taskId,   // 服务端据此定位项目外的审查根
      }),
    });
    if (data.ok) {
      toast("✓ 已修复（含备份）");
      const card = $(`issue-${idx}`);
      if (card) {
        card.classList.add("fixed");
        const actions = card.querySelector(".issue-actions");
        if (actions) actions.remove();
        card.insertAdjacentHTML("beforeend",
          `<div class="fix-ok">✓ 已应用修复（${escapeHtml(data.message)}）</div>`);
      }
    } else {
      toast(data.message, true);
    }
  } catch (err) {
    toast(err.message, true);
  }
}

/* ============================================================
 * 目录选择器（支持任意本地目录，不限于项目内）
 * ============================================================ */

/** 设定/清除审查根：null 表示回到项目根目录 */
function setPickedRoot(path) {
  state.root = path || null;
  $("rootDisplay").value = path || "（项目根目录）";
}

/** 导航到指定目录并渲染盘符与子目录列表 */
async function navigateDir(path) {
  try {
    const data = await api(`/api/fs/list?path=${encodeURIComponent(path || "")}`);
    dirState.current = data.path;
    dirState.parent = data.parent;
    $("dirCrumb").value = data.path || "（选择一个盘符开始）";
    $("btnDirUp").disabled = !data.parent;

    // 盘符快捷跳转
    $("dirDrives").innerHTML = (data.drives || [])
      .map(
        (d) =>
          `<button type="button" class="tab dir-drive" data-drive="${escapeHtml(d)}">${escapeHtml(d)}</button>`
      )
      .join("");
    $("dirDrives").querySelectorAll("[data-drive]").forEach((btn) => {
      btn.addEventListener("click", () => navigateDir(btn.dataset.drive));
    });

    // 子目录：单击下钻
    const list = $("dirList");
    if (!data.dirs.length) {
      list.innerHTML = `<li class="history-empty">（无子目录）</li>`;
    } else {
      list.innerHTML = data.dirs
        .map((d) => `<li data-name="${escapeHtml(d)}">📁 ${escapeHtml(d)}</li>`)
        .join("");
      list.querySelectorAll("li[data-name]").forEach((li) => {
        li.addEventListener("click", () => {
          const base = dirState.current.replace(/[\\/]+$/, "");
          const sep = base.includes("\\") ? "\\" : "/";
          navigateDir(`${base}${sep}${li.dataset.name}`);
        });
      });
    }
  } catch (err) {
    toast(err.message, true);
  }
}

function openDirPicker() {
  $("dirDialog").showModal();
  navigateDir(dirState.current || "");
}

/* ============================================================
 * 审查历史（本页运行内）
 * ============================================================ */

function pushHistory(taskId, target, status) {
  state.history.unshift({ taskId, target, status, time: new Date() });
  renderHistory();
}

function updateHistory(taskId, status) {
  const item = state.history.find((h) => h.taskId === taskId);
  if (item) { item.status = status; renderHistory(); }
}

function renderHistory() {
  const ul = $("historyList");
  if (!state.history.length) {
    ul.innerHTML = `<li class="history-empty">暂无记录</li>`;
    return;
  }
  ul.innerHTML = state.history.map((h) => {
    const active = h.taskId === state.taskId ? " active" : "";
    const statusCls = "h-" + h.status;
    const statusText = { running: "●", done: "✓", error: "✗" }[h.status] || "";
    return `<li class="${active.trim()}" data-id="${h.taskId}" title="${escapeHtml(h.target)}">
      ${escapeHtml(h.target)}
      <span class="h-status ${statusCls}">${statusText}</span>
    </li>`;
  }).join("");
  ul.querySelectorAll("li[data-id]").forEach((li) => {
    li.addEventListener("click", () => {
      state.taskId = li.dataset.id;
      if (state.pollTimer) clearInterval(state.pollTimer);
      state.pollTimer = null;
      $("btnReview").disabled = false;
      pollHistoryTask(state.taskId);
    });
  });
}

/** 点击历史项：拉取该任务的终态并重新渲染 */
async function pollHistoryTask(taskId) {
  try {
    const data = await api(`/api/review/${taskId}/timeline`);
    $("timelineCard").classList.remove("hidden");
    renderTimeline(data.events);
    setStatus(data.status, data.status === "done" ? "已完成" : data.status === "error" ? "失败" : "运行中…");
    if (data.status === "done") renderReport(data);
    else if (data.status === "error") toast(data.error || "任务失败", true);
    else state.pollTimer = setInterval(pollTimeline, 600); // 仍在运行则继续轮询
  } catch (err) {
    toast(err.message, true);
  }
}

/* ============================================================
 * 事件绑定与初始化
 * ============================================================ */

function resetAll() {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = null;
  state.taskId = null;
  $("btnReview").disabled = false;
  hideResults();
  $("timelineCard").classList.add("hidden");
  $("timeline").innerHTML = "";
  $("inputAsk").value = "";
  $("inputPath").value = ".";
  setPickedRoot(null);          // 审查根回到项目目录
  dirState.current = "";
  dirState.parent = null;
  renderHistory();
}

document.addEventListener("DOMContentLoaded", () => {
  // Tabs
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => setMode(tab.dataset.mode));
  });
  // 审查
  $("btnReview").addEventListener("click", startReview);
  $("btnNew").addEventListener("click", resetAll);
  // 目录选择器
  $("btnPickDir").addEventListener("click", openDirPicker);
  $("btnDirUp").addEventListener("click", () => navigateDir(dirState.parent || ""));
  $("btnDirCancel").addEventListener("click", () => $("dirDialog").close());
  $("btnDirPick").addEventListener("click", () => {
    if (!dirState.current) {
      toast("请先进入一个目录", true);
      return;
    }
    setPickedRoot(dirState.current);
    $("dirDialog").close();
    toast(`审查根已设为 ${dirState.current}`);
  });
  // 设置
  $("btnSettings").addEventListener("click", () => {
    settingsMsg("");
    $("settingsDialog").showModal();
    loadConfig();
  });
  $("btnSaveSettings").addEventListener("click", saveSettings);
  $("btnTestConn").addEventListener("click", testConnection);
  $("btnCancelSettings").addEventListener("click", () => $("settingsDialog").close());
  // 服务商切换时自动带出端点/模型预设提示（填空则用预设）
  $("cfgProvider").addEventListener("change", () => {
    $("cfgBaseUrl").value = "";
    $("cfgModel").value = "";
  });

  loadConfig();
});
