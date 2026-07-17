import signal


def python_inner() -> None:
    signal.raise_signal(signal.SIGTRAP)


def python_outer() -> None:
    python_inner()


python_outer()
