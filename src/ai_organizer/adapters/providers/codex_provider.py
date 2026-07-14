from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_organizer.domain.prompts import CompiledPrompt

from .base import AnalysisResult, ProviderError, parse_findings


@dataclass(frozen=True, slots=True)
class CodexRuntime:
    command: tuple[str, ...]
    source: str
    version: str
    compatible: bool


class CodexRuntimeDetector:
    def detect(self) -> CodexRuntime | None:
        command = shutil.which("codex")
        if command:
            try:
                result = subprocess.run(
                    [command, "--version"], capture_output=True, text=True, timeout=10, check=True
                )
                help_result = subprocess.run(
                    [command, "app-server", "--help"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                compatible = help_result.returncode == 0 and "--listen" in help_result.stdout
                return CodexRuntime((command,), "installed", result.stdout.strip(), compatible)
            except (OSError, subprocess.SubprocessError):
                pass
        try:
            from codex_cli_bin import bundled_codex_path

            bundled = bundled_codex_path()
            result = subprocess.run(
                [str(bundled), "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            return CodexRuntime((str(bundled),), "bundled-runtime", result.stdout.strip(), True)
        except (ImportError, OSError, subprocess.SubprocessError):
            return None


class CodexProvider:
    name = "codex"

    def __init__(self, runtime: CodexRuntime, workspace_path: str | None = None) -> None:
        self.runtime = runtime
        self.workspace_path = workspace_path

    def estimate(self, prompt: CompiledPrompt) -> dict[str, int]:
        return {
            "input_characters": len(prompt.text),
            "estimated_input_tokens": len(prompt.text) // 4,
        }

    def analyze(self, prompt: CompiledPrompt) -> AnalysisResult:
        return self._analyze_app_server(prompt)

    def _analyze_app_server(self, prompt: CompiledPrompt) -> AnalysisResult:
        environment = os.environ.copy()
        config_arguments = [
            argument for value in self._config_overrides() for argument in ("-c", value)
        ]
        with tempfile.TemporaryDirectory(prefix="aiorganizer-codex-") as directory:
            process = subprocess.Popen(
                [
                    *self.runtime.command,
                    "app-server",
                    *config_arguments,
                    "--strict-config",
                ],
                cwd=directory,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self._rpc(
                    process,
                    1,
                    "initialize",
                    {
                        "clientInfo": {
                            "name": "aiorganizer",
                            "title": "AIOrganizer",
                            "version": "0.1.0",
                        }
                    },
                )
                self._notify(process, "initialized", {})
                thread = self._rpc(
                    process,
                    2,
                    "thread/start",
                    {
                        "cwd": directory,
                        "sandbox": "read-only",
                        "approvalPolicy": "never",
                    },
                )
                thread_id = thread["result"]["thread"]["id"]
                self._rpc(
                    process,
                    3,
                    "turn/start",
                    {"threadId": thread_id, "input": [{"type": "text", "text": prompt.text}]},
                )
                final_text = self._read_final(process)
                return AnalysisResult(parse_findings(final_text))
            except Exception as error:
                raise ProviderError(
                    f"Codex app-server analysis failed: {type(error).__name__}"
                ) from error
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()

    def _config_overrides(self) -> tuple[str, ...]:
        overrides = [
            'web_search="disabled"',
            "sandbox_workspace_write.network_access=false",
            "mcp_servers={}",
        ]
        if self.workspace_path:
            packaged = Path(sys.executable).stem.casefold().startswith("aiorganizer")
            launcher_args = (
                ["--mcp", "--workspace", self.workspace_path]
                if packaged
                else [
                    "-m",
                    "ai_organizer.mcp_server.server",
                    "--workspace",
                    self.workspace_path,
                ]
            )
            overrides.extend(
                [
                    f"mcp_servers.aiorganizer.command={json.dumps(sys.executable)}",
                    "mcp_servers.aiorganizer.args=" + json.dumps(launcher_args),
                ]
            )
        return tuple(overrides)

    @staticmethod
    def _rpc(
        process: subprocess.Popen[str], request_id: int, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        assert process.stdin and process.stdout
        process.stdin.write(
            json.dumps({"id": request_id, "method": method, "params": params}) + "\n"
        )
        process.stdin.flush()
        while line := process.stdout.readline():
            message = json.loads(line)
            if message.get("id") == request_id:
                return message
        raise EOFError("Codex app-server stopped")

    @staticmethod
    def _notify(process: subprocess.Popen[str], method: str, params: dict[str, Any]) -> None:
        assert process.stdin
        process.stdin.write(json.dumps({"method": method, "params": params}) + "\n")
        process.stdin.flush()

    @staticmethod
    def _read_final(process: subprocess.Popen[str]) -> str:
        assert process.stdout
        fragments: list[str] = []
        while line := process.stdout.readline():
            message = json.loads(line)
            method = message.get("method", "")
            params = message.get("params", {})
            if method == "item/agentMessage/delta":
                fragments.append(str(params.get("delta", "")))
            if method == "turn/completed":
                return "".join(fragments)
        raise EOFError("Codex turn stopped")
