"""Bounded diagnostics for the supported Codex App Server entry point.

This module does not send cloud model turns. Protocol/authentication discovery
is separate from proving that the provider actually disables all action tools.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import tempfile
import time

from .adapters import AdapterError


def discover_codex(executable=None):
    """Resolve an executable file, including the desktop package's launcher.

    The app's Resources/codex-cli path is a directory, not the CLI binary.
    Never scan home directories or authentication files to find a client.
    """
    if executable is not None:
        candidates = [str(executable)]
    else:
        candidates = [shutil.which("codex"),
                      "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex",
                      "/opt/homebrew/bin/codex", "/usr/local/bin/codex"]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(Path(candidate).absolute())
    return None


def _stop(process):
    """Reap the isolated diagnostic process and any children it started."""
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            process.terminate()
    try:
        process.wait(timeout=0.25)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            process.kill()
        process.wait(timeout=0.5)
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            stream.close()


def _protocol_probe(executable, timeout, state_directory):
    # All overrides affect only this child invocation. No global config writes,
    # login, token refresh, thread creation, or model request is performed.
    args = [executable, "--no-daemon", "app-server", "--stdio",
            "--disable", "apps", "--disable", "plugins", "--disable", "hooks",
            "--disable", "shell_tool", "-c", 'web_search="disabled"',
            "-c", "analytics.enabled=false", "-c", "sqlite_home=" + json.dumps(state_directory),
            "-c", "log_dir=" + json.dumps(state_directory)]
    result = {"initialize": "unknown", "auth_kind": "unknown", "outcome": "unknown"}
    process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    selector.register(process.stderr, selectors.EVENT_READ)
    pending = b""
    total_bytes = 0
    deadline = time.monotonic() + timeout

    def send(message):
        process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        process.stdin.flush()

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "localmind-capability-probe", "version": "0.4"},
            "capabilities": {"experimentalApi": True}}})
        while time.monotonic() < deadline:
            if not selector.get_map():
                result["outcome"] = "process_exited"
                result["exit_code"] = process.poll()
                return result
            for key, _ in selector.select(min(0.1, max(0, deadline - time.monotonic()))):
                data = os.read(key.fileobj.fileno(), 65536)
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                total_bytes += len(data)
                if total_bytes > 1024 * 1024:
                    result["outcome"] = "response_limit"
                    return result
                # Never persist or return arbitrary diagnostics, config, emails,
                # credentials, account details, or server stderr.
                if key.fileobj is process.stderr:
                    continue
                pending += data
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    try:
                        message = json.loads(line)
                    except (ValueError, UnicodeDecodeError):
                        result["outcome"] = "invalid_json"
                        return result
                    if not isinstance(message, dict):
                        result["outcome"] = "invalid_message"
                        return result
                    if "method" in message:
                        if "id" in message:
                            # Do not approve or proxy any server-side action.
                            result["outcome"] = "unexpected_server_request"
                            return result
                        continue
                    if message.get("id") == 1:
                        if not isinstance(message.get("result"), dict):
                            result["outcome"] = "initialize_rejected"
                            return result
                        result["initialize"] = "confirmed"
                        send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
                        send({"jsonrpc": "2.0", "id": 2, "method": "account/read",
                              "params": {"refreshToken": False}})
                    elif message.get("id") == 2:
                        payload = message.get("result")
                        if not isinstance(payload, dict):
                            result["outcome"] = "account_read_rejected"
                            return result
                        account = payload.get("account")
                        category = account.get("type") if isinstance(account, dict) else None
                        if category in ("apiKey", "chatgpt", "chatgptAuthTokens"):
                            result["auth_kind"] = category
                        elif account is None:
                            result["auth_kind"] = "not_logged_in"
                        result["outcome"] = "completed"
                        return result
        result["outcome"] = "timeout"
        return result
    except (OSError, ValueError):
        result["outcome"] = "transport_error"
        return result
    finally:
        selector.close()
        _stop(process)


def probe_codex(executable=None, timeout=4.0):
    """Return sanitized, bounded evidence; this is never a cloud acceptance pass."""
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 5:
        raise ValueError("Diagnostic timeout must be greater than zero and at most five seconds")
    path = discover_codex(executable)
    report = {"client": "Codex App Server", "executable": path,
              "cli_version": None, "cli_executable": "unknown", "initialize": "unknown",
              "auth_kind": "unknown", "tool_boundary": "unknown", "cloud_acceptance": "blocked",
              "cloud_model_calls": 0, "credential_files_read_directly": False,
              "global_configuration_modified": False}
    if path is None:
        report["outcome"] = "executable_not_found"
        return report
    try:
        version = subprocess.run([path, "--version"], capture_output=True,
                                 timeout=timeout, check=False)
        match = re.fullmatch(rb"codex-cli ([0-9][A-Za-z0-9.+-]*)\s*", version.stdout)
        if version.returncode != 0 or match is None:
            report["outcome"] = "version_unverified"
            return report
        report["cli_version"] = match.group(1).decode("ascii")
        report["cli_executable"] = "confirmed"
        with tempfile.TemporaryDirectory(prefix="localmind-codex-probe-") as directory:
            report.update(_protocol_probe(path, timeout, directory))
    except subprocess.TimeoutExpired:
        report["outcome"] = "version_timeout"
    except OSError:
        report["outcome"] = "process_unavailable"
    return report


class CodexAppServerAdapter:
    """Reserved answer interface: diagnostic success alone never enables send."""
    name = "codex-app-server-unverified"
    simulated = False

    def __init__(self, executable=None):
        self.executable = executable

    def probe(self, timeout=4.0):
        return probe_codex(self.executable, timeout)

    def send(self, messages, request_id):
        # There is deliberately no caller-controlled bypass boolean/capability
        # report. Implement the verified version-specific boundary first.
        raise AdapterError("CODEX_TOOL_BOUNDARY_UNVERIFIED")
