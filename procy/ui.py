#!/usr/bin/env python3
"""ProCy Monitor UI — web interface for viewing traces, corrections, and evolve runs.

Usage:
    python3 ui.py [--db procy_traces.db] [--port 7862]
"""
from __future__ import annotations

import base64
import json
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from .store import TraceStore

app = Flask(__name__)
store: TraceStore = None  # type: ignore
ASSETS_DIR = Path(__file__).resolve().parent / "assets"

# ── Training job state ──
_train_lock = threading.Lock()
_train_status: dict = {"state": "idle", "log": "", "started_at": None, "finished_at": None}


# ── API routes ──

@app.route("/api/sessions")
def api_sessions():
    return jsonify(store.list_sessions_summary(limit=50))


@app.route("/api/sessions/<session_id>")
def api_session(session_id):
    session = store.get_session(session_id)
    if not session:
        return jsonify({"error": "not found"}), 404
    turns = store.get_turns(session_id)
    corrections = store.get_corrections(session_id)
    evolves = store.get_evolve_runs(session_id)
    actions = [a for a in store.get_actions(session_id) if a.get("tool_name") != "procy_command"]
    return jsonify({
        "session": session,
        "turns": turns,
        "corrections": corrections,
        "evolves": evolves,
        "actions": actions,
    })


@app.route("/api/corrections", methods=["GET"])
def api_corrections():
    session_id = (request.args.get("session_id") or "").strip() or None
    corrections = store.get_corrections(session_id=session_id)
    return jsonify(corrections)


@app.route("/api/corrections", methods=["POST"])
def api_add_correction():
    data = request.json or {}
    session_id = str(data.get("session_id") or "").strip()
    original = str(data.get("original_prompt") or "").strip()
    corrected = str(data.get("corrected_prompt") or "").strip()
    if not session_id:
        return jsonify({"error": "session_id is required"}), 400
    if not original or not corrected:
        return jsonify({"error": "original_prompt and corrected_prompt are required"}), 400
    try:
        cid = store.log_correction(
            session_id=session_id,
            turn_num=int(data.get("turn_num", 0)),
            original=original,
            corrected=corrected,
            note=data.get("note"),
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"id": cid})


@app.route("/api/corrections/<int:correction_id>", methods=["PUT"])
def api_update_correction(correction_id):
    data = request.json or {}
    corrected = str(data.get("corrected_prompt") or "").strip()
    if not corrected:
        return jsonify({"error": "corrected_prompt is required"}), 400
    with store._conn() as c:
        cur = c.execute(
            "UPDATE corrections SET corrected_prompt=?, note=? WHERE id=?",
            (corrected, data.get("note"), correction_id),
        )
    if cur.rowcount <= 0:
        return jsonify({"error": "correction not found"}), 404
    return jsonify({"ok": True})


@app.route("/api/corrections/<int:correction_id>", methods=["DELETE"])
def api_delete_correction(correction_id):
    with store._conn() as c:
        cur = c.execute("DELETE FROM corrections WHERE id=?", (correction_id,))
    if cur.rowcount <= 0:
        return jsonify({"error": "correction not found"}), 404
    return jsonify({"ok": True})


@app.route("/api/evolves/<session_id>")
def api_evolves(session_id):
    evolves = store.get_evolve_runs(session_id)
    return jsonify(evolves)


@app.route("/api/terminal/<session_id>")
def api_terminal(session_id):
    after_id = int(request.args.get("after_id", 0))
    limit = int(request.args.get("limit", 5000))
    turn_num_raw = request.args.get("turn_num")
    turn_num = int(turn_num_raw) if turn_num_raw is not None and str(turn_num_raw).strip() != "" else None
    if limit < 1:
        limit = 1
    if limit > 50000:
        limit = 50000
    events = store.get_terminal_events(session_id, after_id=after_id, limit=limit, turn_num=turn_num)
    out = []
    for e in events:
        payload = e.get("payload") or b""
        if isinstance(payload, memoryview):
            payload = payload.tobytes()
        if isinstance(payload, str):
            payload = payload.encode("utf-8", errors="replace")
        out.append({
            "id": e["id"],
            "turn_num": e["turn_num"],
            "stream": e["stream"],
            "timestamp": e["timestamp"],
            "payload_b64": base64.b64encode(payload).decode("ascii"),
        })
    return jsonify({"events": out, "count": len(out)})


@app.route("/api/evaluator/<session_id>")
def api_evaluator(session_id):
    ev = store.get_evaluator(session_id)
    if not ev:
        return jsonify(None)
    return jsonify(ev)


@app.route("/api/eval-results/<session_id>")
def api_eval_results(session_id):
    results = store.get_eval_results(session_id)
    return jsonify(results)


@app.route("/api/training")
def api_training():
    pairs = store.get_training_pairs()
    return jsonify(pairs)


@app.route("/api/training/all")
def api_training_all():
    """All three categories: human, corrected, proxy."""
    data = store.get_training_data_all()
    return jsonify(data)


@app.route("/api/training/export")
def api_training_export():
    """Export training data as JSONL. ?format=sft|dpo|all (default: all)."""
    fmt = request.args.get("format", "all")
    data = store.get_training_data_all()
    lines = []
    for d in data:
        cat = d["category"]
        if fmt == "sft":
            # SFT: human + corrected prompts as targets
            if cat in ("human", "corrected"):
                lines.append(json.dumps({
                    "instruction": d.get("context") or "Generate the next prompt.",
                    "input": d.get("agent_response", "") or "",
                    "output": d["prompt"],
                    "category": cat,
                }))
        elif fmt == "dpo":
            # DPO: only corrected pairs (rejected=original, chosen=corrected)
            if cat == "corrected" and d.get("original_prompt"):
                lines.append(json.dumps({
                    "prompt": d.get("context") or "Generate the next prompt.",
                    "chosen": d["prompt"],
                    "rejected": d["original_prompt"],
                }))
        else:
            # All: include everything with category tag
            lines.append(json.dumps({
                "prompt": d["prompt"],
                "original_prompt": d.get("original_prompt"),
                "agent_response": d.get("agent_response", ""),
                "category": cat,
                "score": d.get("score"),
                "session_id": d.get("session_id"),
            }))
    return "\n".join(lines), 200, {"Content-Type": "application/jsonl"}


