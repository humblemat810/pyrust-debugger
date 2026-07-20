use std::{
    env,
    fs,
    path::Path,
    process::{Command, ExitCode},
    thread,
    time::{Duration, Instant},
};

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(message) => {
            eprintln!("{message}");
            ExitCode::FAILURE
        }
    }
}

fn run() -> Result<(), String> {
    let registry = env::var("PYRUST_CHILD_REGISTRY")
        .map_err(|_| "PYRUST_CHILD_REGISTRY is required")?;
    let python = env::var("PYRUST_PYTHON").map_err(|_| "PYRUST_PYTHON is required")?;
    let worker = env::var("PYRUST_PROCESS_WORKER")
        .map_err(|_| "PYRUST_PROCESS_WORKER is required")?;
    let labels = [("process-A", "20"), ("process-B", "40")];
    let mut children = Vec::new();

    for (label, value) in labels {
        let child = Command::new(&python)
            .arg(&worker)
            .arg(label)
            .arg(value)
            .env("PYRUST_CHILD_REGISTRY", &registry)
            .spawn()
            .map_err(|error| format!("could not launch {label}: {error}"))?;
        children.push(child);
    }

    let registry_path = Path::new(&registry);
    let deadline = Instant::now() + Duration::from_secs(20);
    loop {
        let attached = fs::read_dir(registry_path)
            .map_err(|error| format!("could not read child registry: {error}"))?
            .filter_map(Result::ok)
            .filter(|entry| entry.file_name().to_string_lossy().starts_with("attached-"))
            .count();
        if attached == children.len() {
            break;
        }
        if Instant::now() >= deadline {
            return Err("PyRust coordinator did not attach to all Rust-parent children".into());
        }
        thread::sleep(Duration::from_millis(20));
    }
    fs::write(registry_path.join("release"), "")
        .map_err(|error| format!("could not release child workers: {error}"))?;

    for mut child in children {
        let status = child
            .wait()
            .map_err(|error| format!("could not wait for child: {error}"))?;
        if !status.success() {
            return Err(format!("child exited with {status}"));
        }
    }
    Ok(())
}
