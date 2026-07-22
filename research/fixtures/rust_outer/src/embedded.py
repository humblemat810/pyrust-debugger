def python_inner() -> None:
    value = 20
    label = "rust-to-python"
    rust_callback()
    after_callback = value + 1
    assert after_callback == 21


def python_outer() -> None:
    value = 21
    python_inner()
    after_inner = value + 1
    assert after_inner == 22
