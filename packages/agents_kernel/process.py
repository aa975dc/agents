"""Subprocess execution, output redaction, and shared timestamps (stdlib only)."""
import datetime
import os
import re
import signal
import subprocess
import tempfile

from agents_kernel.digest import sha256_bytes

OUTPUT_LIMIT = 65536
SECRET_PATTERNS = (
    re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)\b(?:[A-Za-z0-9_.-]*(?:secret|token|password|passwd|api[_-]?key|private[_-]?key)[A-Za-z0-9_.-]*)"
               r"(?:\s*[:=]\s*|_)[A-Za-z0-9._~+/=-]{4,}"),
    re.compile(r"\b(?:sk|ghp|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{12,}\b"),
)


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def redact_output(value):
    raw = value if isinstance(value, bytes) else str(value).encode("utf-8", errors="replace")
    decoded = raw[:OUTPUT_LIMIT].decode("utf-8", errors="replace")
    cleaned = decoded
    for pattern in SECRET_PATTERNS:
        cleaned = pattern.sub("[REDACTED]", cleaned)
    return {"output": cleaned, "output_sha256": sha256_bytes(raw),
            "output_redacted": cleaned != decoded, "truncated": len(raw) > OUTPUT_LIMIT}


def run_argv(argv, cwd, timeout, timeout_message):
    result = {"argv": argv, "started_at": now(), "executed": False, "exit_code": None,
              "timed_out": False, "termination_confirmed": True}
    error = ""
    with tempfile.TemporaryFile() as output:
        process = None
        try:
            process = subprocess.Popen(argv, cwd=str(cwd), stdin=subprocess.DEVNULL,
                                       stdout=output, stderr=subprocess.STDOUT, shell=False,
                                       start_new_session=(os.name == "posix"))
            result["executed"] = True
            result["exit_code"] = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            result.update(timed_out=True, termination_confirmed=False)
            error = timeout_message
            if process is not None:
                try:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGTERM)
                    else:
                        process.terminate()
                    process.wait(timeout=1)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    pass
                finally:
                    try:
                        if os.name == "posix":
                            os.killpg(process.pid, signal.SIGKILL)
                        elif process.poll() is None:
                            process.kill()
                    except ProcessLookupError:
                        pass
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        pass
        except (OSError, ValueError) as exc:
            error = str(exc)
        output.seek(0)
        body = output.read(OUTPUT_LIMIT + 1)
    combined = (error + "\n").encode() + body if error else body
    result.update(finished_at=now(), **redact_output(combined))
    return result
