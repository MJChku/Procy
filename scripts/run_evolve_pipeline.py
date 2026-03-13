#!/usr/bin/env python3
"""End-to-end evolve pipeline: generate prompts, apply code changes, eval.

Simulates the procy evolve workflow for ANN search optimization:
1. Start with a baseline ann_search.py
2. Ask Qwen to suggest an improved version (via prompt)
3. Apply the code change
4. Run eval_ann.py on EXP07
5. Record results in procy DB
6. Feed results back for next iteration
7. Export training data

Usage:
    python3 scripts/run_evolve_pipeline.py --iterations 5
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent
PROCY_DIR = SCRIPTS_DIR.parent
sys.path.insert(0, str(PROCY_DIR))

from procy.store import TraceStore

QWEN_URL = "http://127.0.0.1:18000"
EXP07_PYTHON = "/home/jma/procy_env/bin/python3"
REMOTE_WORK_DIR = "/tmp/procy_ann_bench"


def call_qwen(prompt: str, temperature: float = 0.7) -> str:
    """Call Qwen 32B via local tunnel."""
    payload = json.dumps({
        "model": "Qwen/Qwen2.5-32B-Instruct",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 2000,
        "temperature": temperature,
    }).encode()
    req = urllib.request.Request(
        f"{QWEN_URL}/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"].strip()


def extract_python_code(text: str) -> str | None:
    """Extract python code block from LLM response."""
    # Try ```python ... ``` blocks
    m = re.search(r"```python\s*\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    # Try ``` ... ``` blocks
    m = re.search(r"```\s*\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return None


def run_eval_on_exp07(ann_code: str, eval_code: str) -> dict:
    """Upload code to EXP07 and run the benchmark."""
    # Create remote dir
    subprocess.run(["ssh", "EXP07", f"mkdir -p {REMOTE_WORK_DIR}"], check=True)

    # Upload files
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(ann_code)
        ann_tmp = f.name
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(eval_code)
        eval_tmp = f.name

    try:
        subprocess.run(["scp", ann_tmp, f"EXP07:{REMOTE_WORK_DIR}/ann_search.py"], check=True, capture_output=True)
        subprocess.run(["scp", eval_tmp, f"EXP07:{REMOTE_WORK_DIR}/eval_ann.py"], check=True, capture_output=True)

        # Run benchmark
        result = subprocess.run(
            ["ssh", "EXP07", f"cd {REMOTE_WORK_DIR} && {EXP07_PYTHON} eval_ann.py"],
            capture_output=True, text=True, timeout=120,
        )

        if result.returncode != 0:
            return {"error": result.stderr.strip(), "recall_at_10": 0.0}

        try:
            return json.loads(result.stdout.strip())
        except json.JSONDecodeError:
            return {"error": f"Bad output: {result.stdout[:200]}", "recall_at_10": 0.0}
    finally:
        os.unlink(ann_tmp)
        os.unlink(eval_tmp)


def build_evolve_prompt(base_prompt: str, history: list[dict]) -> str:
    """Build prompt for Qwen to generate improved ann_search.py."""
    parts = [
        "You are optimizing an ANN (Approximate Nearest Neighbor) search implementation.",
        "The code uses hnswlib. Your goal: maximize recall@10 while keeping search reasonably fast.",
        "",
        f"Task: {base_prompt}",
        "",
        "Write a complete, working ann_search.py that defines:",
        "  - build_index(base_vectors) -> index object",
        "  - search_index(index, queries, k) -> numpy array of shape (n_queries, k)",
        "",
        "Only use hnswlib and numpy. The code must be self-contained.",
        "Output ONLY a ```python``` code block, nothing else.",
    ]

    if history:
        parts.append("")
        parts.append("Previous attempts and their results:")
        for h in history[-8:]:
            tag = f"#{h['iteration']}"
            metrics = h.get("metrics", {})
            recall = metrics.get("recall_at_10", "?")
            qps = metrics.get("qps", "?")
            parts.append(f"  [{tag}] recall@10={recall}, qps={qps}")
            # Show the key parameters used
            code_summary = h.get("code_summary", "")
            if code_summary:
                parts.append(f"       params: {code_summary}")

        best = max(history, key=lambda h: h.get("metrics", {}).get("recall_at_10", 0))
        parts.append(f"")
        parts.append(f"  Best so far: #{best['iteration']} with recall@10={best['metrics'].get('recall_at_10', '?')}")
        parts.append(f"  Improve on this. Try different M, ef_construction, ef values, or data preprocessing.")

    return "\n".join(parts)


def extract_hnsw_params(code: str) -> str:
    """Extract key HNSW params from code for summary."""
    params = []
    m = re.search(r"ef_construction\s*=\s*(\d+)", code)
    if m: params.append(f"ef_c={m.group(1)}")
    m = re.search(r"M\s*=\s*(\d+)", code)
    if m: params.append(f"M={m.group(1)}")
    m = re.search(r"set_ef\s*\(\s*(\d+)", code)
    if m: params.append(f"ef={m.group(1)}")
    m = re.search(r"space\s*=\s*['\"](\w+)['\"]", code)
    if m: params.append(f"space={m.group(1)}")
    return ", ".join(params) if params else "unknown"


def run_pipeline(n_iterations: int, db_path: str):
    store = TraceStore(db_path)
    session_id = store.new_session(goal="ANN recall@10 optimization via evolve")

    eval_code = (SCRIPTS_DIR / "eval_ann.py").read_text()
    baseline_code = (SCRIPTS_DIR / "ann_search.py").read_text()
    base_prompt = "Improve the HNSW parameters in ann_search.py to maximize recall@10"

    history = []

    # Run baseline first
    print(f"[#0] Running baseline...")
    metrics = run_eval_on_exp07(baseline_code, eval_code)
    print(f"[#0] baseline: {json.dumps(metrics)}")

    store.log_turn(session_id, 0, "human", base_prompt)
    store.log_evolve(session_id, 0, base_prompt,
                     f"baseline: {json.dumps(metrics)}",
                     metrics, metrics.get("recall_at_10"), "human")

    history.append({
        "iteration": 0,
        "prompt": base_prompt,
        "metrics": metrics,
        "code_summary": extract_hnsw_params(baseline_code),
    })

    current_code = baseline_code

    for i in range(1, n_iterations + 1):
        print(f"\n[#{i}] Generating improved version...")

        evolve_prompt = build_evolve_prompt(base_prompt, history)

        # Log the human/procy turn
        store.log_turn(session_id, i, "procy", evolve_prompt[:500])

        try:
            response = call_qwen(evolve_prompt, temperature=0.5 + 0.1 * (i % 3))
        except Exception as e:
            print(f"[#{i}] Qwen error: {e}")
            break

        new_code = extract_python_code(response)
        if not new_code:
            print(f"[#{i}] Failed to extract code from response")
            store.log_evolve(session_id, i, evolve_prompt[:500],
                           "failed to extract code", None, None, "procy")
            continue

        # Validate it has required functions
        if "def build_index" not in new_code or "def search_index" not in new_code:
            print(f"[#{i}] Missing required functions")
            store.log_evolve(session_id, i, evolve_prompt[:500],
                           "missing build_index/search_index", None, None, "procy")
            continue

        params = extract_hnsw_params(new_code)
        print(f"[#{i}] params: {params}")

        # Run eval
        print(f"[#{i}] Running benchmark...")
        metrics = run_eval_on_exp07(new_code, eval_code)
        recall = metrics.get("recall_at_10", 0)
        print(f"[#{i}] recall@10={recall}, qps={metrics.get('qps', '?')}")

        # Store agent response (the code) and evolve result
        response_summary = f"params: {params}\n{json.dumps(metrics)}"
        evolve_id = store.log_evolve(session_id, i, evolve_prompt[:500],
                                     response_summary, metrics, recall, "procy")
        store.log_turn(session_id, i, "agent_chunk", response_summary)

        history.append({
            "iteration": i,
            "prompt": evolve_prompt[:500],
            "metrics": metrics,
            "code_summary": params,
            "code": new_code,
        })

        # If this is the best, save it
        best = max(history, key=lambda h: h.get("metrics", {}).get("recall_at_10", 0))
        if best["iteration"] == i:
            print(f"[#{i}] *** NEW BEST: recall@10={recall} ***")
            current_code = new_code

        time.sleep(1)  # be nice to Qwen

    store.end_session(session_id)

    # Print summary
    print("\n" + "=" * 60)
    print("EVOLVE SUMMARY")
    print("=" * 60)
    for h in history:
        tag = f"#{h['iteration']}"
        recall = h["metrics"].get("recall_at_10", "?")
        qps = h["metrics"].get("qps", "?")
        params = h.get("code_summary", "")
        marker = " <<<" if h == max(history, key=lambda h: h.get("metrics", {}).get("recall_at_10", 0)) else ""
        print(f"  {tag:>4}  recall@10={recall:<8}  qps={qps:<10}  {params}{marker}")

    # Export training data
    print(f"\nSession: {session_id}")
    print(f"DB: {db_path}")

    # Generate training pairs: for each iteration, the "good" prompt is the one
    # that led to better recall than the previous best
    training_pairs = []
    best_recall = 0
    for h in history:
        recall = h["metrics"].get("recall_at_10", 0)
        if recall > best_recall:
            training_pairs.append({
                "instruction": base_prompt,
                "input": json.dumps({
                    "previous_best_recall": best_recall,
                    "history": [
                        {"tag": f"#{ph['iteration']}",
                         "recall": ph["metrics"].get("recall_at_10", 0),
                         "params": ph.get("code_summary", "")}
                        for ph in history[:h["iteration"]]
                    ]
                }),
                "output": h.get("code", h.get("code_summary", "")),
                "recall": recall,
            })
            best_recall = recall

    train_path = PROCY_DIR / "procy_ann_train.jsonl"
    with open(train_path, "w") as f:
        for pair in training_pairs:
            f.write(json.dumps(pair) + "\n")
    print(f"Training pairs: {len(training_pairs)} -> {train_path}")

    # Also save the best code
    if history:
        best = max(history, key=lambda h: h.get("metrics", {}).get("recall_at_10", 0))
        if "code" in best:
            best_path = SCRIPTS_DIR / "ann_search_best.py"
            best_path.write_text(best["code"])
            print(f"Best code saved to: {best_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", "-n", type=int, default=5)
    parser.add_argument("--db", default=str(Path.home() / ".procy" / "traces.db"))
    args = parser.parse_args()

    run_pipeline(args.iterations, args.db)
