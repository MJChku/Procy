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


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>ProCy Monitor</title>
  <style>
    :root {
      --bg: #f5f7f2; --fg: #1f2a1f; --accent: #2f6f4f; --muted: #5f6f61;
      --card: #ffffff; --bad: #b72136; --ok: #236c42; --border: #d9ded7;
      --proxy: #f4fbf6; --llm: #f8f4ee;
      --edge-proxy: #2f6f4f; --edge-llm: #7e5f2a; --edge-bg: #f6f8f6;
    }
    * { box-sizing: border-box; }
    body { margin:0; font-family: ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial; background: linear-gradient(130deg,#f7f8f3 0%,#eef4ed 100%); color: var(--fg); }
    header { padding:14px 20px; border-bottom:1px solid var(--border); background:rgba(255,255,255,.72); backdrop-filter:blur(6px); position:sticky; top:0; z-index:5; }
    .title { font-size:18px; font-weight:700; }
    .subtitle { font-size:12px; color:var(--muted); margin-top:4px; }
    .layout { display:grid; grid-template-columns:300px 1fr; gap:12px; padding:12px; min-height:calc(100vh - 66px); }
    .panel { background:var(--card); border:1px solid var(--border); border-radius:10px; overflow:hidden; }
    .panel h2 { margin:0; font-size:13px; padding:10px 12px; border-bottom:1px solid var(--border); color:var(--accent); text-transform:uppercase; letter-spacing:.03em; }
    .row { display:flex; align-items:center; justify-content:space-between; gap:8px; }
    .mono { font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace; font-size:12px; }
    .pill { border-radius:999px; border:1px solid var(--border); padding:2px 8px; font-size:11px; line-height:1.5; white-space:nowrap; }
    .ok { color:var(--ok); border-color:#afceb8; background:#edf8f1; }
    .bad { color:var(--bad); border-color:#e2b0b8; background:#fdf0f2; }
    .content { padding:12px; }
    .muted { color:var(--muted); }
    .small { font-size:11px; }

    .session-item { border-bottom:1px solid #edf1eb; padding:10px 12px; cursor:pointer; }
    .session-item:hover { background:#f6faf6; }
    .session-item.active { background:#e8f3ea; }

    .tabbar { display:flex; gap:6px; padding:8px 10px; border-bottom:1px solid var(--border); background:#f8fbf8; }
    .tab-btn { border:1px solid var(--border); background:#fff; color:var(--muted); border-radius:8px; padding:6px 10px; font-size:12px; cursor:pointer; }
    .tab-btn.active { background:#2f6f4f; border-color:#2f6f4f; color:#fff; }

    .turn { border:1px solid var(--border); border-radius:10px; margin-bottom:10px; background:#fcfdfc; padding:8px; }
    .turn-meta { font-size:11px; color:var(--muted); margin-bottom:8px; }
    .edge { border:1px solid #e2e8df; border-radius:8px; background:var(--edge-bg); padding:8px; margin-top:8px; cursor:pointer; width:100%; text-align:left; font:inherit; color:inherit; transition:background 120ms ease, border-color 120ms ease, transform 120ms ease; }
    .edge:hover { border-color:#bfd1c4; transform:translateY(-1px); }
    .edge.human-prompt { border-left:4px solid var(--edge-proxy); background:#eaf7ef; width:calc(100% - 72px); margin-right:72px; }
    .edge.human-prompt:hover { background:#e1f3e9; }
    .edge.agent-response { border-left:4px solid var(--edge-llm); background:#fff4e2; width:calc(100% - 72px); margin-left:72px; cursor:pointer; }
    .edge.agent-response:hover { background:#ffeed5; }
    .edge.procy-prompt { border-left:4px solid #7c3aed; background:#f6edff; width:calc(100% - 72px); margin-right:72px; }
    .edge.procy-prompt:hover { background:#efdeff; }
    .edge-head { display:flex; justify-content:space-between; align-items:center; gap:8px; font-size:11px; color:var(--muted); margin-bottom:6px; }
    .edge-text { font-size:12px; white-space:pre-wrap; word-break:break-word; margin:0; max-height:120px; overflow:auto; }
    .edit-tag { font-size:10px; padding:2px 6px; border-radius:999px; background:#e8f0ff; border:1px solid #b8cbf0; color:#2b5fb5; }

    pre { margin:0; font-size:12px; background:#f4f7f3; border:1px solid #e0e8de; border-radius:8px; padding:8px; white-space:pre-wrap; word-break:break-word; max-height:240px; overflow:auto; }
    button { border:1px solid var(--border); border-radius:8px; padding:8px 12px; cursor:pointer; background:#fff; font:inherit; font-size:12px; }
    button.primary { background:#2f6f4f; border-color:#2f6f4f; color:#fff; }
    button.danger { background:#b72136; border-color:#b72136; color:#fff; }
    textarea, input[type=text] { width:100%; border:1px solid var(--border); border-radius:8px; padding:8px; font:inherit; font-size:12px; background:#fdfefd; }
    textarea { min-height:180px; }
    label { display:block; font-size:11px; color:var(--muted); margin-bottom:4px; }

    /* Slide-left edit panel */
    .slide-left { position:fixed; top:0; left:-45vw; width:45vw; height:100vh; background:#fff; border-right:1px solid var(--border); box-shadow:4px 0 20px rgba(0,0,0,0.08); z-index:20; transition:left 200ms ease; overflow-y:auto; padding:16px; }
    .slide-left.open { left:0; }

    /* Slide-right detail panel */
    .slide-panel { position:fixed; top:0; right:-45vw; width:45vw; height:100vh; background:#fff; border-left:1px solid var(--border); box-shadow:-4px 0 20px rgba(0,0,0,0.08); z-index:20; transition:right 200ms ease; overflow-y:auto; padding:16px; }
    .slide-panel.open { right:0; }
    .slide-panel-header { display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; padding-bottom:8px; border-bottom:1px solid var(--border); }

    .summary-grid { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:8px; margin-top:10px; }
    .metric { border:1px solid var(--border); border-radius:8px; background:#f9fbf8; padding:8px; font-size:12px; }

    .train-table { width:100%; border-collapse:collapse; font-size:12px; margin-top:10px; }
    .train-table th, .train-table td { text-align:left; vertical-align:top; border-bottom:1px solid #edf1eb; padding:8px; }
    .train-cell { max-width:360px; white-space:pre-wrap; word-break:break-word; font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace; font-size:11px; }

    .status-note { font-size:12px; margin-top:8px; color:var(--muted); }

    @media (max-width:1000px) { .layout { grid-template-columns:1fr; } .slide-left { width:85vw; left:-85vw; } .slide-panel { width:85vw; right:-85vw; } }
  </style>
</head>
<body>
  <header>
    <div class="title">ProCy Monitor</div>
    <div class="subtitle">Human prompt (left, green). Agent response (right, gold). Click prompts to correct for training.</div>
  </header>
  <main class="layout">
    <section class="panel">
      <h2>Sessions</h2>
      <div id="sessions"></div>
    </section>
    <section class="panel">
      <h2>Workspace</h2>
      <div class="tabbar">
        <button id="tab-interactions" class="tab-btn active" onclick="selectTab('interactions')">Interactions</button>
        <button id="tab-corrections" class="tab-btn" onclick="selectTab('corrections')">Corrections</button>
        <button id="tab-training" class="tab-btn" onclick="selectTab('training')">Training</button>
      </div>
      <div id="details" class="content muted">Select a session to inspect interactions.</div>
    </section>
  </main>

  <!-- Slide-left: edit/correct panel -->
  <div id="edit-panel" class="slide-left">
    <div class="slide-panel-header">
      <div>
        <div><b id="edit-title">Edit Prompt</b></div>
        <div id="edit-meta" class="small muted"></div>
      </div>
      <button onclick="closeEdit()">Close</button>
    </div>
    <div>
      <label>Current Prompt (read-only)</label>
      <pre id="edge-current" style="max-height:30vh;overflow:auto"></pre>
    </div>
    <div style="margin-top:8px">
      <label>Human Correction (saved for SFT/DPO training)</label>
      <textarea id="edge-edited" style="min-height:30vh"></textarea>
    </div>
    <div style="margin-top:8px">
      <label>Note (optional)</label>
      <input type="text" id="edge-note" placeholder="why this correction is better" />
    </div>
    <div style="margin-top:12px" class="row">
      <div id="edit-status" class="status-note"></div>
      <button class="primary" id="btn-save-edit" onclick="saveEdit()">Save Correction</button>
    </div>
  </div>

  <!-- Slide-right: response detail panel -->
  <div id="slide-panel" class="slide-panel">
    <div class="slide-panel-header">
      <div>
        <div><b id="slide-title">Response Detail</b></div>
        <div id="slide-meta" class="small muted"></div>
      </div>
      <button onclick="closeSlidePanel()">Close</button>
    </div>
    <div id="slide-content"></div>
  </div>

  <script>
    const state = { sessions:[], selectedId:null, sessionData:null, activeTab:'interactions', editTarget:null };

    function esc(s) { if(s===null||s===undefined) return ''; return String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;'); }
    function excerpt(s, n=260) { if(!s) return ''; const r=String(s).trim(); return r.length<=n?r:r.slice(0,n)+'...'; }
    function fmtTs(ts) { if(!ts) return '-'; const d=new Date(ts*1000); return d.toLocaleString(); }

    // ── Init ──
    document.addEventListener('DOMContentLoaded', () => { fetchSessions(); setInterval(refresh, 5000); });
    document.addEventListener('keydown', e => { if(e.key==='Escape'){closeEdit();closeSlidePanel();} });

    function selectTab(tab) {
      state.activeTab = tab;
      ['interactions','corrections','training'].forEach(t => document.getElementById('tab-'+t).classList.toggle('active', t===tab));
      if(state.selectedId) renderWorkspace();
    }

    // ── Sessions ──
    async function fetchSessions() {
      try { const r=await fetch('/api/sessions'); state.sessions=await r.json(); } catch(e) {}
      renderSessions();
      if(!state.selectedId && state.sessions.length>0) selectSession(state.sessions[0].id);
    }

    function renderSessions() {
      const el=document.getElementById('sessions');
      if(!state.sessions.length) { el.innerHTML='<div class="content small muted">No sessions yet.</div>'; return; }
      el.innerHTML=state.sessions.map(s => {
        const active=s.id===state.selectedId?'active':'';
        const statusCls=s.status==='running'?'ok':'muted';
        return `<div class="session-item ${active}" onclick="selectSession('${s.id}')">
          <div class="row"><span class="mono">${esc(s.id.slice(0,8))}</span><span class="pill ${statusCls}">${esc(s.status)}</span></div>
          <div class="small">${esc(s.goal||'')}</div>
          <div class="small muted">${fmtTs(s.started_at)} | ${s.turn_count||0} turns | ${s.correction_count||0} corrections</div>
        </div>`;
      }).join('');
    }

    async function selectSession(id) {
      state.selectedId=id; renderSessions();
      try { const r=await fetch('/api/sessions/'+id); state.sessionData=await r.json(); } catch(e) {}
      renderWorkspace();
    }

    async function refresh() { await fetchSessions(); if(state.selectedId) { try { const r=await fetch('/api/sessions/'+state.selectedId); state.sessionData=await r.json(); renderWorkspace(); } catch(e){} } }

    // ── Workspace ──
    function renderWorkspace() {
      if(state.activeTab==='interactions') renderInteractions();
      else if(state.activeTab==='corrections') renderCorrections();
      else if(state.activeTab==='training') renderTraining();
    }

    function renderInteractions() {
      const el=document.getElementById('details');
      const data=state.sessionData;
      if(!data) { el.innerHTML='<div class="content muted">Select a session.</div>'; return; }
      const session=data.session||{};
      const turns=data.turns||[];
      const corrections=data.corrections||[];

      // Consolidate agent_chunk turns
      const consolidated=[];
      let chunk=null;
      for(const t of turns) {
        if(t.role==='agent_chunk') {
          if(chunk && chunk.turn_num===t.turn_num) { chunk.content+=t.content; chunk.timestamp=t.timestamp; if(t.metadata) chunk.metadata=t.metadata; }
          else { if(chunk) consolidated.push(chunk); chunk={...t, role:'agent'}; }
        } else { if(chunk){consolidated.push(chunk);chunk=null;} consolidated.push(t); }
      }
      if(chunk) consolidated.push(chunk);

      // Group by turn_num: pair human/procy prompt with agent response
      const turnGroups={};
      consolidated.forEach(t => {
        if(!turnGroups[t.turn_num]) turnGroups[t.turn_num]={prompts:[],responses:[]};
        if(t.role==='human'||t.role==='procy') turnGroups[t.turn_num].prompts.push(t);
        else turnGroups[t.turn_num].responses.push(t);
      });

      const turnNums=Object.keys(turnGroups).map(Number).sort((a,b)=>a-b);
      const turnsHtml=turnNums.map(num => {
        const g=turnGroups[num];
        let html='';
        // Check if this turn has a correction
        const corr=corrections.find(c=>c.turn_num===num);
        const corrTag=corr?'<span class="edit-tag">human corrected</span>':'';

        g.prompts.forEach(p => {
          const isProcy=p.role==='procy';
          const cls=isProcy?'procy-prompt':'human-prompt';
          const label=isProcy?'ProCy (evolve) prompt':'Human prompt';
          html+=`<button class="edge ${cls}" onclick="openEdit(${num}, ${JSON.stringify(esc(p.content)).replace(/"/g,'&quot;')})">
            <div class="edge-head"><span>${label} (t${num})</span>${corrTag}</div>
            <pre class="edge-text">${esc(excerpt(p.content,400))}</pre>
          </button>`;
        });
        g.responses.forEach(r => {
          let meta='';
          if(r.metadata) { try { const m=typeof r.metadata==='string'?JSON.parse(r.metadata):r.metadata; if(m.cost_usd) meta+='$'+m.cost_usd.toFixed(4)+' '; } catch(e){} }
          html+=`<div class="edge agent-response" onclick="openSlide(${num}, this)">
            <div class="edge-head"><span>Agent response</span><span>${meta}</span></div>
            <pre class="edge-text">${esc(excerpt(r.content,400))}</pre>
          </div>`;
          // Store full content on element for slide panel
          if(!window._responseCache) window._responseCache={};
          window._responseCache[num]=r.content||'';
        });
        return `<div class="turn"><div class="turn-meta"><div class="row"><span><b>Turn ${num}</b></span><span class="small muted">${g.prompts[0]?fmtTs(g.prompts[0].timestamp):''}</span></div></div>${html}</div>`;
      }).join('');

      const corrCount=corrections.length;
      el.innerHTML=`<div class="content">
        <div class="row"><div><b>${esc(session.goal||'procy session')}</b><div class="small muted mono">${esc(session.id||'')}</div><div class="small muted">${fmtTs(session.started_at)}</div></div><span class="pill ${session.status==='running'?'ok':'muted'}">${esc(session.status||'')}</span></div>
        <div class="summary-grid">
          <div class="metric"><b>Turns</b><br/>${turnNums.length}</div>
          <div class="metric"><b>Corrections</b><br/>${corrCount}</div>
          <div class="metric"><b>Evolve runs</b><br/>${(data.evolves||[]).length}</div>
          <div class="metric"><b>Status</b><br/>${esc(session.status||'-')}</div>
        </div>
        <div style="margin-top:12px;max-height:calc(100vh - 280px);overflow-y:auto;padding-right:4px">${turnsHtml||'<div class="muted">No interactions yet.</div>'}</div>
      </div>`;
    }

    // ── Corrections tab ──
    function renderCorrections() {
      const el=document.getElementById('details');
      fetch('/api/corrections').then(r=>r.json()).then(all => {
        let html=`<div class="content"><div class="row"><b>Corrections (${all.length})</b><div style="display:flex;gap:8px"><button class="primary" onclick="toggleAddForm()">+ Add</button><button onclick="exportTraining()">Export JSONL</button></div></div>`;
        html+=`<div id="add-form" style="display:none;margin-top:10px;padding:10px;border:1px solid var(--border);border-radius:8px;background:#f9fbf8">
          <div style="margin-bottom:6px"><label>Original Prompt</label><textarea id="new-original" rows="2" style="min-height:60px"></textarea></div>
          <div style="margin-bottom:6px"><label>Corrected Prompt</label><textarea id="new-corrected" rows="2" style="min-height:60px"></textarea></div>
          <div style="margin-bottom:6px"><label>Note</label><input type="text" id="new-note" placeholder="why?" /></div>
          <div class="row"><span></span><div style="display:flex;gap:6px"><button class="primary" onclick="saveNewCorrection()">Save</button><button onclick="toggleAddForm()">Cancel</button></div></div>
        </div>`;
        if(all.length===0) { html+='<div class="muted" style="margin-top:12px">No corrections yet. Use <code>!correct</code> in procy or click a prompt above.</div>'; }
        else {
          html+='<table class="train-table" style="margin-top:10px"><thead><tr><th>Turn</th><th>Original</th><th>Corrected</th><th>Note</th><th></th></tr></thead><tbody>';
          all.forEach(c => {
            html+=`<tr><td>t${c.turn_num}</td><td><div class="train-cell">${esc(excerpt(c.original_prompt,200))}</div></td><td><div class="train-cell">${esc(excerpt(c.corrected_prompt,200))}</div></td><td class="small">${esc(c.note||'')}</td><td><button class="danger" onclick="deleteCorrection(${c.id})" style="font-size:11px;padding:4px 8px">Del</button></td></tr>`;
          });
          html+='</tbody></table>';
        }
        html+='</div>';
        el.innerHTML=html;
      });
    }

    function toggleAddForm() { const f=document.getElementById('add-form'); if(f) f.style.display=f.style.display==='none'?'block':'none'; }

    async function saveNewCorrection() {
      const o=document.getElementById('new-original').value.trim();
      const c=document.getElementById('new-corrected').value.trim();
      const n=document.getElementById('new-note').value.trim();
      if(!o||!c) return alert('Both fields required');
      await fetch('/api/corrections',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:state.selectedId||'manual',turn_num:0,original_prompt:o,corrected_prompt:c,note:n||null})});
      renderCorrections();
    }

    async function deleteCorrection(id) { if(!confirm('Delete?')) return; await fetch('/api/corrections/'+id,{method:'DELETE'}); renderCorrections(); }

    // ── Training tab ──
    function renderTraining() {
      const el=document.getElementById('details');
      fetch('/api/training').then(r=>r.json()).then(pairs => {
        let html=`<div class="content"><div class="row"><b>Training Pairs (${pairs.length})</b><button onclick="exportTraining()">Export JSONL</button></div>`;
        if(pairs.length===0) { html+='<div class="muted" style="margin-top:12px">No training data yet. Corrections become SFT pairs.</div>'; }
        else {
          html+='<table class="train-table" style="margin-top:10px"><thead><tr><th>Instruction (original)</th><th>Output (corrected)</th></tr></thead><tbody>';
          pairs.forEach(p => {
            html+=`<tr><td><div class="train-cell">${esc(p.original_prompt)}</div></td><td><div class="train-cell">${esc(p.corrected_prompt)}</div></td></tr>`;
          });
          html+='</tbody></table>';
        }
        html+='</div>';
        el.innerHTML=html;
      });
    }

    async function exportTraining() {
      const r=await fetch('/api/training/export'); const t=await r.text();
      if(!t.trim()) return alert('No data');
      const a=document.createElement('a'); a.href=URL.createObjectURL(new Blob([t],{type:'application/jsonl'})); a.download='procy_train.jsonl'; a.click();
    }

    // ── Slide-left: Edit/Correct ──
    function openEdit(turnNum, currentText) {
      state.editTarget={session_id:state.selectedId, turn_num:turnNum};
      document.getElementById('edit-title').textContent='Edit Prompt (t'+turnNum+')';
      document.getElementById('edit-meta').textContent='session='+((state.selectedId||'').slice(0,8))+' turn='+turnNum;
      // Decode the escaped text
      const ta=document.createElement('textarea'); ta.innerHTML=currentText; const decoded=ta.value;
      document.getElementById('edge-current').textContent=decoded;
      document.getElementById('edge-edited').value=decoded;
      document.getElementById('edge-note').value='';
      document.getElementById('edit-status').textContent='';
      document.getElementById('edit-panel').classList.add('open');
    }

    function closeEdit() { document.getElementById('edit-panel').classList.remove('open'); state.editTarget=null; }

    async function saveEdit() {
      if(!state.editTarget) return;
      const edited=document.getElementById('edge-edited').value.trim();
      const note=document.getElementById('edge-note').value.trim();
      if(!edited) return;
      const original=document.getElementById('edge-current').textContent;
      const st=document.getElementById('edit-status');
      const btn=document.getElementById('btn-save-edit');
      btn.disabled=true; st.textContent='Saving...';
      try {
        await fetch('/api/corrections',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:state.editTarget.session_id,turn_num:state.editTarget.turn_num,original_prompt:original,corrected_prompt:edited,note:note||null})});
        st.textContent='Saved!'; st.style.color='var(--ok)';
        setTimeout(()=>{closeEdit();refresh();},800);
      } catch(e) { st.textContent='Error: '+e; st.style.color='var(--bad)'; }
      finally { btn.disabled=false; }
    }

    // ── Slide-right: Response detail ──
    function openSlide(turnNum) {
      const full=(window._responseCache&&window._responseCache[turnNum])||'';
      document.getElementById('slide-title').textContent='Agent Response (t'+turnNum+')';
      document.getElementById('slide-meta').textContent='turn='+turnNum;
      let html='<h3 style="margin:0 0 6px">Full Response</h3>';
      html+='<pre class="mono" style="background:#f6f8f6;border:1px solid var(--border);border-radius:6px;padding:10px;overflow-x:auto;white-space:pre-wrap;font-size:11px;max-height:70vh;overflow-y:auto">'+esc(full)+'</pre>';
      document.getElementById('slide-content').innerHTML=html;
      document.getElementById('slide-panel').classList.add('open');
    }

    function closeSlidePanel() { document.getElementById('slide-panel').classList.remove('open'); }

    // Close panels on click outside
    document.addEventListener('mousedown', e => {
      const p=e.target.closest('.slide-left,.slide-panel,.edge,button');
      if(p) return;
      if(document.getElementById('edit-panel').classList.contains('open')) closeEdit();
      if(document.getElementById('slide-panel').classList.contains('open')) closeSlidePanel();
    });
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
