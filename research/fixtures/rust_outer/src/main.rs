use std::ffi::CString;

use pyo3::prelude::*;
use pyo3::types::PyModule;

#[inline(never)]
fn rust_outer() -> PyResult<()> {
    Python::attach(|py| {
        let source = CString::new(include_str!("embedded.py")).expect("valid Python source");
        let file_name = CString::new(concat!(env!("CARGO_MANIFEST_DIR"), "/src/embedded.py"))
            .expect("valid source path");
        PyModule::from_code(py, &source, &file_name, c"pyrust_embedded")?;
        Ok(())
    })
}

fn main() -> PyResult<()> {
    rust_outer()
}
