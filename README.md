# ProCy

Transparent prompt proxy between human and AI coding agents. ProCy sits between you and your AI agent (e.g. Claude Code), recording every interaction and learning to generate better prompts over time.

## What it does

- **Wraps** any CLI agent (Claude Code, etc.) in a PTY proxy — zero changes to the agent
- **Records** every prompt, response, and tool call to a local SQLite database
- **Evolves** prompts: a local proxy model (Qwen 14B + LoRA) generates improved prompts based on past results
- **Learns** from human corrections: when you fix a prompt, that becomes training data
- **Trains** the proxy model from the UI — one click to fine-tune on your corrections
- **Replays** terminal sessions via xterm.js in the web UI

## Install

```bash
pip install -e .
```

Requires Python 3.10+. The only runtime dependency is Flask (for the monitor UI).

## Usage

```bash
# Basic: wrap Claude Code
procy

# With a specific agent command
procy --agent "claude --dangerously-skip-permissions"

# Skip the SSH tunnel to GPU server (if not using proxy model)
procy --no-tunnel

# Custom Qwen API URL (if proxy model is hosted elsewhere)
procy --qwen-url http://localhost:18000
```

## ProCy Commands

Inside a procy session, type `!` to enter command mode:

| Command | Description |
|---------|-------------|
| `!evolve N` | Run N iterations of prompt evolution using the proxy model |
| `!status` | Show current session status, evolve progress |
| `!correct` | Correct the last prompt (opens editor) |
| `!help` | Show available commands |

## Web Monitor UI

The monitor UI starts automatically on port 7862 when you launch procy.

Open `http://localhost:7862` to see:

- **Interactions** — Human prompts (green), agent responses (gold), evolve prompts (violet). Click any prompt to correct it.
- **Terminal** — Full PTY replay of the session via xterm.js
- **Evolve** — Tagged tries (#1, #2, ...) with scores, prompts, and responses
- **Corrections** — All human corrections, with add/delete/export
- **Training** — Three categories of training data (human/corrected/proxy), export as SFT or DPO, and a "Start Training" button to fine-tune the proxy model on a remote GPU server

## Training the Proxy Model

ProCy collects three categories of training data:

1. **Human** — Pure human-written prompts (gold standard for SFT)
2. **Corrected** — Proxy prompts that a human fixed (SFT + DPO pairs)
3. **Proxy** — Proxy-generated prompts the human accepted (implicit approval)

To train, either:
- Click **Start Training** in the Training tab (requires a GPU server with Docker + vLLM image)
- Or export JSONL and train manually:

```bash
# On GPU server (inside vllm Docker container):
pip install peft trl datasets accelerate
python3 scripts/train_proxy.py \
    --data train.jsonl \
    --model Qwen/Qwen2.5-14B-Instruct \
    --output /data/proxy_lora \
    --epochs 3
```

The training script supports multi-GPU (4x V100) with fp16 and gradient checkpointing.

## Architecture

```
Human  <-->  ProCy (PTY proxy)  <-->  AI Agent (Claude Code)
               |                           |
               |  records to SQLite        |  raw terminal I/O
               |  generates prompts        |
               v                           v
          Proxy Model              Agent does the work
        (Qwen 14B + LoRA)
               |
               v
          Monitor UI (Flask + xterm.js)
```

## Project Structure

```
procy/
  cli.py        — Main entry point, Procy class, evolve loop
  store.py      — SQLite trace store (sessions, turns, corrections, evolves)
  terminal.py   — PTY proxy session management
  ui.py         — Flask web UI with xterm.js terminal replay
  io.py         — Thread-safe terminal I/O
  agent.py      — Agent process management
  assets/       — xterm.js CSS/JS for terminal replay
scripts/
  train_proxy.py         — LoRA fine-tuning script (multi-GPU)
  run_evolve_pipeline.py — End-to-end evolve pipeline example
  eval_ann.py            — ANN search benchmark evaluator
  ann_search.py          — Baseline ANN implementation
```

## License

MIT
