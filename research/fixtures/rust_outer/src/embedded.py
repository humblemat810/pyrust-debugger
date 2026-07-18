def python_inner() -> None:
    rust_callback()


def python_outer() -> None:
    python_inner()
