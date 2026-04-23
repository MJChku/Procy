"""Schism CLI — usable from any Claude Code session via Bash, no MCP required."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="schism",
        description=(
            "Schism — Claude Code tool discovery and management.\n"
            "All subcommands work via 'bash schism <cmd>' from any Claude Code session,\n"
            "even if the MCP server is not registered. MCP just provides a nicer interface."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    # ── Setup ──────────────────────────────────────────────────────────────────
    sub.add_parser(
        "install",
        help="Install MCP server + /schism slash command into ~/.claude/. "
             "Safe to run mid-session — takes effect at next session start for MCP, "
             "immediately for the slash command and Bash CLI.",
    )

    sub.add_parser("ui", help="Start the web UI on port 7863")
    sub.add_parser("status", help="Show catalog summary, pending runs, installed tools")

    # ── Read operations (Claude uses these via Bash when MCP is unavailable) ──
    sub.add_parser("list", help="List all tools in the catalog")

    get_p = sub.add_parser("get", help="Show full documentation for a tool")
    get_p.add_argument("name", help="Tool name")

    # kept for backward compat
    inspect_p = sub.add_parser("inspect", help="Alias for 'get'")
    inspect_p.add_argument("name")

    search_p = sub.add_parser("search", help="Search tools by keyword")
    search_p.add_argument("query", nargs="+", help="Search terms")

    tree_p = sub.add_parser("tree", help="Show evolution history of a tool")
    tree_p.add_argument("name")

    rollback_p = sub.add_parser("rollback", help="Roll back a tool to a specific generation")
    rollback_p.add_argument("name")
    rollback_p.add_argument("generation", type=int)

    # ── Write operations (Claude uses these via Bash to record events) ────────
    prog_p = sub.add_parser(
        "progress",
        help="Record a mid-task progress event and trigger the Factory. "
             "Call this whenever a sub-problem is solved.",
    )
    prog_p.add_argument("--problem", required=True, help="What challenge was encountered")
    prog_p.add_argument("--solution", required=True, help="How it was solved")
    prog_p.add_argument(
        "--commands", nargs="*", default=[],
        help="Commands/snippets that worked (space-separated, quote each one)",
    )
    prog_p.add_argument("--tool", default="", help="Schism tool name if one was involved")
    prog_p.add_argument("--session", default="", help="Claude Code session ID")
    prog_p.add_argument(
        "--no-factory", action="store_true",
        help="Record only; do not trigger the Factory immediately",
    )

    fb_p = sub.add_parser(
        "feedback",
        help="Record end-of-task feedback and trigger the Factory.",
    )
    fb_p.add_argument("--task", required=True, help="One-sentence task description")
    fb_p.add_argument(
        "--tools-used", default="{}",
        help='JSON dict: {"ToolName": "how used", ...}',
    )
    fb_p.add_argument(
        "--unhelpful", default="{}",
        help='JSON dict: {"ToolName": "why unhelpful", ...}',
    )
    fb_p.add_argument("--challenges", default="", help="Obstacles and how they were overcome")
    fb_p.add_argument("--session", default="", help="Claude Code session ID")
    fb_p.add_argument("--no-factory", action="store_true")

    add_p = sub.add_parser("add", help="Request the Factory to create a new tool")
    add_p.add_argument("description", nargs="+", help="What the tool should do")
    add_p.add_argument("--type", default="mcp", choices=["mcp", "slash_command", "shell_script"])

    factory_p = sub.add_parser("factory", help="Process all unprocessed feedback and progress events")
    factory_p.add_argument(
        "--eagerness", choices=["conservative", "moderate", "aggressive"], default=None,
        help="How eagerly the factory creates new tools (overrides ~/.schism/config.json)",
    )

    args = parser.parse_args()

    dispatch = {
        "install": _cmd_install,
        "ui": _cmd_ui,
        "status": _cmd_status,
        "list": _cmd_list,
        "get": lambda: _cmd_get(args.name),
        "inspect": lambda: _cmd_get(args.name),
        "search": lambda: _cmd_search(" ".join(args.query)),
        "tree": lambda: _cmd_tree(args.name),
        "rollback": lambda: _cmd_rollback(args.name, args.generation),
        "progress": lambda: _cmd_progress(args),
        "feedback": lambda: _cmd_feedback(args),
        "add": lambda: _cmd_add(" ".join(args.description), args.type),
        "factory": lambda: _cmd_factory(args),
    }

    if args.command in dispatch:
        dispatch[args.command]()
    else:
        parser.print_help()


# ── Setup ──────────────────────────────────────────────────────────────────────

def _cmd_install() -> None:
    from .store import SchismStore, SCHISM_HOME
    from .installer import Installer

    SCHISM_HOME.mkdir(parents=True, exist_ok=True)
    store = SchismStore()
    installer = Installer(store)
    server_source = Path(__file__).parent / "server.py"
    if not server_source.exists():
        print(f"Error: cannot find {server_source}", file=sys.stderr)
        sys.exit(1)
    installer.install_all(server_source)
    print()
    print("Note: The /schism slash command and 'schism' Bash CLI are active immediately.")
    print("The MCP server (schism_list, schism_progress, etc.) takes effect at the")
    print("next Claude Code session start. Until then, use: bash schism <command>")


def _cmd_ui() -> None:
    from .store import SchismStore
    from .factory import Factory
    from .installer import Installer
    from .ui import start_ui

    store = SchismStore()
    print("Starting Schism UI at http://127.0.0.1:7863")
    start_ui(store, Factory(store), Installer(store))


# ── Read operations ────────────────────────────────────────────────────────────

def _cmd_list() -> None:
    from .store import SchismStore

    store = SchismStore()
    tools = store.list_tools()
    if not tools:
        print("No tools in catalog. Use: schism add <description>")
        return

    print(f"{'Name':<35} {'Type':<12} {'Gen':>4}  {'Installed':<10}  Capability")
    print("─" * 90)
    for t in tools:
        installed = "yes" if t.get("is_installed") else "—"
        cap = t["capability"][:50]
        print(f"{t['name']:<35} {t['tool_type']:<12} {t['generation']:>4}  {installed:<10}  {cap}")


def _cmd_get(name: str) -> None:
    from .store import SchismStore
    import time

    store = SchismStore()
    tool = store.get_tool(name)
    if not tool:
        # Try fuzzy search
        results = store.search_tools(name)
        if results:
            print(f"Tool '{name}' not found. Did you mean: {results[0]['name']}?", file=sys.stderr)
        else:
            print(f"Tool '{name}' not found.", file=sys.stderr)
        sys.exit(1)

    print(f"Name:       {tool['name']}")
    print(f"Type:       {tool['tool_type']}  |  Generation: {tool['generation']}")
    print(f"Installed:  {'yes' if tool.get('is_installed') else 'no'}")
    print(f"\nCapability:\n  {tool['capability']}")

    for section, key in [("Use Cases", "use_cases"), ("Patterns", "patterns"), ("Requirements", "requirements")]:
        items = tool.get(key) or []
        if items:
            print(f"\n{section}:")
            for item in items:
                print(f"  - {item}")

    print(f"\nCode:\n{'─'*60}")
    print(tool.get("code", "(no code)"))

    generations = store.get_tool_generations(tool["id"])
    if len(generations) > 1:
        print(f"\nGeneration history ({len(generations)} versions):")
        for g in generations:
            ts = time.strftime("%Y-%m-%d", time.localtime(g["created_at"]))
            cur = " ◄" if g["generation"] == tool["generation"] else ""
            note = g.get("evolution_note") or ""
            print(f"  Gen {g['generation']:>2}  [{ts}]{cur}  {note[:70]}")


def _cmd_search(query: str) -> None:
    from .store import SchismStore

    store = SchismStore()
    try:
        results = store.search_tools(query)
    except Exception:
        all_tools = store.list_tools()
        q = query.lower()
        results = [t for t in all_tools if q in t["name"].lower() or q in t["capability"].lower()]

    if not results:
        print(f"No tools matched '{query}'.")
        return
    print(f"Results for '{query}':")
    for t in results:
        print(f"  {t['name']} — {t['capability']}")


def _cmd_tree(name: str) -> None:
    from .store import SchismStore
    import time

    store = SchismStore()
    tool = store.get_tool(name)
    if not tool:
        print(f"Tool '{name}' not found.", file=sys.stderr)
        sys.exit(1)

    generations = store.get_tool_generations(tool["id"])
    print(f"Evolution of '{name}' (current: gen {tool['generation']})")
    print("─" * 50)
    for g in generations:
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(g["created_at"]))
        cur = " ◄ current" if g["generation"] == tool["generation"] else ""
        note = g.get("evolution_note") or "Initial creation"
        print(f"  Gen {g['generation']}  [{ts}]{cur}")
        print(f"    {g['capability']}")
        print(f"    Note: {note}")


def _cmd_rollback(name: str, generation: int) -> None:
    from .store import SchismStore
    from .installer import Installer

    store = SchismStore()
    tool = store.get_tool(name)
    if not tool:
        print(f"Tool '{name}' not found.", file=sys.stderr)
        sys.exit(1)
    store.rollback_tool(tool["id"], generation)
    tool = store.get_tool(name)
    if tool.get("is_installed"):
        Installer(store).install_mcp_tool(tool, tool["code"])
        print(f"Rolled back '{name}' to gen {generation} and reinstalled.")
    else:
        print(f"Rolled back '{name}' to gen {generation}.")


# ── Write operations ───────────────────────────────────────────────────────────

def _cmd_progress(args: argparse.Namespace) -> None:
    from .store import SchismStore
    from .factory import Factory
    import threading

    store = SchismStore()
    session = args.session or f"bash_session"

    pid = store.record_progress(
        executive_session_id=session,
        problem=args.problem,
        solution=args.solution,
        commands_used=args.commands or [],
        tool_name=args.tool or "",
    )
    print(f"Progress recorded (id={pid}).")

    if not args.no_factory:
        print("Triggering Factory...")
        factory = Factory(store)
        try:
            run_id = factory.process_progress(pid)
            run = store.get_factory_run(run_id)
            actions = run.get("actions") or []
            if actions:
                for a in actions:
                    print(f"  → {a.get('type','?')} '{a.get('tool','?')}'")
            else:
                print("  → Factory: no new tool warranted.")
        except Exception as e:
            print(f"  → Factory error: {e}", file=sys.stderr)


def _cmd_feedback(args: argparse.Namespace) -> None:
    from .store import SchismStore
    from .factory import Factory

    store = SchismStore()
    session = args.session or "bash_session"

    try:
        tools_used = json.loads(args.tools_used)
    except json.JSONDecodeError:
        print("Error: --tools-used must be valid JSON", file=sys.stderr)
        sys.exit(1)
    try:
        tools_unhelpful = json.loads(args.unhelpful)
    except json.JSONDecodeError:
        print("Error: --unhelpful must be valid JSON", file=sys.stderr)
        sys.exit(1)

    fid = store.submit_feedback(
        executive_session_id=session,
        task=args.task,
        tools_used=tools_used,
        tools_unhelpful=tools_unhelpful,
        challenges=args.challenges or "",
    )
    print(f"Feedback recorded (id={fid}).")

    if not args.no_factory:
        print("Triggering Factory...")
        factory = Factory(store)
        try:
            run_id = factory.process_feedback(fid)
            run = store.get_factory_run(run_id)
            actions = run.get("actions") or []
            if actions:
                for a in actions:
                    print(f"  → {a.get('type','?')} '{a.get('tool','?')}'")
            else:
                print("  → Factory: no tool changes.")
        except Exception as e:
            print(f"  → Factory error: {e}", file=sys.stderr)


def _cmd_add(description: str, tool_type: str) -> None:
    from .store import SchismStore
    from .factory import Factory

    store = SchismStore()
    factory = Factory(store)
    print(f"Generating tool for: {description[:80]}")
    try:
        tool_id = factory.generate_tool(
            name=_auto_name(description),
            description=description,
            tool_type=tool_type,
        )
        tool = store.get_tool_by_id(tool_id)
        print(f"Created: {tool['name']} (gen 1)")
        print(f"  {tool['capability']}")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def _cmd_factory(args=None) -> None:
    from .store import SchismStore
    from .factory import Factory

    store = SchismStore()
    eagerness = getattr(args, "eagerness", None)
    factory = Factory(store, eagerness=eagerness)

    pending_fb = store.get_unprocessed_feedback()
    pending_pr = store.get_unprocessed_progress()

    if not pending_fb and not pending_pr:
        print("Nothing to process.")
        return

    if pending_pr:
        print(f"Processing {len(pending_pr)} progress event(s)...")
        for ev in pending_pr:
            print(f"  [{ev['id']}] {ev['problem'][:70]}")
            try:
                run_id = factory.process_progress(ev["id"])
                run = store.get_factory_run(run_id)
                actions = run.get("actions") or []
                summary = ", ".join(f"{a.get('type')} {a.get('tool')}" for a in (actions or [])) or "no change"
                print(f"      → {run.get('status')}: {summary}")
            except Exception as e:
                print(f"      → error: {e}", file=sys.stderr)

    if pending_fb:
        print(f"Processing {len(pending_fb)} feedback item(s)...")
        for fb in pending_fb:
            print(f"  [{fb['id']}] {fb['task'][:70]}")
            try:
                run_id = factory.process_feedback(fb["id"])
                run = store.get_factory_run(run_id)
                actions = run.get("actions") or []
                summary = ", ".join(f"{a.get('type')} {a.get('tool')}" for a in (actions or [])) or "no change"
                print(f"      → {run.get('status')}: {summary}")
            except Exception as e:
                print(f"      → error: {e}", file=sys.stderr)


def _cmd_status() -> None:
    from .store import SchismStore, SCHISM_HOME
    from .installer import CLAUDE_SETTINGS
    import time

    store = SchismStore()
    tools = store.list_tools()
    installed = [t for t in tools if t.get("is_installed")]
    pending_fb = store.get_unprocessed_feedback()
    pending_pr = store.get_unprocessed_progress()
    runs = store.list_factory_runs(limit=5)

    print("Schism Status")
    print("─" * 40)
    print(f"  Catalog:    {len(tools)} tools ({len(installed)} installed)")
    print(f"  DB:         {store.db_path}")
    print(f"  Settings:   {CLAUDE_SETTINGS}")
    print(f"  Pending:    {len(pending_fb)} feedback, {len(pending_pr)} progress events")

    if runs:
        print("\nRecent factory runs:")
        for r in runs:
            ts = time.strftime("%m-%d %H:%M", time.localtime(r["created_at"]))
            dur = f"{r['duration_s']:.1f}s" if r.get("duration_s") else "—"
            print(f"  #{r['id']} [{ts}] {r['status']:<10} {dur}")

    if pending_fb or pending_pr:
        print("\nRun 'schism factory' to process pending items.")


# ── Helpers ────────────────────────────────────────────────────────────────────

def _auto_name(description: str) -> str:
    import re
    words = re.sub(r"[^a-zA-Z0-9 ]", "", description).split()[:4]
    return "_".join(w.capitalize() for w in words if w)


if __name__ == "__main__":
    main()
