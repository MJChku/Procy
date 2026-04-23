"""Schism installer — manages Claude Code integration on disk."""
from __future__ import annotations

import json
import os
import shutil
import stat
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .store import SchismStore

CLAUDE_HOME = Path.home() / ".claude"
SCHISM_HOME = Path.home() / ".schism"
CLAUDE_SETTINGS = CLAUDE_HOME / "settings.json"
CLAUDE_COMMANDS = CLAUDE_HOME / "commands"
SCHISM_TOOLS_DIR = SCHISM_HOME / "tools"
SCHISM_BACKUPS_DIR = SCHISM_HOME / "backups"

# The /schism slash command content
SCHISM_SLASH_COMMAND = """\
---
description: >
  Activate the Schism tool framework. Subcommands: list, search, get, tree,
  rollback, add, catchup. Works in any session via Bash; MCP tools are a bonus.
---

# Schism Tool Framework

## STEP 0 — Silent bootstrap (complete before saying anything to the user)

Run these Bash tool calls silently. Do NOT output any results or narration to
the user. Do NOT say "Schism isn't on PATH" or "let me find it" or anything
else. Just run the commands.

1. Check if schism is available:
   ```
   which schism 2>/dev/null || echo NOT_FOUND
   ```

2. If output was `NOT_FOUND`, find the source repo:
   ```
   find ~ -maxdepth 6 -name "pyproject.toml" -path "*/schism*" \
     -not -path "*/.venv/*" -not -path "*/site-packages/*" 2>/dev/null | head -3
   ```

3. If a path was found in step 2:
   - Extract the directory containing `pyproject.toml`
   - Run silently: `pip install -e <that_directory> -q && schism install`

4. If no path found in step 2:
   - Only now tell the user: "Schism source not found. Clone the repo and run:
     `pip install -e . && schism install`"
   - Then stop.

5. Verify schism is now working: `schism list 2>/dev/null | head -1`
   If this fails, tell the user installation failed and show the error.

6. Start the UI in the background if not already running (silently):
   ```
   pgrep -f "schism ui" >/dev/null 2>&1 || (nohup schism ui >/tmp/schism-ui.log 2>&1 &)
   ```

Once step 0 is complete without error, proceed to STEP 1 with no mention of
bootstrap to the user. You may include "UI: http://127.0.0.1:7863" in the
tool catalog output as a one-liner.

---

## STEP 1 — Activate the framework

You are now operating with the Schism framework active. Use **two equivalent
modes** — prefer MCP if available, use Bash otherwise.

**Mode A — MCP** (if schism was installed before this session started):
Call `schism_list()`, `schism_progress()`, `schism_feedback()`, etc. directly.

**Mode B — Bash CLI** (always available, even installed mid-session):
Use `bash schism <subcommand>`. If any MCP call errors, switch to Bash.

### On bare `/schism` (no subcommand)

1. Load the tool catalog:
   - MCP: `schism_list()`
   - Bash: `bash schism list`
2. For relevant tools, get full details:
   - MCP: `schism_get("ToolName")`
   - Bash: `bash schism get ToolName`
3. Use available tools proactively in the task.
4. **YOU MUST report progress mid-task** (see mandatory rules below).
5. **YOU MUST submit feedback at task completion** (see mandatory rules below).

---

## MANDATORY REPORTING RULES (non-negotiable)

These are not optional. Violating them means Schism cannot learn.

### Rule 1 — Progress reporting (call immediately each time)

You MUST call `schism_progress` (MCP) or `bash schism progress` (Bash)
**immediately and without waiting** whenever ANY of these happen:

- You run a command that worked after trial and error
- You implement a workaround for a non-obvious obstacle
- You discover an approach that took multiple attempts
- You write a one-off script or snippet to solve a problem on the fly
- You figure out a non-obvious flag, option, or API behavior

Do NOT batch progress events. Report each one the moment it happens.

**MCP:**
```
schism_progress(
    problem="Yosys synthesis timed out on large modules",
    solution="Split the design into sub-modules and synthesized independently",
    commands_used=["yosys -p 'synth_nangate45' top.v", "bash split_modules.sh"],
    tool_name="",
    executive_session_id=""
)
```

**Bash:**
```bash
bash schism progress \
  --problem "Yosys synthesis timed out on large modules" \
  --solution "Split design into sub-modules" \
  --commands "yosys -p 'synth_nangate45' top.v" "bash split_modules.sh"
```

### Rule 2 — End-of-task feedback (once, at task completion)

You MUST call `schism_feedback` ONCE before giving the user your final response.
This is the last thing you do before finishing a task.

**MCP:**
```
schism_feedback(
    task="Optimized RTL area by 25% without timing regression",
    tools_used={"Yosys_simple_pipeline": "extracted PPA metrics"},
    tools_unhelpful={},
    challenges="Yosys was slow; split modules to work around it",
    executive_session_id=""
)
```

**Bash:**
```bash
bash schism feedback \
  --task "Optimized RTL area by 25%" \
  --tools-used '{"Yosys_simple_pipeline": "extracted PPA"}' \
  --challenges "Yosys was slow on large designs"
```

After submitting feedback, call `schism_factory_status()` or `bash schism status`
to confirm the factory started.

---

## `/schism catchup` — retroactive reporting for mid-session activation

Use this when Schism was just installed or activated after work has already begun.

**IMPORTANT:** Generate a single session ID first and reuse it for ALL events:
```bash
SESSION_ID=$(python3 -c "import time; print(f'catchup_{int(time.time())}')")
```

1. Review the current conversation from the beginning.
2. For **each problem you solved** or **non-trivial approach you used**, report it
   using the SAME session ID:
   - MCP: `schism_progress(problem="...", solution="...", commands_used=[...], executive_session_id=SESSION_ID)`
   - Bash: `bash schism progress --problem "..." --solution "..." --commands "..." --session $SESSION_ID`
3. For **any fully completed task**, also submit end-of-task feedback with the same ID:
   - MCP: `schism_feedback(task="...", tools_used={...}, challenges="...", executive_session_id=SESSION_ID)`
   - Bash: `bash schism feedback --task "..." --challenges "..." --session $SESSION_ID`
4. Be thorough — the Factory learns from each event and may generate new tools.

---

## Other subcommands

| Subcommand | MCP | Bash |
|---|---|---|
| List tools | `schism_list()` | `bash schism list` |
| Search | `schism_search("query")` | `bash schism search query` |
| Get details | `schism_get("Name")` | `bash schism get Name` |
| Evolution tree | `schism_tree("Name")` | `bash schism tree Name` |
| Rollback | `schism_rollback("Name", 2)` | `bash schism rollback Name 2` |
| Add new tool | `schism_add("description")` | `bash schism add description` |
| Process pending | `schism_factory_status()` | `bash schism factory` |
"""


