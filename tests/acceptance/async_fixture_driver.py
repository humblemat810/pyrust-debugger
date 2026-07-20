"""Two asyncio tasks that independently enter the Python-outer Rust fixture."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path


FIXTURE = Path(__file__).resolve().parents[2] / "research" / "fixtures" / "python_outer"
sys.path.insert(0, str(FIXTURE))

import pyrust_native  # noqa: E402


async def async_worker(label: str, value: int, gate: asyncio.Event) -> int:
    task_name = asyncio.current_task().get_name()  # type: ignore[union-attr]
    await gate.wait()
    await asyncio.sleep(0)
    result = pyrust_native.rust_outer(value)
    return result


async def main() -> None:
    gate = asyncio.Event()
    workers = [
        asyncio.create_task(
            async_worker("async-A", 20, gate),
            name="async-A",
        ),
        asyncio.create_task(
            async_worker("async-B", 40, gate),
            name="async-B",
        ),
    ]
    await asyncio.sleep(0)
    gate.set()
    results = await asyncio.gather(*workers)
    print(f"async results: {results}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
