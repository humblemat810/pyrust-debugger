"""Python payload executed inside the acceptance fixture's subinterpreter."""

from __future__ import annotations

import importlib.util
import os


library = os.environ["PYRUST_SUBINTERP_LIBRARY"]
spec = importlib.util.spec_from_file_location("pyrust_subinterp", library)
if spec is None or spec.loader is None:
    raise RuntimeError(f"cannot load subinterpreter fixture from {library}")
pyrust_subinterp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pyrust_subinterp)


def finalize_subinterpreter(value: int) -> int:
    final_label = "python-leaf"
    return value


def subinterpreter_worker(value: int) -> int:
    interpreter_label = "secondary-interpreter"
    native_result = pyrust_subinterp.dispatch_payload(value)
    return finalize_subinterpreter(native_result)


subinterpreter_worker(35)
