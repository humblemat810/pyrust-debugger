"""Run a PyO3 call from a CPython subinterpreter on a dedicated OS thread."""

from __future__ import annotations

import _interpreters
import os
import threading
from pathlib import Path


def main() -> None:
    interpreter = _interpreters.create()
    errors: list[BaseException] = []
    library = os.environ["PYRUST_SUBINTERP_LIBRARY"]
    source = f"""
import importlib.util

spec = importlib.util.spec_from_file_location("pyrust_subinterp", {library!r})
pyrust_subinterp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pyrust_subinterp)

def finalize_subinterpreter(value):
    final_label = "python-leaf"
    return value

def subinterpreter_worker(value):
    interpreter_label = "secondary-interpreter"
    native_result = pyrust_subinterp.dispatch_payload(value)
    return finalize_subinterpreter(native_result)

subinterpreter_worker(35)
"""
    script = (
        f"exec(compile({source!r}, {str(Path(__file__).resolve())!r}, 'exec'))"
    )

    def run() -> None:
        try:
            result = _interpreters.exec(interpreter, script)
            if result is not None:
                raise RuntimeError(result.formatted)
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(
        target=run,
        name="pyrust-subinterpreter",
    )
    worker.start()
    worker.join()
    _interpreters.destroy(interpreter)
    if errors:
        raise errors[0]


if __name__ == "__main__":
    main()