@app.route("/api/training/start", methods=["POST"])
def api_training_start():
    """Start a training job on EXP07."""
    global _train_status
    with _train_lock:
        if _train_status["state"] == "running":
            return jsonify({"error": "Training already running"}), 409

    body = request.get_json(silent=True) or {}
    model = body.get("model", "Qwen/Qwen3.5-35B-A3B-GPTQ-Int4")
    epochs = body.get("epochs", 3)
    lr = body.get("lr", 2e-4)
    host = body.get("host", "EXP07")

    def run_training():
        global _train_status
        with _train_lock:
            _train_status = {"state": "running", "log": "", "started_at": time.time(), "finished_at": None}

        def log(msg):
            with _train_lock:
                _train_status["log"] += msg + "\n"

        try:
            # 1. Export training data
            data = store.get_training_data_all()
            sft_lines = []
            for d in data:
                if d["category"] in ("human", "corrected"):
                    sft_lines.append(json.dumps({
                        "instruction": d.get("context") or "Generate the next prompt.",
                        "input": d.get("agent_response", "") or "",
                        "output": d["prompt"],
                        "category": d["category"],
                    }))
            if not sft_lines:
                log("ERROR: No SFT training data (need human or corrected prompts)")
                with _train_lock:
                    _train_status["state"] = "failed"
                    _train_status["finished_at"] = time.time()
                return

            log(f"Exported {len(sft_lines)} SFT examples")

            # 2. Write to temp file and SCP to host
            with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
                f.write("\n".join(sft_lines))
                tmp_path = f.name

            # Also SCP the training script
            script_path = Path(__file__).resolve().parent.parent / "scripts" / "train_proxy.py"

            log(f"Uploading to {host}:/tmp/procy_train/ ...")
            subprocess.run(["ssh", host, "mkdir -p /tmp/procy_train"], check=True, capture_output=True, timeout=10)
            subprocess.run(["scp", tmp_path, f"{host}:/tmp/procy_train/train.jsonl"], check=True, capture_output=True, timeout=30)
            if script_path.exists():
                subprocess.run(["scp", str(script_path), f"{host}:/tmp/procy_train/train_proxy.py"], check=True, capture_output=True, timeout=30)
            Path(tmp_path).unlink(missing_ok=True)
            log("Upload complete")

            # 3. Start Docker training
            docker_cmd = (
                f"docker run --gpus all --rm --name procy_train_job"
                f" --entrypoint bash"
                f" -v /tmp/procy_train:/data"
                f" -v /dev/shm/hf_cache:/root/.cache/huggingface"
                f" -e HF_HOME=/root/.cache/huggingface"
                f" vllm/vllm-openai:latest"
                f" -c 'pip install peft trl datasets accelerate 2>&1 | tail -3"
                f" && python3 /data/train_proxy.py"
                f" --data /data/train.jsonl"
                f" --output /data/proxy_lora_14b"
                f" --model {model}"
                f" --epochs {epochs}"
                f" --lr {lr}"
                f" 2>&1'"
            )
            log(f"Starting training: {model}, {epochs} epochs, lr={lr}")
            log(f"Host: {host}")

            # Remove any leftover container
            subprocess.run(["ssh", host, "docker rm -f procy_train_job"], capture_output=True, timeout=10)

            result = subprocess.run(
                ["ssh", host, docker_cmd],
                capture_output=True, text=True, timeout=600,
            )
            log(result.stdout[-3000:] if len(result.stdout) > 3000 else result.stdout)
            if result.stderr:
                log("STDERR: " + result.stderr[-1000:])

            if result.returncode == 0:
                log("Training complete!")
                with _train_lock:
                    _train_status["state"] = "done"
            else:
                log(f"Training failed (exit code {result.returncode})")
                with _train_lock:
                    _train_status["state"] = "failed"

        except Exception as e:
            log(f"ERROR: {e}")
            with _train_lock:
                _train_status["state"] = "failed"
        finally:
            with _train_lock:
                _train_status["finished_at"] = time.time()

    t = threading.Thread(target=run_training, daemon=True, name="procy-train")
    t.start()
    return jsonify({"status": "started"})


@app.route("/api/training/status")
def api_training_status():
    with _train_lock:
        return jsonify(_train_status)


