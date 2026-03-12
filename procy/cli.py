#!/usr/bin/env python3
"""procy CLI — system-level entry point.

Usage (from anywhere):
    procy                  # wraps `claude` with procy, starts UI on :7861
    procy --agent codex    # wrap codex instead
    procy --no-ui          # skip auto-starting the monitor UI
    procy --no-tunnel      # skip SSH tunnel to EXP07 for Qwen
"""
from __future__ import annotations

import argparse
import atexit
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import re
from pathlib import Path

# ── Paths ──

PROCY_HOME = Path.home() / ".procy"
DEFAULT_DB = PROCY_HOME / "traces.db"
TUNNEL_PID_FILE = PROCY_HOME / "tunnel.pid"
UI_PID_FILE = PROCY_HOME / "ui.pid"

# SSH tunnel config
QWEN_SSH_HOST = "EXP07"        # from ~/.ssh/config
QWEN_REMOTE_PORT = 8000        # vllm on EXP07
QWEN_LOCAL_PORT = 18000        # local tunnel port (high to avoid conflicts)


def ensure_home():
    PROCY_HOME.mkdir(parents=True, exist_ok=True)


# ── ANSI helpers ──

def _info(msg: str):
    sys.stderr.write(f"\033[35m[procy]\033[0m {msg}\n")
    sys.stderr.flush()

def _dim(msg: str):
    sys.stderr.write(f"\033[2m  {msg}\033[0m\n")
    sys.stderr.flush()

def _err(msg: str):
    sys.stderr.write(f"\033[31m[procy]\033[0m {msg}\n")
    sys.stderr.flush()


# ── SSH Tunnel ──

