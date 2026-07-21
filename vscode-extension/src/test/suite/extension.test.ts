import * as assert from "node:assert";
import * as path from "node:path";
import * as vscode from "vscode";
import {
  childProcesses,
  processDescription,
  processTreeChildren,
  PyRustProcessTreeProvider,
  rootProcesses,
  sourceUri,
  stackFrameNodes,
  type PyRustProcessTree,
} from "../../processTree";

function withTimeout<T>(
  promise: Promise<T>,
  milliseconds: number,
  label: string,
): Promise<T> {
  return Promise.race([
    promise,
    new Promise<T>((_, reject) => {
      setTimeout(
        () => reject(new Error(`${label} exceeded ${milliseconds} ms`)),
        milliseconds,
      );
    }),
  ]);
}

async function waitForExtensionActivation(
  extension: vscode.Extension<unknown>,
): Promise<void> {
  await withTimeout(
    new Promise<void>((resolve) => {
      const poll = () => {
        if (extension.isActive) {
          resolve();
          return;
        }
        setTimeout(poll, 25);
      };
      poll();
    }),
    20_000,
    "PyRust extension activation",
  );
}

export async function runSmokeTest(): Promise<void> {
  const extension = vscode.extensions.getExtension(
    "pyrust.pyrust-debugger",
  );
  if (!extension) {
    throw new Error("PyRust extension was not discovered");
  }

  const folder = vscode.workspace.workspaceFolders?.[0];
  if (!folder) {
    throw new Error("extension test requires the repository workspace");
  }
  let resolveExpressionMode: () => void;
  let rejectExpressionMode: (error: Error) => void;
  const expressionMode = new Promise<void>((resolve, reject) => {
    resolveExpressionMode = resolve;
    rejectExpressionMode = reject;
  });
  const initialized = new Promise<void>((resolve) => {
    vscode.debug.registerDebugAdapterTrackerFactory("pyrust", {
      createDebugAdapterTracker() {
        return {
          onWillReceiveMessage(message: unknown) {
            const candidate = message as {
              command?: string;
              arguments?: { consoleMode?: string };
            };
            if (
              candidate.command === "launch" &&
              candidate.arguments?.consoleMode === "evaluate"
            ) {
              resolveExpressionMode();
            } else if (candidate.command === "launch") {
              rejectExpressionMode(
                new Error(
                  "PyRust launch did not request CodeLLDB expression mode",
                ),
              );
            }
          },
          onDidSendMessage(message: unknown) {
            const candidate = message as {
              type?: string;
              event?: string;
            };
            if (
              candidate.type === "event" &&
              candidate.event === "initialized"
            ) {
              resolve();
            }
          },
        };
      },
    });
  });
  const started = new Promise<vscode.DebugSession>((resolve) => {
    const subscription = vscode.debug.onDidStartDebugSession((session) => {
      if (session.type === "pyrust") {
        subscription.dispose();
        resolve(session);
      }
    });
  });
  const terminated = new Promise<void>((resolve) => {
    const subscription = vscode.debug.onDidTerminateDebugSession((session) => {
      if (session.type === "pyrust") {
        subscription.dispose();
        resolve();
      }
    });
  });
  const launched = await vscode.debug.startDebugging(folder, {
    type: "pyrust",
    request: "launch",
    name: "PyRust extension-host smoke",
    program: path.join(folder.uri.fsPath, ".venv", "bin", "python"),
    args: [
      path.join(
        folder.uri.fsPath,
        "research",
        "fixtures",
        "python_outer",
        "app.py",
      ),
    ],
    cwd: folder.uri.fsPath,
    terminal: "console",
    consoleMode: "evaluate",
    sourceLanguages: ["rust"],
  });
  assert.strictEqual(launched, true, "VS Code rejected the pyrust launch");

  const session = await withTimeout(started, 20_000, "debug session start");
  assert.strictEqual(session.type, "pyrust");
  await withTimeout(
    expressionMode,
    20_000,
    "CodeLLDB expression-mode launch request",
  );
  await waitForExtensionActivation(extension);
  assert.strictEqual(
    extension.isActive,
    true,
    "PyRust did not activate when the debug session started",
  );
  await withTimeout(initialized, 20_000, "adapter initialization");
  await withTimeout(terminated, 30_000, "debug session termination");
}

