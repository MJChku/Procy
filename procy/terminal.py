"""Transparent PTY proxy — sit between human and CLI, sniffing/modifying I/O.

Like `script` command: Claude Code sees a real terminal, TUI works perfectly.
Procy logs everything flowing through and can intercept prompts.
"""
from __future__ import annotations

import errno
import fcntl
import os
import re
import select
import signal
import struct
import sys
import termios
import time
import tty
from typing import Callable

from .io import write_bytes

class ProxySession:
    """Transparent PTY proxy between the user's terminal and a CLI process."""

    def __init__(
        self,
        cmd: list[str],
        cwd: str | None = None,
        env: dict | None = None,
        on_output: Callable[[bytes], None] | None = None,
        on_input: Callable[[bytes], bytes | None] | None = None,
        on_resize: Callable[[int, int], None] | None = None,
    ):
        """
        Args:
            cmd: Command to run (e.g. ["claude"])
            cwd: Working directory
            env: Environment overrides
            on_output: Called with each chunk of output from the CLI (for logging).
                       Does NOT modify output — just observes.
            on_input: Called with each chunk of input from the user.
                      Return modified bytes to change what gets sent, or None to pass through.
        """
        self.cmd = cmd
        self.cwd = cwd or os.getcwd()
        self.on_output = on_output
        self.on_input = on_input
        self.on_resize = on_resize
        self._env = {**os.environ, **(env or {})}
        self._env.pop("CLAUDECODE", None)
        self.child_pid: int | None = None
        self.master_fd: int | None = None

    def run(self) -> int:
        """Run the proxy session. Blocks until the child exits. Returns exit code."""
        # Create PTY
        master_fd, slave_fd = os.openpty()
        self.master_fd = master_fd

        # Match the user's terminal size
        if sys.stdin.isatty():
            _copy_terminal_size(sys.stdin.fileno(), master_fd)
            if self.on_resize:
                size = _read_terminal_size(sys.stdin.fileno())
                if size:
                    try:
                        self.on_resize(size[1], size[0])
                    except Exception:
                        pass

        # Fork
        pid = os.fork()
        if pid == 0:
            # ── Child: run the CLI on the slave PTY ──
            os.close(master_fd)
            os.setsid()

            # Set slave as controlling terminal
            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
            os.dup2(slave_fd, 0)
            os.dup2(slave_fd, 1)
            os.dup2(slave_fd, 2)
            if slave_fd > 2:
                os.close(slave_fd)

            os.chdir(self.cwd)
            os.execvpe(self.cmd[0], self.cmd, self._env)
            # Never reached
        else:
            # ── Parent: proxy I/O ──
            os.close(slave_fd)
            self.child_pid = pid
            exit_code = self._proxy_loop(master_fd)
            return exit_code

    def _proxy_loop(self, master_fd: int) -> int:
        """Forward bytes between stdin↔master_fd, sniffing/modifying."""
        old_settings = None
        old_handler = None

        try:
            # Put user's terminal in raw mode (pass all keys through)
            if sys.stdin.isatty():
                old_settings = termios.tcgetattr(sys.stdin.fileno())
                tty.setraw(sys.stdin.fileno())

            # Handle terminal resize
            def handle_sigwinch(signum, frame):
                if sys.stdin.isatty():
                    _copy_terminal_size(sys.stdin.fileno(), master_fd)
                    if self.on_resize:
                        size = _read_terminal_size(sys.stdin.fileno())
                        if size:
                            try:
                                self.on_resize(size[1], size[0])
                            except Exception:
                                pass
                    # Forward SIGWINCH to child
                    if self.child_pid:
                        os.kill(self.child_pid, signal.SIGWINCH)

            old_handler = signal.signal(signal.SIGWINCH, handle_sigwinch)

            # Main I/O loop
            while True:
                try:
                    fds = [sys.stdin.fileno(), master_fd]
                    ready, _, _ = select.select(fds, [], [], 0.1)
                except (select.error, ValueError):
                    break

                if sys.stdin.fileno() in ready:
                    # User typed something
                    try:
                        data = os.read(sys.stdin.fileno(), 4096)
                    except OSError:
                        break
                    if not data:
                        break

                    # Let on_input intercept/modify
                    if self.on_input:
                        modified = self.on_input(data)
                        if modified is not None:
                            data = modified

                    # Forward to child
                    try:
                        os.write(master_fd, data)
                    except OSError:
                        break

                if master_fd in ready:
                    # Child produced output
                    try:
                        data = os.read(master_fd, 4096)
                    except OSError:
                        break
                    if not data:
                        break

                    # Forward to user's terminal
                    write_bytes(data)

                    # Let on_output observe after forwarding so observers can
                    # redraw overlays/prompts on top of freshly printed output.
                    if self.on_output:
                        try:
                            self.on_output(data)
                        except Exception:
                            pass  # Don't let logging errors break the session

                # Check if child is still alive
                try:
                    wpid, status = os.waitpid(self.child_pid, os.WNOHANG)
                    if wpid != 0:
                        if os.WIFEXITED(status):
                            return os.WEXITSTATUS(status)
                        return 1
                except ChildProcessError:
                    return 0

        finally:
            # Restore terminal
            if old_settings is not None:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_settings)
            if old_handler is not None:
                signal.signal(signal.SIGWINCH, old_handler)
            try:
                os.close(master_fd)
            except OSError:
                pass

        return 0


def _copy_terminal_size(src_fd: int, dst_fd: int):
    """Copy terminal dimensions from src to dst."""
    try:
        size = fcntl.ioctl(src_fd, termios.TIOCGWINSZ, b"\x00" * 8)
        fcntl.ioctl(dst_fd, termios.TIOCSWINSZ, size)
    except OSError:
        pass


def _read_terminal_size(fd: int) -> tuple[int, int] | None:
    """Return terminal size as (rows, cols)."""
    try:
        size = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\x00" * 8)
        rows, cols, _, _ = struct.unpack("HHHH", size)
        return rows, cols
    except OSError:
        return None


# ── Convenience ──

def run_proxy(
    cmd: list[str] = None,
    cwd: str | None = None,
    on_output: Callable[[bytes], None] | None = None,
    on_input: Callable[[bytes], bytes | None] | None = None,
    on_resize: Callable[[int, int], None] | None = None,
) -> int:
    """Run a transparent proxy session."""
    cmd = cmd or ["claude"]
    proxy = ProxySession(
        cmd=cmd,
        cwd=cwd,
        on_output=on_output,
        on_input=on_input,
        on_resize=on_resize,
    )
    return proxy.run()


if __name__ == "__main__":
    # Simple demo: proxy claude and log all output to a file
    log = open("/tmp/procy_capture.log", "wb")

    def capture_output(data: bytes):
        log.write(data)
        log.flush()

    print("Starting procy transparent proxy over claude...")
    print("Everything works normally — procy is invisible, just logging.")
    print("─" * 60)
    exit_code = run_proxy(on_output=capture_output)
    log.close()
    print(f"\nSession ended (exit {exit_code}). Log: /tmp/procy_capture.log")
