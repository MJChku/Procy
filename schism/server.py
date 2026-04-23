"""Schism MCP server — exposes tool catalog and factory to Claude Code sessions."""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

# Allow running from ~/.schism/ where store/factory are copied alongside server.py
_here = Path(__file__).parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))

from mcp.server.fastmcp import FastMCP

# Import store and factory (works both as package and standalone in ~/.schism/)
try:
    from .store import SchismStore
    from .factory import Factory
    from .installer import Installer
except ImportError:
    from store import SchismStore  # type: ignore
    from factory import Factory  # type: ignore
    from installer import Installer  # type: ignore

SCHISM_HOME = Path.home() / ".schism"

mcp = FastMCP("schism")
_store = SchismStore()
_factory = Factory(_store)  # reads eagerness from ~/.schism/config.json at init
_installer = Installer(_store)

# ── MCP tools ─────────────────────────────────────────────────────────────────

@mcp.tool()
def schism_list(category: str = "", session_id: str = "") -> str:
    """List Schism tools available in the current session.

    Args:
        category: Optional tool type filter (e.g. 'mcp').
        session_id: The current executive session ID. Tools are scoped per session —
                    only tools created in this session are returned. Pass the session ID
                    you generated at /schism activation.

    Returns a formatted list showing tool names, types, generation numbers,
    and one-line capability statements. Use schism_get(name) for full details.
    """
    sid = session_id.strip() or None
    tools = _store.list_tools(
        tool_type=category if category else None,
        session_id=sid,
    )
    if not tools:
        scope = f"session '{sid}'" if sid else "the catalog"
        return f"No tools in {scope} yet. Use schism_add() or report progress to have the Factory create one."

    lines = [f"Schism Tools — session: {sid or 'all'} ({len(tools)} tools)\n" + "=" * 40]
    for t in tools:
        installed_marker = " [installed]" if t.get("is_installed") else ""
        lines.append(
            f"  {t['name']}  (gen {t['generation']}, {t['tool_type']}){installed_marker}\n"
            f"    {t['capability']}"
        )
    return "\n".join(lines)


@mcp.tool()
def schism_search(query: str, session_id: str = "") -> str:
    """Search the tool catalog by keyword, scoped to the current session.

    Args:
        query: Search terms (name, capability, use cases).
        session_id: The current executive session ID (same one passed to schism_list).

    Returns ranked results showing tool names and capabilities.
    """
    if not query.strip():
        return "Please provide a search query."
    sid = session_id.strip() or None
    try:
        results = _store.search_tools(query, session_id=sid)
    except Exception:
        all_tools = _store.list_tools(session_id=sid)
        q = query.lower()
        results = [
            t for t in all_tools
            if q in t["name"].lower()
            or q in t["capability"].lower()
            or any(q in uc.lower() for uc in (t.get("use_cases") or []))
        ]

    if not results:
        return f"No tools matched '{query}' in session '{sid}'."

    lines = [f"Search results for '{query}':"]
    for t in results:
        lines.append(f"  {t['name']} — {t['capability']}")
    return "\n".join(lines)


@mcp.tool()
def schism_get(name: str, session_id: str = "") -> str:
    """Get full documentation for a named tool in the current session.

    Args:
        name: Tool name.
        session_id: The current executive session ID (same one passed to schism_list).

    Returns: capability statement, use cases, usage patterns, requirements,
    generation history summary, and the complete MCP tool code.
    """
    sid = session_id.strip() or None
    tool = _store.get_tool(name, session_id=sid)
    if not tool:
        close = _store.search_tools(name, session_id=sid)
        suggestion = f"\nDid you mean: {close[0]['name']}?" if close else ""
        return f"Tool '{name}' not found in session '{sid}'.{suggestion}"

    use_cases = "\n".join(f"  - {uc}" for uc in (tool.get("use_cases") or []))
    patterns = "\n".join(f"  - {p}" for p in (tool.get("patterns") or []))
    requirements = "\n".join(f"  - {r}" for r in (tool.get("requirements") or []))
    installed = "Yes" if tool.get("is_installed") else "No"

    return f"""\
Tool: {tool['name']}
Type: {tool['tool_type']}  |  Generation: {tool['generation']}  |  Installed: {installed}

Capability:
  {tool['capability']}

Use Cases:
{use_cases or "  (none documented)"}

Usage Patterns:
{patterns or "  (none documented)"}

Requirements:
{requirements or "  (none)"}

Code:
```python
{tool['code']}
```"""


