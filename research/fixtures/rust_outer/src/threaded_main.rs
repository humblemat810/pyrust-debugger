use std::{
    ffi::CString,
    sync::{Arc, Barrier},
    thread,
};

use pyo3::{prelude::*, types::PyModule};

#[pyfunction]
#[inline(never)]
fn rust_callback() -> PyResult<()> {
    std::hint::black_box(());
    Ok(())
}

// Each Rust worker owns one Python callback path. The barrier guarantees both
// workers exist before either can reach the native breakpoint.
#[inline(never)]
fn rust_outer(label: String, value: i64, ready: Arc<Barrier>) -> PyResult<()> {
    ready.wait();
    Python::attach(|py| {
        let source =
            CString::new(include_str!("threaded_embedded.py")).expect("valid Python source");
        let file_name = CString::new(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/src/threaded_embedded.py"
        ))
        .expect("valid source path");
        let module = PyModule::from_code(py, &source, &file_name, c"pyrust_threaded")?;
        module.add_function(wrap_pyfunction!(rust_callback, &module)?)?;
        let python_outer = module.getattr("python_outer")?;
        python_outer.call1((label, value))?;
        Ok(())
    })
}

fn main() -> PyResult<()> {
    let ready = Arc::new(Barrier::new(3));
    let workers = [("rust-worker-A", 20_i64), ("rust-worker-B", 40_i64)]
        .into_iter()
        .map(|(label, value)| {
            let ready = Arc::clone(&ready);
            thread::Builder::new()
                .name(label.to_owned())
                .spawn(move || rust_outer(label.to_owned(), value, ready))
                .expect("worker starts")
        })
        .collect::<Vec<_>>();

    ready.wait();
    for worker in workers {
        worker.join().expect("worker did not panic")?;
    }
    Ok(())
}
