use std::ffi::CString;

use pyo3::{prelude::*, types::PyModule};

#[pyfunction]
#[inline(never)]
fn rust_callback() -> PyResult<()> {
    std::hint::black_box(());
    Ok(())
}

// Keep the outer Rust frames live while Python calls back into Rust.
#[inline(never)]
fn rust_outer() -> PyResult<()> {
    Python::attach(|py| {
        // Embedded CPython does not necessarily import sitecustomize during
        // initialization. Load PyRust's opt-in debugpy bootstrap explicitly.
        if std::env::var("PYRUST_DEBUGPY_ENABLE").as_deref() == Ok("1") {
            PyModule::import(py, "sitecustomize")?;
        }
        let source = CString::new(include_str!("embedded.py")).expect("valid Python source");
        let file_name = CString::new(concat!(env!("CARGO_MANIFEST_DIR"), "/src/embedded.py"))
            .expect("valid source path");
        let module = PyModule::from_code(py, &source, &file_name, c"pyrust_embedded")?;
        module.add_function(wrap_pyfunction!(rust_callback, &module)?)?;
        let python_outer = module.getattr("python_outer")?;
        python_outer.call0()?;
        python_outer.call0()?;
        Ok(())
    })
}

fn main() -> PyResult<()> {
    rust_outer()
}