@mcp.tool()
def schism_progress(
    problem: str,
    solution: str,
    commands_used: list[str],
    tool_name: str = "",
    executive_session_id: str = "",
) -> str:
    """Report mid-task progress whenever a sub-problem is solved.

    Call this proactively during a task — do NOT wait until the end.
    Trigger conditions (call as soon as ONE of these happens):
      - You run a non-obvious command that works after trial and error
      - You implement a workaround or custom solution to a problem
      - You discover an approach that took multiple attempts to figure out
      - You overcome a challenge using a technique that may not be obvious next time

    Args:
        problem: What challenge or obstacle was encountered (1-2 sentences).
        solution: How it was solved — what approach, command, or insight worked.
        commands_used: The actual commands, code snippets, or function calls that solved it.
                       Include full command strings with arguments.
        tool_name: Name of a Schism tool if one was used (empty string if none).
        executive_session_id: Current Claude Code session ID (empty string if unknown).

    The Factory will immediately evaluate whether to:
    - Add this as a new usage pattern to an existing Schism tool (if tool_name is set)
    - Create a new Schism tool encapsulating this solution (if novel enough)
    - Skip it (if too trivial or task-specific)
    """
    if not problem.strip() or not solution.strip():
        return "Error: 'problem' and 'solution' cannot be empty."

    session_id = executive_session_id or f"session_{int(__import__('time').time())}"
    progress_id = _store.record_progress(
        executive_session_id=session_id,
        problem=problem,
        solution=solution,
        commands_used=commands_used or [],
        tool_name=tool_name,
    )

    def _run_factory():
        try:
            _factory.process_progress(progress_id)
        except Exception:
            pass

    import threading
    threading.Thread(target=_run_factory, daemon=True).start()

    return (
        f"Progress recorded (id={progress_id}). "
        f"Factory evaluating whether to create/update a tool from this solution."
    )


@mcp.tool()
def schism_feedback(
    task: str,
    tools_used: dict,
    tools_unhelpful: dict,
    challenges: str,
    executive_session_id: str = "",
) -> str:
    """Submit feedback about a completed task. Triggers the Factory to evolve tools.

    Args:
        task: One sentence describing what was accomplished.
        tools_used: Dict mapping tool name to how it was used.
                    Example: {"Yosys_pipeline": "extracted PPA metrics from design.v"}
        tools_unhelpful: Dict mapping tool name to why it wasn't helpful.
                         Example: {"slow_tool": "took 10min for a 3s task"}
        challenges: Free text describing obstacles encountered and how they were overcome.
        executive_session_id: The current Claude Code session ID (use "" if unknown).
    """
    if not task.strip():
        return "Error: 'task' cannot be empty."

    session_id = executive_session_id or f"session_{int(__import__('time').time())}"
    feedback_id = _store.submit_feedback(
        executive_session_id=session_id,
        task=task,
        tools_used=tools_used or {},
        tools_unhelpful=tools_unhelpful or {},
        challenges=challenges or "",
    )

    # Trigger factory in background thread (non-blocking)
    def _run_factory():
        try:
            _factory.process_feedback(feedback_id)
        except Exception as e:
            # Store the error — don't crash the MCP server
            run = _store.list_factory_runs(limit=1)
            if run and run[0].get("status") == "running":
                _store.update_factory_run(run[0]["id"], status="failed", error=str(e))

    thread = threading.Thread(target=_run_factory, daemon=True)
    thread.start()

    tool_count = len(tools_used) + len(tools_unhelpful)
    return (
        f"Feedback recorded (id={feedback_id}). "
        f"Factory started in background to process {tool_count} tool(s) and challenges. "
        f"Use schism_factory_status() to check progress."
    )


@mcp.tool()
def schism_add(description: str, tool_type: str = "mcp") -> str:
    """Request the Factory to create a new tool from a description.

    Args:
        description: What the tool should do. Be specific about inputs, outputs,
                     and any external commands or APIs it wraps.
        tool_type: One of 'mcp' (default), 'slash_command', 'shell_script'.

    Returns a status message with the factory run ID.
    """
    if not description.strip():
        return "Error: 'description' cannot be empty."
    if tool_type not in ("mcp", "slash_command", "shell_script"):
        return f"Error: tool_type must be one of: mcp, slash_command, shell_script"

    run_id = _store.create_factory_run(feedback_id=None, mode=_factory._detect_mode(), model=_factory.model)
    _store.update_factory_run(run_id, status="running")

    # Auto-generate a name from the description
    import re
    words = re.sub(r"[^a-zA-Z0-9 ]", "", description).split()[:4]
    auto_name = "_".join(w.capitalize() for w in words if w)

    def _run():
        try:
            import time
            start = time.time()
            tool_id = _factory.generate_tool(
                name=auto_name,
                description=description,
                tool_type=tool_type,
                factory_run_id=run_id,
            )
            tool = _store.get_tool_by_id(tool_id)
            _store.update_factory_run(
                run_id, status="success",
                actions=[{"type": "create", "tool": tool["name"], "tool_id": tool_id}],
                duration_s=time.time() - start,
            )
        except Exception as e:
            _store.update_factory_run(run_id, status="failed", error=str(e))

    threading.Thread(target=_run, daemon=True).start()
    return (
        f"Factory started (run_id={run_id}). "
        f"Generating '{auto_name}' ({tool_type}). "
        f"Use schism_factory_status(run_id={run_id}) to check progress."
    )


