"""Claude Code CLI wrapper — forwards prompts, streams structured events."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Generator


@dataclass
class ToolCall:
    name: str
    input: dict
    result: str | None = None


@dataclass
class AgentResponse:
    text: str = ""
    thinking: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    session_id: str = ""
    cost_usd: float = 0.0
    num_turns: int = 0
    stop_reason: str = ""
    raw_events: list[dict] = field(default_factory=list)


@dataclass
class StreamEvent:
    """One parsed event from the agent stream."""
    type: str  # 'thinking', 'text', 'tool_use', 'tool_result', 'done', 'error'
    content: str = ""
    tool_name: str = ""
    tool_input: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)


class ClaudeCodeAgent:
    """Wraps Claude Code CLI for non-interactive use with full tracing."""

    def __init__(
        self,
        max_turns: int = 20,
        max_budget_usd: float | None = None,
        permission_mode: str = "bypassPermissions",
        system_prompt: str | None = None,
        append_system_prompt: str | None = None,
        allowed_tools: list[str] | None = None,
        cwd: str | None = None,
    ):
        self.max_turns = max_turns
        self.max_budget_usd = max_budget_usd
        self.permission_mode = permission_mode
        self.system_prompt = system_prompt
        self.append_system_prompt = append_system_prompt
        self.allowed_tools = allowed_tools
        self.cwd = cwd

    def _build_cmd(
        self, prompt: str,
        resume_session: str | None = None,
        continue_last: bool = False,
    ) -> list[str]:
        cmd = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose"]
        cmd += ["--max-turns", str(self.max_turns)]
        cmd += ["--permission-mode", self.permission_mode]
        if self.max_budget_usd:
            cmd += ["--max-budget-usd", str(self.max_budget_usd)]
        if self.system_prompt:
            cmd += ["--system-prompt", self.system_prompt]
        if self.append_system_prompt:
            cmd += ["--append-system-prompt", self.append_system_prompt]
        if self.allowed_tools:
            cmd += ["--allowedTools", ",".join(self.allowed_tools)]
        if resume_session:
            cmd += ["--resume", resume_session]
        elif continue_last:
            cmd += ["--continue"]
        return cmd

    def stream(
        self, prompt: str,
        resume_session: str | None = None,
        continue_last: bool = False,
    ) -> Generator[StreamEvent, None, AgentResponse]:
        """Stream events from Claude Code. Yields StreamEvents, returns AgentResponse."""
        cmd = self._build_cmd(prompt, resume_session, continue_last)
        env = {**os.environ, "CLAUDECODE": ""}

        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=self.cwd, env=env, text=True,
        )

        response = AgentResponse()
        pending_tool: ToolCall | None = None

        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue

                response.raw_events.append(evt)
                etype = evt.get("type", "")

                if etype == "system":
                    response.session_id = evt.get("session_id", "")

                elif etype == "assistant":
                    msg = evt.get("message", {})
                    for c in msg.get("content", []):
                        ct = c.get("type", "")
                        if ct == "thinking":
                            text = c.get("thinking", "")
                            response.thinking += text
                            yield StreamEvent(type="thinking", content=text)
                        elif ct == "text":
                            text = c.get("text", "")
                            response.text += text
                            yield StreamEvent(type="text", content=text)
                        elif ct == "tool_use":
                            tc = ToolCall(name=c["name"], input=c.get("input", {}))
                            pending_tool = tc
                            response.tool_calls.append(tc)
                            yield StreamEvent(
                                type="tool_use", tool_name=c["name"],
                                tool_input=c.get("input", {}),
                            )

                elif etype == "user":
                    msg = evt.get("message", {})
                    for c in msg.get("content", []):
                        if isinstance(c, dict) and c.get("type") == "tool_result":
                            result_text = str(c.get("content", ""))
                            if pending_tool:
                                pending_tool.result = result_text
                                pending_tool = None
                            yield StreamEvent(
                                type="tool_result", content=result_text,
                            )

                elif etype == "result":
                    response.cost_usd = evt.get("total_cost_usd", 0)
                    response.num_turns = evt.get("num_turns", 0)
                    response.stop_reason = evt.get("stop_reason", "")
                    yield StreamEvent(
                        type="done",
                        metadata={
                            "cost_usd": response.cost_usd,
                            "num_turns": response.num_turns,
                            "stop_reason": response.stop_reason,
                        },
                    )

        finally:
            proc.wait()

        return response

    def run(
        self, prompt: str,
        resume_session: str | None = None,
        continue_last: bool = False,
        on_event: callable | None = None,
    ) -> AgentResponse:
        """Run prompt to completion. Optionally call on_event for each StreamEvent."""
        gen = self.stream(prompt, resume_session, continue_last)
        response = None
        try:
            while True:
                evt = next(gen)
                if on_event:
                    on_event(evt)
        except StopIteration as e:
            response = e.value
        return response or AgentResponse()