@app.route("/assets/<path:filename>")
def assets(filename):
    return send_from_directory(str(ASSETS_DIR), filename)


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
  <link rel="stylesheet" href="/assets/xterm.css" />
  <script src="/assets/xterm.min.js"></script>
  <script src="/assets/xterm-addon-fit.js"></script>
  <style>
    :root {
      --bg: #f5f7f2; --fg: #1f2a1f; --accent: #2f6f4f; --muted: #5f6f61;
      --card: #ffffff; --bad: #b72136; --ok: #236c42; --border: #d9ded7;
      --proxy: #f4fbf6; --llm: #f8f4ee;
      --edge-proxy: #2f6f4f; --edge-llm: #7e5f2a; --edge-bg: #f6f8f6;
      --slide-left-w: 30vw; --slide-right-w: 62vw;
    }
    * { box-sizing: border-box; }
    body { margin:0; font-family: ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial; background: linear-gradient(130deg,#f7f8f3 0%,#eef4ed 100%); color: var(--fg); }
    header { padding:14px 20px; border-bottom:1px solid var(--border); background:rgba(255,255,255,.72); backdrop-filter:blur(6px); position:sticky; top:0; z-index:5; }
    .title { font-size:18px; font-weight:700; }
    .subtitle { font-size:12px; color:var(--muted); margin-top:4px; }
    .layout { padding:12px; }
    .layout.show-sessions { display:grid; grid-template-columns:300px 1fr; gap:12px; }
    .panel { background:var(--card); border:1px solid var(--border); border-radius:10px; overflow:hidden; }
    .panel h2 { margin:0; font-size:13px; padding:10px 12px; border-bottom:1px solid var(--border); color:var(--accent); text-transform:uppercase; letter-spacing:.03em; }
    #sessions-panel.hidden { display:none; }
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
    .slide-left { position:fixed; top:0; left:calc(-1 * var(--slide-left-w)); width:var(--slide-left-w); height:100vh; background:#fff; border-right:1px solid var(--border); box-shadow:4px 0 20px rgba(0,0,0,0.08); z-index:20; transition:left 200ms ease; overflow-y:auto; padding:14px; }
    .slide-left.open { left:0; }

    /* Slide-right detail panel */
    .slide-panel { position:fixed; top:0; right:calc(-1 * var(--slide-right-w)); width:var(--slide-right-w); height:100vh; background:#fff; border-left:1px solid var(--border); box-shadow:-4px 0 20px rgba(0,0,0,0.08); z-index:20; transition:right 200ms ease; overflow-y:auto; padding:14px; }
    .slide-panel.open { right:0; }
    .slide-panel-header { display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; padding-bottom:8px; border-bottom:1px solid var(--border); }

    .summary-grid { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:8px; margin-top:10px; }
    .metric { border:1px solid var(--border); border-radius:8px; background:#f9fbf8; padding:8px; font-size:12px; }

    .train-table { width:100%; border-collapse:collapse; font-size:12px; margin-top:10px; }
    .train-table th, .train-table td { text-align:left; vertical-align:top; border-bottom:1px solid #edf1eb; padding:8px; }
    .train-cell { max-width:360px; white-space:pre-wrap; word-break:break-word; font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace; font-size:11px; }

    .status-note { font-size:12px; margin-top:8px; color:var(--muted); }
    .terminal-wrap { border:1px solid var(--border); border-radius:8px; overflow:hidden; background:#f8faf8; overscroll-behavior: contain; }
    .terminal-host { height:calc(100vh - 180px); padding:6px; overscroll-behavior: contain; }
    #slide-terminal { overscroll-behavior: contain; }
    .xterm .xterm-viewport { overscroll-behavior: contain; }
    .xterm { height:100% !important; }

    @media (max-width:1000px) { .layout { grid-template-columns:1fr; } :root { --slide-left-w: 92vw; --slide-right-w: 96vw; } }
  </style>
</head>
<body>
  <header>
    <div class="title">ProCy Monitor</div>
    <div class="subtitle">Human prompt (left, green). Agent response (right, gold). ProCy evolve prompts (violet). Click prompts to correct for training.</div>
  </header>
  <main id="main-layout" class="layout show-sessions">
    <section id="sessions-panel" class="panel">
      <h2>Sessions</h2>
      <div id="sessions"></div>
    </section>
    <section class="panel">
      <div class="tabbar">
        <button id="btn-back" style="display:none;font-size:11px;padding:4px 8px" onclick="showSessions()">← Sessions</button>
        <button id="tab-interactions" class="tab-btn active" onclick="selectTab('interactions')">Interactions</button>
        <button id="tab-terminal" class="tab-btn" onclick="selectTab('terminal')">Terminal</button>
        <button id="tab-evolve" class="tab-btn" onclick="selectTab('evolve')">Evolve</button>
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
    const state = { sessions:[], selectedId:null, sessionData:null, sessionFingerprint:'', activeTab:'interactions', editTarget:null, showAllCorrections:false, term:null, termFit:null, terminalSessionId:null, termAfterId:0, slideTerm:null, slideTermFit:null, refreshInFlight:false, evolveReq:0 };
    const _assetLoadPromises = {};
    const _loadedCssHrefs = new Set();

    function loadScriptOnce(url) {
      if (_assetLoadPromises[url]) return _assetLoadPromises[url];
      _assetLoadPromises[url] = new Promise((resolve, reject) => {
        const s = document.createElement('script');
        s.src = url;
        s.async = true;
        s.onload = () => resolve(true);
        s.onerror = () => reject(new Error('failed '+url));
        document.head.appendChild(s);
      });
      return _assetLoadPromises[url];
    }

    function loadCssOnce(url) {
      if (_loadedCssHrefs.has(url)) return Promise.resolve(true);
      return new Promise((resolve, reject) => {
        const l = document.createElement('link');
        l.rel = 'stylesheet';
        l.href = url;
        l.onload = () => { _loadedCssHrefs.add(url); resolve(true); };
        l.onerror = () => reject(new Error('failed '+url));
        document.head.appendChild(l);
      });
    }

    async function ensureXtermRuntime() {
      if (window.Terminal) return true;
      const cssCandidates = [
        '/assets/xterm.css',
        'https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/css/xterm.css'
      ];
      const jsCandidates = [
        '/assets/xterm.min.js',
        'https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/lib/xterm.min.js'
      ];
      for (const u of cssCandidates) {
        try { await loadCssOnce(u); break; } catch(e) {}
      }
      for (const u of jsCandidates) {
        try { await loadScriptOnce(u); if (window.Terminal) break; } catch(e) {}
      }
      if (!window.Terminal) return false;
      if (!window.FitAddon) {
        const fitCandidates = [
          '/assets/xterm-addon-fit.js',
          'https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.10.0/lib/addon-fit.js'
        ];
        for (const u of fitCandidates) {
          try { await loadScriptOnce(u); if (window.FitAddon) break; } catch(e) {}
        }
      }
      return !!window.Terminal;
    }

    function cleanTraceText(s) {
      if(s===null||s===undefined) return '';
      const raw = String(s).replace(/\r/g,'');
      const cleaned = raw
        .split('\n')
        .map((ln)=>ln.replace(/\u001b\[[0-9;]*[A-Za-z]/g,''))
        .filter((ln)=>{
          const t = ln.trim();
          if(!t) return false;
          // Drop prompt marker residue like "|" or ">" that sometimes leaks
          // into stored turns from terminal integrations.
          if(/^[|>›❯\\\[\]]+$/.test(t)) return false;
          return true;
        });
      return cleaned.join('\n').replace(/\n{3,}/g,'\n\n').trim();
    }
    function esc(s) { if(s===null||s===undefined) return ''; return String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;'); }
    function excerpt(s, n=260) { if(!s) return ''; const r=cleanTraceText(s); return r.length<=n?r:r.slice(0,n)+'...'; }
    function fmtTs(ts) { if(!ts) return '-'; const d=new Date(ts*1000); return d.toLocaleString(); }
    function getDataFingerprint(data) {
      if(!data) return '';
      const turns = Array.isArray(data.turns) ? data.turns : [];
      const last = turns.length ? turns[turns.length - 1] : null;
      const lastText = last ? String(last.content || '') : '';
      const lastTail = lastText.slice(-64);
      return [
        String((data.session || {}).status || ''),
        turns.length,
        last ? Number(last.turn_num || 0) : 0,
        last ? String(last.role || '') : '',
        lastText.length,
        lastTail,
        Array.isArray(data.actions) ? data.actions.length : 0,
        Array.isArray(data.corrections) ? data.corrections.length : 0,
        Array.isArray(data.evolves) ? data.evolves.length : 0
      ].join('|');
    }
    function getWorkspaceScroller() {
      return document.documentElement;
    }
    async function apiGet(url) {
      return fetch(url, { cache: 'no-store' });
    }
    function attachTerminalWheelGuard(host) {
      if(!host || host.dataset.wheelGuardAttached === '1') return;
      host.dataset.wheelGuardAttached = '1';
      host.addEventListener('wheel', (ev) => {
        const viewport = host.querySelector('.xterm-viewport');
        if(!viewport) {
          ev.stopPropagation();
          return;
        }
        const dy = Number(ev.deltaY || 0);
        const atTop = viewport.scrollTop <= 0;
        const atBottom = (viewport.scrollTop + viewport.clientHeight) >= (viewport.scrollHeight - 1);
        if ((dy < 0 && atTop) || (dy > 0 && atBottom)) {
          ev.preventDefault();
        }
        ev.stopPropagation();
      }, { passive: false });
    }

    // ── Init ──
    document.addEventListener('DOMContentLoaded', () => { fetchSessions(); setInterval(refresh, 1500); });
    document.addEventListener('keydown', e => { if(e.key==='Escape'){closeEdit();closeSlidePanel();} });

    function selectTab(tab) {
      state.activeTab = tab;
      _evolveFingerprint = '';  // force re-render when switching tabs
      ['interactions','terminal','evolve','corrections','training'].forEach(t => document.getElementById('tab-'+t).classList.toggle('active', t===tab));
      if(state.selectedId) renderWorkspace();
    }

    // ── Sessions ──
    async function fetchSessions() {
      try { const r=await apiGet('/api/sessions'); state.sessions=await r.json(); } catch(e) {}
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
      // Hide sessions panel, show back button
      document.getElementById('sessions-panel').classList.add('hidden');
      document.getElementById('main-layout').classList.remove('show-sessions');
      document.getElementById('btn-back').style.display='';
      try {
        const r=await apiGet('/api/sessions/'+id);
        state.sessionData=await r.json();
        state.sessionFingerprint=getDataFingerprint(state.sessionData);
      } catch(e) {}
      renderWorkspace();
    }

    function showSessions() {
      document.getElementById('sessions-panel').classList.remove('hidden');
      document.getElementById('main-layout').classList.add('show-sessions');
      document.getElementById('btn-back').style.display='none';
    }

    async function refresh() {
      if(state.refreshInFlight) return;
      state.refreshInFlight = true;
      try {
        // Update sessions list silently (don't re-render unless count changed)
        try {
          const r0=await apiGet('/api/sessions');
          const newSessions=await r0.json();
          if(newSessions.length !== state.sessions.length) {
            state.sessions=newSessions;
            renderSessions();
          } else {
            state.sessions=newSessions;
          }
        } catch(e){}

        if(state.activeTab==='terminal' && state.selectedId && state.term && state.terminalSessionId===state.selectedId) {
          await refreshTerminalIncremental();
        }
        if(state.selectedId) {
          try {
            const r=await apiGet('/api/sessions/'+state.selectedId);
            const newData=await r.json();
            const newFp=getDataFingerprint(newData);
            const changed = newFp !== state.sessionFingerprint;
            state.sessionData=newData;
            if(changed) {
              state.sessionFingerprint=newFp;
              if(state.activeTab==='terminal' && state.term && state.terminalSessionId===state.selectedId) {
                // Keep existing terminal; incremental refresh already done above.
              } else if(state.activeTab==='evolve') {
                // Evolve tab handles its own fingerprint/scroll internally
                renderEvolve();
              } else {
                const savedY = window.scrollY;
                const wasBottom = (window.innerHeight + window.scrollY) >= (document.documentElement.scrollHeight - 24);
                renderWorkspace();
                requestAnimationFrame(() => {
                  if(wasBottom) window.scrollTo(0, document.documentElement.scrollHeight);
                  else window.scrollTo(0, savedY);
                });
              }
            }
            // Always poll evolve tab for new eval results (eval run doesn't change session data)
            if(state.activeTab==='evolve' && !changed) {
              renderEvolve();
            }
          } catch(e){}
        }
      } finally {
        state.refreshInFlight = false;
      }
    }

    // ── Workspace ──
    function renderWorkspace() {
      if(state.activeTab==='interactions') renderInteractions();
      else if(state.activeTab==='terminal') renderTerminal();
      else if(state.activeTab==='evolve') renderEvolve();
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

      // Consolidate agent_chunk rows into one logical agent row.
      const consolidated=[];
      let chunk=null;
      for(const t of turns) {
        if(t.role==='agent_chunk') {
          if(chunk && chunk.turn_num===t.turn_num) {
            chunk.content += (t.content||'');
            chunk.timestamp = t.timestamp;
            if(t.metadata) chunk.metadata=t.metadata;
          } else {
            if(chunk) consolidated.push(chunk);
            chunk={...t, role:'agent'};
          }
        } else {
          if(chunk){consolidated.push(chunk);chunk=null;}
          consolidated.push(t);
        }
      }
      if(chunk) consolidated.push(chunk);

      const sequence = consolidated.filter(t => t.role==='human' || t.role==='procy' || t.role==='agent');
      const promptCount = sequence.filter(t => t.role==='human' || t.role==='procy').length;
      const responseCount = sequence.filter(t => t.role==='agent').length;

      if(!window._promptCache) window._promptCache={};
      if(!window._responseCache) window._responseCache={};

      const sequenceHtml = sequence.map((t, idx) => {
        const ts = fmtTs(t.timestamp);
        if(t.role==='human' || t.role==='procy') {
          const isProcy = t.role==='procy';
          const cls = isProcy ? 'procy-prompt' : 'human-prompt';
          const label = isProcy ? 'ProCy (evolve) prompt' : 'Human prompt';
          const corr = corrections.find(c => Number(c.turn_num||0)===Number(t.turn_num||0));
          const corrTag = corr ? '<span class="edit-tag">human corrected</span>' : '';
          const key = `${Number(t.turn_num||0)}:${t.role}:${idx}:${Math.random().toString(36).slice(2,8)}`;
          window._promptCache[key]=cleanTraceText(t.content||'');
          return `<button class="edge ${cls}" onclick="openEditByKey(${Number(t.turn_num||0)}, '${key}')">
            <div class="edge-head"><span>${label}</span><span class="small muted">${ts}</span>${corrTag}</div>
            <pre class="edge-text">${esc(excerpt(t.content, 500))}</pre>
          </button>`;
        }
        let meta='';
        if(t.metadata) {
          try {
            const m=typeof t.metadata==='string'?JSON.parse(t.metadata):t.metadata;
            if(m && m.model) meta += String(m.model);
            if(m && m.cost_usd) meta += (meta?' | ':'') + '$' + Number(m.cost_usd).toFixed(4);
          } catch(e){}
        }
        const respKey = `${Number(t.turn_num||0)}:agent:${idx}:${Math.random().toString(36).slice(2,8)}`;
        window._responseCache[respKey]=cleanTraceText(t.content||'');
        return `<div class="edge agent-response" onclick="openSlide(${Number(t.turn_num||0)}, '${respKey}')">
          <div class="edge-head"><span>Agent response</span><span>${esc(meta)}${meta?' | ':''}${ts}</span></div>
          <pre class="edge-text">${esc('Click to open terminal replay')}</pre>
        </div>`;
      }).join('');

      const corrCount = corrections.length;
      el.innerHTML=`<div style="padding:6px 12px">
        <div class="row" style="margin-bottom:6px"><div><b>${esc(session.goal||'procy session')}</b> <span class="small muted mono">${esc((session.id||'').slice(0,8))}</span></div><span class="pill ${session.status==='running'?'ok':'muted'}">${esc(session.status||'')}</span><span class="small muted">${promptCount}p/${responseCount}r/${corrCount}c</span></div>
        <div id="interactions-scroller">${sequenceHtml||'<div class="muted">No interactions yet.</div>'}</div>
      </div>`;
    }

    function b64ToBytes(b64) {
      const raw = atob(b64);
      const out = new Uint8Array(raw.length);
      for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
      return out;
    }

    function b64ToText(b64) {
      const bytes = b64ToBytes(b64);
      return new TextDecoder('utf-8', {fatal:false}).decode(bytes);
    }

    function applyTerminalEvent(term, evt) {
      if (!evt || !term) return false;
      if (evt.stream === 'stdout') {
        term.write(b64ToBytes(evt.payload_b64));
        return true;
      }
      if (evt.stream === 'meta') {
        try {
          const meta = JSON.parse(b64ToText(evt.payload_b64));
          if (meta && meta.type === 'resize') {
            const cols = Number(meta.cols || 0);
            const rows = Number(meta.rows || 0);
            if (cols > 0 && rows > 0) term.resize(cols, rows);
          }
        } catch (e) {}
      }
      return false;
    }

    async function renderTerminal() {
      const el=document.getElementById('details');
      if(!state.selectedId) { el.innerHTML='<div class="content muted">Select a session.</div>'; return; }
      el.innerHTML=`<div class="content">
        <div class="row">
          <b>Terminal Replay</b>
          <div style="display:flex;gap:8px">
            <button onclick="renderTerminal()">Reload</button>
            <button onclick="clearTerminalReplay()">Clear</button>
            <button onclick="terminalScroll('top')">Top</button>
            <button onclick="terminalScroll('up')">PgUp</button>
            <button onclick="terminalScroll('down')">PgDn</button>
            <button onclick="terminalScroll('bottom')">Bottom</button>
          </div>
        </div>
        <div class="small muted" style="margin:6px 0 10px 0">Raw PTY bytes rendered with xterm.js (no text cleanup).</div>
        <div class="terminal-wrap"><div id="terminal-host" class="terminal-host"></div></div>
      </div>`;
      const host=document.getElementById('terminal-host');
      if(!host) return;
      const xtermOk = await ensureXtermRuntime();
      if(!xtermOk || !window.Terminal) {
        host.innerHTML='<div class="small muted" style="padding:12px;background:#fff">xterm.js failed to load (CDN blocked). Please allow jsdelivr CDN.</div>';
        return;
      }
      if(state.term) {
        try { state.term.dispose(); } catch(e) {}
        state.term=null;
      }
      state.termFit = null;
      state.term = new window.Terminal({
        convertEol: false,
        allowProposedApi: false,
        cursorBlink: false,
        scrollback: 200000,
        fontSize: 13,
        theme: { background: '#fbfdfb', foreground: '#1f2a1f', cursor: '#2f6f4f' }
      });
      if (window.FitAddon && window.FitAddon.FitAddon) {
        state.termFit = new window.FitAddon.FitAddon();
        state.term.loadAddon(state.termFit);
      }
      state.term.open(host);
      attachTerminalWheelGuard(host);
      if (state.termFit) {
        try { state.termFit.fit(); } catch(e) {}
        // Re-fit on resize
        const ro = new ResizeObserver(() => {
          try { if(state.termFit) state.termFit.fit(); } catch(e) {}
        });
        ro.observe(host);
      }
      state.term.focus();
      state.terminalSessionId = state.selectedId;
      state.termAfterId = 0;
      state.term.write('\x1b[2J\x1b[H');
      let afterId = 0;
      const page = 5000;
      for (let i = 0; i < 30; i++) {
        const r = await apiGet(`/api/terminal/${encodeURIComponent(state.selectedId)}?after_id=${afterId}&limit=${page}`);
        if(!r.ok) break;
        const data = await r.json();
        const events = data.events || [];
        if(!events.length) break;
        for (const evt of events) {
          afterId = evt.id;
          applyTerminalEvent(state.term, evt);
        }
        if(events.length < page) break;
      }
      state.termAfterId = afterId;
    }

    async function refreshTerminalIncremental() {
      if(!state.selectedId || !state.term || state.terminalSessionId!==state.selectedId) return;
      const host = document.getElementById('terminal-host');
      const viewport = host ? host.querySelector('.xterm-viewport') : null;
      const wasNearBottom = !viewport || ((viewport.scrollTop + viewport.clientHeight) >= (viewport.scrollHeight - 8));
      let afterId = Number(state.termAfterId || 0);
      let wrote = false;
      const page = 5000;
      for (let i = 0; i < 6; i++) {
        const r = await apiGet(`/api/terminal/${encodeURIComponent(state.selectedId)}?after_id=${afterId}&limit=${page}`);
        if(!r.ok) break;
        const data = await r.json();
        const events = data.events || [];
        if(!events.length) break;
        for (const evt of events) {
          afterId = evt.id;
          wrote = applyTerminalEvent(state.term, evt) || wrote;
        }
        if(events.length < page) break;
      }
      state.termAfterId = afterId;
      if(wrote && wasNearBottom) {
        state.term.scrollToBottom();
      }
    }

    function clearTerminalReplay() {
      if(state.term) state.term.clear();
    }

    function terminalScroll(which) {
      const t = state.term;
      if(!t) return;
      if(which==='top') t.scrollToTop();
      else if(which==='bottom') t.scrollToBottom();
      else if(which==='up') t.scrollLines(-Math.max(10, Math.floor((t.rows||24)*0.8)));
      else if(which==='down') t.scrollLines(Math.max(10, Math.floor((t.rows||24)*0.8)));
    }

    // ── Evolve tab ──
    let _evolveFingerprint = '';
    function renderEvolve() {
      const reqId = ++state.evolveReq;
      const el=document.getElementById('details');
      if(!state.selectedId) { el.innerHTML='<div class="content muted">Select a session.</div>'; return; }
      Promise.all([
        apiGet('/api/evolves/'+state.selectedId).then(r=>r.json()),
        apiGet('/api/evaluator/'+state.selectedId).then(r=>r.json()),
        apiGet('/api/eval-results/'+state.selectedId).then(r=>r.json()),
      ]).then(([evolves, evaluator, evalResults]) => {
        if(reqId !== state.evolveReq || state.activeTab!=='evolve') return;
        // Skip re-render if nothing changed
        const lastEval = evalResults.length ? evalResults[evalResults.length-1] : null;
        const fp = JSON.stringify([evolves.length, evalResults.length, lastEval?.id, evolves[evolves.length-1]?.score]);
        if(fp === _evolveFingerprint) return;
        _evolveFingerprint = fp;

        if(!window._evolvePromptCache) window._evolvePromptCache={};

        // Match eval results to evolve iterations
        const evalByIter = {};
        evalResults.forEach(er => { if(er.iteration!==null) evalByIter[er.iteration]=er; });

        let html='<div style="padding:6px 12px">';

        // Results summary grid
        if(evalResults.length) {
          const metricKeys = new Set();
          evalResults.forEach(er => { if(er.metrics && typeof er.metrics==='object') Object.keys(er.metrics).forEach(k => metricKeys.add(k)); });
          const keys = [...metricKeys];
          html+=`<div style="margin-bottom:12px"><b>Results (${evalResults.length} evals)</b>
            <table class="train-table" style="margin-top:6px"><thead><tr><th>#</th>${keys.map(k=>`<th>${esc(k)}</th>`).join('')}<th>Time</th></tr></thead><tbody>`;
          evalResults.forEach(er => {
            const tag = er.iteration!==null&&er.iteration!==undefined ? er.iteration : '-';
            const m = (er.metrics && typeof er.metrics==='object') ? er.metrics : {};
            html+=`<tr><td>${tag}</td>${keys.map(k=>`<td>${m[k]!==undefined?Number(m[k]).toFixed(4):'-'}</td>`).join('')}<td>${er.duration_s?Number(er.duration_s).toFixed(1)+'s':'-'}</td></tr>`;
          });
          html+='</tbody></table></div>';
        }

        if(!evolves.length) {
          html+='<div class="muted">No evolve runs yet. Use <code>!evolve N</code> in procy.</div></div>';
          el.innerHTML=html;
          return;
        }

        // Chat history style: prompt left, response right
        evolves.forEach(ev => {
          const ts = fmtTs(ev.timestamp);
          const scoreHtml = ev.score!==null&&ev.score!==undefined
            ? `<span class="pill ok">${Number(ev.score).toFixed(4)}</span>` : '';
          const er = evalByIter[ev.iteration];
          const metricsStr = er && er.metrics && typeof er.metrics==='object'
            ? Object.entries(er.metrics).map(([k,v])=>`${k}=${typeof v==='number'?v.toFixed(4):v}`).join(', ') : '';

          // Prompt (left, violet — clickable to correct)
          const promptText = cleanTraceText(ev.prompt||'');
          const pKey = `evolve:${ev.iteration}:${ev.id}`;
          window._evolvePromptCache[pKey] = promptText;
          html+=`<button class="edge procy-prompt" onclick="openEditByKey(${ev.iteration}, '${pKey}')">
            <div class="edge-head"><span>Evolve #${ev.iteration} prompt</span><span class="small muted">${scoreHtml} ${esc(ev.source)} ${ts}</span></div>
            <pre class="edge-text">${esc(excerpt(ev.prompt, 300))}</pre>
          </button>`;

          // Response (right, gold)
          const resp = cleanTraceText(ev.response_summary||'');
          if(resp) {
            html+=`<div class="edge agent-response">
              <div class="edge-head"><span>Response</span><span class="small muted">${metricsStr ? esc(metricsStr) : ''}</span></div>
              <pre class="edge-text">${esc(excerpt(resp, 400))}</pre>
            </div>`;
          }

          // Eval metrics (compact, below response)
          if(metricsStr) {
            html+=`<div style="margin:2px 0 8px 72px;font-size:11px;color:var(--ok)">${esc(metricsStr)}${er.duration_s ? ' ('+Number(er.duration_s).toFixed(1)+'s)' : ''}</div>`;
          }
        });
        html+='</div>';
        el.innerHTML=html;
      });
    }

    // ── Corrections tab ──
    function renderCorrections() {
      const savedScrollY = window.scrollY;
      const el=document.getElementById('details');
      const qs = (!state.showAllCorrections && state.selectedId)
        ? `?session_id=${encodeURIComponent(state.selectedId)}`
        : '';
      apiGet('/api/corrections'+qs).then(r=>r.json()).then(all => {
        const visible = state.showAllCorrections || !state.selectedId
          ? all
          : all.filter(c => c.session_id === state.selectedId);
        const scopeLabel = state.showAllCorrections || !state.selectedId ? 'all sessions' : `session ${state.selectedId.slice(0,8)}`;
        let html=`<div class="content"><div class="row"><b>Corrections (${visible.length})</b><div style="display:flex;gap:8px"><button onclick="toggleCorrectionScope()">${state.showAllCorrections?'Show Current Session':'Show All'}</button><button class="primary" onclick="toggleAddForm()">+ Add</button><button onclick="exportTraining()">Export JSONL</button></div></div><div class="small muted" style="margin-top:4px">${scopeLabel}</div>`;
        html+=`<div id="add-form" style="display:none;margin-top:10px;padding:10px;border:1px solid var(--border);border-radius:8px;background:#f9fbf8">
          <div style="margin-bottom:6px"><label>Original Prompt</label><textarea id="new-original" rows="2" style="min-height:60px"></textarea></div>
          <div style="margin-bottom:6px"><label>Corrected Prompt</label><textarea id="new-corrected" rows="2" style="min-height:60px"></textarea></div>
          <div style="margin-bottom:6px"><label>Note</label><input type="text" id="new-note" placeholder="why?" /></div>
          <div class="row"><span></span><div style="display:flex;gap:6px"><button class="primary" onclick="saveNewCorrection()">Save</button><button onclick="toggleAddForm()">Cancel</button></div></div>
        </div>`;
        if(visible.length===0) { html+='<div class="muted" style="margin-top:12px">No corrections yet. Use <code>!correct</code> in procy or click a prompt above.</div>'; }
        else {
          html+='<table class="train-table" style="margin-top:10px"><thead><tr><th>Turn</th><th>Original</th><th>Corrected</th><th>Note</th><th></th></tr></thead><tbody>';
          visible.forEach(c => {
            html+=`<tr><td>t${c.turn_num}</td><td><div class="train-cell">${esc(excerpt(c.original_prompt,200))}</div></td><td><div class="train-cell">${esc(excerpt(c.corrected_prompt,200))}</div></td><td class="small">${esc(c.note||'')}</td><td><button class="danger" onclick="deleteCorrection(${c.id})" style="font-size:11px;padding:4px 8px">Del</button></td></tr>`;
          });
          html+='</tbody></table>';
        }
        html+='</div>';
        el.innerHTML=html;
        requestAnimationFrame(() => window.scrollTo(0, savedScrollY));
      });
    }

    function toggleCorrectionScope() {
      state.showAllCorrections = !state.showAllCorrections;
      renderCorrections();
    }

    function toggleAddForm() { const f=document.getElementById('add-form'); if(f) f.style.display=f.style.display==='none'?'block':'none'; }

    async function saveNewCorrection() {
      const o=document.getElementById('new-original').value.trim();
      const c=document.getElementById('new-corrected').value.trim();
      const n=document.getElementById('new-note').value.trim();
      if(!o||!c) return alert('Both fields required');
      if(!state.selectedId) return alert('Select a session first');
      const res = await fetch('/api/corrections',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:state.selectedId,turn_num:0,original_prompt:o,corrected_prompt:c,note:n||null})});
      if(!res.ok){
        const err = await res.json().catch(()=>({error:'failed to save'}));
        alert(err.error || 'failed to save');
        return;
      }
      renderCorrections();
    }

    async function deleteCorrection(id) { if(!confirm('Delete?')) return; await fetch('/api/corrections/'+id,{method:'DELETE'}); renderCorrections(); }

    // ── Training tab ──
    function renderTraining() {
      const savedScrollY = window.scrollY;
      const el=document.getElementById('details');
      Promise.all([
        apiGet('/api/training/all').then(r=>r.json()),
        apiGet('/api/training/status').then(r=>r.json()),
      ]).then(([items, trainStatus]) => {
        const byCategory = {human:[], corrected:[], proxy:[]};
        items.forEach(d => { if(byCategory[d.category]) byCategory[d.category].push(d); });
        const hc=byCategory.human.length, cc=byCategory.corrected.length, pc=byCategory.proxy.length;
        const isRunning = trainStatus.state === 'running';
        const stateColors = {idle:'var(--muted)', running:'#c47a20', done:'var(--ok)', failed:'var(--bad)'};
        const stateColor = stateColors[trainStatus.state] || 'var(--muted)';
        let html=`<div class="content">
          <div class="row"><b>Training Data (${items.length})</b>
            <div style="display:flex;gap:6px">
              <button onclick="exportTraining('sft')">Export SFT</button>
              <button onclick="exportTraining('dpo')">Export DPO</button>
              <button onclick="exportTraining('all')">Export All</button>
            </div>
          </div>
          <div class="summary-grid" style="margin-top:8px;grid-template-columns:repeat(3,1fr)">
            <div class="metric" style="border-left:3px solid var(--ok)"><b>Human</b><br/>${hc}<div class="small muted">SFT target</div></div>
            <div class="metric" style="border-left:3px solid #c47a20"><b>Corrected</b><br/>${cc}<div class="small muted">SFT + DPO</div></div>
            <div class="metric" style="border-left:3px solid #7e5fa0"><b>Proxy</b><br/>${pc}<div class="small muted">accepted</div></div>
          </div>

          <div style="margin-top:14px;padding:12px;border:1px solid var(--border);border-radius:8px;background:#f9fbf8">
            <div class="row" style="margin-bottom:8px">
              <div><b>Train Proxy Model</b> <span class="small" style="color:${stateColor};font-weight:600">${esc(trainStatus.state)}</span></div>
              <div style="display:flex;gap:6px">
                <button class="primary" onclick="startTraining()" ${isRunning?'disabled':''}>
                  ${isRunning ? 'Training...' : 'Start Training'}
                </button>
                ${isRunning ? '<button onclick="renderTraining()">Refresh</button>' : ''}
              </div>
            </div>
            <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:8px">
              <div><label>Model</label><input type="text" id="train-model" value="Qwen/Qwen3.5-27B" style="font-size:11px"/></div>
              <div><label>Epochs</label><input type="text" id="train-epochs" value="3" /></div>
              <div><label>Learning Rate</label><input type="text" id="train-lr" value="0.0002" /></div>
            </div>
            ${trainStatus.log ? `<details ${isRunning?'open':''}><summary class="small" style="cursor:pointer;color:var(--muted)">Training log</summary><pre class="mono" style="background:#f6f8f6;color:var(--fg);border:1px solid var(--border);border-radius:6px;padding:8px;margin-top:6px;font-size:11px;max-height:300px;overflow-y:auto;white-space:pre-wrap">${esc(trainStatus.log)}</pre></details>` : ''}
          </div>`;

        if(!items.length) {
          html+='<div class="muted" style="margin-top:12px">No training data yet. Human prompts, corrections, and evolve runs all contribute.</div>';
        } else {
          html+='<table class="train-table" style="margin-top:10px"><thead><tr><th>Category</th><th>Prompt</th><th>Original (if corrected)</th><th>Agent Response</th><th>Score</th></tr></thead><tbody>';
          items.forEach(d => {
            const catColors = {human:'var(--ok)', corrected:'#c47a20', proxy:'#7e5fa0'};
            const catColor = catColors[d.category]||'var(--muted)';
            const score = d.score!==null&&d.score!==undefined ? Number(d.score).toFixed(4) : '-';
            html+=`<tr>
              <td><span style="color:${catColor};font-weight:600">${esc(d.category)}</span></td>
              <td><div class="train-cell">${esc(excerpt(d.prompt,300))}</div></td>
              <td><div class="train-cell">${d.original_prompt ? esc(excerpt(d.original_prompt,200)) : '<span class="muted">-</span>'}</div></td>
              <td><div class="train-cell">${esc(excerpt(d.agent_response||'',200))}</div></td>
              <td>${score}</td>
            </tr>`;
          });
          html+='</tbody></table>';
        }
        html+='</div>';
        el.innerHTML=html;
        requestAnimationFrame(() => window.scrollTo(0, savedScrollY));
      });
    }

    async function startTraining() {
      const model = document.getElementById('train-model')?.value || 'Qwen/Qwen3.5-27B';
      const epochs = parseInt(document.getElementById('train-epochs')?.value || '3');
      const lr = parseFloat(document.getElementById('train-lr')?.value || '0.0002');
      try {
        const r = await fetch('/api/training/start', {
          method:'POST',
          headers:{'Content-Type':'application/json'},
          body: JSON.stringify({model, epochs, lr}),
        });
        const data = await r.json();
        if(!r.ok) { alert(data.error || 'Failed to start'); return; }
        // Poll for updates
        const poll = setInterval(async () => {
          const sr = await apiGet('/api/training/status');
          const st = await sr.json();
          if(st.state !== 'running') { clearInterval(poll); renderTraining(); return; }
          renderTraining();
        }, 3000);
        renderTraining();
      } catch(e) { alert('Error: '+e); }
    }

    async function exportTraining(fmt) {
      fmt = fmt || 'all';
      const r=await fetch('/api/training/export?format='+fmt); const t=await r.text();
      if(!t.trim()) return alert('No data for format: '+fmt);
      const a=document.createElement('a'); a.href=URL.createObjectURL(new Blob([t],{type:'application/jsonl'})); a.download='procy_train_'+fmt+'.jsonl'; a.click();
    }

    // ── Slide-left: Edit/Correct ──
    function openEditByKey(turnNum, promptKey) {
      const currentText = (window._promptCache && window._promptCache[promptKey])
        || (window._evolvePromptCache && window._evolvePromptCache[promptKey])
        || '';
      openEdit(turnNum, currentText);
    }

    function openEdit(turnNum, currentText) {
      state.editTarget={session_id:state.selectedId, turn_num:turnNum};
      document.getElementById('edit-title').textContent='Edit Prompt (t'+turnNum+')';
      document.getElementById('edit-meta').textContent='session='+((state.selectedId||'').slice(0,8))+' turn='+turnNum;
      document.getElementById('edge-current').textContent=currentText;
      document.getElementById('edge-edited').value=currentText;
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
      const original=cleanTraceText(document.getElementById('edge-current').textContent);
      const st=document.getElementById('edit-status');
      const btn=document.getElementById('btn-save-edit');
      btn.disabled=true; st.textContent='Saving...';
      try {
        const res = await fetch('/api/corrections',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:state.editTarget.session_id,turn_num:state.editTarget.turn_num,original_prompt:original,corrected_prompt:edited,note:note||null})});
        if(!res.ok){
          const err = await res.json().catch(()=>({error:'failed to save'}));
          throw new Error(err.error || `HTTP ${res.status}`);
        }
        st.textContent='Saved!'; st.style.color='var(--ok)';
        setTimeout(()=>{closeEdit();refresh();},800);
      } catch(e) { st.textContent='Error: '+e; st.style.color='var(--bad)'; }
      finally { btn.disabled=false; }
    }

    // ── Slide-right: Response detail ──
    async function openSlide(turnNum, responseKey) {
      const key = (responseKey===undefined || responseKey===null) ? String(turnNum) : String(responseKey);
      const full=(window._responseCache&&window._responseCache[key])||(window._responseCache&&window._responseCache[String(turnNum)])||'';
      document.getElementById('slide-title').textContent='Agent Response (t'+turnNum+')';
      document.getElementById('slide-meta').textContent='turn='+turnNum;
      let html='<h3 style="margin:0 0 6px">Terminal Replay (Exact PTY)</h3>';
      html+='<div class="small muted" style="margin-bottom:6px">Replayed from session start up to this turn. Includes terminal resize events.</div>';
      html+='<div id="slide-mode" class="small" style="margin-bottom:6px;color:#236c42">Mode: xterm replay</div>';
      html+='<div style="margin:0 0 8px 0;display:flex;gap:8px"><button onclick="slideTerminalScroll(\'top\')">Top</button><button onclick="slideTerminalScroll(\'up\')">PgUp</button><button onclick="slideTerminalScroll(\'down\')">PgDn</button><button onclick="slideTerminalScroll(\'bottom\')">Bottom</button></div>';
      html+='<div id="slide-terminal" style="height:calc(100vh - 140px);border:1px solid var(--border);border-radius:6px;overflow:hidden;background:#fbfdfb"></div>';
      html+='<div style="margin-top:8px"><button onclick="toggleSlideFallback()">Toggle Text Fallback</button></div>';
      html+='<div id="slide-fallback" style="display:none;margin-top:8px"><h3 style="margin:0 0 6px">Text Fallback</h3><pre class="mono" style="background:#f6f8f6;border:1px solid var(--border);border-radius:6px;padding:10px;overflow-x:auto;white-space:pre-wrap;font-size:11px;max-height:30vh;overflow-y:auto">'+esc(full)+'</pre></div>';
      document.getElementById('slide-content').innerHTML=html;
      document.getElementById('slide-panel').classList.add('open');
      const xtermOk = await ensureXtermRuntime();
      if(!xtermOk || !window.Terminal || !state.selectedId) {
        const mode=document.getElementById('slide-mode'); if(mode) { mode.textContent='Mode: text fallback (xterm unavailable)'; mode.style.color='#b72136'; }
        return;
      }
      if(state.slideTerm) {
        try { state.slideTerm.dispose(); } catch(e) {}
        state.slideTerm = null;
      }
      state.slideTermFit = null;
      const host=document.getElementById('slide-terminal');
      if(!host) return;
      state.slideTerm = new window.Terminal({
        convertEol: false,
        cursorBlink: false,
        scrollback: 100000,
        fontSize: 12,
        theme: { background: '#fbfdfb', foreground: '#1f2a1f', cursor: '#2f6f4f' }
      });
      if (window.FitAddon && window.FitAddon.FitAddon) {
        state.slideTermFit = new window.FitAddon.FitAddon();
        state.slideTerm.loadAddon(state.slideTermFit);
      }
      state.slideTerm.open(host);
      attachTerminalWheelGuard(host);
      if (state.slideTermFit) {
        try { state.slideTermFit.fit(); } catch(e) {}
        const ro = new ResizeObserver(() => {
          try { if(state.slideTermFit) state.slideTermFit.fit(); } catch(e) {}
        });
        ro.observe(host);
      }
      state.slideTerm.focus();
      state.slideTerm.write('\x1b[2J\x1b[H');
      let afterId = 0;
      let wrote = false;
      let done = false;
      for(let i=0;i<40 && !done;i++){
        const r = await apiGet(`/api/terminal/${encodeURIComponent(state.selectedId)}?after_id=${afterId}&limit=5000`);
        if(!r.ok) break;
        const data = await r.json();
        const events = data.events || [];
        if(!events.length) break;
        for(const evt of events){
          afterId = evt.id;
          if (Number(evt.turn_num || 0) > Number(turnNum || 0)) {
            done = true;
            break;
          }
          wrote = applyTerminalEvent(state.slideTerm, evt) || wrote;
        }
        if(events.length < 5000) break;
      }
      if(!wrote){
        const mode=document.getElementById('slide-mode'); if(mode) { mode.textContent='Mode: text fallback (no terminal bytes for this turn)'; mode.style.color='#b72136'; }
      }
    }

    function slideTerminalScroll(which) {
      const t = state.slideTerm;
      if(!t) return;
      if(which==='top') t.scrollToTop();
      else if(which==='bottom') t.scrollToBottom();
      else if(which==='up') t.scrollLines(-Math.max(10, Math.floor((t.rows||24)*0.8)));
      else if(which==='down') t.scrollLines(Math.max(10, Math.floor((t.rows||24)*0.8)));
    }

    function toggleSlideFallback() {
      const fb=document.getElementById('slide-fallback');
      if(!fb) return;
      fb.style.display = fb.style.display === 'none' ? 'block' : 'none';
    }

    function closeSlidePanel() {
      document.getElementById('slide-panel').classList.remove('open');
      if(state.slideTerm) {
        try { state.slideTerm.dispose(); } catch(e) {}
        state.slideTerm = null;
      }
      state.slideTermFit = null;
    }

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
