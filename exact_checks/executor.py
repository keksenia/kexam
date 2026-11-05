"""Isolated local executor. Trusted code only; requests cannot name programs.

The supervisor enforces the wall deadline and kills/reaps an overrun worker.
Memory is a peak-RSS watchdog in the worker, not a portable OS hard allocation
cap: macOS does not provide the required RLIMIT_AS/RSS semantics. A result whose
observed peak exceeds the profile is never accepted. See README for this limit.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import threading
import time

from .rational import CheckError, ResourceLimit

ROOT = Path(__file__).resolve().parent.parent
TRANSPORT_BYTES = 8 * 1024 * 1024
OUTPUT_BYTES = 1024 * 1024
EXECUTION_PROFILE = "exact-worker-rss-watchdog-1"


class ExecutionFailure(CheckError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class IsolatedExecutor:
    """One warmed worker shared by all eight operations in one Pipeline.

    Failed workers are not restarted by execute: no implicit retry. A later
    prepare may start a replacement for a different request; the Pipeline keeps
    the same work deadline and never repeats the failed request automatically.
    """
    def __init__(self):
        self.process = None
        self.buffer = bytearray()
        self.lock = threading.RLock()
        self.fingerprint = None

    def _read(self, deadline):
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            while b"\n" not in self.buffer:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise ExecutionFailure("timeout")
                block = os.read(self.process.stdout.fileno(), 16384)
                if not block:
                    raise ExecutionFailure("worker_failed")
                self.buffer.extend(block)
                if len(self.buffer) > OUTPUT_BYTES:
                    raise ResourceLimit("Ответ рабочего процесса превышает предел транспорта.")
            line, _, tail = self.buffer.partition(b"\n")
            self.buffer = bytearray(tail)
            try:
                return json.loads(line)
            except (ValueError, UnicodeError):
                raise ExecutionFailure("invalid_worker_response") from None

    def prepare(self, timeout_ms, fingerprint):
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                if self.fingerprint != fingerprint:
                    self.close()
                    raise ExecutionFailure("configuration_changed")
                return
            self.close()
            deadline = time.monotonic() + max(0, min(timeout_ms, 2000)) / 1000
            if deadline <= time.monotonic():
                raise ExecutionFailure("timeout")
            try:
                self.process = subprocess.Popen(
                    [sys.executable, "-B", "-m", "exact_checks.worker"],
                    cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True,
                    env={k: v for k, v in os.environ.items()
                         if k not in {"PYTHONPATH", "PYTHONSTARTUP", "PYTHONINSPECT"}},
                )
                os.set_blocking(self.process.stdin.fileno(), False)
                ready = self._read(deadline)
                if ready.get("error"):
                    raise ExecutionFailure(ready["error"])
                if ready.get("ready") is not True or ready.get("source_sha256") != fingerprint:
                    raise ExecutionFailure("configuration_changed")
                self.fingerprint = fingerprint
            except BaseException:
                self.close()
                raise

    def execute(self, operation_id, request, result, timeout_ms, fingerprint, *, source_input=None):
        with self.lock:
            if self.process is None or self.process.poll() is not None:
                raise ExecutionFailure("worker_unavailable")
            if fingerprint != self.fingerprint:
                self.close()
                raise ExecutionFailure("configuration_changed")
            deadline = time.monotonic() + max(0, timeout_ms) / 1000
            try:
                payload = json.dumps(dict(operation=operation_id, request=request, result=result,
                                          source_input=source_input, timeout_ms=timeout_ms), ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode() + b"\n"
                if len(payload) > TRANSPORT_BYTES:
                    raise ResourceLimit("Контекст рабочего процесса превышает 8 МиБ.")
                # Nonblocking writes are part of the same deadline as reading,
                # calculation, formatting and validation of the returned fact.
                offset = 0
                with selectors.DefaultSelector() as selector:
                    selector.register(self.process.stdin, selectors.EVENT_WRITE)
                    while offset < len(payload):
                        remaining = deadline - time.monotonic()
                        if remaining <= 0 or not selector.select(remaining):
                            raise ExecutionFailure("timeout")
                        try:
                            offset += os.write(self.process.stdin.fileno(), payload[offset:])
                        except BlockingIOError:
                            continue
                response = self._read(deadline)
                if time.monotonic() > deadline:
                    raise ExecutionFailure("timeout")
                if response.get("error"):
                    raise ExecutionFailure(response["error"])
                if response.get("source_sha256") != self.fingerprint or not isinstance(response.get("fact"), dict):
                    raise ExecutionFailure("invalid_worker_response")
                if response.get("peak_rss_bytes", 2**63) > 64 * 1024 * 1024:
                    raise ResourceLimit("Рабочий процесс превысил 64 МиБ RSS.")
                fact = response["fact"]
                evidence = json.loads(fact["code_check"]["evidence"])
                evidence["execution"] = dict(profile=EXECUTION_PROFILE, isolation="subprocess",
                                             deadline_ms=timeout_ms, peak_rss_bytes=response["peak_rss_bytes"],
                                             memory=dict(limit_mib=64, mode="peak_rss_watchdog", poll_ms=5,
                                                         short_overshoot_possible=True))
                fact["code_check"]["evidence"] = json.dumps(evidence, ensure_ascii=False, sort_keys=True,
                                                            separators=(",", ":"), allow_nan=False)
                if time.monotonic() > deadline:
                    raise ExecutionFailure("timeout")
                return fact
            except ExecutionFailure as exc:
                # A rejected context is an isolated input error. The worker has
                # completed that request and is still ready for independent work.
                if exc.code not in {"invalid_operation_context", "invalid_input"}:
                    self.close()
                raise
            except BaseException:
                # Never leave a timed-out calculation running in the background.
                self.close()
                raise

    def close(self):
        with self.lock:
            process, self.process = self.process, None
            self.buffer.clear()
            self.fingerprint = None
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait()
                for stream in (process.stdin, process.stdout):
                    if stream is not None:
                        stream.close()
