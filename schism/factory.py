"""Schism Factory — processes Executive feedback to generate and evolve tools."""
from __future__ import annotations

import json
import re
import textwrap
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .store import SchismStore

# ── Tool format spec (static preamble for all factory prompts) ────────────────

TOOL_FORMAT_SPEC = textwrap.dedent("""
You are the Schism Factory. Your job is to generate or update tools for Claude Code.

A tool is a Python MCP (Model Context Protocol) server that Claude Code can call.
You MUST output EXACTLY the following format — no other text before or after:

---BEGIN TOOL---
name: <ToolName_with_underscores>
capability: <one line — what this tool does>
use_cases:
  - <use case 1>
  - <use case 2>
  - <use case 3>
patterns:
  - <example_function_call(arg="value") → expected output>
  - <another_example()>
requirements:
  - <requirement 1, e.g. "tool_name >= version installed in PATH">
  - <or "API key: set FOO_API_KEY environment variable">
evolution_note: <why this version was created or what changed>
---BEGIN CODE---
from mcp.server.fastmcp import FastMCP
import subprocess
import os

mcp = FastMCP("<ToolName>")

@mcp.tool()
def <function_name>(<typed_args>) -> str:
    \"\"\"<docstring matching capability>\"\"\"
    # implementation
    ...

if __name__ == "__main__":
    mcp.run()
---END TOOL---

Rules:
- The code MUST be a complete, runnable Python MCP server using FastMCP
- The code MUST import only stdlib modules plus 'mcp'
- Function arguments MUST have type annotations
- The tool name uses CamelCase_with_underscores; function name uses snake_case
- Use subprocess.run() for shell commands, capture output as text
- Return results as formatted strings (not raw bytes)
- Include error handling: return error messages as strings, never raise
- The evolution_note MUST explain why this tool is being created/modified

Tool naming rules — the name must be SPECIFIC enough to understand the exact action:
- Include the specific command, flag, or technique — NOT a generic category
- Include the domain and the precise operation
- BAD (too vague): "Yosys_fix", "Plugin_helper", "Log_tool", "Output_handler", "Yosys_plugin_output_fix"
- GOOD (specific): "Yosys_-qq_log_suppression_bypass", "Yosys_plugin_so_tmpfs_compiler",
  "Yosys_synth_parallel_submodule", "Git_log_oneline_author_filter", "Tmpfs_workspace_symlink_setup"
- Pattern: Domain_SpecificVerb_ExactDetail (e.g. "Yosys_read_verilog_-defer_flag")
""").strip()


SCHISM_CONFIG_PATH = Path.home() / ".schism" / "config.json"

_EAGERNESS_HINTS = {
    "conservative": (
        "Only output YES if the solution is clearly reusable, non-trivial, and would save "
        "significant time in future sessions. When in doubt, output NO."
    ),
    "moderate": (
        "Consider YES if: the solution uses non-obvious commands, took multiple attempts to "
        "figure out, or would be useful to have ready in future sessions.\n"
        "Consider NO if: the solution is trivial, highly task-specific, or already covered "
        "by standard tools."
    ),
    "aggressive": (
        "Output YES unless the solution is completely trivial (e.g. a single well-known "
        "command with no special flags). When in doubt, output YES."
    ),
}


