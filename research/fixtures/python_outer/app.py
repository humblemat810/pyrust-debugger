import pyrust_native


def python_inner(value: int) -> int:
    label = "python-to-rust"
    return pyrust_native.rust_outer(value)


def python_outer() -> int:
    value = 20
    return python_inner(value)


if __name__ == "__main__":
    print(f"python -> rust result: {python_outer()}")