class Installer:
    """Installs and manages Schism components in Claude Code's configuration."""

    def __init__(self, store: "SchismStore | None" = None, dry_run: bool = False):
        self.store = store
        self.dry_run = dry_run

    # ── One-time setup ────────────────────────────────────────────────────────

    def install_all(self, server_source_path: Path) -> None:
        """Full one-time install: dirs, MCP server, slash command, settings."""
        self._ensure_dirs()
        self._copy_server(server_source_path)
        self.install_schism_command()
        self.install_schism_mcp()
        print("Schism installed successfully.")
        print(f"  MCP server: {SCHISM_HOME / 'server.py'}")
        print(f"  Slash command: {CLAUDE_COMMANDS / 'schism.md'}")
        print("Start a Claude Code session and type /schism to activate.")

    def _ensure_dirs(self) -> None:
        if not self.dry_run:
            SCHISM_HOME.mkdir(parents=True, exist_ok=True)
            SCHISM_TOOLS_DIR.mkdir(parents=True, exist_ok=True)
            SCHISM_BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
            CLAUDE_COMMANDS.mkdir(parents=True, exist_ok=True)

    def _copy_server(self, server_source: Path) -> None:
        dest = SCHISM_HOME / "server.py"
        if not self.dry_run:
            shutil.copy2(server_source, dest)
            # Also copy store and factory so server.py can import them
            src_dir = server_source.parent
            for module in ("store.py", "factory.py", "agent.py", "io.py", "__init__.py"):
                src = src_dir / module
                if src.exists():
                    shutil.copy2(src, SCHISM_HOME / module)

    def install_schism_command(self) -> None:
        """Write ~/.claude/commands/schism.md."""
        dest = CLAUDE_COMMANDS / "schism.md"
        if not self.dry_run:
            CLAUDE_COMMANDS.mkdir(parents=True, exist_ok=True)
            dest.write_text(SCHISM_SLASH_COMMAND, encoding="utf-8")

    def install_schism_mcp(self) -> None:
        """Register the Schism MCP server in ~/.claude/settings.json."""
        server_path = str(SCHISM_HOME / "server.py")
        entry = {
            "command": "python3",
            "args": [server_path],
            "type": "stdio",
        }
        self._add_mcp_server("schism", entry)

    # ── Per-tool MCP installation ─────────────────────────────────────────────

    def install_mcp_tool(self, tool: dict, code: str) -> str:
        """Write tool server file and register in settings.json. Returns install_path."""
        tool_name = tool["name"]
        tool_dir = SCHISM_TOOLS_DIR / tool_name
        install_path = str(tool_dir / "server.py")

        if not self.dry_run:
            tool_dir.mkdir(parents=True, exist_ok=True)
            (tool_dir / "server.py").write_text(code, encoding="utf-8")

            entry = {
                "command": "python3",
                "args": [install_path],
                "type": "stdio",
            }
            backup = self._add_mcp_server(f"schism_tool_{tool_name}", entry)

            if self.store:
                self.store.record_install(
                    tool_id=tool["id"],
                    generation=tool["generation"],
                    install_path=install_path,
                    settings_backup_path=str(backup) if backup else None,
                )
        return install_path

    def uninstall_mcp_tool(self, tool: dict) -> bool:
        """Remove tool's MCP entry from settings.json."""
        tool_name = tool["name"]
        mcp_key = f"schism_tool_{tool_name}"

        if not self.dry_run:
            settings = self._load_settings()
            mcp_servers = settings.get("mcpServers", {})
            if mcp_key not in mcp_servers:
                return False
            backup = self._backup_settings()
            del mcp_servers[mcp_key]
            settings["mcpServers"] = mcp_servers
            self._save_settings(settings)

            if self.store:
                install = self.store.get_active_install(tool["id"])
                if install:
                    self.store.record_uninstall(install["id"])
        return True

    # ── Settings management ───────────────────────────────────────────────────

    def _add_mcp_server(self, key: str, entry: dict) -> Path | None:
        """Add an mcpServers entry to settings.json atomically."""
        settings = self._load_settings()
        if "mcpServers" not in settings:
            settings["mcpServers"] = {}

        if settings["mcpServers"].get(key) == entry:
            return None  # Already installed, no change needed

        backup = self._backup_settings()
        settings["mcpServers"][key] = entry
        if not self.dry_run:
            self._save_settings(settings)
        return backup

    def _backup_settings(self) -> Path:
        """Copy settings.json to ~/.schism/backups/settings_<timestamp>.json."""
        ts = int(time.time())
        backup_path = SCHISM_BACKUPS_DIR / f"settings_{ts}.json"
        if not self.dry_run:
            SCHISM_BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
            if CLAUDE_SETTINGS.exists():
                shutil.copy2(CLAUDE_SETTINGS, backup_path)
            else:
                backup_path.write_text("{}", encoding="utf-8")
        return backup_path

    def _load_settings(self) -> dict:
        if not CLAUDE_SETTINGS.exists():
            return {}
        try:
            return json.loads(CLAUDE_SETTINGS.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_settings(self, data: dict) -> None:
        """Atomic write: write to .tmp then rename."""
        tmp = CLAUDE_SETTINGS.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(CLAUDE_SETTINGS)

    def preview_install(self, tool: dict) -> dict:
        """Return what would be done without writing anything."""
        tool_name = tool["name"]
        install_path = str(SCHISM_TOOLS_DIR / tool_name / "server.py")
        mcp_key = f"schism_tool_{tool_name}"
        settings = self._load_settings()
        return {
            "install_path": install_path,
            "mcp_key": mcp_key,
            "settings_entry": {
                "command": "python3",
                "args": [install_path],
                "type": "stdio",
            },
            "already_installed": mcp_key in settings.get("mcpServers", {}),
            "settings_path": str(CLAUDE_SETTINGS),
        }
