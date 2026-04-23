# Schism

A Claude Code add-on that builds a self-improving tool ecosystem as you work.

Claude Code solves problems. Schism watches, learns, and turns those solutions into reusable tools — automatically. The more you use it, the better your toolkit gets.

---

## How it works

Every Claude Code conversation is an **Executive session**. As Claude works, it reports progress events (problems solved, commands discovered) and end-of-task feedback. A separate **Factory session** — another Claude instance — reads that feedback and generates or improves **MCP tools** that Claude can call in future sessions.

```
You  ──── task ────▶  Claude Code (Executive session)
                           │
                    solves sub-problems
                    reports progress ──▶  ~/.schism/catalog.db
                           │                     │
                    task complete         Factory session
                    reports feedback ────▶  (another Claude)
                                                 │
                                         generates / updates
                                         MCP tools ──▶  ~/.claude/settings.json
                                                 │
                                       next session: Claude has
                                       new tools to call
```

Tools are Python MCP servers with structured documentation:
- **Name** — specific and action-oriented
- **Capability** — one-line statement of what it does
- **Use cases** — when to reach for it
- **Patterns** — quick copy-paste usage examples
- **Requirements** — dependencies, API keys, VPN, etc.

---

## Install

**Requirements:** Python 3.10+, Claude Code CLI installed and authenticated.

```bash
git clone <this-repo>
cd schism
pip install -e .
schism install
```

`schism install` does three things:
1. Creates `~/.schism/` (tool catalog database, server files)
2. Writes `~/.claude/commands/schism.md` (the `/schism` slash command)
3. Registers the Schism MCP server in `~/.claude/settings.json`

Start a new Claude Code session after installing — the MCP tools become available at session start.

---

## Quickstart

```
# In any Claude Code session:
/schism
```

Claude will:
1. Load the tool catalog
2. Use relevant tools for your task
3. Report progress mid-task as it solves things
4. Submit a feedback summary when the task is done

The Factory processes the feedback in the background and evolves the catalog.

---

## Installing mid-session

You don't need to install Schism before starting a session. If it's already on PATH:

```bash
# In Claude Code:
!schism install
```

If it's cloned but not yet installed:

```bash
!cd /path/to/schism && pip install -e . && schism install
```

If you don't know where it's cloned:

```bash
!find ~ -name "pyproject.toml" -path "*/schism/*" 2>/dev/null
# Then: pip install -e <that path> && schism install
```

After installing mid-session:
- `/schism` slash command and `bash schism` CLI work **immediately**
- MCP tools (`schism_list`, `schism_progress`, etc.) become available at the **next** session start
- Until then, the slash command instructs Claude to use `bash schism <command>` as a fallback — everything works either way

---

## The `/schism` slash command

### Activate the framework
```
/schism
```
Loads the tool catalog, activates progress reporting, and sets up end-of-task feedback.

### Catch up on a session already in progress
```
/schism catchup
```
When Schism is activated mid-session, this tells Claude to review the conversation from the beginning, retroactively report every problem it solved and solution it found, and submit that to the Factory. Use this right after installing mid-session to capture work already done.

### Browse tools
```
/schism list
/schism search <query>
/schism get <ToolName>
```

### Inspect a tool's evolution
```
/schism tree <ToolName>
```
Shows every generation of a tool with the reason it was changed.

### Roll back a tool
```
/schism rollback <ToolName> <generation>
```

### Request a new tool manually
```
/schism add <description of what the tool should do>
```
Triggers the Factory immediately, without needing a task feedback cycle.

---

## CLI reference

Everything available in Claude Code via `/schism` is also available as a shell command — useful for scripting, testing, or sessions where the slash command isn't set up yet.

### Setup
```bash
schism install     # one-time setup
schism status      # catalog summary, pending factory runs
schism ui          # start web UI at http://127.0.0.1:7863
```

### Read the catalog
```bash
schism list
schism search "rtl analysis"
schism get Yosys_simple_pipeline
schism tree Yosys_simple_pipeline
```

### Record events (Claude calls these via Bash when MCP isn't available)
```bash
# Mid-task progress: call whenever a sub-problem is solved
schism progress \
  --problem "Yosys timed out on large modules" \
  --solution "Split the design into sub-modules, synthesized independently" \
  --commands "yosys -p 'synth_nangate45' top.v" "bash split_modules.sh" \
  --session my-session-id

# End-of-task feedback
schism feedback \
  --task "Optimized genetic sequencer RTL, reduced area by 25%" \
  --tools-used '{"Yosys_simple_pipeline": "extracted PPA metrics"}' \
  --challenges "Yosys was slow on this design size; used module splitting instead" \
  --session my-session-id
```

### Tool management
```bash
schism add "run Yosys synthesis and return area/power/timing as JSON"
schism rollback Yosys_simple_pipeline 2
schism factory     # process all pending feedback and progress events now
```

---

## How the Factory works

The Factory is a separate Claude instance (spawned via `claude -p <prompt> --output-format stream-json`). No API key needed — it uses the same Claude Code authentication you already have.

When feedback arrives, the Factory decides:

| Signal | Factory action |
|---|---|
| A tool was used and a new pattern was discovered | Add the pattern to that tool's documentation (new generation) |
| A tool was mentioned in the challenges as inadequate | Rewrite the tool implementation |
| A tool was marked unhelpful | Add an anti-pattern note to its use cases |
| The challenges describe a procedure Claude had to invent | Generate a new MCP tool for that procedure |

Each decision creates a new **generation** of a tool. Old generations are never deleted — you can always roll back.

---

## Progress reporting

The most valuable feedback comes from mid-task events, not just the final summary. Claude calls `schism_progress` (or `bash schism progress`) whenever it:

- Runs a command that worked after trial and error
- Implements a workaround for a non-obvious obstacle  
- Discovers an approach that took multiple attempts
- Writes a one-off script to solve something on the fly

**Example progress event:**
```
Problem:  Synthesis kept failing with "cannot find module" on parametrized designs
Solution: Pre-process the Verilog with yosys read_verilog -defer before synthesis
Commands: yosys -p "read_verilog -defer design.v; synth_nangate45" 
```

The Factory evaluates this immediately: if it's novel enough, it becomes a new tool or a pattern on an existing one. If it's too task-specific, it's skipped.

---

## Tool evolution example

Over three sessions working on RTL optimization, a tool might evolve like this:

```
Gen 1  [2026-04-18]  Initial creation
  Capability: Run Yosys synthesis and return PPA metrics as JSON
  Note:       Created from first session — basic synthesis pipeline

Gen 2  [2026-04-19]  Added pattern from session feedback
  Capability: Run Yosys synthesis and return PPA metrics as JSON
  Note:       Added -defer flag pattern after feedback that parametrized designs failed

Gen 3  [2026-04-21]  Rewritten after performance feedback
  Capability: Run Yosys synthesis (parallel sub-module mode) and return PPA as JSON
  Note:       Rewrote to support parallel synthesis after feedback that single-threaded
              was too slow for designs > 50k gates
```

View this with `/schism tree Yosys_simple_pipeline` or in the Evolution tab of the web UI.

---

## Web UI

```bash
schism ui
# Open http://127.0.0.1:7863
```

Four tabs:

**Tools** — Browse the catalog. Expand any tool to see its full code, patterns, and requirements. Install or uninstall individual MCP tools. Roll back to any previous generation.

**Evolution** — Select a tool to see its full generation timeline with dates and evolution notes. Useful for understanding why a tool changed over time.

**Sessions** — Each Executive session with its task summary. Expand to see all progress events (with commands used) and feedback submitted.

**Factory Activity** — Live feed of factory runs: what was processed, what actions were taken, how long it took.

---

## Worked example: RTL optimization

**Session 1:** No tools installed yet.

```
/schism
```

Claude loads an empty catalog and starts working on the task. Partway through:

```
schism_progress(
    problem="No tool to quickly extract gate count from Verilog",
    solution="Used yosys with synth -top and stat command",
    commands_used=["yosys -p 'read_verilog top.v; synth -top top; stat' 2>&1 | grep 'Number of cells'"]
)
```

Factory receives this, decides it's tool-worthy, generates `Yosys_Gate_Counter`. At task end:

```
schism_feedback(
    task="Optimized AES encryption module, reduced area by 18%",
    tools_used={},
    tools_unhelpful={},
    challenges="Had to figure out how to extract gate counts manually — took several attempts with yosys flags"
)
```

---

**Session 2:** `Yosys_Gate_Counter` is now in the catalog.

```
/schism
```

Claude sees the tool, uses it immediately when it needs gate counts. No trial and error. Halfway through:

```
schism_progress(
    problem="Yosys_Gate_Counter was slow on modules with deep hierarchy",
    solution="Added -flatten flag before stat command",
    commands_used=["yosys -p 'read_verilog top.v; synth -top top; flatten; stat'"],
    tool_name="Yosys_Gate_Counter"
)
```

Factory updates `Yosys_Gate_Counter` gen 1 → gen 2, adding the `-flatten` pattern.

---

**Session 3:** The improved tool is available. The cycle continues.

---

## Project structure

```
schism/
  __init__.py    — Package metadata
  server.py      — FastMCP server (the interface Claude Code connects to)
  store.py       — SQLite catalog (tools, generations, sessions, feedback, factory runs)
  factory.py     — Factory: processes feedback, generates tools via Claude CLI
  installer.py   — Manages ~/.claude/settings.json and ~/.claude/commands/
  agent.py       — Claude Code CLI subprocess wrapper (used by Factory)
  cli.py         — schism command-line interface
  ui.py          — Flask web UI (port 7863)
  io.py          — Thread-safe I/O utilities

~/.schism/
  catalog.db     — SQLite database (all tools, sessions, feedback)
  server.py      — Deployed copy of the MCP server
  tools/         — Generated MCP tool files
    <ToolName>/
      server.py  — The tool's MCP server

~/.claude/
  commands/
    schism.md    — The /schism slash command
  settings.json  — MCP server registrations (schism + each installed tool)
```

---

## Requirements

- Python 3.10+
- `mcp >= 1.0` (installed automatically)
- Claude Code CLI (`claude`) authenticated and working
- No API key needed — the Factory uses your existing Claude Code session
