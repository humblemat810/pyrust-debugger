def python_inner() -> None:
    value = 20
    label = "rust-to-python"
    rust_callback()


def python_outer() -> None:
    value = 21
    python_inner()