def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def start_tunnel() -> subprocess.Popen | None:
    """Start SSH tunnel: localhost:QWEN_LOCAL_PORT -> EXP07:8000."""
    if _port_in_use(QWEN_LOCAL_PORT):
        _dim(f"tunnel already active on :{QWEN_LOCAL_PORT}")
        return None

    _info(f"opening SSH tunnel to {QWEN_SSH_HOST}:{QWEN_REMOTE_PORT} -> localhost:{QWEN_LOCAL_PORT}")
    proc = subprocess.Popen(
        [
            "ssh", "-N", "-L",
            f"{QWEN_LOCAL_PORT}:127.0.0.1:{QWEN_REMOTE_PORT}",
            QWEN_SSH_HOST,
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=30",
            "-o", "ConnectTimeout=10",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    # Wait a moment and check it's alive
    time.sleep(2)
    if proc.poll() is not None:
        err = proc.stderr.read().decode() if proc.stderr else ""
        _err(f"SSH tunnel failed: {err.strip()}")
        return None

    # Verify the port is now open
    if not _port_in_use(QWEN_LOCAL_PORT):
        _err("tunnel started but port not reachable — check SSH config")
        proc.kill()
        return None

    TUNNEL_PID_FILE.write_text(str(proc.pid))
    _dim(f"tunnel pid={proc.pid}")
    return proc


def stop_tunnel(proc: subprocess.Popen | None):
    if proc and proc.poll() is None:
        proc.terminate()
        proc.wait(timeout=5)
    TUNNEL_PID_FILE.unlink(missing_ok=True)


# ── UI Server ──

def start_ui(db_path: str, port: int = 7861) -> subprocess.Popen | None:
    """Start the monitor UI as a background process."""
    if _port_in_use(port):
        _dim(f"UI already running on :{port}")
        return None

    _info(f"starting monitor UI on http://localhost:{port}")
    # Run the UI module directly
    proc = subprocess.Popen(
        [sys.executable, "-c",
         f"from procy.ui import main; import sys; sys.argv = ['procy-ui', '--db', '{db_path}', '--port', '{port}']; main()"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1)
    if proc.poll() is not None:
        _err("UI failed to start")
        return None

    UI_PID_FILE.write_text(str(proc.pid))
    _dim(f"UI pid={proc.pid}")
    return proc


def stop_ui(proc: subprocess.Popen | None):
    if proc and proc.poll() is None:
        proc.terminate()
        proc.wait(timeout=5)
    UI_PID_FILE.unlink(missing_ok=True)


# ── Strip ANSI ──

def _strip_ansi(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    return re.sub(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07|\r", "", text)


# ── Procy Core (inlined to avoid circular import issues) ──

from .terminal import ProxySession
from .store import TraceStore


class Procy:
    def __init__(
        self,
        agent_cmd: list[str],
        cwd: str | None = None,
        db_path: str = str(DEFAULT_DB),
        qwen_url: str | None = None,
    ):
        self.agent_cmd = agent_cmd
        self.cwd = cwd or os.getcwd()
        self.store = TraceStore(db_path)
        self.qwen_url = qwen_url

        self.session_id: str | None = None
        self.turn_num = 0
        self.last_human_prompt: str = ""
        self._input_buffer = b""
        self._output_buffer = b""
        self._evolving = False
        self._stop_evolve = False
        self._proxy: ProxySession | None = None
        self._capture_output = False
        self._captured_output = b""
        self._saved_terminal = None

    def _on_input(self, data: bytes) -> bytes | None:
        self._input_buffer += data

        # Check for Enter key
        if b"\r" in data or b"\n" in data:
            line = self._input_buffer.decode("utf-8", errors="replace").strip()
            self._input_buffer = b""

            if line.startswith("!"):
                # Procy command — already swallowed keystrokes, now handle
                # Print newline to move past the echoed "!command" text
                sys.stderr.write("\r\n")
                sys.stderr.flush()
                self._handle_command(line)
                return b""  # swallow the Enter too

            if line:
                self.turn_num += 1
                self.last_human_prompt = line
                self.store.log_turn(self.session_id, self.turn_num, "human", line)
            return None  # pass through unchanged

        # While typing: if buffer starts with '!', swallow the keystroke
        # and echo it locally so the agent never sees it
        buf_str = self._input_buffer.decode("utf-8", errors="replace")
        if buf_str.startswith("!"):
            # Handle backspace (0x7f) while in ! mode
            if data == b"\x7f":
                if len(self._input_buffer) > 1:
                    # Remove the backspace byte AND the char before it
                    text = self._input_buffer.decode("utf-8", errors="replace")
                    text = text[:-1]  # remove the \x7f
                    if text:
                        text = text[:-1]  # remove char before it
                    self._input_buffer = text.encode("utf-8")
                    # Erase character on screen: backspace + space + backspace
                    sys.stderr.write("\b \b")
                    sys.stderr.flush()
                else:
                    # Backspaced past '!' — clear buffer, nothing to send
                    self._input_buffer = b""
                    sys.stderr.write("\b \b")
                    sys.stderr.flush()
                return b""  # swallow

            # Handle Ctrl-C: cancel the ! command
            if data == b"\x03":
                self._input_buffer = b""
                sys.stderr.write("^C\r\n")
                sys.stderr.flush()
                return b""  # swallow

            # Echo the character locally (agent doesn't see it)
            ch = data.decode("utf-8", errors="replace")
            sys.stderr.write(ch)
            sys.stderr.flush()
            return b""  # swallow — don't send to agent

        return None  # not a ! command, pass through normally

    def _on_output(self, data: bytes):
        if self._capture_output:
            self._captured_output += data
        text = _strip_ansi(data)
        if text.strip():
            self.store.log_turn(
                self.session_id, self.turn_num, "agent_chunk", text[:2000],
            )

    def _handle_command(self, line: str):
        parts = line.split()
        cmd = parts[0].lower()
        if cmd == "!help":
            self._show_help()
        elif cmd == "!evolve":
            n = int(parts[1]) if len(parts) > 1 else 3
            self._start_evolve(n)
        elif cmd == "!correct":
            self._do_correct()
        elif cmd == "!status":
            self._show_status()
        elif cmd == "!history":
            self._show_history()
        elif cmd == "!train":
            self._do_train()
        elif cmd == "!stop":
            self._stop_evolve = True
            _info("stopping evolve...")
        else:
            _err(f"unknown command: {cmd}. Type !help")

    def _show_help(self):
        _info("Commands:")
        _dim("!evolve N    — auto-generate N prompt variants via Qwen")
        _dim("!correct     — correct the last prompt (logs for SFT training)")
        _dim("!status      — session info")
        _dim("!history     — prompt/correction history")
        _dim("!train       — export SFT training pairs as JSONL")
        _dim("!stop        — stop ongoing evolve")
        _dim("!help        — this message")

    def _show_status(self):
        turns = self.store.get_turns(self.session_id) if self.session_id else []
        corrections = self.store.get_corrections(self.session_id) if self.session_id else []
        _info(f"session: {(self.session_id or 'none')[:8]}")
        _dim(f"turns: {len(turns)}  corrections: {len(corrections)}")
        _dim(f"last prompt: {self.last_human_prompt[:80]}")
        _dim(f"db: {self.store.db_path}")
        if self.qwen_url:
            _dim(f"qwen: {self.qwen_url}")

    def _show_history(self):
        turns = self.store.get_turns(self.session_id) if self.session_id else []
        corrections = self.store.get_corrections(self.session_id) if self.session_id else []
        _info("History:")
        for t in turns:
            if t["role"] == "human":
                _dim(f"\033[32m[human t{t['turn_num']}]\033[0m {t['content'][:120]}")
        if corrections:
            _dim("\033[33mCorrections:\033[0m")
            for c in corrections:
                _dim(f"  original: {c['original_prompt'][:80]}")
                _dim(f"  corrected: {c['corrected_prompt'][:80]}")

    def _do_correct(self):
        if not self.last_human_prompt:
            _err("no prompt to correct")
            return
        _info(f"Original: {self.last_human_prompt[:200]}")
        _info("Type corrected prompt (or empty to cancel):")
        import termios, tty
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._saved_terminal)
        try:
            corrected = input("\033[33m[correct]> \033[0m")
        except (EOFError, KeyboardInterrupt):
            corrected = ""
        finally:
            tty.setraw(sys.stdin.fileno())
        if not corrected.strip():
            _dim("cancelled")
            return
        self.store.log_correction(
            self.session_id, self.turn_num,
            self.last_human_prompt, corrected.strip(),
        )
        _info("Correction logged. Use !train to export.")

    def _do_train(self):
        pairs = self.store.get_training_pairs()
        if not pairs:
            _err("no corrections to train from")
            return
        out_path = Path(self.cwd) / "procy_train.jsonl"
        with open(out_path, "w") as f:
            for p in pairs:
                f.write(json.dumps({
                    "instruction": p["original_prompt"],
                    "output": p["corrected_prompt"],
                }) + "\n")
        _info(f"exported {len(pairs)} pairs to {out_path}")

    def _start_evolve(self, n: int):
        if not self.last_human_prompt:
            _err("no previous prompt to evolve from")
            return
        if not self.qwen_url:
            _err("no Qwen available. Need --tunnel or --qwen-url")
            return
        self._evolving = True
        self._stop_evolve = False
        t = threading.Thread(target=self._evolve_loop, args=(n,), daemon=True)
        t.start()

    def _evolve_loop(self, n: int):
        import urllib.request
        corrections = self.store.get_corrections(self.session_id) or []
        evolve_history = self.store.get_evolve_runs(self.session_id) or []
        for i in range(1, n + 1):
            if self._stop_evolve:
                _info(f"evolve stopped at {i-1}/{n}")
                break
            _info(f"evolve {i}/{n}")
            new_prompt = self._generate_prompt(corrections, evolve_history)
            if not new_prompt:
                _err("failed to generate prompt, skipping")
                continue
            _dim(f"injecting: {new_prompt[:120]}")
            self.turn_num += 1
            self.last_human_prompt = new_prompt
            self.store.log_turn(self.session_id, self.turn_num, "procy", new_prompt)
            self.store.log_evolve(self.session_id, i, new_prompt, None, None, None, "procy")
            self._inject_prompt(new_prompt)
            time.sleep(2)
            self._wait_for_agent_idle(timeout=120)
            evolve_history.append({"iteration": i, "prompt": new_prompt, "score": None, "source": "procy"})
            _info(f"evolve {i}/{n} done")
            time.sleep(1)
        self._evolving = False
        _info("evolve complete. Use !correct to fix any, !train to export.")

    def _generate_prompt(self, corrections: list, history: list) -> str | None:
        import urllib.request
        meta_parts = [
            "You are a prompt engineer. Generate ONE improved prompt for a coding agent.",
            f"Base prompt:\n{self.last_human_prompt}\n",
        ]
        if corrections:
            meta_parts.append("Human corrections:")
            for c in corrections[-5:]:
                meta_parts.append(f"  Before: {c['original_prompt'][:200]}")
                meta_parts.append(f"  After: {c['corrected_prompt'][:200]}")
        if history:
            meta_parts.append("Previous evolve attempts:")
            for h in history[-5:]:
                meta_parts.append(f"  {h['prompt'][:150]}")
        meta_parts.append("\nOutput ONLY the new prompt. No explanation, no markdown, just the prompt text.")
        payload = json.dumps({
            "model": "Qwen/Qwen2.5-32B-Instruct",
            "messages": [{"role": "user", "content": "\n".join(meta_parts)}],
            "max_tokens": 1000,
            "temperature": 0.7,
        }).encode()
        try:
            url = f"{self.qwen_url.rstrip('/')}/v1/chat/completions"
            req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                return data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            _err(f"Qwen error: {e}")
            return None

    def _inject_prompt(self, prompt: str):
        if self._proxy and self._proxy.master_fd:
            os.write(self._proxy.master_fd, (prompt + "\r").encode())

    def _wait_for_agent_idle(self, timeout: float = 120):
        start = time.time()
        while time.time() - start < timeout:
            if self._stop_evolve:
                return
            time.sleep(1)
            recent = self._captured_output[-500:] if self._captured_output else b""
            text = recent.decode("utf-8", errors="replace")
            if "\u276f" in text or "\n> " in text:
                time.sleep(1)
                return
        _dim("timeout waiting for agent")

    def run(self) -> int:
        self.session_id = self.store.new_session(goal=f"procy @ {os.getcwd()}")

        _info(f"session {self.session_id[:8]}")
        _dim(f"agent: {' '.join(self.agent_cmd)}")
        _dim(f"db: {self.store.db_path}")
        if self.qwen_url:
            _dim(f"qwen: {self.qwen_url}")
        _dim("type !help for procy commands")
        sys.stderr.write("─" * 60 + "\n")
        sys.stderr.flush()

        import termios
        if sys.stdin.isatty():
            self._saved_terminal = termios.tcgetattr(sys.stdin.fileno())

        self._capture_output = True
        self._proxy = ProxySession(
            cmd=self.agent_cmd,
            cwd=self.cwd,
            on_output=self._on_output,
            on_input=self._on_input,
        )
        exit_code = self._proxy.run()
        self.store.end_session(self.session_id)
        _info(f"session ended. traces: {self.store.db_path}")
        return exit_code


# ── CLI entry point ──

def main():
    ensure_home()

    parser = argparse.ArgumentParser(
        prog="procy",
        description="procy — transparent prompt proxy for AI agents",
    )
    parser.add_argument("--agent", default="claude",
                        help="agent CLI command (default: claude)")
    parser.add_argument("--cwd", default=None,
                        help="working directory for the agent")
    parser.add_argument("--db", default=str(DEFAULT_DB),
                        help=f"trace database (default: {DEFAULT_DB})")
    parser.add_argument("--qwen-url", default=None,
                        help="Qwen API URL (default: auto-tunnel to EXP07)")
    parser.add_argument("--no-ui", action="store_true",
                        help="don't auto-start the monitor UI")
    parser.add_argument("--no-tunnel", action="store_true",
                        help="don't auto-start SSH tunnel to EXP07")
    parser.add_argument("--ui-port", type=int, default=7862,
                        help="monitor UI port (default: 7862)")
    args = parser.parse_args()

    tunnel_proc = None
    ui_proc = None

    def cleanup():
        stop_tunnel(tunnel_proc)
        stop_ui(ui_proc)

    atexit.register(cleanup)

    # SSH tunnel for Qwen
    qwen_url = args.qwen_url
    if not args.no_tunnel and not qwen_url:
        tunnel_proc = start_tunnel()
        if tunnel_proc or _port_in_use(QWEN_LOCAL_PORT):
            qwen_url = f"http://127.0.0.1:{QWEN_LOCAL_PORT}"

    # Monitor UI
    if not args.no_ui:
        ui_proc = start_ui(args.db, args.ui_port)

    # Run procy
    agent_cmd = args.agent.split()
    procy = Procy(
        agent_cmd=agent_cmd,
        cwd=args.cwd,
        db_path=args.db,
        qwen_url=qwen_url,
    )

    try:
        exit_code = procy.run()
    finally:
        cleanup()

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
