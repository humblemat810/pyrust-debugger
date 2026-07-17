import pyrust_native


def python_inner(value: int) -> int:
    return pyrust_native.rust_outer(value)


def python_outer() -> int:
    return python_inner(20)


if __name__ == "__main__":
    print(f"python -> rust result: {python_outer()}")
