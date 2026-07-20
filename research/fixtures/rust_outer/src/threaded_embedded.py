def python_inner(label: str, value: int) -> None:
    worker_label = label
    worker_value = value
    rust_callback()


def python_outer(label: str, value: int) -> None:
    python_inner(label, value)
