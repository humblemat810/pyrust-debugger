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

#[pyfunction]
#[inline(never)]
fn rust_outer_with_rust_threads(py: Python<'_>, value: i64) -> PyResult<i64> {
    // Release the GIL so multiple Python callers can each create Rust workers.
    let total = py.detach(move || {
        (1_i64..=2)
            .map(|worker| {
                let worker_value = value + worker;
                std::thread::Builder::new()
                    .name(format!("rust-child-{value}-{worker}"))
                    .spawn(move || rust_inner(worker_value))
                    .expect("Rust worker starts")
                    .join()
                    .expect("Rust worker does not panic")
            })
            .sum()
    });
    Ok(total)
}

#[pymodule]
fn pyrust_native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(rust_inner, module)?)?;
    module.add_function(wrap_pyfunction!(rust_outer, module)?)?;
    module.add_function(wrap_pyfunction!(rust_outer_with_rust_threads, module)?)?;
    Ok(())
}