@mcp.tool()
def schism_tree(name: str) -> str:
    """Show the full evolution history of a tool across all generations.

    Displays each generation's capability, evolution note, and creation date.
    """
    tool = _store.get_tool(name)
    if not tool:
        return f"Tool '{name}' not found."

    generations = _store.get_tool_generations(tool["id"])
    if not generations:
        return f"No generation history found for '{name}'."

    import time as _time
    lines = [f"Evolution tree for: {name} (current: gen {tool['generation']})\n" + "─" * 50]
    for g in generations:
        ts = _time.strftime("%Y-%m-%d %H:%M", _time.localtime(g["created_at"]))
        current = " ◄ current" if g["generation"] == tool["generation"] else ""
        note = g.get("evolution_note") or "Initial creation"
        lines.append(
            f"  Gen {g['generation']}  [{ts}]{current}\n"
            f"    Capability: {g['capability']}\n"
            f"    Note: {note}"
        )
    return "\n".join(lines)


@mcp.tool()
def schism_rollback(name: str, generation: int) -> str:
    """Roll back a tool to a specific generation and reinstall it.

    Use schism_tree(name) to see available generations.
    """
    tool = _store.get_tool(name)
    if not tool:
        return f"Tool '{name}' not found."

    if generation == tool["generation"]:
        return f"Tool '{name}' is already at generation {generation}."

    try:
        _store.rollback_tool(tool["id"], generation)
    except ValueError as e:
        return f"Rollback failed: {e}"

    # Reinstall if currently installed
    if tool.get("is_installed"):
        tool_refreshed = _store.get_tool(name)
        _installer.install_mcp_tool(tool_refreshed, tool_refreshed["code"])
        return f"Rolled back '{name}' to generation {generation} and reinstalled."

    return f"Rolled back '{name}' to generation {generation}. Run schism_install('{name}') to apply."


@mcp.tool()
def schism_install(name: str) -> str:
    """Install a tool from the catalog into Claude Code.

    Writes the MCP server file and registers it in ~/.claude/settings.json.
    Claude Code must be restarted to pick up newly installed tools.
    """
    tool = _store.get_tool(name)
    if not tool:
        return f"Tool '{name}' not found."
    if tool.get("is_installed"):
        return f"Tool '{name}' is already installed at {tool.get('install_path')}."

    install_path = _installer.install_mcp_tool(tool, tool["code"])
    return (
        f"Installed '{name}' at {install_path}.\n"
        f"Restart Claude Code to activate the new MCP tool."
    )


@mcp.tool()
def schism_factory_status(run_id: int = 0) -> str:
    """Check the status of Factory runs.

    If run_id is 0 or omitted, shows the 5 most recent runs.
    """
    if run_id:
        run = _store.get_factory_run(run_id)
        if not run:
            return f"Factory run {run_id} not found."
        return _format_run(run)

    runs = _store.list_factory_runs(limit=5)
    if not runs:
        return "No factory runs yet."
    return "\n\n".join(_format_run(r) for r in runs)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _format_run(run: dict) -> str:
    import time as _time
    ts = _time.strftime("%Y-%m-%d %H:%M", _time.localtime(run["created_at"]))
    actions = run.get("actions") or []
    action_str = ", ".join(
        f"{a.get('type','?')} {a.get('tool','?')}" for a in (actions if isinstance(actions, list) else [])
    ) or "none"
    duration = f"{run['duration_s']:.1f}s" if run.get("duration_s") else "—"
    error_str = f"\n  Error: {run['error']}" if run.get("error") else ""
    return (
        f"Run #{run['id']}  [{ts}]  status={run['status']}  duration={duration}\n"
        f"  Actions: {action_str}"
        f"{error_str}"
    )


# ── Entrypoint ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    SCHISM_HOME.mkdir(parents=True, exist_ok=True)
    mcp.run()
