"""Private fixed-code worker for executor.py; no dynamic program execution."""
from __future__ import annotations

import json
import resource
import signal
import sys
import time

from .operations import ExactOperation, source_fingerprint
from .protocol import LIMITS
from .executor import EXECUTION_PROFILE, TRANSPORT_BYTES, OUTPUT_BYTES

MEMORY_BYTES = LIMITS["memory_mib_worker"] * 1024 * 1024


class MemoryExceeded(Exception):
    pass


def peak_rss_bytes():
    # getrusage: bytes on Darwin, KiB on Linux. Unsupported platforms fail closed.
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return usage
    if sys.platform.startswith("linux"):
        return usage * 1024
    raise MemoryExceeded()


def check_memory(*_):
    if peak_rss_bytes() > MEMORY_BYTES:
        raise MemoryExceeded()


def emit(value):
    data = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    if len(data) > OUTPUT_BYTES:
        data = b'{"error":"resource_limit"}'
    sys.stdout.buffer.write(data + b"\n")
    sys.stdout.buffer.flush()


def main():
    fingerprint = source_fingerprint()
    try:
        check_memory()
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        signal.signal(signal.SIGALRM, check_memory)
    except (MemoryExceeded, OSError, ValueError, AttributeError):
        emit({"error": "resource_limit"})
        return
    emit(dict(ready=True, source_sha256=fingerprint, execution_profile=EXECUTION_PROFILE,
              peak_rss_bytes=peak_rss_bytes()))
    while True:
        line = sys.stdin.buffer.readline(TRANSPORT_BYTES + 1)
        if not line:
            return
        if len(line) > TRANSPORT_BYTES or not line.endswith(b"\n"):
            emit({"error": "resource_limit"})
            return
        started = time.monotonic()
        try:
            signal.setitimer(signal.ITIMER_REAL, 0.005, 0.005)
            check_memory()
            message = json.loads(line)
            operation = ExactOperation(message["operation"])
            fact = operation.execute(message["request"], message["result"],
                                     source_input=message.get("source_input"))
            check_memory()
            response = dict(fact=fact, source_sha256=fingerprint, peak_rss_bytes=peak_rss_bytes())
            # The parent's deadline additionally covers transport and serialization.
            if (time.monotonic() - started) * 1000 > message["timeout_ms"]:
                response = {"error": "timeout"}
            emit(response)
        except MemoryExceeded:
            signal.setitimer(signal.ITIMER_REAL, 0)
            emit({"error": "resource_limit"})
            return
        except Exception as exc:
            signal.setitimer(signal.ITIMER_REAL, 0)
            code = getattr(exc, "code", "invalid_operation_context")
            if code not in {"timeout", "resource_limit", "invalid_input", "invalid_operation_context"}:
                code = "worker_failed"
            emit({"error": code})
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)


if __name__ == "__main__":
    main()
