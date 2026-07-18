# Research Fixtures

These programs began as debugger research fixtures. They now also drive the
fixture-bound ADR 0002 and ADR 0003 acceptance proofs.

## Python outer, Rust inner

```text
app.py::python_outer
  -> app.py::python_inner
     -> pyrust_native::rust_outer
        -> pyrust_native::rust_inner
```

Build and run:

```bash
source "$HOME/.cargo/env"
PYO3_PYTHON="$PWD/.venv/bin/python" \
  .venv/bin/maturin develop \
  --manifest-path research/fixtures/python_outer/Cargo.toml
.venv/bin/python research/fixtures/python_outer/app.py
```

## Rust outer, Python inner

```text
main
  -> rust_outer
     -> embedded.py::python_outer
        -> embedded.py::python_inner
           -> rust_callback
```

Build and run:

```bash
source "$HOME/.cargo/env"
PYO3_PYTHON="$PWD/.venv/bin/python" \
  cargo build --manifest-path research/fixtures/rust_outer/Cargo.toml

PY_LIBDIR=$(.venv/bin/python -c \
  'import sysconfig; print(sysconfig.get_config_var("LIBDIR"))')
LD_LIBRARY_PATH="$PY_LIBDIR" \
  research/fixtures/rust_outer/target/debug/rust-outer-python-inner
```

The embedded Python path invokes `rust_callback` twice so acceptance can verify
two deterministic stop epochs.

## CodeLLDB DAP evidence

After installing the CodeLLDB platform package, capture each case with:

```bash
.venv/bin/python research/tools/codelldb_dap_probe.py python-outer
.venv/bin/python research/tools/codelldb_dap_probe.py rust-outer
```

The probe is a diagnostic DAP client. It launches CodeLLDB, waits for the
fixture stop, requests native threads and frames, and invokes the CPython 3.14
remote unwinder while CodeLLDB still owns the stopped process. It does not
merge frames or implement a debugger.

To verify that CodeLLDB exposes LLDB scripted frames through ordinary DAP:

```bash
.venv/bin/python research/tools/codelldb_dap_probe.py \
  python-outer --mock-frame-provider
```

This prepends one hard-coded, clearly labeled Python source frame. It tests only
the presentation mechanism; it does not inspect CPython.