def _load_config() -> dict:
    try:
        return json.loads(SCHISM_CONFIG_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


class Factory:
    """Generates and updates Schism tools via Claude Code CLI or Anthropic API."""

    def __init__(
        self,
        store: "SchismStore",
        model: str = "claude-sonnet-4-6",
        eagerness: str | None = None,
    ):
        self.store = store
        self.model = model
        cfg = _load_config()
        self.eagerness = eagerness or cfg.get("eagerness", "moderate")
        if self.eagerness not in _EAGERNESS_HINTS:
            self.eagerness = "moderate"

    # ── Public API ────────────────────────────────────────────────────────────

    def process_progress(self, progress_id: int) -> int:
        """Process a mid-task progress event. Returns factory_run_id.

        If a Schism tool was involved → add the discovered command as a new pattern.
        If no tool was involved → evaluate whether to create a new tool from the solution.
        """
        events = self.store.list_progress(limit=1000)
        ev = next((e for e in events if e["id"] == progress_id), None)
        if not ev:
            raise ValueError(f"Progress event {progress_id} not found")

        run_id = self.store.create_factory_run(
            progress_id=progress_id,
            mode=self._detect_mode(),
            model=self.model,
        )
        self.store.update_factory_run(run_id, status="running")

        actions = []
        start = time.time()
        try:
            tool_name = ev.get("tool_name", "").strip()
            problem = ev.get("problem", "")
            solution = ev.get("solution", "")
            commands = ev.get("commands_used") or []
            session_id = ev.get("executive_session_id") or None

            if tool_name:
                # A known Schism tool was involved — add the discovered pattern
                tool = self.store.get_tool(tool_name, session_id=session_id)
                if tool:
                    new_gen = self.update_tool(
                        tool["id"],
                        feedback_excerpt=(
                            f"New usage pattern discovered:\n"
                            f"Problem: {problem}\n"
                            f"Solution: {solution}\n"
                            f"Commands: {chr(10).join(commands)}"
                        ),
                        change_type="add_pattern",
                        factory_run_id=run_id,
                    )
                    actions.append({"type": "update", "tool": tool_name, "generation": new_gen})
            else:
                # No Schism tool — check if this solution warrants a new tool
                candidate = self._evaluate_tool_candidate(problem, solution, commands)
                if candidate:
                    tool_id = self.generate_tool(
                        name=candidate["name"],
                        description=candidate["description"],
                        context={
                            "problem": problem,
                            "solution": solution,
                            "commands": "\n".join(commands),
                        },
                        factory_run_id=run_id,
                        session_id=session_id,
                    )
                    actions.append({"type": "create", "tool": candidate["name"], "tool_id": tool_id})

            duration = time.time() - start
            self.store.update_factory_run(run_id, status="success", actions=actions, duration_s=duration)
            self.store.mark_progress_processed(progress_id)

        except Exception as exc:
            self.store.update_factory_run(
                run_id, status="failed", error=str(exc), duration_s=time.time() - start
            )
            raise

        return run_id

    def _evaluate_tool_candidate(
        self, problem: str, solution: str, commands: list[str]
    ) -> dict | None:
        """Ask Claude whether this solution is worth turning into a reusable tool.
        Returns {name, description} or None."""
        if not solution or len(solution) < 30:
            return None

        hint = _EAGERNESS_HINTS[self.eagerness]
        prompt = textwrap.dedent(f"""
            An AI assistant solved this problem during a task:

            Problem: {problem}
            Solution: {solution}
            Commands used: {chr(10).join(f"  - {c}" for c in commands) if commands else "  (none recorded)"}

            Should this become a reusable Schism tool?
            If YES, output a single JSON line: {{"name": "Specific_Tool_Name", "description": "what it does"}}
            If NO, output the word: NO

            {hint}

            The tool name must be SPECIFIC — include the exact command/flag/technique, not a generic category.
            Example: "Yosys_-qq_log_suppression_bypass" not "Yosys_output_fix".
        """).strip()

        response = self._call_claude(prompt).strip()
        if response.upper().startswith("NO"):
            return None
        for line in response.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    obj = json.loads(line)
                    if "name" in obj and "description" in obj:
                        return obj
                except json.JSONDecodeError:
                    pass
        return None

    def process_feedback(self, feedback_id: int) -> int:
        """Process feedback entry, generate/update tools. Returns factory_run_id."""
        feedback = self.store.list_feedback(limit=1000)
        fb = next((f for f in feedback if f["id"] == feedback_id), None)
        if not fb:
            raise ValueError(f"Feedback {feedback_id} not found")

        run_id = self.store.create_factory_run(
            feedback_id=feedback_id,
            mode=self._detect_mode(),
            model=self.model,
        )
        self.store.update_factory_run(run_id, status="running")

        actions = []
        start = time.time()
        try:
            tools_used: dict = fb.get("tools_used") or {}
            tools_unhelpful: dict = fb.get("tools_unhelpful") or {}
            challenges: str = fb.get("challenges", "")
            session_id: str | None = fb.get("executive_session_id") or None

            # Update tools that were used
            for tool_name, usage_note in tools_used.items():
                tool = self.store.get_tool(tool_name, session_id=session_id)
                if tool:
                    change_type = "add_pattern"
                    # If challenges mention the tool negatively, rewrite it
                    if tool_name.lower() in challenges.lower() and any(
                        word in challenges.lower()
                        for word in ("slow", "fail", "error", "wrong", "bad", "couldn't", "inaccurate")
                    ):
                        change_type = "rewrite"
                    new_gen = self.update_tool(
                        tool["id"],
                        feedback_excerpt=f"Task: {fb['task']}\nUsage: {usage_note}\nChallenges: {challenges}",
                        change_type=change_type,
                        factory_run_id=run_id,
                    )
                    actions.append({"type": "update", "tool": tool_name, "generation": new_gen})

            # Add anti-pattern notes to unhelpful tools
            for tool_name, reason in tools_unhelpful.items():
                tool = self.store.get_tool(tool_name, session_id=session_id)
                if tool:
                    new_gen = self.update_tool(
                        tool["id"],
                        feedback_excerpt=f"Task: {fb['task']}\nNot helpful because: {reason}",
                        change_type="add_use_case",
                        factory_run_id=run_id,
                    )
                    actions.append({"type": "update", "tool": tool_name, "generation": new_gen})

            # Identify new tool candidates from challenges
            new_tools = self._identify_new_tools(fb["task"], challenges)
            for candidate in new_tools:
                tool_id = self.generate_tool(
                    name=candidate["name"],
                    description=candidate["description"],
                    context={"task": fb["task"], "challenges": challenges},
                    factory_run_id=run_id,
                    session_id=session_id,
                )
                actions.append({"type": "create", "tool": candidate["name"], "tool_id": tool_id})

            duration = time.time() - start
            self.store.update_factory_run(
                run_id, status="success",
                actions=actions, duration_s=duration,
            )
            self.store.mark_feedback_processed(feedback_id)

        except Exception as exc:
            self.store.update_factory_run(
                run_id, status="failed",
                error=str(exc), duration_s=time.time() - start,
            )
            raise

        return run_id

    def generate_tool(
        self,
        name: str,
        description: str,
        tool_type: str = "mcp",
        context: dict | None = None,
        factory_run_id: int | None = None,
        session_id: str | None = None,
    ) -> int:
        """Generate a brand-new tool. Returns tool_id."""
        prompt = self._build_generation_prompt(name, description, tool_type, context)
        response = self._call_claude(prompt)
        artifact = self._parse_tool_artifact(response)

        tool_id = self.store.create_tool(
            name=artifact["name"],
            capability=artifact["capability"],
            use_cases=artifact["use_cases"],
            patterns=artifact["patterns"],
            requirements=artifact["requirements"],
            code=artifact["code"],
            tool_type=tool_type,
            evolution_note=artifact.get("evolution_note", f"Initial generation for: {description}"),
            factory_run_id=factory_run_id,
            session_id=session_id,
        )
        return tool_id

    def update_tool(
        self,
        tool_id: int,
        feedback_excerpt: str,
        change_type: str,
        factory_run_id: int | None = None,
    ) -> int:
        """Generate a new generation for an existing tool. Returns new generation number."""
        tool = self.store.get_tool_by_id(tool_id)
        if not tool:
            raise ValueError(f"Tool {tool_id} not found")

        prompt = self._build_update_prompt(tool, feedback_excerpt, change_type)
        response = self._call_claude(prompt)
        artifact = self._parse_tool_artifact(response)

        new_gen = self.store.update_tool_generation(
            tool_id=tool_id,
            capability=artifact.get("capability", tool["capability"]),
            use_cases=artifact.get("use_cases", tool["use_cases"]),
            patterns=artifact.get("patterns", tool["patterns"]),
            requirements=artifact.get("requirements", tool["requirements"]),
            code=artifact.get("code", tool["code"]),
            evolution_note=artifact.get("evolution_note", f"Updated based on feedback: {change_type}"),
            factory_run_id=factory_run_id,
        )
        return new_gen

    # ── Internal ──────────────────────────────────────────────────────────────

    def _detect_mode(self) -> str:
        """Use API mode if ANTHROPIC_API_KEY is set, otherwise CLI mode."""
        return "api" if _anthropic_available() else "cli"

    def _call_claude(self, prompt: str) -> str:
        """Call Claude via CLI subprocess (primary) or Anthropic API (optional)."""
        if _anthropic_available():
            return self._call_api(prompt)
        return self._call_cli(prompt)

    def _call_cli(self, prompt: str) -> str:
        """Spawn `claude -p <prompt> --output-format stream-json` and return text output."""
        from .agent import ClaudeCodeAgent
        agent = ClaudeCodeAgent(
            max_turns=5,
            permission_mode="bypassPermissions",
        )
        response = agent.run(prompt)
        return response.text

    def _call_api(self, prompt: str) -> str:
        """Use Anthropic SDK with prompt caching on the static preamble."""
        import anthropic
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=self.model,
            max_tokens=4096,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": TOOL_FORMAT_SPEC,
                            "cache_control": {"type": "ephemeral"},
                        },
                        {
                            "type": "text",
                            "text": prompt,
                        },
                    ],
                }
            ],
        )
        return response.content[0].text

    def _build_generation_prompt(
        self,
        name: str,
        description: str,
        tool_type: str,
        context: dict | None,
    ) -> str:
        ctx_text = ""
        if context:
            ctx_text = "\n".join(f"{k}: {v}" for k, v in context.items())

        return f"""{TOOL_FORMAT_SPEC}

---

Generate a new Schism tool with the following specification:

Tool name: {name}
Tool type: {tool_type}
Description: {description}
{f"Context:{chr(10)}{ctx_text}" if ctx_text else ""}

Generate the complete tool artifact now."""

    def _build_update_prompt(
        self,
        tool: dict,
        feedback_excerpt: str,
        change_type: str,
    ) -> str:
        change_instructions = {
            "add_pattern": "Add new usage patterns based on how the tool was actually used. Keep all existing content, only add new patterns.",
            "add_use_case": "The tool was not helpful for certain cases. Add clarifying notes to use_cases about when NOT to use this tool. Keep code unchanged.",
            "rewrite": "Rewrite the tool based on performance/correctness feedback. Improve the implementation while preserving the same interface.",
            "fix_requirements": "Update the requirements section to accurately reflect all dependencies needed.",
        }.get(change_type, "Improve the tool based on feedback.")

        return f"""{TOOL_FORMAT_SPEC}

---

Update the following existing tool based on feedback.

CURRENT TOOL:
name: {tool['name']}
capability: {tool['capability']}
use_cases: {json.dumps(tool.get('use_cases', []), indent=2)}
patterns: {json.dumps(tool.get('patterns', []), indent=2)}
requirements: {json.dumps(tool.get('requirements', []), indent=2)}
Current generation: {tool['generation']}

---BEGIN CURRENT CODE---
{tool['code']}
---END CURRENT CODE---

FEEDBACK:
{feedback_excerpt}

INSTRUCTION: {change_instructions}

Output the full updated tool artifact."""

    def _parse_tool_artifact(self, response: str) -> dict:
        """Extract structured data from factory response."""
        # Find the artifact block
        match = re.search(
            r"---BEGIN TOOL---\s*(.*?)\s*---BEGIN CODE---\s*(.*?)\s*---END TOOL---",
            response, re.DOTALL
        )
        if not match:
            raise ValueError(
                f"Factory response did not contain a valid tool artifact.\n"
                f"Response (first 500 chars): {response[:500]}"
            )

        header = match.group(1).strip()
        code = match.group(2).strip()

        artifact: dict = {"code": code}

        # Parse YAML-like header fields
        name_m = re.search(r"^name:\s*(.+)$", header, re.MULTILINE)
        artifact["name"] = name_m.group(1).strip() if name_m else ""

        cap_m = re.search(r"^capability:\s*(.+)$", header, re.MULTILINE)
        artifact["capability"] = cap_m.group(1).strip() if cap_m else ""

        note_m = re.search(r"^evolution_note:\s*(.+)$", header, re.MULTILINE)
        artifact["evolution_note"] = note_m.group(1).strip() if note_m else ""

        artifact["use_cases"] = _parse_list_block(header, "use_cases")
        artifact["patterns"] = _parse_list_block(header, "patterns")
        artifact["requirements"] = _parse_list_block(header, "requirements")

        if not artifact["name"]:
            raise ValueError("Factory artifact missing 'name' field")
        if not artifact["capability"]:
            raise ValueError("Factory artifact missing 'capability' field")
        if not code.strip():
            raise ValueError("Factory artifact has empty code block")

        return artifact

    def _identify_new_tools(self, task: str, challenges: str) -> list[dict]:
        """Ask Claude to identify new tool candidates from challenges text."""
        if not challenges or len(challenges) < 50:
            return []

        prompt = textwrap.dedent(f"""
            An AI assistant completed this task: "{task}"

            During the task, these challenges arose and were overcome:
            {challenges}

            Identify UP TO 2 new reusable tools that could automate or simplify the
            procedures described above. For each tool, output a JSON object on its own line:
            {{"name": "Specific_Tool_Name", "description": "what it does in one sentence"}}

            Tool name rules: be SPECIFIC — include exact command/flag/technique.
            BAD: "Yosys_fix", "Log_handler". GOOD: "Yosys_-qq_log_suppressor", "Tmpfs_symlink_workspace_setup".

            Output ONLY JSON lines, one per tool. If no good tool candidates exist, output nothing.
        """).strip()

        response = self._call_claude(prompt)
        candidates = []
        for line in response.strip().splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    obj = json.loads(line)
                    if "name" in obj and "description" in obj:
                        candidates.append(obj)
                except json.JSONDecodeError:
                    pass
        return candidates[:2]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _anthropic_available() -> bool:
    """True if anthropic SDK is installed and API key is set."""
    try:
        import anthropic  # noqa: F401
        return bool(
            __import__("os").environ.get("ANTHROPIC_API_KEY")
        )
    except ImportError:
        return False


def _parse_list_block(text: str, field_name: str) -> list[str]:
    """Parse a YAML-style list block like:
        field_name:
          - item1
          - item2
    """
    pattern = rf"^{field_name}:\s*\n((?:[ \t]+-[^\n]*\n?)+)"
    match = re.search(pattern, text, re.MULTILINE)
    if not match:
        return []
    block = match.group(1)
    items = re.findall(r"[ \t]+-\s*(.+)", block)
    return [item.strip() for item in items if item.strip()]
