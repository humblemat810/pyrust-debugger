"""Black-box proof for the Rust-parent process and native-thread tree."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from typing import Any

from .dap_support import (
    DapClient,
    DapError,
    PYTHON,
    ROOT,
    RUST_SOURCE,
    launch_arguments,
    proxy_command,
)
from .run_acceptance import initialize
from .run_thread_acceptance import _evaluate_value, _locals


RUST_FIXTURE = ROOT / "research" / "fixtures" / "rust_outer"
_configured_target = os.environ.get("CARGO_TARGET_DIR")
CARGO_TARGET_DIR = (
    Path(_configured_target) if _configured_target else RUST_FIXTURE / "target"
)
if not CARGO_TARGET_DIR.is_absolute():
    CARGO_TARGET_DIR = ROOT / CARGO_TARGET_DIR
RUST_BINARY = CARGO_TARGET_DIR / "debug" / "rust-outer-python-process-threads"
WORKER = ROOT / "tests" / "acceptance" / "process_thread_worker.py"

CRITERIA = (
    "AC-PTM-01",
    "AC-PTM-02",
    "AC-PTM-03",
    "AC-PTM-04",
    "AC-PTM-05",
    "AC-PTM-06",
    "AC-PTM-07",
)

CHILD_LABELS = {"process-A": 20, "process-B": 40}
TREE_TIMEOUT_SECONDS = 30.0


def _start_fixture(registry: Path) -> tuple[DapClient, dict[str, Any]]:
    client = DapClient(proxy_command())
    initialize(client)
    launch = client.send(
        "launch",
        launch_arguments(
            program=RUST_BINARY,
            args=[],
        )
        | {
            "env": {
                "PYRUST_CHILD_REGISTRY": str(registry),
                "PYRUST_PYTHON": str(PYTHON),
                "PYRUST_PROCESS_THREAD_WORKER": str(WORKER),
            },
            "pyrustChildRegistryPath": str(registry),
            "pyrustProcessMode": "children",
            "pyrustThreadMode": "single",
        },
    )
    client.event("initialized", timeout=10)
    breakpoint_request = client.send(
        "setBreakpoints",
        {
            "source": {"path": str(RUST_SOURCE)},
            "breakpoints": [{"line": 6}],
            "sourceModified": False,
        },
    )
    client.response(breakpoint_request, timeout=10)
    configuration = client.send("configurationDone")
    client.response(configuration, timeout=10)
    client.response(launch, timeout=15)
    return client, client.event("stopped", timeout=TREE_TIMEOUT_SECONDS)


def _read_records(registry: Path) -> dict[int, dict[str, Any]]:
    records: dict[int, dict[str, Any]] = {}
    for path in sorted(registry.glob("child-*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        process_id = payload.get("processId", payload.get("pid"))
        if not isinstance(process_id, int) or process_id <= 0:
            raise DapError(f"child record has no positive process ID: {payload}")
        if process_id in records:
            raise DapError(f"duplicate child process record: {process_id}")
        records[process_id] = payload
    return records


def _wait_for_records(registry: Path) -> dict[int, dict[str, Any]]:
    deadline = time.monotonic() + TREE_TIMEOUT_SECONDS
    while True:
        records = _read_records(registry)
        if len(records) == 2 and {item.get("label") for item in records.values()} == set(
            CHILD_LABELS
        ):
            if all(
                isinstance(item.get("threads"), list)
                and len(item["threads"]) == 2
                and (registry / f"workers-ready-{process_id}").is_file()
                for process_id, item in records.items()
            ):
                return records
        if time.monotonic() >= deadline:
            raise DapError(f"child records did not stabilize: {records}")
        time.sleep(0.05)


def _process_tree(client: DapClient) -> list[dict[str, Any]]:
    request = client.send("pyrust/processTree")
    body = client.response(request, timeout=10).get("body", {})
    processes = body.get("processes")
    if not isinstance(processes, list) or not all(
        isinstance(process, dict) for process in processes
    ):
        raise DapError(f"process-tree payload was malformed: {processes}")
    return processes


def _wait_for_tree(
    client: DapClient,
    predicate: Any,
    *,
    timeout: float = TREE_TIMEOUT_SECONDS,
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    latest: list[dict[str, Any]] = []
    while True:
        latest = _process_tree(client)
        if predicate(latest):
            return latest
        if time.monotonic() >= deadline:
            raise DapError(f"process tree did not reach expected state: {latest}")
        time.sleep(0.05)


def _records_by_label(records: dict[int, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(record["label"]): record
        for record in records.values()
        if isinstance(record.get("label"), str)
    }


def _expected_threads(record: dict[str, Any]) -> dict[int, str]:
    threads = record.get("threads")
    if not isinstance(threads, list) or len(threads) != 2:
        raise DapError(f"child record did not publish two worker threads: {record}")
    result: dict[int, str] = {}
    for thread in threads:
        if not isinstance(thread, dict):
            raise DapError(f"child worker record was malformed: {thread}")
        thread_id = thread.get("threadId")
        name = thread.get("name")
        if (
            not isinstance(thread_id, int)
            or thread_id <= 0
            or not isinstance(name, str)
            or not name
        ):
            raise DapError(f"child worker identity was malformed: {thread}")
        if thread_id in result:
            raise DapError(f"child published duplicate worker TID: {record}")
        result[thread_id] = name
    return result


def _assert_tree_shape(
    tree: list[dict[str, Any]],
    records: dict[int, dict[str, Any]],
    parent_process_id: int,
    *,
    require_threads: bool,
) -> dict[int, dict[str, Any]]:
    by_pid = {
        process.get("processId"): process
        for process in tree
        if isinstance(process.get("processId"), int)
    }
    if parent_process_id not in by_pid:
        raise DapError(f"process tree omitted Rust parent {parent_process_id}: {tree}")
    parent = by_pid[parent_process_id]
    if parent.get("parentProcessId") is not None:
        raise DapError(f"Rust parent was nested under another process: {parent}")
    if parent.get("label") != "Rust parent process":
        raise DapError(f"Rust parent label was not readable: {parent}")
    if parent.get("role") != "Rust parent process":
        raise DapError(f"Rust parent role was not readable: {parent}")
    if "rust-outer-python-process-threads" not in str(parent.get("command", "")):
        raise DapError(f"Rust parent command summary was missing: {parent}")

    expected_child_ids = set(records)
    if not expected_child_ids.issubset(by_pid):
        raise DapError(f"process tree omitted child processes: {tree}")
    for process in by_pid.values():
        if any(key in process for key in ("children", "tasks", "futures", "awaits")):
            raise DapError(f"process tree invented a task or child payload: {process}")
        if not isinstance(process.get("isStopped"), bool):
            raise DapError(f"process stopped state was malformed: {process}")
        threads = process.get("threads")
        if not isinstance(threads, list):
            raise DapError(f"process thread list was malformed: {process}")
        for thread in threads:
            if not isinstance(thread, dict) or any(
                key in thread for key in ("children", "tasks", "futures", "awaits")
            ):
                raise DapError(f"thread payload was not a direct leaf: {thread}")
            if not isinstance(thread.get("threadId"), int) or not isinstance(
                thread.get("name"), str
            ):
                raise DapError(f"thread identity was malformed: {thread}")
            if not isinstance(thread.get("isStopped"), bool):
                raise DapError(f"thread stopped state was malformed: {thread}")

    for process_id, record in records.items():
        process = by_pid[process_id]
        label = record.get("label")
        if process.get("parentProcessId") != parent_process_id:
            raise DapError(f"child {process_id} had the wrong parent: {process}")
        if process.get("label") != label:
            raise DapError(f"child label was not preserved: {process}")
        if process.get("role") != "Python child process":
            raise DapError(f"child role was not preserved: {process}")
        command = str(process.get("command", ""))
        if "process_thread_worker.py" not in command or str(label) not in command:
            raise DapError(f"child command summary was not durable: {process}")
        if require_threads:
            expected = _expected_threads(record)
            actual = {
                thread.get("threadId"): thread.get("name")
                for thread in process["threads"]
            }
            if not set(expected).issubset(actual):
                raise DapError(
                    f"child {label} did not expose both native worker TIDs: "
                    f"{actual!r} missing {set(expected) - set(actual)!r}"
                )
            if any(
                actual[thread_id] != expected[thread_id]
                for thread_id in expected
            ):
                raise DapError(
                    f"child {label} did not preserve worker names: {actual!r}"
                )
    return by_pid


def _inspect_stop(
    client: DapClient,
    stopped: dict[str, Any],
    records: dict[int, dict[str, Any]],
) -> tuple[int, int, int]:
    body = stopped.get("body") or {}
    process_id = body.get("systemProcessId")
    thread_id = body.get("threadId")
    if not isinstance(process_id, int) or process_id not in records:
        raise DapError(f"stopped event had an unknown child process: {stopped}")
    expected_threads = _expected_threads(records[process_id])
    if not isinstance(thread_id, int) or thread_id not in expected_threads:
        raise DapError(
            f"stopped event had an unknown worker TID for {process_id}: {stopped}"
        )

    threads_request = client.send("threads")
    threads = client.response(threads_request, timeout=10).get("body", {}).get(
        "threads"
    )
    if not isinstance(threads, list) or not any(
        item.get("id") == thread_id for item in threads if isinstance(item, dict)
    ):
        raise DapError(f"stopped worker was not exposed by DAP threads: {threads}")

    stack_request = client.send(
        "stackTrace",
        {"threadId": thread_id, "startFrame": 0, "levels": 80},
    )
    frames = client.response(stack_request, timeout=15).get("body", {}).get(
        "stackFrames"
    )
    if not isinstance(frames, list):
        raise DapError(f"mixed stack was malformed: {frames}")
    leaves = [str(frame.get("name", "")).rsplit("::", 1)[-1] for frame in frames]
    if leaves[:2] != ["rust_inner", "rust_outer"]:
        raise DapError(f"worker lost the Rust stack prefix: {leaves[:8]}")
    rust_frame = next(
        (frame for frame in frames if str(frame.get("name", "")).endswith("rust_inner")),
        None,
    )
    if not isinstance(rust_frame, dict):
        raise DapError(f"mixed stack did not contain rust_inner: {leaves}")
    rust_source = (rust_frame.get("source") or {}).get("path")
    if not isinstance(rust_source, str) or not rust_source.endswith(
        "research/fixtures/python_outer/src/lib.rs"
    ):
        raise DapError(f"rust_inner source was not lib.rs:6: {rust_frame}")
    if rust_frame.get("line") != 6:
        raise DapError(f"rust_inner stopped at the wrong source line: {rust_frame}")

    python_frame = next(
        (frame for frame in frames if frame.get("name") == "python_worker"),
        None,
    )
    if not isinstance(python_frame, dict):
        raise DapError(f"mixed stack lost python_worker: {leaves[:20]}")
    frame_id = python_frame.get("id")
    if not isinstance(frame_id, int):
        raise DapError(f"python_worker frame had no synthetic ID: {python_frame}")
    locals_snapshot = _locals(client, frame_id)
    label = records[process_id].get("label")
    value = CHILD_LABELS.get(label)
    if not isinstance(label, str) or value is None:
        raise DapError(f"child record had an unsupported label: {records[process_id]}")
    if locals_snapshot.get("label") != repr(label) or locals_snapshot.get(
        "value"
    ) != str(value):
        raise DapError(
            f"worker locals did not match process identity: "
            f"{locals_snapshot} for {process_id}"
        )
    if _evaluate_value(client, frame_id) != str(int(value) + 1):
        raise DapError(f"worker evaluation leaked process state: {locals_snapshot}")
    return process_id, thread_id, frame_id


def _assert_stale_frame(client: DapClient, frame_id: int) -> None:
    request = client.send("scopes", {"frameId": frame_id})
    response = client.wait_for(
        lambda message: message.get("type") == "response"
        and message.get("request_seq") == request,
        timeout=10,
    )
    if response.get("success", True):
        raise DapError("continued child retained its synthetic Python frame")


def _continue(client: DapClient, thread_id: int) -> None:
    request = client.send(
        "continue",
        {"threadId": thread_id, "singleThread": True},
    )
    client.response(request, timeout=10)


def _thread_states(tree: list[dict[str, Any]], process_id: int) -> dict[int, bool]:
    process = next(
        (item for item in tree if item.get("processId") == process_id),
        None,
    )
    if not isinstance(process, dict):
        raise DapError(f"process {process_id} was missing from process tree")
    return {
        thread["threadId"]: thread["isStopped"]
        for thread in process.get("threads", [])
        if isinstance(thread, dict)
        and isinstance(thread.get("threadId"), int)
        and isinstance(thread.get("isStopped"), bool)
    }


def _assert_no_sibling_resume(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    sibling_process_id: int,
) -> None:
    before_states = _thread_states(before, sibling_process_id)
    after_states = _thread_states(after, sibling_process_id)
    if set(before_states) != set(after_states):
        raise DapError("continuing one child erased or added sibling worker threads")
    if any(before_states[thread_id] and not after_states[thread_id] for thread_id in before_states):
        raise DapError("continuing one child resumed a sibling worker")


def _wait_for_stopped_process(
    client: DapClient,
    process_id: int,
    *,
    timeout: float = TREE_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DapError(f"child {process_id} did not stop again")
        stopped = client.event("stopped", timeout=min(5.0, remaining))
        if (stopped.get("body") or {}).get("systemProcessId") == process_id:
            return stopped


def _drain_child(
    client: DapClient,
    selected_process_id: int,
    sibling_process_id: int,
    current_stop: tuple[int, int, int],
    records: dict[int, dict[str, Any]],
) -> dict[str, Any] | None:
    """Resume one child until its subtree disappears; leave its sibling stopped."""

    sibling_stop: dict[str, Any] | None = None
    _, thread_id, frame_id = current_stop
    _continue(client, thread_id)
    _assert_stale_frame(client, frame_id)
    selected_states = _thread_states(_process_tree(client), selected_process_id)
    remaining_workers = set(_expected_threads(records[selected_process_id])) - {thread_id}
    if not remaining_workers or not all(
        worker_id in selected_states for worker_id in remaining_workers
    ):
        raise DapError(
            "continuing one worker hid a sibling worker in the same process"
        )
    deadline = time.monotonic() + TREE_TIMEOUT_SECONDS
    while True:
        tree = _process_tree(client)
        if selected_process_id not in {
            process.get("processId") for process in tree
        }:
            return sibling_stop
        if time.monotonic() >= deadline:
            raise DapError(f"selected child did not exit: {tree}")
        try:
            stopped = client.event("stopped", timeout=2)
        except DapError:
            time.sleep(0.05)
            continue
        process_id = (stopped.get("body") or {}).get("systemProcessId")
        if process_id == selected_process_id:
            _, thread_id, _ = _inspect_stop(client, stopped, records)
            _continue(client, thread_id)
        elif process_id == sibling_process_id:
            _inspect_stop(client, stopped, records)
            sibling_stop = stopped
        else:
            raise DapError(f"unexpected process stop during child drain: {stopped}")


def _finish_child(
    client: DapClient,
    process_id: int,
    current_stop: dict[str, Any],
    records: dict[int, dict[str, Any]],
) -> None:
    stopped = current_stop
    inspect_current_stop = True
    deadline = time.monotonic() + TREE_TIMEOUT_SECONDS
    while True:
        tree = _process_tree(client)
        if process_id not in {process.get("processId") for process in tree}:
            return
        if time.monotonic() >= deadline:
            raise DapError(f"child {process_id} did not finish: {tree}")
        if inspect_current_stop:
            _, thread_id, _ = _inspect_stop(client, stopped, records)
            _continue(client, thread_id)
            inspect_current_stop = False
        try:
            stopped = client.event("stopped", timeout=2)
        except DapError:
            time.sleep(0.05)
            continue
        stopped_process_id = (stopped.get("body") or {}).get("systemProcessId")
        if stopped_process_id != process_id:
            raise DapError(f"unexpected sibling stop while finishing {process_id}: {stopped}")
        inspect_current_stop = True


def process_thread_happy_path() -> None:
    with TemporaryDirectory(prefix="pyrust-process-thread-registry-") as directory:
        registry = Path(directory)
        client, first_stop = _start_fixture(registry)
        try:
            records = _wait_for_records(registry)
            by_label = _records_by_label(records)
            if set(by_label) != set(CHILD_LABELS):
                raise DapError(f"unexpected child labels: {by_label}")
            for label, value in CHILD_LABELS.items():
                if by_label[label].get("value", value) != value:
                    # The worker record intentionally carries the command value
                    # in its command, while the launch contract owns the value.
                    command = str(by_label[label].get("command", ""))
                    if not command.endswith(f"{label} {value}"):
                        raise DapError(f"child command lost its value: {by_label[label]}")

            first_process_id = (first_stop.get("body") or {}).get("systemProcessId")
            if not isinstance(first_process_id, int):
                raise DapError(f"first stop had no child process ID: {first_stop}")
            parent_process_ids = {
                record.get("parentProcessId", record.get("parentPid"))
                for record in records.values()
            }
            if len(parent_process_ids) != 1:
                raise DapError(f"children did not share one real parent: {records}")
            parent_process_id = next(iter(parent_process_ids))
            if not isinstance(parent_process_id, int) or parent_process_id <= 0:
                raise DapError(f"child parent identity was malformed: {records}")
            if any(
                record.get("processId", record.get("pid")) == parent_process_id
                for record in records.values()
            ):
                raise DapError(f"parent and child process IDs collided: {records}")

            tree = _wait_for_tree(
                client,
                lambda snapshot: (
                    all(
                        process_id in {item.get("processId") for item in snapshot}
                        for process_id in records
                    )
                    and all(
                        set(_expected_threads(record)).issubset(
                            {
                                thread.get("threadId")
                                for process in snapshot
                                if process.get("processId") == process_id
                                for thread in process.get("threads", [])
                                if isinstance(thread, dict)
                            }
                        )
                        for process_id, record in records.items()
                    )
                ),
            )
            by_pid = _assert_tree_shape(
                tree,
                records,
                parent_process_id,
                require_threads=True,
            )
            parent = by_pid[parent_process_id]
            if parent.get("processId") != parent_process_id:
                raise DapError("process tree parent PID did not match child records")

            selected_process_id, selected_thread_id, selected_frame_id = _inspect_stop(
                client,
                first_stop,
                records,
            )
            if selected_process_id not in records:
                raise DapError(f"first stop was not a child: {first_stop}")
            sibling_process_id = next(
                process_id
                for process_id in records
                if process_id != selected_process_id
            )
            before_continue = _assert_tree_shape(
                _process_tree(client),
                records,
                parent_process_id,
                require_threads=True,
            )
            sibling_stop = _drain_child(
                client,
                selected_process_id,
                sibling_process_id,
                (selected_process_id, selected_thread_id, selected_frame_id),
                records,
            )
            after_selected_exit = _wait_for_tree(
                client,
                lambda snapshot: (
                    selected_process_id
                    not in {item.get("processId") for item in snapshot}
                    and parent_process_id in {item.get("processId") for item in snapshot}
                    and sibling_process_id in {item.get("processId") for item in snapshot}
                ),
            )
            _assert_no_sibling_resume(
                list(before_continue.values()),
                after_selected_exit,
                sibling_process_id,
            )
            if sibling_stop is None:
                sibling_stop = _wait_for_stopped_process(
                    client,
                    sibling_process_id,
                )
                _inspect_stop(client, sibling_stop, records)

            _finish_child(client, sibling_process_id, sibling_stop, records)
            final_tree = _wait_for_tree(
                client,
                lambda snapshot: not any(
                    process.get("processId") in records for process in snapshot
                ),
            )
            if any(process.get("processId") in records for process in final_tree):
                raise DapError(f"child process cleanup was incomplete: {final_tree}")
            for process_id in records:
                if not (registry / f"complete-{process_id}").is_file():
                    raise DapError(f"child {process_id} did not publish completion")
            print(f"AC-PTM evidence: parent pid={parent_process_id}")
            print(
                "AC-PTM evidence: child pids="
                + ", ".join(
                    f"{record['label']}:{process_id}"
                    for process_id, record in sorted(records.items())
                )
            )
            print(
                "AC-PTM evidence: worker tids="
                + ", ".join(
                    f"{record['label']}:{sorted(_expected_threads(record))}"
                    for record in records.values()
                )
            )
            print(
                "AC-PTM evidence: tree labels/roles/commands and direct thread "
                "ownership verified"
            )
            print("AC-PTM evidence: mixed stack rust_inner -> rust_outer -> python_worker at lib.rs:6")
            print(
                f"AC-PTM evidence: sibling {sibling_process_id} remained visible "
                "while selected subtree exited"
            )
            print("AC-PTM evidence: no task/future hierarchy keys appeared")
        finally:
            client.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=CRITERIA)
    args = parser.parse_args()
    selected = (args.only,) if args.only else CRITERIA
    results: dict[str, bool] = {criterion: False for criterion in selected}
    try:
        process_thread_happy_path()
        for criterion in selected:
            results[criterion] = True
    except (DapError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"Process/thread acceptance: {error}", flush=True)

    for criterion in selected:
        print(f"{criterion} {'PASS' if results[criterion] else 'FAIL'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
