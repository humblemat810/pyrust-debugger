"""Run a PyO3 call from a CPython subinterpreter on a dedicated OS thread."""

from __future__ import annotations

import _interpreters
import threading
from pathlib import Path


def main() -> None:
    interpreter = _interpreters.create()
    errors: list[BaseException] = []
    payload = Path(__file__).with_name("subinterpreter_payload.py").resolve()
    script = (
        f"_path = {str(payload)!r}\n"
        "with open(_path, encoding='utf-8') as _stream:\n"
        "    _source = _stream.read()\n"
        "exec(compile(_source, _path, 'exec'))\n"
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
