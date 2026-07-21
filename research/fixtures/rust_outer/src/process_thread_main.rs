use std::{
    env, fs,
    path::{Path, PathBuf},
    process::{Child, Command, ExitCode},
    thread,
    time::{Duration, Instant},
};

const COORDINATOR_TIMEOUT: Duration = Duration::from_secs(30);
const DEFAULT_CHILD_EXIT_TIMEOUT: Duration = Duration::from_secs(45);
const BREAKPOINT_HOLD_TIMEOUT_ENV: &str = "PYRUST_BREAKPOINT_HOLD_TIMEOUT_SECONDS";

struct ChildSpec {
    label: &'static str,
    value: &'static str,
}

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
    let registry =
        env::var("PYRUST_CHILD_REGISTRY").map_err(|_| "PYRUST_CHILD_REGISTRY is required")?;
    let python = env::var("PYRUST_PYTHON").map_err(|_| "PYRUST_PYTHON is required")?;
    let worker = env::var("PYRUST_PROCESS_THREAD_WORKER")
        .map_err(|_| "PYRUST_PROCESS_THREAD_WORKER is required")?;
    let registry = PathBuf::from(registry);
    fs::create_dir_all(&registry)
        .map_err(|error| format!("could not create child registry: {error}"))?;
    remove_release_marker(&registry)?;

    let specs = [
        ChildSpec {
            label: "process-A",
            value: "20",
        },
        ChildSpec {
            label: "process-B",
            value: "40",
        },
    ];
    let mut children = Vec::with_capacity(specs.len());
    for spec in specs {
        match Command::new(&python)
            .arg(&worker)
            .arg(spec.label)
            .arg(spec.value)
            .env("PYRUST_CHILD_REGISTRY", &registry)
            .env("PYTHONUNBUFFERED", "1")
            .spawn()
        {
            Ok(child) => children.push((spec.label, child)),
            Err(error) => {
                terminate_children(&mut children);
                return Err(format!("could not launch {}: {error}", spec.label));
            }
        }
    }

    let child_ids = children
        .iter()
        .map(|(_, child)| child.id())
        .collect::<Vec<_>>();
    let result = coordinate_children(&registry, &child_ids)
        .and_then(|()| wait_for_children(&mut children, breakpoint_hold_timeout()?));
    if let Err(error) = result {
        terminate_children(&mut children);
        return Err(error);
    }
    Ok(())
}

fn breakpoint_hold_timeout() -> Result<Duration, String> {
    let Some(value) = env::var_os(BREAKPOINT_HOLD_TIMEOUT_ENV) else {
        return Ok(DEFAULT_CHILD_EXIT_TIMEOUT);
    };
    let value = value
        .to_str()
        .ok_or_else(|| format!("{BREAKPOINT_HOLD_TIMEOUT_ENV} must be valid UTF-8"))?;
    let seconds = value
        .parse::<u64>()
        .map_err(|_| format!("{BREAKPOINT_HOLD_TIMEOUT_ENV} must be a positive integer"))?;
    if seconds == 0 {
        return Err(format!(
            "{BREAKPOINT_HOLD_TIMEOUT_ENV} must be a positive integer"
        ));
    }
    Ok(Duration::from_secs(seconds))
}

fn coordinate_children(registry: &Path, child_ids: &[u32]) -> Result<(), String> {
    let records = child_ids
        .iter()
        .map(|process_id| registry.join(format!("child-{process_id}.json")))
        .collect::<Vec<_>>();
    wait_for_paths(
        &records,
        COORDINATOR_TIMEOUT,
        "Python children did not register process records",
    )?;

    let attachments = child_ids
        .iter()
        .map(|process_id| registry.join(format!("attached-{process_id}")))
        .collect::<Vec<_>>();
    wait_for_paths(
        &attachments,
        COORDINATOR_TIMEOUT,
        "PyRust coordinator did not attach to all process/thread children",
    )?;
    fs::write(registry.join("release"), "")
        .map_err(|error| format!("could not release child workers: {error}"))?;

    let worker_records = child_ids
        .iter()
        .map(|process_id| registry.join(format!("workers-ready-{process_id}")))
        .collect::<Vec<_>>();
    wait_for_paths(
        &worker_records,
        COORDINATOR_TIMEOUT,
        "Python children did not make both worker threads observable",
    )
}

fn wait_for_paths(paths: &[PathBuf], timeout: Duration, failure: &str) -> Result<(), String> {
    let deadline = Instant::now() + timeout;
    while !paths.iter().all(|path| path.is_file()) {
        if Instant::now() >= deadline {
            return Err(failure.into());
        }
        thread::sleep(Duration::from_millis(20));
    }
    Ok(())
}

fn wait_for_children(
    children: &mut [(&'static str, Child)],
    timeout: Duration,
) -> Result<(), String> {
    let deadline = Instant::now() + timeout;
    let mut finished = vec![false; children.len()];
    while finished.iter().any(|is_finished| !is_finished) {
        for (index, (label, child)) in children.iter_mut().enumerate() {
            if finished[index] {
                continue;
            }
            if let Some(status) = child
                .try_wait()
                .map_err(|error| format!("could not poll {label}: {error}"))?
            {
                if !status.success() {
                    return Err(format!("{label} exited with {status}"));
                }
                finished[index] = true;
            }
        }
        if finished.iter().all(|is_finished| *is_finished) {
            return Ok(());
        }
        if Instant::now() >= deadline {
            return Err("process/thread children did not exit before the fixture timeout".into());
        }
        thread::sleep(Duration::from_millis(20));
    }
    Ok(())
}

fn terminate_children(children: &mut [(&'static str, Child)]) {
    for (_, child) in children.iter_mut() {
        if child.try_wait().ok().flatten().is_none() {
            let _ = child.kill();
        }
    }
    for (_, child) in children.iter_mut() {
        let _ = child.wait();
    }
}

fn remove_release_marker(registry: &Path) -> Result<(), String> {
    let release = registry.join("release");
    match fs::remove_file(&release) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(format!("could not clear stale release marker: {error}")),
    }
}
