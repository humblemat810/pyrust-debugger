# 75-Second Demo Narration

Use this as a guide, not a script that must be read word-for-word.

## 0-10 Seconds: Problem

> PyRust is a VS Code debugger prototype for applications that cross between
> CPython and Rust. Normally, a native breakpoint makes it hard to understand
> which language and execution context led here.

Show `lib.rs` with the breakpoint on `rust_inner`.

## 10-25 Seconds: Start

> This launch starts Python worker threads. Each enters a PyO3 Rust extension,
> and Rust creates named worker threads.

Start `PyRust: Python and Rust Threads` and wait for the stop.

## 25-45 Seconds: Mixed Stack

> The built-in Call Stack is still the debugger's authoritative frame view.
> Here we are stopped in `rust_inner`, inside
> `rust_outer_with_rust_threads`.

Show the Call Stack and the selected Rust source line.

## 45-65 Seconds: Process Tree

> PyRust adds this Process Tree because the normal Call Stack is flat across
> native threads. It shows one Python process and its real native threads.
> The named Rust worker is a sibling of the Python worker, not a fake nested
> Python frame, because it is a separate OS thread.

Show and expand **PyRust Process Tree**.

## 65-75 Seconds: Interaction And Limits

> Clicking a Process Tree frame navigates to its source. PyRust currently
> supports Rust breakpoints, live debugpy Python stops, and read-only Python
> recovery inside Rust-owned stops on Linux with CPython 3.14. A Python Step
> Into can hand off to a configured Rust breakpoint; automatic cross-language
> destination discovery is future work.

Click `rust_inner` in the Process Tree, briefly show the amber navigation
decoration, then end the recording.
