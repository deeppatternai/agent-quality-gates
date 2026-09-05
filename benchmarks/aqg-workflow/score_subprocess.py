#!/usr/bin/env python3
"""Run one hidden scorer inside the WS-7 macOS sandbox.

This process loads the hidden checks before it ever imports model-written code.
Each call to the model function is executed by a separate worker process; the
trusted checker receives only a JSON value (or an exception), never a callable
that can mutate the checker's interpreter. The parent imports and unlinks the
checks before the worker starts. Worker stdout is captured, so only this trusted
parent can write the JSON result protocol to stdout.
"""
from __future__ import annotations

import importlib.util
import contextlib
import json
import os
import resource
import signal
import subprocess
import sys
import tempfile
import builtins
import datetime
from pathlib import Path


def _load(path: Path):
    spec = importlib.util.spec_from_file_location("_ws7_score_" + path.stem, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_WORKER = r'''
import importlib.util
import contextlib
import json
import resource
import sys
import datetime
from pathlib import Path

def load(path):
    spec = importlib.util.spec_from_file_location("_ws7_solution", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def pack(value):
    if isinstance(value, datetime.datetime):
        return {"__ws7_type__": "datetime", "iso": value.isoformat()}
    if isinstance(value, datetime.date):
        return {"__ws7_type__": "date", "iso": value.isoformat()}
    if isinstance(value, tuple):
        return {"__ws7_type__": "tuple", "items": [pack(item) for item in value]}
    if isinstance(value, list):
        return [pack(item) for item in value]
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("solution returned a dictionary with a non-string key")
        return {key: pack(item) for key, item in value.items()}
    return value

def unpack(value):
    if isinstance(value, list):
        return [unpack(item) for item in value]
    if isinstance(value, dict):
        if value.get("__ws7_type__") == "tuple" and isinstance(value.get("items"), list):
            return tuple(unpack(item) for item in value["items"])
        if value.get("__ws7_type__") == "date" and isinstance(value.get("iso"), str):
            return datetime.date.fromisoformat(value["iso"])
        if value.get("__ws7_type__") == "datetime" and isinstance(value.get("iso"), str):
            return datetime.datetime.fromisoformat(value["iso"])
        return {key: unpack(item) for key, item in value.items()}
    return value

try:
    resource.setrlimit(resource.RLIMIT_CPU, (2, 2))
    resource.setrlimit(resource.RLIMIT_FSIZE, (1_000_000, 1_000_000))
except BaseException as exc:
    result = {"ok": False, "kind": "harness", "error_type": type(exc).__name__, "error": str(exc)}
else:
    try:
        # Correct solutions occasionally print debugging output. Keep that output
        # out of the JSON protocol stream, including module-import side effects;
        # direct fd writes still fail closed as invalid worker output.
        with contextlib.redirect_stdout(sys.stderr):
            fn = getattr(load(Path(sys.argv[1])), sys.argv[2])
            value = fn(*[unpack(item) for item in json.loads(sys.argv[3])])
            packed = pack(value)
        result = {"ok": True, "value": packed}
    except BaseException as exc:
        builtin_exception_types = [
            cls.__name__ for cls in type(exc).__mro__
            if cls.__module__ == "builtins" and issubclass(cls, Exception)
        ]
        result = {
            "ok": False, "kind": "solution", "error_type": type(exc).__name__,
            "builtin_exception_types": builtin_exception_types, "error": str(exc),
        }
print(json.dumps(result, sort_keys=True))
'''


class _HarnessFailure(BaseException):
    """Scorer infrastructure failure that task checks must not misclassify."""


class _SolutionFailure(BaseException):
    """A worker-level model-solution fault that remains an ITT defect."""


def _kill_worker_group(pid: int) -> None:
    """Reap a one-shot worker and any descendants it attempted to leave behind."""
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _target(solution: Path, func_name: str):
    """Return a checker-safe callable backed by a one-shot worker process."""
    def invoke(*args):
        try:
            encoded_args = json.dumps([_pack(item) for item in args])
        except (TypeError, ValueError) as exc:
            raise _HarnessFailure(f"checker passed non-JSON arguments: {exc}") from exc
        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stdout_file, \
                tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stderr_file:
            try:
                process = subprocess.Popen(
                    [sys.executable, "-I", "-c", _WORKER, str(solution), func_name, encoded_args],
                    cwd=str(solution.parent),
                    stdout=stdout_file,
                    stderr=stderr_file,
                    text=True,
                    start_new_session=True,
                )
                process.communicate(timeout=2)
            except subprocess.TimeoutExpired as exc:
                _kill_worker_group(process.pid)
                process.communicate()
                raise _SolutionFailure("solution worker timed out") from exc
            except OSError as exc:
                raise _HarnessFailure(f"solution worker could not start: {exc}") from exc
            finally:
                if "process" in locals() and process.poll() is None:
                    _kill_worker_group(process.pid)
                    process.communicate()
                elif "process" in locals():
                    # The leader may have exited while a model-created child is
                    # still in its dedicated group.  The just-created PGID is
                    # safe to reap immediately; absent groups raise harmlessly.
                    _kill_worker_group(process.pid)
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout = stdout_file.read(1_000_001)
            stderr = stderr_file.read(1_000_001)
        if process.returncode != 0:
            raise _SolutionFailure(f"solution worker exited {process.returncode}: {stderr[-200:]}")
        try:
            response = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise _SolutionFailure("solution worker emitted invalid JSON") from exc
        if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
            raise _SolutionFailure("solution worker response has an invalid shape")
        if not response["ok"]:
            if response.get("kind") == "harness":
                raise _HarnessFailure(
                    "solution worker resource setup failed: "
                    f"{response.get('error_type', 'unknown')}: {response.get('error', '')}"
                )
            if response.get("kind") != "solution":
                raise _HarnessFailure("solution worker reported an invalid failure kind")
            error_class = RuntimeError
            candidates = response.get("builtin_exception_types")
            if isinstance(candidates, list):
                for name in candidates:
                    candidate = getattr(builtins, name, None)
                    if isinstance(candidate, type) and issubclass(candidate, Exception):
                        error_class = candidate
                        break
            raise error_class(str(response.get("error", "solution worker failed")))
        return _unpack(response.get("value"))
    return invoke


def _pack(value):
    if isinstance(value, datetime.datetime):
        return {"__ws7_type__": "datetime", "iso": value.isoformat()}
    if isinstance(value, datetime.date):
        return {"__ws7_type__": "date", "iso": value.isoformat()}
    if isinstance(value, tuple):
        return {"__ws7_type__": "tuple", "items": [_pack(item) for item in value]}
    if isinstance(value, list):
        return [_pack(item) for item in value]
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("checker passed a dictionary with a non-string key")
        return {key: _pack(item) for key, item in value.items()}
    return value


def _unpack(value):
    if isinstance(value, list):
        return [_unpack(item) for item in value]
    if isinstance(value, dict):
        if value.get("__ws7_type__") == "tuple" and isinstance(value.get("items"), list):
            return tuple(_unpack(item) for item in value["items"])
        if value.get("__ws7_type__") == "date" and isinstance(value.get("iso"), str):
            return datetime.date.fromisoformat(value["iso"])
        if value.get("__ws7_type__") == "datetime" and isinstance(value.get("iso"), str):
            return datetime.datetime.fromisoformat(value["iso"])
        return {key: _unpack(item) for key, item in value.items()}
    return value


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 3:
        raise SystemExit("usage: score_subprocess.py SOLUTION CHECKS FUNCTION")
    solution = Path(args[0])
    checks = Path(args[1])
    func_name = args[2]
    try:
        # Load the checks before the model worker exists, then remove the only
        # on-disk copy. `run_checks` keeps the trusted module object alive in
        # this parent, while the worker gets only the solution path.
        sys.dont_write_bytecode = True
        module = _load(checks)
        run_checks = module.run_checks
        # Optional per-check contract (2026-08-28). A task that has not been
        # converted simply omits it and the legacy failures-only path stands;
        # the wire protocol therefore stays additive and old ledgers remain
        # readable. `checks` absent means "not reported", never "no checks ran".
        run_all = getattr(module, "run_all_checks", None)
        checks.unlink()
        target = _target(solution, func_name)
        if run_all is None:
            failures = run_checks(target)
            result = {"errored": False, "error": None, "failures": list(failures)}
        else:
            # Transport the report RAW and derive nothing here. An earlier
            # revision built `failures` and `checks` in this process with
            # `str()`/`bool()` casts, which made a truthy non-bool `ok` (say
            # "False") record a FAILING check as passed — on the paid
            # collection path, while the in-process path rejected it. Audit
            # 39542cac, four voices of four. Validation and derivation now
            # happen in exactly one place, `run.normalize_check_report`, so
            # the two paths cannot diverge again.
            result = {
                "errored": False, "error": None,
                "report": [list(item) if isinstance(item, (list, tuple)) else item
                           for item in run_all(target)],
            }
    except _SolutionFailure as exc:  # task checks must not erase a timeout/crash
        result = {"errored": True, "error": f"solution failure: {exc}", "failures": []}
    except _HarnessFailure as exc:
        result = {"harness_failed": True, "errored": True, "error": f"harness failure: {exc}", "failures": []}
    except Exception as exc:  # aqg: top-level boundary
        result = {"harness_failed": True, "errored": True, "error": f"harness failure: {type(exc).__name__}: {exc}", "failures": []}
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