export function runProcessTreeModelTest(): void {
  const tree: PyRustProcessTree = {
    processes: [
      {
        processId: 100,
        parentProcessId: null,
        label: "Python parent process",
        role: "Python parent process",
        command: "rust-parent --mode process-thread",
        isActive: true,
        isStopped: false,
        threads: [{ threadId: 101, name: "Main thread", isStopped: false }],
      },
      {
        processId: 200,
        parentProcessId: 100,
        label: "worker-A",
        role: "Python child process",
        command: "python process_thread_worker.py process-A 20",
        isActive: false,
        isStopped: true,
        threads: [
          {
            threadId: 201,
            name: "process-A-worker-1",
            isStopped: true,
          },
          {
            threadId: 203,
            name: "process-A-worker-2",
            isStopped: true,
          },
        ],
      },
      {
        processId: 201,
        parentProcessId: 100,
        label: "worker-B",
        role: "Python child process",
        command: "python process_thread_worker.py process-B 40",
        isActive: false,
        isStopped: false,
        threads: [{ threadId: 202, name: "Main thread", isStopped: false }],
      },
    ],
  };

  assert.deepStrictEqual(
    rootProcesses(tree).map((process) => process.processId),
    [100],
  );
  assert.deepStrictEqual(
    childProcesses(tree, 100).map((process) => process.processId),
    [200, 201],
  );
  assert.deepStrictEqual(childProcesses(tree, 200), []);

  const parent = tree.processes[0];
  assert.strictEqual(
    processDescription(parent),
    "Python parent process | running | command: rust-parent --mode process-thread",
  );
  assert.deepStrictEqual(
    processTreeChildren(tree, parent).map((node) =>
      node.kind === "thread"
        ? `thread:${node.thread.threadId}`
        : `process:${node.process.processId}`,
    ),
    ["thread:101", "process:200", "process:201"],
  );

  const workerA = tree.processes[1];
  const workerChildren = processTreeChildren(tree, workerA);
  assert.deepStrictEqual(
    workerChildren.map((node) =>
      node.kind === "thread"
        ? `thread:${node.thread.threadId}`
        : `process:${node.process.processId}`,
    ),
    ["thread:201", "thread:203"],
  );

  const provider = new PyRustProcessTreeProvider();
  const processItem = provider.getTreeItem({ kind: "process", process: workerA });
  assert.strictEqual(processItem.label, "worker-A (pid 200)");
  assert.strictEqual(
    processItem.description,
    "Python child process | stopped | command: python process_thread_worker.py process-A 20",
  );

  const threadNode = workerChildren[0];
  assert.strictEqual(threadNode.kind, "thread");
  if (threadNode.kind !== "thread") {
    throw new Error("worker-A direct child was not a native thread");
  }
  const threadItem = provider.getTreeItem(threadNode);
  assert.strictEqual(threadItem.label, "process-A-worker-1 (tid 201)");
  assert.strictEqual(threadItem.description, "stopped");

  const frames = stackFrameNodes(workerA, threadNode.thread, [
    {
      id: 301,
      name: "pyrust_native::rust_inner",
      line: 6,
      source: { name: "lib.rs", path: "/workspace/lib.rs" },
    },
    {
      id: 302,
      name: "python_worker",
      line: 124,
      source: {
        name: "process_thread_worker.py",
        path: "/workspace/process_thread_worker.py",
      },
    },
  ]);
  assert.deepStrictEqual(
    frames.map((node) => node.kind),
    ["frame", "frame"],
  );
  const frameItem = provider.getTreeItem(frames[0]);
  assert.strictEqual(frameItem.label, "pyrust_native::rust_inner");
  assert.strictEqual(frameItem.description, "lib.rs:6");
  assert.strictEqual(
    sourceUri("/workspaces/pyrust-debugger/lib.rs").scheme,
    "file",
  );
  assert.strictEqual(
    sourceUri(
      "vscode-remote://dev-container%2Bexample/workspaces/pyrust-debugger/lib.rs",
    ).scheme,
    "vscode-remote",
  );
}
