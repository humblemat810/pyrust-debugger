# Reusing the Python-to-Rust Thread Launch

## Status

The `pyrust` launch type is an alpha prototype. It supports a CPython process
that enters a debug-built PyO3 extension and stops at Rust source breakpoints.
Set `pyrustPythonDebug: true` to enable Python source breakpoints and normal
debugpy evaluation in Python-owned stops. Rust-owned stops retain their
read-only snapshot behavior. Cross-language stepping and a production
installer for arbitrary workspaces remain unsupported.

## Copyable Configuration

Copy [launch.python-rust-threads.jsonc](../templates/launch.python-rust-threads.jsonc)
into the application's `.vscode/launch.json`, then replace:

- `program` with the application Python executable;
- `args[0]` with the Python entry script or module launcher;
- `cwd` with the application working directory.

Do not copy the fixture's `preLaunchTask`: it builds this repository's sample
PyO3 extension, not the application's native module.

Copy [settings.pyrust.jsonc](../templates/settings.pyrust.jsonc) into the
application's `.vscode/settings.json` and replace every absolute path. The
adapter is currently loaded from a PyRust debugger checkout, so the two
`pyrust.*Path` settings are required outside this repository.

## Required Environment

1. Linux x86_64 with CodeLLDB installed.
2. The application Python process can import its PyO3 extension.
3. The Rust extension is built without stripping debug symbols.
4. The Python version used by `pyrust.pythonPath` is CPython 3.14 and has this
   repository's dependencies installed.
5. `pyrust.codelldbPath` and `pyrust.liblldbPath` are both set when CodeLLDB
   cannot be found automatically. They must refer to the same CodeLLDB build.

## Thread Semantics

If a Python thread directly calls a Rust function, the stopped Rust frame can
show its Python caller in the mixed Call Stack.

If that Rust function creates another Rust OS thread, the new Rust thread is a
separate execution context. Its Process Tree entry is a sibling of the Python
thread under the process, not a child of that Python thread. Its Call Stack
must not invent a Python caller from the original thread.

## Example In This Repository

`PyRust: Python and Rust Threads` is a concrete version of this template. It
starts two Python worker threads. Each enters `rust_outer_with_rust_threads`,
which creates two named Rust worker threads. Set the breakpoint at
`research/fixtures/python_outer/src/lib.rs:6` to stop those Rust workers.
