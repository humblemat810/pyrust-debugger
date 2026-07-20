use std::{
    ffi::CString,
    future::Future,
    pin::Pin,
    task::{Context, Poll, Waker},
    thread,
};

use pyo3::{prelude::*, types::PyModule};

#[pyfunction]
#[inline(never)]
fn rust_callback() -> PyResult<()> {
    std::hint::black_box(());
    Ok(())
}

struct YieldOnce(bool);

impl Future for YieldOnce {
    type Output = ();

    fn poll(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Self::Output> {
        if self.0 {
            Poll::Ready(())
        } else {
            self.0 = true;
            cx.waker().wake_by_ref();
            Poll::Pending
        }
    }
}

// This is a real Rust async function. Its first await lets the tiny executor
// poll both futures before either enters Python.
#[inline(never)]
async fn rust_outer(label: &'static str, value: i64) -> PyResult<()> {
    YieldOnce(false).await;
    Python::attach(|py| {
        let source =
            CString::new(include_str!("async_embedded.py")).expect("valid Python source");
        let file_name = CString::new(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/src/async_embedded.py"
        ))
        .expect("valid source path");
        let module = PyModule::from_code(py, &source, &file_name, c"pyrust_async")?;
        module.add_function(wrap_pyfunction!(rust_callback, &module)?)?;
        let python_outer = module.getattr("python_outer")?;
        let coroutine = python_outer.call1((label, value))?;
        py.import("asyncio")?.getattr("run")?.call1((coroutine,))?;
        Ok(())
    })
}

#[inline(never)]
async fn async_task(label: &'static str, value: i64) -> PyResult<()> {
    rust_outer(label, value).await
}

fn main() -> PyResult<()> {
    let mut first = Box::pin(async_task("rust-async-A", 20));
    let mut second = Box::pin(async_task("rust-async-B", 40));
    let waker = Waker::noop();
    let mut context = Context::from_waker(waker);
    let mut first_result = None;
    let mut second_result = None;

    while first_result.is_none() || second_result.is_none() {
        if first_result.is_none() {
            if let Poll::Ready(result) = first.as_mut().poll(&mut context) {
                first_result = Some(result);
            }
        }
        if second_result.is_none() {
            if let Poll::Ready(result) = second.as_mut().poll(&mut context) {
                second_result = Some(result);
            }
        }
        thread::yield_now();
    }

    first_result.expect("first future completed")?;
    second_result.expect("second future completed")?;
    Ok(())
}
