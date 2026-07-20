import asyncio


async def python_inner(label: str, value: int) -> None:
    worker_label = label
    worker_value = value
    task_name = asyncio.current_task().get_name()
    await asyncio.sleep(0)
    rust_callback()


async def python_outer(label: str, value: int) -> None:
    await python_inner(label, value)
