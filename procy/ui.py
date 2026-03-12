#!/usr/bin/env python3
"""ProCy Monitor UI — web interface for viewing traces, corrections, and evolve runs.

Usage:
    python3 ui.py [--db procy_traces.db] [--port 7861]
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from flask import Flask, jsonify, request
from .store import TraceStore

app = Flask(__name__)
store: TraceStore = None  # type: ignore


# ── API routes ──

@app.route("/api/sessions")
def api_sessions():
    sessions = store.list_sessions(limit=50)
    for s in sessions:
        turns = store.get_turns(s["id"])
        corrections = store.get_corrections(s["id"])
        evolves = store.get_evolve_runs(s["id"])
        s["turn_count"] = len([t for t in turns if t["role"] == "human"])
        s["correction_count"] = len(corrections)
        s["evolve_count"] = len(evolves)
    return jsonify(sessions)


@app.route("/api/sessions/<session_id>")
def api_session(session_id):
    session = store.get_session(session_id)
    if not session:
        return jsonify({"error": "not found"}), 404
    turns = store.get_turns(session_id)
    corrections = store.get_corrections(session_id)
    evolves = store.get_evolve_runs(session_id)
    actions = store.get_actions(session_id)
    return jsonify({
        "session": session,
        "turns": turns,
        "corrections": corrections,
        "evolves": evolves,
        "actions": actions,
    })


@app.route("/api/corrections", methods=["GET"])
def api_corrections():
    corrections = store.get_corrections()
    return jsonify(corrections)


@app.route("/api/corrections", methods=["POST"])
def api_add_correction():
    data = request.json
    cid = store.log_correction(
        session_id=data["session_id"],
        turn_num=data.get("turn_num", 0),
        original=data["original_prompt"],
        corrected=data["corrected_prompt"],
        note=data.get("note"),
    )
    return jsonify({"id": cid})


@app.route("/api/corrections/<int:correction_id>", methods=["PUT"])
def api_update_correction(correction_id):
    data = request.json
    with store._conn() as c:
        c.execute(
            "UPDATE corrections SET corrected_prompt=?, note=? WHERE id=?",
            (data["corrected_prompt"], data.get("note"), correction_id),
        )
    return jsonify({"ok": True})


@app.route("/api/corrections/<int:correction_id>", methods=["DELETE"])
def api_delete_correction(correction_id):
    with store._conn() as c:
        c.execute("DELETE FROM corrections WHERE id=?", (correction_id,))
    return jsonify({"ok": True})


@app.route("/api/training")
def api_training():
    pairs = store.get_training_pairs()
    return jsonify(pairs)


@app.route("/api/training/export")
def api_training_export():
    pairs = store.get_training_pairs()
    lines = []
    for p in pairs:
        lines.append(json.dumps({
            "instruction": p["original_prompt"],
            "output": p["corrected_prompt"],
        }))
    return "\n".join(lines), 200, {"Content-Type": "application/jsonl"}


# ── Main page ──

@app.route("/")
def index():
    return INDEX_HTML


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ProCy Monitor</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
    background: #0d1117; color: #c9d1d9;
    display: flex; height: 100vh; overflow: hidden;
}

/* Sidebar */
#sidebar {
    width: 280px; min-width: 280px;
    background: #161b22; border-right: 1px solid #30363d;
    display: flex; flex-direction: column; overflow: hidden;
}
#sidebar-header {
    padding: 16px; border-bottom: 1px solid #30363d;
    font-size: 18px; font-weight: 600; color: #c084fc;
}
#sidebar-header span { font-size: 12px; color: #8b949e; display: block; margin-top: 4px; }
#session-list {
    flex: 1; overflow-y: auto; padding: 8px;
}
.session-item {
    padding: 10px 12px; margin: 4px 0; border-radius: 8px;
    cursor: pointer; border: 1px solid transparent;
    transition: all 0.15s;
}
.session-item:hover { background: #1c2333; border-color: #30363d; }
.session-item.active { background: #1c2333; border-color: #c084fc; }
.session-item .sid { font-size: 13px; font-weight: 500; color: #e6edf3; }
.session-item .meta { font-size: 11px; color: #8b949e; margin-top: 4px; }
.session-item .badges { margin-top: 4px; display: flex; gap: 6px; }
.badge {
    font-size: 10px; padding: 2px 6px; border-radius: 4px;
    font-weight: 500;
}
.badge-turns { background: #1f3a2e; color: #3fb950; }
.badge-corrections { background: #3d2a1a; color: #f0883e; }
.badge-evolve { background: #2d1f4e; color: #c084fc; }

/* Main content */
#main {
    flex: 1; display: flex; flex-direction: column; overflow: hidden;
}

/* Tabs */
#tabs {
    display: flex; border-bottom: 1px solid #30363d; background: #161b22;
    padding: 0 16px;
}
.tab {
    padding: 10px 20px; cursor: pointer; font-size: 13px; font-weight: 500;
    color: #8b949e; border-bottom: 2px solid transparent;
    transition: all 0.15s;
}
.tab:hover { color: #e6edf3; }
.tab.active { color: #c084fc; border-bottom-color: #c084fc; }

/* Tab content */
.tab-content { display: none; flex: 1; overflow-y: auto; padding: 20px; }
.tab-content.active { display: block; }

/* Conversation view */
#conversation { max-width: 900px; margin: 0 auto; width: 100%; }
.turn-card {
    margin: 12px 0; display: flex; flex-direction: column;
}
.turn-card.human { align-items: flex-start; }
.turn-card.agent { align-items: flex-end; }
.turn-card.procy { align-items: flex-start; }
.turn-bubble {
    max-width: 75%; padding: 12px 16px; border-radius: 12px;
    font-size: 14px; line-height: 1.5; white-space: pre-wrap;
    word-break: break-word;
}
.turn-card.human .turn-bubble {
    background: #1f3a2e; border: 1px solid #238636; border-bottom-left-radius: 4px;
}
.turn-card.agent .turn-bubble {
    background: #1c2333; border: 1px solid #30363d; border-bottom-right-radius: 4px;
}
.turn-card.procy .turn-bubble {
    background: #2d1f4e; border: 1px solid #8957e5; border-bottom-left-radius: 4px;
}
.turn-label {
    font-size: 11px; font-weight: 600; margin-bottom: 4px; text-transform: uppercase;
    letter-spacing: 0.5px;
}
.turn-card.human .turn-label { color: #3fb950; }
.turn-card.agent .turn-label { color: #8b949e; }
.turn-card.procy .turn-label { color: #c084fc; }
.turn-time { font-size: 10px; color: #484f58; margin-top: 4px; }
.turn-meta { font-size: 11px; color: #8b949e; margin-top: 4px; }

/* Corrections tab */
.correction-card {
    background: #161b22; border: 1px solid #30363d; border-radius: 8px;
    padding: 16px; margin: 12px 0;
}
.correction-card .label { font-size: 11px; font-weight: 600; text-transform: uppercase; margin-bottom: 6px; }
.correction-card .original { color: #f85149; margin-bottom: 12px; }
.correction-card .corrected { color: #3fb950; }
.correction-card textarea {
    width: 100%; background: #0d1117; border: 1px solid #30363d; color: #e6edf3;
    border-radius: 6px; padding: 8px; font-family: inherit; font-size: 13px;
    resize: vertical; min-height: 60px;
}
.correction-card .actions { margin-top: 8px; display: flex; gap: 8px; }
.btn {
    padding: 6px 14px; border-radius: 6px; border: 1px solid #30363d;
    background: #21262d; color: #c9d1d9; cursor: pointer; font-size: 12px;
    font-weight: 500; transition: all 0.15s;
}
.btn:hover { background: #30363d; }
.btn-primary { background: #238636; border-color: #238636; color: #fff; }
.btn-primary:hover { background: #2ea043; }
.btn-danger { background: #da3633; border-color: #da3633; color: #fff; }
.btn-danger:hover { background: #f85149; }
.btn-purple { background: #8957e5; border-color: #8957e5; color: #fff; }
.btn-purple:hover { background: #a371f7; }

/* Evolve tab */
.evolve-card {
    background: #161b22; border: 1px solid #30363d; border-radius: 8px;
    padding: 16px; margin: 12px 0; border-left: 3px solid #8957e5;
}
.evolve-card .iter-badge {
    display: inline-block; background: #2d1f4e; color: #c084fc;
    padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600;
}
.evolve-card .prompt { margin-top: 8px; font-size: 14px; line-height: 1.5; }
.evolve-card .score { margin-top: 8px; font-size: 13px; color: #8b949e; }

/* Training tab */
#training-content { max-width: 900px; margin: 0 auto; width: 100%; }
.training-pair {
    background: #161b22; border: 1px solid #30363d; border-radius: 8px;
    padding: 16px; margin: 12px 0; display: flex; gap: 16px;
}
.training-pair .col { flex: 1; }
.training-pair .col-label {
    font-size: 11px; font-weight: 600; text-transform: uppercase;
    margin-bottom: 6px;
}
.training-pair .instruction { color: #f0883e; }
.training-pair .output { color: #3fb950; }
.training-pair pre {
    font-size: 13px; white-space: pre-wrap; word-break: break-word;
    line-height: 1.5;
}

/* Empty state */
.empty-state {
    text-align: center; padding: 60px 20px; color: #484f58;
}
.empty-state .icon { font-size: 48px; margin-bottom: 16px; }
.empty-state .title { font-size: 18px; color: #8b949e; margin-bottom: 8px; }

/* Actions bar */
.toolbar {
    display: flex; justify-content: space-between; align-items: center;
    margin-bottom: 16px; padding: 8px 0;
}
.toolbar .count { font-size: 13px; color: #8b949e; }

/* Scrollbar */
::-webkit-scrollbar { width: 8px; }
::-webkit-scrollbar-track { background: #0d1117; }
::-webkit-scrollbar-thumb { background: #30363d; border-radius: 4px; }
::-webkit-scrollbar-thumb:hover { background: #484f58; }

/* Status indicator */
.status { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
.status-running { background: #3fb950; animation: pulse 1.5s infinite; }
.status-done { background: #8b949e; }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }

/* Add correction form */
#add-correction-form {
    background: #161b22; border: 1px solid #30363d; border-radius: 8px;
    padding: 16px; margin-bottom: 16px; display: none;
}
#add-correction-form .form-row { margin-bottom: 12px; }
#add-correction-form label {
    display: block; font-size: 12px; font-weight: 500; color: #8b949e;
    margin-bottom: 4px;
}
#add-correction-form textarea, #add-correction-form input {
    width: 100%; background: #0d1117; border: 1px solid #30363d; color: #e6edf3;
    border-radius: 6px; padding: 8px; font-family: inherit; font-size: 13px;
}
</style>
</head>
<body>

<div id="sidebar">
    <div id="sidebar-header">
        ProCy Monitor
        <span>Prompt proxy traces</span>
    </div>
    <div id="session-list"></div>
</div>

<div id="main">
    <div id="tabs">
        <div class="tab active" data-tab="conversation">Conversation</div>
        <div class="tab" data-tab="corrections">Corrections</div>
        <div class="tab" data-tab="evolve">Evolve Runs</div>
        <div class="tab" data-tab="training">Training Data</div>
    </div>

    <div id="tab-conversation" class="tab-content active">
        <div id="conversation">
            <div class="empty-state">
                <div class="title">Select a session</div>
                <div>Choose a session from the sidebar to view the conversation</div>
            </div>
        </div>
    </div>

    <div id="tab-corrections" class="tab-content">
        <div style="max-width:900px;margin:0 auto;width:100%">
            <div class="toolbar">
                <span class="count" id="correction-count"></span>
                <div style="display:flex;gap:8px">
                    <button class="btn btn-primary" onclick="toggleAddCorrection()">+ Add Correction</button>
                    <button class="btn btn-purple" onclick="exportTraining()">Export JSONL</button>
                </div>
            </div>
            <div id="add-correction-form">
                <div class="form-row">
                    <label>Original Prompt</label>
                    <textarea id="new-original" rows="3" placeholder="The original prompt..."></textarea>
                </div>
                <div class="form-row">
                    <label>Corrected Prompt</label>
                    <textarea id="new-corrected" rows="3" placeholder="The improved prompt..."></textarea>
                </div>
                <div class="form-row">
                    <label>Note (optional)</label>
                    <input id="new-note" placeholder="Why this correction?" />
                </div>
                <div class="actions">
                    <button class="btn btn-primary" onclick="saveNewCorrection()">Save</button>
                    <button class="btn" onclick="toggleAddCorrection()">Cancel</button>
                </div>
            </div>
            <div id="corrections-list"></div>
        </div>
    </div>

    <div id="tab-evolve" class="tab-content">
        <div style="max-width:900px;margin:0 auto;width:100%">
            <div id="evolve-list">
                <div class="empty-state">
                    <div class="title">No evolve runs yet</div>
                    <div>Use <code>!evolve N</code> in procy to auto-generate prompts</div>
                </div>
            </div>
        </div>
    </div>

    <div id="tab-training" class="tab-content">
        <div id="training-content">
            <div class="toolbar">
                <span class="count" id="training-count"></span>
                <button class="btn btn-purple" onclick="exportTraining()">Export JSONL</button>
            </div>
            <div id="training-list"></div>
        </div>
    </div>
</div>

<script>
// ── State ──
let currentSession = null;
let sessionData = null;
let allSessions = [];

// ── Init ──
document.addEventListener('DOMContentLoaded', () => {
    loadSessions();
    setupTabs();
    // Auto-refresh every 5s
    setInterval(refreshCurrent, 5000);
});

function setupTabs() {
    document.querySelectorAll('.tab').forEach(tab => {
        tab.addEventListener('click', () => {
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
            tab.classList.add('active');
            document.getElementById('tab-' + tab.dataset.tab).classList.add('active');
        });
    });
}

// ── Sessions ──
async function loadSessions() {
    const resp = await fetch('/api/sessions');
    allSessions = await resp.json();
    renderSessions();
    // Auto-select first if none selected
    if (!currentSession && allSessions.length > 0) {
        selectSession(allSessions[0].id);
    }
}

function renderSessions() {
    const el = document.getElementById('session-list');
    if (allSessions.length === 0) {
        el.innerHTML = '<div class="empty-state"><div class="title">No sessions</div></div>';
        return;
    }
    el.innerHTML = allSessions.map(s => {
        const isActive = s.id === currentSession;
        const started = new Date(s.started_at * 1000);
        const timeStr = started.toLocaleDateString() + ' ' + started.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
        const statusCls = s.status === 'running' ? 'status-running' : 'status-done';
        return `
            <div class="session-item ${isActive ? 'active' : ''}" onclick="selectSession('${s.id}')">
                <div class="sid">
                    <span class="status ${statusCls}"></span>
                    ${s.id.slice(0, 8)}
                </div>
                <div class="meta">${timeStr}${s.goal ? ' — ' + escHtml(s.goal) : ''}</div>
                <div class="badges">
                    ${s.turn_count ? `<span class="badge badge-turns">${s.turn_count} turns</span>` : ''}
                    ${s.correction_count ? `<span class="badge badge-corrections">${s.correction_count} corrections</span>` : ''}
                    ${s.evolve_count ? `<span class="badge badge-evolve">${s.evolve_count} evolves</span>` : ''}
                </div>
            </div>
        `;
    }).join('');
}

async function selectSession(id) {
    currentSession = id;
    renderSessions();
    await loadSession(id);
}

async function loadSession(id) {
    const resp = await fetch(`/api/sessions/${id}`);
    sessionData = await resp.json();
    renderConversation();
    renderCorrections();
    renderEvolves();
    renderTraining();
}

async function refreshCurrent() {
    if (currentSession) {
        await loadSession(currentSession);
    }
    await loadSessions();
}

// ── Conversation ──
function renderConversation() {
    const el = document.getElementById('conversation');
    if (!sessionData || !sessionData.turns || sessionData.turns.length === 0) {
        el.innerHTML = '<div class="empty-state"><div class="title">No turns yet</div></div>';
        return;
    }

    // Group agent_chunk turns into consolidated agent messages per turn_num
    const consolidated = [];
    let currentChunk = null;

    for (const turn of sessionData.turns) {
        if (turn.role === 'agent_chunk') {
            if (currentChunk && currentChunk.turn_num === turn.turn_num) {
                currentChunk.content += turn.content;
            } else {
                if (currentChunk) consolidated.push(currentChunk);
                currentChunk = { ...turn, role: 'agent' };
            }
        } else {
            if (currentChunk) {
                consolidated.push(currentChunk);
                currentChunk = null;
            }
            consolidated.push(turn);
        }
    }
    if (currentChunk) consolidated.push(currentChunk);

    el.innerHTML = consolidated.map(turn => {
        const roleCls = turn.role === 'human' ? 'human' : turn.role === 'procy' ? 'procy' : 'agent';
        const label = turn.role === 'human' ? 'Human' : turn.role === 'procy' ? 'ProCy' : 'Agent';
        const ts = new Date(turn.timestamp * 1000).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'});
        let metaHtml = '';
        if (turn.metadata) {
            try {
                const meta = typeof turn.metadata === 'string' ? JSON.parse(turn.metadata) : turn.metadata;
                if (meta.cost_usd) metaHtml += `<span>$${meta.cost_usd.toFixed(4)}</span>`;
                if (meta.num_turns) metaHtml += `<span>${meta.num_turns} turns</span>`;
            } catch(e) {}
        }
        // Truncate very long agent output for display
        let content = turn.content || '';
        if (content.length > 3000) {
            content = content.slice(0, 3000) + '\n\n... [truncated]';
        }
        return `
            <div class="turn-card ${roleCls}">
                <div class="turn-label">${label} (t${turn.turn_num})</div>
                <div class="turn-bubble">${escHtml(content)}</div>
                <div class="turn-time">${ts} ${metaHtml ? '<span class="turn-meta">' + metaHtml + '</span>' : ''}</div>
            </div>
        `;
    }).join('');
}

// ── Corrections ──
function renderCorrections() {
    const list = document.getElementById('corrections-list');
    const count = document.getElementById('correction-count');
    const corrections = sessionData ? sessionData.corrections : [];

    // Also load all corrections across sessions
    fetch('/api/corrections').then(r => r.json()).then(all => {
        count.textContent = `${all.length} correction${all.length !== 1 ? 's' : ''} total`;
        if (all.length === 0) {
            list.innerHTML = '<div class="empty-state"><div class="title">No corrections yet</div><div>Use <code>!correct</code> in procy to correct prompts for training</div></div>';
            return;
        }
        list.innerHTML = all.map(c => `
            <div class="correction-card" data-id="${c.id}">
                <div class="label original" style="color:#f85149">Original (t${c.turn_num})</div>
                <div style="margin-bottom:12px;font-size:14px;color:#f0883e">${escHtml(c.original_prompt)}</div>
                <div class="label" style="color:#3fb950">Corrected</div>
                <textarea class="corrected-input" rows="3">${escHtml(c.corrected_prompt)}</textarea>
                <div style="margin-top:6px">
                    <div class="label" style="color:#8b949e">Note</div>
                    <input class="note-input" value="${escHtml(c.note || '')}" placeholder="Why?" style="width:100%;background:#0d1117;border:1px solid #30363d;color:#e6edf3;border-radius:6px;padding:6px;font-size:13px" />
                </div>
                <div class="actions">
                    <button class="btn btn-primary" onclick="updateCorrection(${c.id}, this)">Save</button>
                    <button class="btn btn-danger" onclick="deleteCorrection(${c.id})">Delete</button>
                </div>
            </div>
        `).join('');
    });
}

function toggleAddCorrection() {
    const form = document.getElementById('add-correction-form');
    form.style.display = form.style.display === 'none' ? 'block' : 'none';
}

async function saveNewCorrection() {
    const original = document.getElementById('new-original').value.trim();
    const corrected = document.getElementById('new-corrected').value.trim();
    const note = document.getElementById('new-note').value.trim();
    if (!original || !corrected) return alert('Both fields required');
    await fetch('/api/corrections', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            session_id: currentSession || 'manual',
            turn_num: 0,
            original_prompt: original,
            corrected_prompt: corrected,
            note: note || null,
        }),
    });
    document.getElementById('new-original').value = '';
    document.getElementById('new-corrected').value = '';
    document.getElementById('new-note').value = '';
    toggleAddCorrection();
    renderCorrections();
}

async function updateCorrection(id, btn) {
    const card = btn.closest('.correction-card');
    const corrected = card.querySelector('.corrected-input').value;
    const note = card.querySelector('.note-input').value;
    await fetch(`/api/corrections/${id}`, {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ corrected_prompt: corrected, note: note || null }),
    });
    btn.textContent = 'Saved!';
    setTimeout(() => btn.textContent = 'Save', 1500);
}

async function deleteCorrection(id) {
    if (!confirm('Delete this correction?')) return;
    await fetch(`/api/corrections/${id}`, { method: 'DELETE' });
    renderCorrections();
}

// ── Evolve ──
function renderEvolves() {
    const el = document.getElementById('evolve-list');
    if (!sessionData || !sessionData.evolves || sessionData.evolves.length === 0) {
        el.innerHTML = '<div class="empty-state"><div class="title">No evolve runs</div><div>Use <code>!evolve N</code> in procy</div></div>';
        return;
    }
    el.innerHTML = sessionData.evolves.map(e => {
        const ts = new Date(e.timestamp * 1000).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'});
        return `
            <div class="evolve-card">
                <span class="iter-badge">Iteration ${e.iteration}</span>
                <span style="font-size:11px;color:#8b949e;margin-left:8px">${ts}</span>
                <span style="font-size:11px;color:#8b949e;margin-left:8px">source: ${e.source}</span>
                <div class="prompt">${escHtml(e.prompt)}</div>
                ${e.score !== null ? `<div class="score">Score: ${e.score}</div>` : ''}
                ${e.response_summary ? `<div class="score">Response: ${escHtml(e.response_summary)}</div>` : ''}
            </div>
        `;
    }).join('');
}

// ── Training ──
function renderTraining() {
    fetch('/api/training').then(r => r.json()).then(pairs => {
        const el = document.getElementById('training-list');
        const count = document.getElementById('training-count');
        count.textContent = `${pairs.length} training pair${pairs.length !== 1 ? 's' : ''}`;
        if (pairs.length === 0) {
            el.innerHTML = '<div class="empty-state"><div class="title">No training data</div><div>Corrections become training pairs for SFT</div></div>';
            return;
        }
        el.innerHTML = pairs.map(p => `
            <div class="training-pair">
                <div class="col">
                    <div class="col-label instruction">Instruction (original)</div>
                    <pre>${escHtml(p.original_prompt)}</pre>
                </div>
                <div class="col">
                    <div class="col-label output">Output (corrected)</div>
                    <pre>${escHtml(p.corrected_prompt)}</pre>
                </div>
            </div>
        `).join('');
    });
}

async function exportTraining() {
    const resp = await fetch('/api/training/export');
    const text = await resp.text();
    if (!text.trim()) return alert('No training data to export');
    const blob = new Blob([text], {type: 'application/jsonl'});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = 'procy_train.jsonl'; a.click();
    URL.revokeObjectURL(url);
}

// ── Helpers ──
function escHtml(s) {
    if (!s) return '';
    return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
</script>
</body>
</html>
"""


def main():
    import argparse
    parser = argparse.ArgumentParser(description="ProCy Monitor UI")
    parser.add_argument("--db", default="procy_traces.db", help="Trace database path")
    parser.add_argument("--port", type=int, default=7862, help="Port (default: 7862)")
    parser.add_argument("--host", default="0.0.0.0", help="Host (default: 0.0.0.0)")
    args = parser.parse_args()

    global store
    store = TraceStore(args.db)

    print(f"\033[1;35m  ProCy Monitor\033[0m")
    print(f"\033[2m  db: {args.db}\033[0m")
    print(f"\033[2m  http://{args.host}:{args.port}\033[0m")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
