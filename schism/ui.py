"""Schism web UI — Flask app for inspecting and managing the tool catalog."""
from __future__ import annotations

import difflib
import json
import threading
from pathlib import Path

from flask import Flask, jsonify, request

from .store import SchismStore
from .factory import Factory
from .installer import Installer

UI_PORT = 7863

app = Flask(__name__, static_folder=None)
app.config["JSON_SORT_KEYS"] = False

_store: SchismStore | None = None
_factory: Factory | None = None
_installer: Installer | None = None


def create_app(store: SchismStore, factory: Factory, installer: Installer) -> Flask:
    global _store, _factory, _installer
    _store = store
    _factory = factory
    _installer = installer
    return app


# ── API routes ────────────────────────────────────────────────────────────────

@app.route("/api/tools")
def api_tools():
    tool_type = request.args.get("type")
    return jsonify(_store.list_tools(tool_type=tool_type))


@app.route("/api/tools/<int:tool_id>")
def api_tool(tool_id):
    tool = _store.get_tool_by_id(tool_id)
    if not tool:
        return jsonify({"error": "not found"}), 404
    tool["generations"] = _store.get_tool_generations(tool_id)
    tool["active_install"] = _store.get_active_install(tool_id)
    return jsonify(tool)


@app.route("/api/tools/<int:tool_id>/install", methods=["POST"])
def api_tool_install(tool_id):
    tool = _store.get_tool_by_id(tool_id)
    if not tool:
        return jsonify({"error": "not found"}), 404
    try:
        install_path = _installer.install_mcp_tool(tool, tool["code"])
        return jsonify({"install_path": install_path, "status": "installed"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/tools/<int:tool_id>/uninstall", methods=["POST"])
def api_tool_uninstall(tool_id):
    tool = _store.get_tool_by_id(tool_id)
    if not tool:
        return jsonify({"error": "not found"}), 404
    ok = _installer.uninstall_mcp_tool(tool)
    return jsonify({"success": ok})


@app.route("/api/tools/<int:tool_id>/rollback", methods=["POST"])
def api_tool_rollback(tool_id):
    body = request.get_json(silent=True) or {}
    generation = body.get("generation")
    if not generation:
        return jsonify({"error": "generation required"}), 400
    try:
        _store.rollback_tool(tool_id, int(generation))
        tool = _store.get_tool_by_id(tool_id)
        if tool and tool.get("is_installed"):
            _installer.install_mcp_tool(tool, tool["code"])
        return jsonify({"status": "rolled_back", "generation": generation})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/tools/<int:tool_id>/diff")
def api_tool_diff(tool_id):
    v1 = request.args.get("v1", type=int)
    v2 = request.args.get("v2", type=int)
    if not v1 or not v2:
        return jsonify({"error": "v1 and v2 required"}), 400
    generations = _store.get_tool_generations(tool_id)
    gen_map = {g["generation"]: g for g in generations}
    if v1 not in gen_map or v2 not in gen_map:
        return jsonify({"error": "generation not found"}), 404
    diff = "\n".join(difflib.unified_diff(
        gen_map[v1]["code"].splitlines(),
        gen_map[v2]["code"].splitlines(),
        fromfile=f"gen-{v1}",
        tofile=f"gen-{v2}",
        lineterm="",
    ))
    return jsonify({"diff": diff})


@app.route("/api/tools/create", methods=["POST"])
def api_tool_create():
    body = request.get_json(silent=True) or {}
    name = body.get("name", "").strip()
    description = body.get("description", "").strip()
    tool_type = body.get("tool_type", "mcp")
    if not name or not description:
        return jsonify({"error": "name and description required"}), 400

    run_id = _store.create_factory_run(mode=_factory._detect_mode(), model=_factory.model)
    _store.update_factory_run(run_id, status="running")

    def _bg():
        import time
        start = time.time()
        try:
            tool_id = _factory.generate_tool(
                name=name, description=description,
                tool_type=tool_type, factory_run_id=run_id,
            )
            _store.update_factory_run(
                run_id, status="success",
                actions=[{"type": "create", "tool": name, "tool_id": tool_id}],
                duration_s=time.time() - start,
            )
        except Exception as e:
            _store.update_factory_run(run_id, status="failed", error=str(e),
                                       duration_s=time.time() - start)

    threading.Thread(target=_bg, daemon=True).start()
    return jsonify({"run_id": run_id, "status": "generating"})


@app.route("/api/sessions")
def api_sessions():
    return jsonify(_store.list_sessions())


@app.route("/api/sessions/<session_id>/feedback")
def api_session_feedback(session_id):
    return jsonify({
        "feedback": _store.list_feedback(session_id=session_id),
        "progress": _store.list_progress(session_id=session_id),
    })


@app.route("/api/feedback")
def api_feedback():
    limit = request.args.get("limit", 50, type=int)
    return jsonify(_store.list_feedback(limit=limit))


@app.route("/api/progress")
def api_progress():
    limit = request.args.get("limit", 50, type=int)
    return jsonify(_store.list_progress(limit=limit))


@app.route("/api/factory/runs")
def api_factory_runs():
    limit = request.args.get("limit", 20, type=int)
    return jsonify(_store.list_factory_runs(limit=limit))


@app.route("/api/factory/runs/<int:run_id>")
def api_factory_run(run_id):
    run = _store.get_factory_run(run_id)
    if not run:
        return jsonify({"error": "not found"}), 404
    return jsonify(run)


@app.route("/api/factory/reprocess-progress/<int:progress_id>", methods=["POST"])
def api_reprocess_progress(progress_id):
    import time as _time
    run_id = _store.create_factory_run(
        progress_id=progress_id, mode=_factory._detect_mode(), model=_factory.model
    )
    _store.update_factory_run(run_id, status="running")

    def _bg():
        start = _time.time()
        try:
            _factory.process_progress(progress_id)
            # process_progress creates its own run; just mark ours done
            _store.update_factory_run(run_id, status="success", duration_s=_time.time()-start)
        except Exception as e:
            _store.update_factory_run(run_id, status="failed", error=str(e),
                                       duration_s=_time.time()-start)

    threading.Thread(target=_bg, daemon=True).start()
    return jsonify({"run_id": run_id, "status": "started"})


@app.route("/api/factory/reprocess-feedback/<int:feedback_id>", methods=["POST"])
def api_reprocess_feedback(feedback_id):
    import time as _time
    run_id = _store.create_factory_run(
        feedback_id=feedback_id, mode=_factory._detect_mode(), model=_factory.model
    )
    _store.update_factory_run(run_id, status="running")

    def _bg():
        start = _time.time()
        try:
            _factory.process_feedback(feedback_id)
            _store.update_factory_run(run_id, status="success", duration_s=_time.time()-start)
        except Exception as e:
            _store.update_factory_run(run_id, status="failed", error=str(e),
                                       duration_s=_time.time()-start)

    threading.Thread(target=_bg, daemon=True).start()
    return jsonify({"run_id": run_id, "status": "started"})


# ── Main HTML ──────────────────────────────────────────────────────────────────

INDEX_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Schism — Tool Manager</title>
<style>
  :root {
    --bg: #0d1117; --surface: #161b22; --border: #30363d;
    --text: #c9d1d9; --muted: #8b949e; --accent: #58a6ff;
    --green: #3fb950; --yellow: #d29922; --red: #f85149; --purple: #bc8cff;
    --gen-badge: #21262d;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font: 14px/1.5 'Segoe UI',system-ui,sans-serif; }
  header { background: var(--surface); border-bottom: 1px solid var(--border);
           padding: 12px 24px; display: flex; align-items: center; gap: 16px; }
  header h1 { font-size: 18px; font-weight: 600; color: var(--accent); }
  header span { color: var(--muted); font-size: 12px; }
  .tabbar { display: flex; gap: 2px; background: var(--surface);
            border-bottom: 1px solid var(--border); padding: 0 24px; }
  .tab-btn { background: none; border: none; color: var(--muted); cursor: pointer;
             padding: 10px 16px; font-size: 13px; border-bottom: 2px solid transparent; }
  .tab-btn.active { color: var(--text); border-bottom-color: var(--accent); }
  .tab-panel { display: none; padding: 24px; }
  .tab-panel.active { display: block; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 11px;
           font-weight: 600; }
  .badge-mcp { background: #1f3346; color: var(--accent); }
  .badge-gen { background: var(--gen-badge); color: var(--muted); }
  .badge-installed { background: #1a3a25; color: var(--green); }
  .badge-pending { background: #2d2100; color: var(--yellow); }
  .badge-success { background: #1a3a25; color: var(--green); }
  .badge-failed { background: #3a1a1a; color: var(--red); }
  .badge-running { background: #1a2a3a; color: var(--accent); }
  button.btn { padding: 5px 12px; border-radius: 6px; border: 1px solid var(--border);
               background: var(--surface); color: var(--text); cursor: pointer; font-size: 12px; }
  button.btn:hover { border-color: var(--accent); color: var(--accent); }
  button.btn-primary { background: var(--accent); color: #000; border-color: var(--accent); }
  button.btn-primary:hover { opacity: .85; color: #000; }
  button.btn-danger { border-color: var(--red); color: var(--red); }
  .expand-btn { background: none; border: none; color: var(--muted); cursor: pointer;
                font-size: 11px; margin-top: 6px; text-decoration: underline; }
  pre { background: #0d1117; border: 1px solid var(--border); border-radius: 6px;
        padding: 12px; overflow-x: auto; font-size: 12px; font-family: monospace;
        white-space: pre; max-height: 400px; overflow-y: auto; }
  .search-bar { display: flex; gap: 8px; margin-bottom: 16px; }
  .search-bar input { flex: 1; background: var(--surface); border: 1px solid var(--border);
                      border-radius: 6px; padding: 8px 12px; color: var(--text); font-size: 13px; }
  .search-bar input:focus { outline: none; border-color: var(--accent); }
  /* ── Tools table ── */
  .tools-table { width: 100%; border-collapse: collapse; }
  .tools-table th { text-align: left; padding: 8px 12px; color: var(--muted); font-size: 11px;
                    font-weight: 600; text-transform: uppercase; letter-spacing: .05em;
                    border-bottom: 2px solid var(--border); white-space: nowrap; }
  .tool-row td { padding: 10px 12px; font-size: 13px; border-bottom: 1px solid var(--border);
                 vertical-align: middle; }
  .tool-row:hover td { background: var(--surface); }
  .tool-row td.name-cell { font-weight: 600; white-space: nowrap; }
  .tool-row td.cap-cell { color: var(--muted); font-size: 12px; }
  .tool-row td.act-cell { white-space: nowrap; }
  .tool-detail-row { display: none; }
  .tool-detail-row.open { display: table-row; }
  .tool-detail-row td { padding: 16px 20px; background: var(--surface);
                        border-bottom: 2px solid var(--accent); }
  /* ── Timeline ── */
  .timeline { display: flex; flex-direction: column; gap: 0; }
  .timeline-item { display: flex; gap: 16px; padding: 12px 0;
                   border-bottom: 1px solid var(--border); }
  .timeline-gen { width: 48px; text-align: right; color: var(--accent); font-weight: 700;
                  font-size: 18px; flex-shrink: 0; }
  .timeline-content { flex: 1; }
  .timeline-meta { font-size: 11px; color: var(--muted); margin-bottom: 4px; }
  .expanded-content { display: none; }
  .expanded-content.open { display: block; }
  /* ── Sessions ── */
  .session-row { padding: 10px 0; border-bottom: 1px solid var(--border); cursor: pointer; }
  .session-row:hover { color: var(--accent); }
  .session-detail { display: none; padding: 12px; background: var(--surface);
                    border-radius: 6px; margin-top: 8px; }
  .session-detail.open { display: block; }
  .legend { font-size: 12px; color: var(--muted); margin-bottom: 16px; }
  /* ── Factory ── */
  .run-row { padding: 10px 0; border-bottom: 1px solid var(--border); }
  .factory-section { margin-bottom: 32px; }
  .factory-section h3 { font-size: 14px; font-weight: 600; margin-bottom: 12px;
                        color: var(--muted); text-transform: uppercase; letter-spacing: .05em; }
  /* ── Modal ── */
  .modal-overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.7);
                   z-index: 100; align-items: center; justify-content: center; }
  .modal-overlay.open { display: flex; }
  .modal { background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
           padding: 24px; width: 560px; max-width: 95vw; }
  .modal h3 { margin-bottom: 16px; }
  .modal label { font-size: 12px; color: var(--muted); display: block; margin-bottom: 4px; margin-top: 12px; }
  .modal input, .modal textarea, .modal select {
    width: 100%; background: var(--bg); border: 1px solid var(--border);
    border-radius: 6px; padding: 8px 12px; color: var(--text); font-size: 13px;
    font-family: inherit;
  }
  .modal textarea { height: 100px; resize: vertical; }
  .modal-actions { display: flex; gap: 8px; margin-top: 20px; justify-content: flex-end; }
  .empty { color: var(--muted); text-align: center; padding: 48px; }
  #toast { position: fixed; bottom: 24px; right: 24px; background: var(--surface);
           border: 1px solid var(--border); border-radius: 8px; padding: 12px 20px;
           display: none; z-index: 200; font-size: 13px; }
</style>
</head>
<body>
<header>
  <h1>⚡ Schism</h1>
  <span id="header-count">Loading...</span>
  <span style="flex:1"></span>
  <button class="btn btn-primary" onclick="openCreateModal()">+ Create Tool</button>
</header>

<div class="tabbar">
  <button class="tab-btn active" onclick="selectTab('tools')">Tools</button>
  <button class="tab-btn" onclick="selectTab('evolution')">Evolution</button>
  <button class="tab-btn" onclick="selectTab('sessions')">Sessions</button>
  <button class="tab-btn" onclick="selectTab('factory')">Factory Activity</button>
</div>

<!-- TOOLS TAB -->
<div id="tab-tools" class="tab-panel active">
  <div class="search-bar">
    <input id="tool-search" type="text" placeholder="Search tools..." oninput="filterTools(this.value)">
  </div>
  <div id="tools-container"></div>
</div>

<!-- EVOLUTION TAB -->
<div id="tab-evolution" class="tab-panel">
  <div style="margin-bottom:16px">
    <select id="evo-tool-select" onchange="loadEvolution(this.value)"
            style="background:var(--surface);border:1px solid var(--border);border-radius:6px;
                   padding:8px 12px;color:var(--text);font-size:13px;min-width:240px">
      <option value="">Select a tool...</option>
    </select>
  </div>
  <div id="evolution-content"></div>
</div>

<!-- SESSIONS TAB -->
<div id="tab-sessions" class="tab-panel">
  <div class="legend">
    <span class="badge badge-success">processed</span> Factory evaluated this event and may have created/updated a tool &nbsp;
    <span class="badge badge-pending">unprocessed</span> Factory has not yet run on this event — click Reprocess to trigger it
  </div>
  <div id="sessions-list"></div>
</div>

<!-- FACTORY TAB -->
<div id="tab-factory" class="tab-panel">
  <div class="factory-section">
    <h3>Recent Factory Runs</h3>
    <div id="factory-runs-list"></div>
  </div>
</div>

<!-- Create Tool Modal -->
<div class="modal-overlay" id="create-modal">
  <div class="modal">
    <h3>Create New Tool</h3>
    <label>Tool Name</label>
    <input id="new-tool-name" placeholder="My_Tool_Name" type="text">
    <label>Description (be specific about what it does)</label>
    <textarea id="new-tool-desc" placeholder="Wraps the X command to do Y, taking Z as input and returning W..."></textarea>
    <label>Type</label>
    <select id="new-tool-type">
      <option value="mcp">MCP Tool (Claude can call directly)</option>
      <option value="slash_command">Slash Command</option>
      <option value="shell_script">Shell Script</option>
    </select>
    <div class="modal-actions">
      <button class="btn" onclick="closeCreateModal()">Cancel</button>
      <button class="btn btn-primary" onclick="submitCreate()">Generate Tool</button>
    </div>
  </div>
</div>

<div id="toast"></div>

<script>
let allTools = [];
const expandedTools = new Set();   // IDs of currently-open tool detail rows
const detailCache = new Map();     // id -> rendered HTML (avoids re-fetch on refresh)

// ── Tab switching ──────────────────────────────────────────────────────────
function selectTab(name) {
  document.querySelectorAll('.tab-btn').forEach((b,i) => {
    const names = ['tools','evolution','sessions','factory'];
    b.classList.toggle('active', names[i] === name);
  });
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.getElementById('tab-'+name).classList.add('active');
  if (name === 'evolution') loadToolSelect();
  if (name === 'sessions') loadSessions();
  if (name === 'factory') loadFactoryRuns();
}

// ── Toast ──────────────────────────────────────────────────────────────────
function toast(msg, ok=true) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.display = 'block';
  t.style.borderColor = ok ? 'var(--green)' : 'var(--red)';
  setTimeout(() => t.style.display = 'none', 3000);
}

// ── Tools tab ──────────────────────────────────────────────────────────────
async function loadTools() {
  const res = await fetch('/api/tools');
  allTools = await res.json();
  document.getElementById('header-count').textContent =
    allTools.length + ' tool' + (allTools.length !== 1 ? 's' : '');
  renderTools(allTools);
}

function filterTools(q) {
  q = q.toLowerCase();
  renderTools(q
    ? allTools.filter(t =>
        t.name.toLowerCase().includes(q) ||
        t.capability.toLowerCase().includes(q) ||
        (t.use_cases||[]).some(u => u.toLowerCase().includes(q))
      )
    : allTools
  );
}

function renderTools(tools) {
  const container = document.getElementById('tools-container');
  if (!tools.length) {
    container.innerHTML = '<div class="empty">No tools found. Use <strong>/schism add</strong> in Claude Code or click Create Tool.</div>';
    return;
  }
  const rows = tools.map(t => toolRows(t)).join('');
  container.innerHTML = `<table class="tools-table">
    <thead><tr>
      <th>Name</th><th>Type</th><th>Gen</th><th>Status</th>
      <th style="width:40%">Capability</th><th>Actions</th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
  // Re-open any rows that were expanded before the refresh
  expandedTools.forEach(id => {
    const dr = document.getElementById('detail-row-'+id);
    if (dr) {
      dr.classList.add('open');
      const inner = document.getElementById('detail-inner-'+id);
      if (inner && detailCache.has(id)) inner.innerHTML = detailCache.get(id);
    }
  });
}

function toolRows(t) {
  const instBadge = t.is_installed
    ? '<span class="badge badge-installed">installed</span>'
    : '<span style="color:var(--muted);font-size:11px">—</span>';
  const installBtn = t.is_installed
    ? `<button class="btn btn-danger" onclick="event.stopPropagation();uninstallTool(${t.id})">Uninstall</button>`
    : `<button class="btn btn-primary" onclick="event.stopPropagation();installTool(${t.id})">Install</button>`;
  return `
<tr class="tool-row" onclick="toggleExpand(${t.id})" id="tool-row-${t.id}">
  <td class="name-cell">${esc(t.name)}</td>
  <td><span class="badge badge-mcp">${esc(t.tool_type)}</span></td>
  <td><span class="badge badge-gen">gen ${t.generation}</span></td>
  <td>${instBadge}</td>
  <td class="cap-cell">${esc(t.capability)}</td>
  <td class="act-cell" style="display:flex;gap:6px;padding:10px 12px">
    ${installBtn}
    <button class="btn" onclick="event.stopPropagation();toggleExpand(${t.id})">Details</button>
  </td>
</tr>
<tr class="tool-detail-row" id="detail-row-${t.id}">
  <td colspan="6"><div id="detail-inner-${t.id}">Loading...</div></td>
</tr>`;
}

async function toggleExpand(id) {
  const dr = document.getElementById('detail-row-'+id);
  if (!dr) return;
  const isOpen = dr.classList.toggle('open');
  if (isOpen) {
    expandedTools.add(id);
    const inner = document.getElementById('detail-inner-'+id);
    if (!detailCache.has(id)) {
      const res = await fetch('/api/tools/'+id);
      const t = await res.json();
      const html = renderToolDetail(t);
      detailCache.set(id, html);
      if (inner) inner.innerHTML = html;
    } else if (inner) {
      inner.innerHTML = detailCache.get(id);
    }
  } else {
    expandedTools.delete(id);
  }
}

function renderToolDetail(t) {
  const patterns = (t.patterns||[]).map(p => `<li>${esc(p)}</li>`).join('') || '<li>(none)</li>';
  const reqs = (t.requirements||[]).map(r => `<li>${esc(r)}</li>`).join('') || '<li>(none)</li>';
  const useCases = (t.use_cases||[]).map(u => `<li>${esc(u)}</li>`).join('') || '<li>(none)</li>';
  const genOptions = (t.generations||[]).map(g =>
    `<option value="${g.generation}" ${g.generation===t.generation?'selected':''}>${g.generation}</option>`
  ).join('');
  return `
<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:12px">
  <div>
    <p style="font-size:12px;font-weight:600;margin-bottom:4px">Use Cases</p>
    <ul style="font-size:12px;color:var(--muted);margin-left:16px">${useCases}</ul>
  </div>
  <div>
    <p style="font-size:12px;font-weight:600;margin-bottom:4px">Patterns</p>
    <ul style="font-size:12px;color:var(--muted);margin-left:16px">${patterns}</ul>
    <p style="font-size:12px;font-weight:600;margin:8px 0 4px">Requirements</p>
    <ul style="font-size:12px;color:var(--muted);margin-left:16px">${reqs}</ul>
  </div>
</div>
<p style="font-size:12px;margin-bottom:6px">
  <strong>Rollback to gen:</strong>
  <select id="rb-sel-${t.id}" style="margin-left:8px;background:var(--bg);border:1px solid var(--border);
          border-radius:4px;padding:2px 6px;color:var(--text);font-size:12px">${genOptions}</select>
  <button class="btn" style="margin-left:6px" onclick="rollback(${t.id})">Apply</button>
</p>
<pre>${esc(t.code||'')}</pre>`;
}

async function installTool(id) {
  const res = await fetch('/api/tools/'+id+'/install', {method:'POST'});
  const data = await res.json();
  if (data.error) { toast(data.error, false); return; }
  toast('Installed. Restart Claude Code to activate.');
  detailCache.delete(id);
  loadTools();
}

async function uninstallTool(id) {
  if (!confirm('Uninstall this tool?')) return;
  await fetch('/api/tools/'+id+'/uninstall', {method:'POST'});
  toast('Uninstalled.');
  detailCache.delete(id);
  loadTools();
}

async function rollback(id) {
  const gen = parseInt(document.getElementById('rb-sel-'+id).value);
  const res = await fetch('/api/tools/'+id+'/rollback', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({generation: gen})
  });
  const data = await res.json();
  if (data.error) { toast(data.error, false); return; }
  toast(`Rolled back to gen ${gen}.`);
  detailCache.delete(id);
  loadTools();
}

// ── Evolution tab ──────────────────────────────────────────────────────────
async function loadToolSelect() {
  const sel = document.getElementById('evo-tool-select');
  const cur = sel.value;
  sel.innerHTML = '<option value="">Select a tool...</option>' +
    allTools.map(t => `<option value="${t.id}" ${t.id==cur?'selected':''}>${esc(t.name)}</option>`).join('');
  if (cur) loadEvolution(cur);
}

async function loadEvolution(toolId) {
  if (!toolId) { document.getElementById('evolution-content').innerHTML = ''; return; }
  const res = await fetch('/api/tools/'+toolId);
  const t = await res.json();
  const gens = (t.generations||[]).slice().reverse();
  const html = gens.map(g => {
    const ts = new Date(g.created_at*1000).toLocaleString();
    const isCur = g.generation === t.generation;
    return `
<div class="timeline-item">
  <div class="timeline-gen">G${g.generation}</div>
  <div class="timeline-content">
    <div class="timeline-meta">${ts}${isCur ? ' <strong style="color:var(--accent)">◄ current</strong>':''}</div>
    <div style="font-size:13px;margin-bottom:4px">${esc(g.capability)}</div>
    ${g.evolution_note ? `<div style="font-size:12px;color:var(--muted);font-style:italic">${esc(g.evolution_note)}</div>` : ''}
    <button class="expand-btn" onclick="toggleEvoCode(${g.id})">Show code</button>
    <div id="evo-code-${g.id}" class="expanded-content"><pre>${esc(g.code||'')}</pre></div>
  </div>
</div>`;
  }).join('') || '<div class="empty">No generation history.</div>';

  document.getElementById('evolution-content').innerHTML =
    `<div class="timeline">${html}</div>`;
}

function toggleEvoCode(genId) {
  document.getElementById('evo-code-'+genId).classList.toggle('open');
}

// ── Sessions tab ───────────────────────────────────────────────────────────
async function loadSessions() {
  const res = await fetch('/api/sessions');
  const sessions = await res.json();
  const el = document.getElementById('sessions-list');
  if (!sessions.length) {
    el.innerHTML = '<div class="empty">No Executive sessions recorded yet.</div>';
    return;
  }
  el.innerHTML = sessions.map(s => {
    const ts = new Date(s.started_at*1000).toLocaleString();
    return `
<div class="session-row" onclick="toggleSession('${esc(s.id)}')">
  <strong>${esc(s.task_summary||s.id)}</strong>
  <span style="color:var(--muted);font-size:12px;margin-left:12px">${ts}</span>
</div>
<div class="session-detail" id="sd-${s.id.replace(/[^a-z0-9]/gi,'_')}">
  Loading...
</div>`;
  }).join('');
}

async function toggleSession(id) {
  const key = id.replace(/[^a-z0-9]/gi,'_');
  const el = document.getElementById('sd-'+key);
  el.classList.toggle('open');
  if (el.classList.contains('open') && el.textContent.trim() === 'Loading...') {
    const res = await fetch('/api/sessions/'+encodeURIComponent(id)+'/feedback');
    const data = await res.json();
    const fbs = data.feedback || [];
    const progs = data.progress || [];
    if (!fbs.length && !progs.length) {
      el.innerHTML = '<em style="color:var(--muted)">No feedback or progress events recorded.</em>';
      return;
    }

    const progHtml = progs.length ? `
<div style="margin-bottom:12px">
  <strong style="color:var(--accent)">Progress Events (${progs.length})</strong>
  ${progs.map(p => `
  <div style="margin:8px 0 0 12px;padding:8px;background:var(--bg);border-radius:6px;font-size:12px">
    <span style="float:right;display:flex;gap:6px;align-items:center">
      <span class="badge ${p.processed?'badge-success':'badge-pending'}">${p.processed?'processed':'unprocessed'}</span>
      ${!p.processed ? `<button class="btn" style="font-size:11px;padding:2px 8px" onclick="reprocessProgress(${p.id},this)">Reprocess</button>` : ''}
    </span>
    ${p.tool_name ? `<span class="badge badge-mcp" style="margin-right:6px">${esc(p.tool_name)}</span>` : ''}
    <strong>Problem:</strong> ${esc(p.problem)}<br>
    <strong>Solution:</strong> ${esc(p.solution)}<br>
    ${(p.commands_used||[]).length ? `<strong>Commands:</strong><br>${(p.commands_used||[]).map(c=>`<code style="display:block;margin-left:12px">${esc(c)}</code>`).join('')}` : ''}
  </div>`).join('')}
</div>` : '';

    const fbHtml = fbs.length ? `
<div>
  <strong style="color:var(--accent)">Task Feedback (${fbs.length})</strong>
  ${fbs.map(fb => `
  <div style="margin:8px 0 0 12px;padding:8px;background:var(--bg);border-radius:6px;font-size:12px">
    <span style="float:right;display:flex;gap:6px;align-items:center">
      <span class="badge ${fb.processed?'badge-success':'badge-pending'}">${fb.processed?'processed':'unprocessed'}</span>
      ${!fb.processed ? `<button class="btn" style="font-size:11px;padding:2px 8px" onclick="reprocessFeedback(${fb.id},this)">Reprocess</button>` : ''}
    </span>
    <strong>Task:</strong> ${esc(fb.task)}<br>
    <strong>Tools used:</strong> ${Object.entries(fb.tools_used||{}).map(([k,v])=>`${k}: ${v}`).join('; ') || '—'}<br>
    ${Object.keys(fb.tools_unhelpful||{}).length ? `<strong>Not helpful:</strong> ${Object.keys(fb.tools_unhelpful).join(', ')}<br>` : ''}
    <strong>Challenges:</strong> ${esc(fb.challenges)}
  </div>`).join('')}
</div>` : '';

    el.innerHTML = progHtml + fbHtml;
  }
}

async function reprocessProgress(id, btn) {
  btn.disabled = true; btn.textContent = 'Running...';
  const res = await fetch('/api/factory/reprocess-progress/'+id, {method:'POST'});
  const data = await res.json();
  if (data.error) { toast(data.error, false); btn.disabled=false; btn.textContent='Reprocess'; return; }
  toast('Factory run started (run #'+data.run_id+'). Check Factory Activity.');
  btn.textContent = 'Triggered';
}

async function reprocessFeedback(id, btn) {
  btn.disabled = true; btn.textContent = 'Running...';
  const res = await fetch('/api/factory/reprocess-feedback/'+id, {method:'POST'});
  const data = await res.json();
  if (data.error) { toast(data.error, false); btn.disabled=false; btn.textContent='Reprocess'; return; }
  toast('Factory run started (run #'+data.run_id+'). Check Factory Activity.');
  btn.textContent = 'Triggered';
}

// ── Factory tab ────────────────────────────────────────────────────────────
async function loadFactoryRuns() {
  const res = await fetch('/api/factory/runs?limit=30');
  const runs = await res.json();
  const el = document.getElementById('factory-runs-list');
  if (!runs.length) {
    el.innerHTML = '<div class="empty">No factory runs yet. Submit feedback or add a tool.</div>';
    return;
  }
  el.innerHTML = runs.map(r => {
    const ts = new Date(r.created_at*1000).toLocaleString();
    const dur = r.duration_s ? r.duration_s.toFixed(1)+'s' : '—';
    const actions = Array.isArray(r.actions)
      ? r.actions.map(a => `${a.type} ${a.tool}`).join(', ') : '—';
    return `
<div class="run-row">
  <span class="badge badge-${r.status}">${r.status}</span>
  <span style="color:var(--muted);font-size:12px;margin-left:8px">#${r.id} · ${ts} · ${dur}</span>
  <br><span style="font-size:12px;color:var(--text)">${esc(actions)}</span>
  ${r.error ? `<br><span style="font-size:12px;color:var(--red)">${esc(r.error)}</span>` : ''}
</div>`;
  }).join('');
}

// ── Create modal ───────────────────────────────────────────────────────────
function openCreateModal() { document.getElementById('create-modal').classList.add('open'); }
function closeCreateModal() { document.getElementById('create-modal').classList.remove('open'); }

async function submitCreate() {
  const name = document.getElementById('new-tool-name').value.trim();
  const desc = document.getElementById('new-tool-desc').value.trim();
  const type = document.getElementById('new-tool-type').value;
  if (!name || !desc) { toast('Name and description required.', false); return; }
  closeCreateModal();
  const res = await fetch('/api/tools/create', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({name, description: desc, tool_type: type})
  });
  const data = await res.json();
  toast(`Factory started (run #${data.run_id}). Check Factory Activity tab.`);
  setTimeout(loadTools, 2000);
}

// ── Utilities ──────────────────────────────────────────────────────────────
function esc(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ── Init ───────────────────────────────────────────────────────────────────
loadTools();
setInterval(loadTools, 30000);
</script>
</body>
</html>
"""


@app.route("/")
def index():
    from flask import Response
    return Response(INDEX_HTML, mimetype="text/html")


def start_ui(store: SchismStore, factory: Factory, installer: Installer,
             port: int = UI_PORT) -> None:
    create_app(store, factory, installer)
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
