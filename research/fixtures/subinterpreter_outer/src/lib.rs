use std::ffi::c_longlong;
use std::ptr;

use pyo3_ffi::*;

static mut MODULE_DEF: PyModuleDef = PyModuleDef {
    m_base: PyModuleDef_HEAD_INIT,
    m_name: c"pyrust_subinterp".as_ptr(),
    m_doc: c"Subinterpreter-safe Rust fixture for PyRust.".as_ptr(),
    m_size: 0,
    m_methods: (&raw mut METHODS).cast(),
    m_slots: (&raw mut SLOTS).cast(),
    m_traverse: None,
    m_clear: None,
    m_free: None,
};

static mut METHODS: [PyMethodDef; 2] = [
    PyMethodDef {
        ml_name: c"dispatch_payload".as_ptr(),
        ml_meth: PyMethodDefPointer {
            PyCFunctionFast: dispatch_payload,
        },
        ml_flags: METH_FASTCALL,
        ml_doc: c"Call a Rust leaf from a CPython subinterpreter.".as_ptr(),
    },
    PyMethodDef::zeroed(),
];

static mut SLOTS: [PyModuleDef_Slot; 2] = [
    PyModuleDef_Slot {
        slot: Py_mod_multiple_interpreters,
        value: Py_MOD_PER_INTERPRETER_GIL_SUPPORTED,
    },
    PyModuleDef_Slot {
        slot: 0,
        value: ptr::null_mut(),
    },
];

#[unsafe(no_mangle)]
pub unsafe extern "C" fn PyInit_pyrust_subinterp() -> *mut PyObject {
    unsafe { PyModuleDef_Init(&raw mut MODULE_DEF) }
}

#[inline(never)]
fn calculate_leaf(payload: i64) -> i64 {
    std::hint::black_box(payload + 7)
}

unsafe extern "C" fn dispatch_payload(
    _self: *mut PyObject,
    args: *mut *mut PyObject,
    nargs: Py_ssize_t,
) -> *mut PyObject {
    if nargs != 1 {
        unsafe {
            PyErr_SetString(
                PyExc_TypeError,
                c"dispatch_payload expected one integer".as_ptr(),
            );
        }
        return ptr::null_mut();
    }
    let payload = unsafe { PyLong_AsLongLong(*args) };
    if !unsafe { PyErr_Occurred() }.is_null() {
        return ptr::null_mut();
    }
    let result = calculate_leaf(payload as i64);
    unsafe { PyLong_FromLongLong(result as c_longlong) }
}
