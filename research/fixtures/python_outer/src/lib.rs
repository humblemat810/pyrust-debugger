use pyo3::prelude::*;

#[pyfunction]
#[inline(never)]
fn rust_inner(value: i64) -> i64 {
    let result = std::hint::black_box(value + 1);
    result * 2
}

#[pyfunction]
#[inline(never)]
fn rust_outer(value: i64) -> i64 {
    rust_inner(value)
}

#[pymodule]
fn pyrust_native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(rust_inner, module)?)?;
    module.add_function(wrap_pyfunction!(rust_outer, module)?)?;
    Ok(())
}
