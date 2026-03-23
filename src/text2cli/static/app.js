(function () {
  "use strict";

  const state = {
    workspace: "main",
    files: [],
    tree: null,
    commits: [],
    events: [],
    diff: [],
    openTabs: [],
    activeTab: null,
    tabContents: {},
    tabModified: {},
    sections: { commits: true, events: true },
    llmEnabled: false,
    llmModel: null,
    collapsedDirs: {},
  };

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => document.querySelectorAll(sel);

  async function api(method, path, body) {
    const opts = { method, headers: {} };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(path, opts);
    return res.json();
  }

  // ── State Loading ────────────────────────────────────

  async function loadWorkspaces() {
    const data = await api("GET", "/api/workspaces");
    const sel = $("#workspace-select");
    sel.innerHTML = "";
    for (const ws of data.workspaces || []) {
      const opt = document.createElement("option");
      opt.value = ws.name;
      opt.textContent = ws.name;
      if (ws.name === state.workspace) opt.selected = true;
      sel.appendChild(opt);
    }
  }

  async function loadState() {
    const [stateData, treeData] = await Promise.all([
      api("GET", `/api/state?workspace=${encodeURIComponent(state.workspace)}`),
      api("GET", `/api/tree?workspace=${encodeURIComponent(state.workspace)}`),
    ]);
    state.files = stateData.files || [];
    state.commits = stateData.commits || [];
    state.events = stateData.events || [];
    state.diff = stateData.diff || [];
    state.tree = treeData.tree || null;
    renderAll();
  }

  async function loadConfig() {
    try {
      const [cfg, cacheInfo] = await Promise.all([
        api("GET", "/api/config"),
        api("GET", "/api/cache-stats"),
      ]);
      state.llmEnabled = cfg.llm_enabled || false;
      state.llmModel = cfg.llm_model || null;
      state.searchEnabled = cfg.search_enabled || false;
      state.cacheBackend = cacheInfo.backend || "none";
      state.cacheHitRate = cacheInfo.hit_rate || 0;
    } catch (e) { /* ignore */ }
  }

  function renderAll() {
    renderFileTree();
    renderCommits();
    renderEvents();
    renderTabs();
    renderEditor();
    renderStatus();
  }

  // ── File Tree ────────────────────────────────────────

  function renderFileTree() {
    const container = $("#file-tree");
    if (state.tree && state.tree.children && state.tree.children.length) {
      container.innerHTML = "";
      renderTreeChildren(container, state.tree.children, 0, "");
    } else if (state.files.length) {
      container.innerHTML = "";
      for (const f of state.files) {
        const div = document.createElement("div");
        div.className = "tree-item";
        if (f.staged) div.classList.add("staged");
        if (state.activeTab === f.path) div.classList.add("active");
        const name = f.path.split("/").pop();
        div.innerHTML = `<span class="tree-icon">${fileIcon(name)}</span><span class="tree-name">${esc(f.path)}</span>`;
        if (f.staged) div.innerHTML += ' <span class="badge badge-staged">staged</span>';
        div.onclick = () => openFile(f.path);
        container.appendChild(div);
      }
    } else {
      container.innerHTML = '<div class="tree-item" style="color:var(--text-muted);font-style:italic">No files yet</div>';
    }
  }

  function renderTreeChildren(container, children, depth, parentPath) {
    const dirs = children.filter((c) => c.type === "dir").sort((a, b) => a.name.localeCompare(b.name));
    const files = children.filter((c) => c.type === "file").sort((a, b) => a.name.localeCompare(b.name));

    for (const dir of dirs) {
      const dirPath = parentPath ? `${parentPath}/${dir.name}` : dir.name;
      const isCollapsed = state.collapsedDirs[dirPath];
      const div = document.createElement("div");
      div.className = "tree-item";
      div.style.fontWeight = "500";
      let indent = "";
      for (let i = 0; i < depth; i++) indent += '<span class="tree-indent"></span>';
      const arrow = isCollapsed ? "▸" : "▾";
      div.innerHTML = `${indent}<span class="tree-icon" style="font-size:10px;width:12px">${arrow}</span><span class="tree-icon">📁</span><span class="tree-name">${esc(dir.name)}</span>`;
      div.onclick = () => { state.collapsedDirs[dirPath] = !state.collapsedDirs[dirPath]; renderFileTree(); };
      container.appendChild(div);

      if (!isCollapsed && dir.children && dir.children.length) {
        renderTreeChildren(container, dir.children, depth + 1, dirPath);
      }
    }

    for (const file of files) {
      const div = document.createElement("div");
      div.className = "tree-item";
      if (file.staged) div.classList.add("staged");
      if (state.activeTab === file.path) div.classList.add("active");
      let indent = "";
      for (let i = 0; i < depth; i++) indent += '<span class="tree-indent"></span>';
      div.innerHTML = `${indent}<span class="tree-indent"></span><span class="tree-icon">${fileIcon(file.name)}</span><span class="tree-name">${esc(file.name)}</span>`;
      if (file.staged) div.innerHTML += ' <span class="badge badge-staged">staged</span>';
      div.onclick = () => openFile(file.path);
      container.appendChild(div);
    }
  }

  function fileIcon(name) {
    if (name.endsWith(".md")) return "📝";
    if (name.endsWith(".py")) return "🐍";
    if (name.endsWith(".js")) return "📜";
    if (name.endsWith(".json")) return "{}";
    if (name.endsWith(".yaml") || name.endsWith(".yml")) return "⚙";
    if (name.endsWith(".txt")) return "📄";
    if (name.endsWith(".sh")) return "⌨";
    return "📄";
  }

  // ── Tabs ─────────────────────────────────────────────

  async function openFile(path) {
    if (!state.openTabs.includes(path)) {
      state.openTabs.push(path);
    }

    if (!(path in state.tabContents) || !state.tabModified[path]) {
      try {
        const data = await api("GET", `/api/file?workspace=${encodeURIComponent(state.workspace)}&path=${encodeURIComponent(path)}`);
        state.tabContents[path] = data.content || "";
        state.tabModified[path] = false;
      } catch (e) {
        state.tabContents[path] = "";
        state.tabModified[path] = false;
      }
    }

    state.activeTab = path;
    renderTabs();
    renderEditor();
    renderFileTree();
  }

  function closeTab(path, evt) {
    if (evt) evt.stopPropagation();
    state.openTabs = state.openTabs.filter((t) => t !== path);
    delete state.tabContents[path];
    delete state.tabModified[path];
    if (state.activeTab === path) {
      state.activeTab = state.openTabs.length ? state.openTabs[state.openTabs.length - 1] : null;
    }
    renderTabs();
    renderEditor();
    renderFileTree();
  }

  function renderTabs() {
    const container = $("#editor-tabs");
    container.innerHTML = "";
    for (const path of state.openTabs) {
      const name = path.split("/").pop();
      const tab = document.createElement("div");
      tab.className = "editor-tab";
      if (path === state.activeTab) tab.classList.add("active");
      if (state.tabModified[path]) tab.classList.add("modified");

      tab.innerHTML = `
        <span class="tab-dot"></span>
        <span>${esc(name)}</span>
        <button class="tab-close" data-path="${esc(path)}">×</button>
      `;
      tab.onclick = () => { state.activeTab = path; renderTabs(); renderEditor(); renderFileTree(); };
      tab.querySelector(".tab-close").onclick = (e) => closeTab(path, e);
      container.appendChild(tab);
    }
  }

  // ── Editor ───────────────────────────────────────────

  function renderEditor() {
    const content = $("#editor-content");

    if (!state.activeTab) {
      content.innerHTML = `
        <div class="editor-empty">
          <div class="editor-empty-icon">⬡</div>
          <div class="editor-empty-text">
            Agent-native Workspace Database<br>
            <span style="color:var(--text-muted);font-size:12px">Select a file or use the chat to get started</span>
          </div>
          <div class="editor-empty-shortcuts">
            <span><kbd>/ls</kbd> list files</span>
            <span><kbd>/write path content</kbd> create file</span>
            <span><kbd>/find *.md</kbd> search files</span>
            <span><kbd>/grep pattern</kbd> search content</span>
            <span><kbd>/commit msg</kbd> commit changes</span>
          </div>
        </div>
      `;
      return;
    }

    const text = state.tabContents[state.activeTab] || "";
    const lines = text.split("\n");

    let lineNums = "";
    for (let i = 1; i <= Math.max(lines.length, 1); i++) {
      lineNums += i + "\n";
    }

    content.innerHTML = `
      <div class="code-editor">
        <div class="line-numbers" id="line-nums">${lineNums}</div>
        <textarea class="code-textarea" id="code-area" spellcheck="false">${esc(text)}</textarea>
      </div>
    `;

    const textarea = $("#code-area");
    const lineDiv = $("#line-nums");

    textarea.addEventListener("input", () => {
      state.tabContents[state.activeTab] = textarea.value;
      state.tabModified[state.activeTab] = true;
      renderTabs();
      updateLineNumbers(textarea, lineDiv);
    });

    textarea.addEventListener("scroll", () => {
      lineDiv.scrollTop = textarea.scrollTop;
    });

    textarea.addEventListener("keydown", (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "s") {
        e.preventDefault();
        saveCurrentFile();
      }
      if (e.key === "Tab") {
        e.preventDefault();
        const start = textarea.selectionStart;
        const end = textarea.selectionEnd;
        textarea.value = textarea.value.substring(0, start) + "    " + textarea.value.substring(end);
        textarea.selectionStart = textarea.selectionEnd = start + 4;
        textarea.dispatchEvent(new Event("input"));
      }
    });
  }

  function updateLineNumbers(textarea, lineDiv) {
    const lines = textarea.value.split("\n");
    let nums = "";
    for (let i = 1; i <= lines.length; i++) nums += i + "\n";
    lineDiv.textContent = nums;
  }

  async function saveCurrentFile() {
    if (!state.activeTab) return;
    const content = state.tabContents[state.activeTab];
    await api("PUT", "/api/file", {
      workspace: state.workspace,
      path: state.activeTab,
      content: content,
    });
    state.tabModified[state.activeTab] = false;
    renderTabs();
    await loadState();
    addAgentMessage(`已保存 \`${state.activeTab}\` 到 staged.`);
  }

  // ── Commits & Events ─────────────────────────────────

  function renderCommits() {
    const container = $("#commit-list");
    if (!state.commits.length) {
      container.innerHTML = '<div class="commit-item" style="color:var(--text-muted)">No commits</div>';
      return;
    }
    container.innerHTML = state.commits.map((c) => `
      <div class="commit-item" title="${esc(c.id)}">
        <span class="commit-hash">${esc(c.id.substring(0, 10))}</span>
        <span class="commit-msg">${esc(c.message)}</span>
      </div>
    `).join("");
  }

  function renderEvents() {
    const container = $("#event-list");
    if (!state.events.length) {
      container.innerHTML = '<div class="event-item" style="color:var(--text-muted)">No events</div>';
      return;
    }
    container.innerHTML = state.events.slice(0, 15).map((e) => `
      <div class="event-item">
        <span class="event-type">${esc(e.event_type)}</span>
        <span>${esc(e.workspace_name)}</span>
      </div>
    `).join("");
  }

  // ── Status Bar ───────────────────────────────────────

  function renderStatus() {
    $("#status-workspace").textContent = state.workspace;
    const head = state.commits.length ? state.commits[0].id.substring(0, 10) : "—";
    $("#status-head").textContent = `HEAD: ${head}`;
    $("#status-files").textContent = `${state.files.length} files`;
    const staged = state.files.filter((f) => f.staged).length;
    $("#status-staged").textContent = staged ? `${staged} staged` : "";
    const cacheEl = $("#status-cache");
    if (cacheEl && state.cacheBackend) {
      cacheEl.textContent = `cache: ${state.cacheBackend}`;
    }
    const engineEl = document.querySelector(".statusbar-right span:last-child");
    if (engineEl) {
      let label = state.llmEnabled ? `LLM: ${state.llmModel || "enabled"}` : "Rule Agent";
      if (state.searchEnabled) label += " +Search";
      engineEl.textContent = label;
    }
  }

  // ── Chat ─────────────────────────────────────────────

  const MUTATING_TOOLS = new Set([
    "write_file", "append_file", "sed", "rm", "cp", "mv", "touch",
    "commit", "rollback", "snapshot", "python_exec", "shell_exec", "script",
  ]);

  async function sendChat() {
    const input = $("#chat-input");
    const message = input.value.trim();
    if (!message) return;

    input.value = "";
    input.style.height = "36px";
    addUserMessage(message);
    setStatus("thinking...");

    const stepsContainer = addStreamingMessage();
    const collectedActions = [];

    try {
      const resp = await fetch("/api/chat-stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workspace: state.workspace, message }),
      });

      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ error: "Request failed" }));
        appendStep(stepsContainer, "error", err.error || "Request failed");
        finalizeStreamingMessage(stepsContainer, err.error || "Request failed");
        setStatus("ready");
        return;
      }

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        const lines = buffer.split("\n");
        buffer = lines.pop() || "";

        let eventType = "message";
        for (const line of lines) {
          if (line.startsWith("event: ")) {
            eventType = line.slice(7).trim();
          } else if (line.startsWith("data: ")) {
            const raw = line.slice(6);
            try {
              const data = JSON.parse(raw);
              handleStreamEvent(stepsContainer, eventType, data, collectedActions);
            } catch (_) {}
            eventType = "message";
          }
        }
      }

      await loadState();
      for (const action of collectedActions) {
        if (MUTATING_TOOLS.has(action.tool)) {
          const p = action.args?.path;
          if (p && state.openTabs.includes(p)) {
            try {
              const fresh = await api("GET", `/api/file?workspace=${encodeURIComponent(state.workspace)}&path=${encodeURIComponent(p)}`);
              state.tabContents[p] = fresh.content || "";
              state.tabModified[p] = false;
              if (state.activeTab === p) renderEditor();
            } catch (_) {}
          }
        }
      }
    } catch (e) {
      appendStep(stepsContainer, "error", e.message);
    }
    setStatus("ready");
  }

  function handleStreamEvent(container, eventType, data, actions) {
    if (eventType === "token") {
      appendToken(container, data);
      setStatus("generating...");
    } else if (eventType === "thinking") {
      setThinkingBubble(container, data);
      setStatus("thinking...");
    } else if (eventType === "tool_call") {
      finalizeTokensAsThinking(container);
      const argsStr = Object.entries(data.args || {})
        .map(([k, v]) => {
          const vs = String(v);
          return `${k}=${vs.length > 80 ? vs.slice(0, 80) + "..." : vs}`;
        })
        .join(", ");
      appendStep(container, "tool", `${data.tool}(${argsStr})`);
      actions.push(data);
      setStatus(`calling ${data.tool}...`);
    } else if (eventType === "tool_result") {
      const cls = data.status === "error" ? "step-error" : "step-ok";
      appendStep(container, cls, data.output || "(no output)");
      if (MUTATING_TOOLS.has(data.tool)) {
        loadState();
      }
    } else if (eventType === "reply") {
      collapseThinking(container);
      setReplyBubble(container, data);
      setStatus("done");
    } else if (eventType === "error") {
      appendStep(container, "error", data);
    } else if (eventType === "done") {
      finalizeTokensAsReply(container, data.reply);
      setStatus("done");
    }
  }

  function addStreamingMessage() {
    const container = $("#chat-messages");
    const div = document.createElement("div");
    div.className = "chat-message agent streaming";
    div.innerHTML = `
      <span class="chat-sender">agent</span>
      <div class="chat-steps"></div>
    `;
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
    return div;
  }

  function setThinkingBubble(msgDiv, text) {
    let bubble = msgDiv.querySelector(".chat-thinking");
    if (!bubble) {
      bubble = document.createElement("div");
      bubble.className = "chat-thinking";
      const steps = msgDiv.querySelector(".chat-steps");
      steps.parentNode.insertBefore(bubble, steps);
    }
    bubble.innerHTML = `<span class="thinking-label">Thinking</span><span class="thinking-text">${esc(text)}</span>`;
    scrollChat(msgDiv);
  }

  function collapseThinking(msgDiv) {
    const bubble = msgDiv.querySelector(".chat-thinking");
    if (bubble && !bubble.classList.contains("collapsed")) {
      bubble.classList.add("collapsed");
      bubble.onclick = () => bubble.classList.toggle("collapsed");
    }
  }

  function appendToken(msgDiv, text) {
    let stream = msgDiv.querySelector(".chat-token-stream");
    if (!stream) {
      stream = document.createElement("div");
      stream.className = "chat-token-stream";
      stream.dataset.raw = "";
      const steps = msgDiv.querySelector(".chat-steps");
      steps.parentNode.insertBefore(stream, steps);
    }
    stream.dataset.raw += text;
    stream.textContent = stream.dataset.raw;
    scrollChat(msgDiv);
  }

  function finalizeTokensAsThinking(msgDiv) {
    const stream = msgDiv.querySelector(".chat-token-stream");
    if (!stream || !stream.dataset.raw) return;
    const text = stream.dataset.raw;
    stream.remove();
    setThinkingBubble(msgDiv, text);
    collapseThinking(msgDiv);
  }

  function finalizeTokensAsReply(msgDiv, fallbackReply) {
    const stream = msgDiv.querySelector(".chat-token-stream");
    collapseThinking(msgDiv);
    if (stream && stream.dataset.raw) {
      const text = stream.dataset.raw;
      stream.remove();
      if (!msgDiv.querySelector(".chat-bubble-reply")) {
        setReplyBubble(msgDiv, text);
      }
    } else if (fallbackReply && !msgDiv.querySelector(".chat-bubble-reply")) {
      setReplyBubble(msgDiv, fallbackReply);
    }
  }

  function appendStep(msgDiv, type, text) {
    const steps = msgDiv.querySelector(".chat-steps");
    const step = document.createElement("div");
    step.className = `chat-step chat-step-${type}`;
    if (type === "tool") {
      step.innerHTML = `<span class="step-icon">⚙</span> <code>${esc(text)}</code>`;
    } else if (type === "step-ok") {
      step.innerHTML = `<span class="step-icon">✓</span> <span class="step-output">${esc(text)}</span>`;
    } else if (type === "step-error" || type === "error") {
      step.innerHTML = `<span class="step-icon">✗</span> <span class="step-output step-err">${esc(text)}</span>`;
    }
    steps.appendChild(step);
    scrollChat(msgDiv);
  }

  function setReplyBubble(msgDiv, text) {
    let bubble = msgDiv.querySelector(".chat-bubble-reply");
    if (!bubble) {
      bubble = document.createElement("div");
      bubble.className = "chat-bubble chat-bubble-reply";
      msgDiv.appendChild(bubble);
    }
    bubble.innerHTML = formatReply(text);
    scrollChat(msgDiv);
  }

  function scrollChat(el) {
    const container = el.closest(".chat-messages");
    if (container) container.scrollTop = container.scrollHeight;
  }

  function addUserMessage(text) {
    const container = $("#chat-messages");
    const div = document.createElement("div");
    div.className = "chat-message user";
    div.innerHTML = `
      <span class="chat-sender">you</span>
      <div class="chat-bubble">${esc(text)}</div>
    `;
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
  }

  function addAgentMessage(text, actions) {
    const container = $("#chat-messages");
    const div = document.createElement("div");
    div.className = "chat-message agent";

    let actionsHtml = "";
    if (actions && actions.length) {
      actionsHtml = '<div class="chat-actions">' +
        actions.map((a) => `<span class="chat-action-badge">${esc(a.tool)}</span>`).join("") +
        "</div>";
    }

    div.innerHTML = `
      <span class="chat-sender">agent</span>
      <div class="chat-bubble">${formatReply(text)}</div>
      ${actionsHtml}
    `;
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
  }

  function formatReply(text) {
    return esc(text)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  }

  function setStatus(s) {
    const label = state.llmEnabled ? `${state.llmModel || "LLM"} · ${s}` : `rule · ${s}`;
    $("#chat-status").textContent = label;
  }

  // ── Dialogs ──────────────────────────────────────────

  function showModal(title, fields, onSubmit) {
    const container = $("#modal-container");
    const fieldHtml = fields.map((f) =>
      `<input class="modal-input" id="modal-${f.id}" placeholder="${esc(f.placeholder)}" value="${esc(f.value || "")}">`
    ).join("");

    container.innerHTML = `
      <div class="modal-overlay" onclick="if(event.target===this)app.closeModal()">
        <div class="modal">
          <h3>${esc(title)}</h3>
          ${fieldHtml}
          <div class="modal-actions">
            <button class="titlebar-btn" onclick="app.closeModal()">Cancel</button>
            <button class="titlebar-btn primary" id="modal-submit">OK</button>
          </div>
        </div>
      </div>
    `;

    const firstInput = container.querySelector(".modal-input");
    if (firstInput) setTimeout(() => firstInput.focus(), 50);

    $("#modal-submit").onclick = () => {
      const values = {};
      for (const f of fields) values[f.id] = $(`#modal-${f.id}`).value;
      onSubmit(values);
      closeModal();
    };

    container.querySelector(".modal-input")?.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); $("#modal-submit").click(); }
    });
  }

  function closeModal() {
    $("#modal-container").innerHTML = "";
  }

  function showCommitDialog() {
    const staged = state.files.filter((f) => f.staged);
    if (!staged.length && !state.diff.length) {
      addAgentMessage("没有 staged 变更可提交.");
      return;
    }
    showModal("Commit", [{ id: "message", placeholder: "Commit message..." }], async (vals) => {
      if (!vals.message.trim()) return;
      try {
        const result = await api("POST", "/api/commit", {
          workspace: state.workspace,
          message: vals.message,
        });
        addAgentMessage(`提交完成: ${result.commit_id || result.message || "ok"}`);
        for (const tab of state.openTabs) state.tabModified[tab] = false;
        renderTabs();
        await loadState();
      } catch (e) {
        addAgentMessage(`提交失败: ${e.message}`);
      }
    });
  }

  function showNewFileDialog() {
    showModal("New File", [
      { id: "path", placeholder: "File path (e.g. docs/plan.md)" },
    ], async (vals) => {
      if (!vals.path.trim()) return;
      await api("PUT", "/api/file", {
        workspace: state.workspace,
        path: vals.path,
        content: "",
      });
      await loadState();
      openFile(vals.path);
      addAgentMessage(`已创建 \`${vals.path}\`.`);
    });
  }

  async function showDiff() {
    const data = await api("GET", `/api/state?workspace=${encodeURIComponent(state.workspace)}`);
    const changes = data.diff || [];
    if (!changes.length) {
      addAgentMessage("没有 staged 变更.");
      return;
    }

    state.activeTab = null;
    renderTabs();

    const content = $("#editor-content");
    $("#editor-empty").classList.add("hidden");

    let html = '<div class="diff-view">';
    for (const change of changes) {
      html += `<div class="diff-header">${esc(change.path)} (${esc(change.op)})</div>`;
      if (change.diff) {
        const lines = change.diff.split("\n");
        for (const line of lines) {
          let cls = "context";
          if (line.startsWith("+")) cls = "add";
          else if (line.startsWith("-")) cls = "remove";
          else if (line.startsWith("@@")) cls = "hunk";
          html += `<div class="diff-line ${cls}">${esc(line)}</div>`;
        }
      }
    }
    html += "</div>";
    content.innerHTML = html;
  }

  async function forkWorkspace() {
    showModal("Fork Workspace", [
      { id: "name", placeholder: "New workspace name" },
    ], async (vals) => {
      if (!vals.name.trim()) return;
      try {
        await api("POST", "/api/workspaces", {
          name: vals.name,
          from_workspace: state.workspace,
        });
        await loadWorkspaces();
        state.workspace = vals.name;
        $("#workspace-select").value = vals.name;
        await loadState();
        addAgentMessage(`已创建工作区 \`${vals.name}\` (从 \`${state.workspace}\` fork).`);
      } catch (e) {
        addAgentMessage(`Fork 失败: ${e.message}`);
      }
    });
  }

  function toggleSection(name) {
    state.sections[name] = !state.sections[name];
    const list = $(`#${name === "commits" ? "commit-list" : "event-list"}`);
    const toggle = $(`#${name}-toggle`);
    if (state.sections[name]) {
      list.classList.remove("hidden");
      toggle.textContent = "▾";
    } else {
      list.classList.add("hidden");
      toggle.textContent = "▸";
    }
  }

  // ── Init ─────────────────────────────────────────────

  function esc(s) {
    if (s == null) return "";
    const d = document.createElement("div");
    d.textContent = String(s);
    return d.innerHTML;
  }

  async function init() {
    await loadConfig();
    await loadWorkspaces();
    await loadState();

    $("#workspace-select").addEventListener("change", async (e) => {
      state.workspace = e.target.value;
      state.openTabs = [];
      state.activeTab = null;
      state.tabContents = {};
      state.tabModified = {};
      await loadState();
    });

    const chatInput = $("#chat-input");
    chatInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendChat();
      }
    });

    chatInput.addEventListener("input", () => {
      chatInput.style.height = "36px";
      chatInput.style.height = Math.min(chatInput.scrollHeight, 120) + "px";
    });
  }

  async function refresh() {
    await loadWorkspaces();
    await loadState();
  }

  window.app = {
    sendChat,
    showCommitDialog,
    showNewFileDialog,
    showDiff,
    forkWorkspace,
    closeModal,
    refresh,
    toggleSection,
  };

  init();
})();
