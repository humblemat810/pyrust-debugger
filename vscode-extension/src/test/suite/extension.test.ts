import * as assert from "node:assert";
import * as path from "node:path";
import * as vscode from "vscode";
import {
  applyFrameDocumentLanguage,
  childProcesses,
  frameLanguage,
  frameOpenTarget,
  nativeFrameContent,
  nodeBelongsToSnapshot,
  processDescription,
  processTreeChildren,
  PyRustProcessTreeProvider,
  renderDisassembly,
  rootProcesses,
  sameFrameLocation,
  sourceUri,
  stackNavigationCommand,
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

export async function runProcessTreeModelTest(): Promise<void> {
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
  const firstFrame = frames[0];
  assert.strictEqual(firstFrame.kind, "frame");
  if (firstFrame.kind !== "frame") {
    throw new Error("first stack node was not a frame");
  }
  const frameItem = provider.getTreeItem(firstFrame);
  assert.strictEqual(frameItem.label, "pyrust_native::rust_inner");
  assert.strictEqual(frameItem.description, "lib.rs:6");
  assert.strictEqual(frameOpenTarget(firstFrame.frame), "source");
  assert.strictEqual(
    frameOpenTarget({
      id: 303,
      name: "__futex_abstimed_wait_common64",
      source: {
        name: "@__GI___futex_abstimed_wait_cancelable64",
        origin: "disassembly",
        sourceReference: 1000,
      },
      instructionPointerReference: "0x7FFFF7C98D71",
    }),
    "disassembly",
  );
  assert.strictEqual(
    frameOpenTarget({ id: 304, name: "unresolved native frame" }),
    "unavailable",
  );
  assert.strictEqual(
    frameOpenTarget({
      id: 305,
      name: "expired CodeLLDB source",
      source: { sourceReference: 1000 },
    }),
    "disassembly",
  );
  assert.strictEqual(firstFrame.frameIndex, 0);
  assert.strictEqual(frameLanguage(firstFrame.frame), "rust");
  const pythonFrame = frames[1];
  assert.strictEqual(pythonFrame.kind, "frame");
  if (pythonFrame.kind !== "frame") {
    throw new Error("second stack node was not a frame");
  }
  assert.strictEqual(frameLanguage(pythonFrame.frame), "python");
  assert.strictEqual(
    frameLanguage({
      id: 306,
      name: "native runtime",
      instructionPointerReference: "0x1234",
    }),
    "asm",
  );
  assert.strictEqual(stackNavigationCommand(0, 2), "workbench.action.debug.callStackDown");
  assert.strictEqual(stackNavigationCommand(2, 0), "workbench.action.debug.callStackUp");
  assert.strictEqual(stackNavigationCommand(1, 1), undefined);
  let pythonDocument = await vscode.workspace.openTextDocument({
    language: "rust",
    content: "value + 1",
  });
  pythonDocument = await applyFrameDocumentLanguage(
    pythonDocument,
    pythonFrame.frame,
  );
  assert.strictEqual(pythonDocument.languageId, "python");
  const snapshotFrame = {
    ...firstFrame,
    sessionId: "session-1",
    generation: 7,
  };
  assert.strictEqual(
    nodeBelongsToSnapshot(snapshotFrame, "session-1", 7),
    true,
  );
  assert.strictEqual(
    nodeBelongsToSnapshot(snapshotFrame, "session-1", 8),
    false,
  );
  assert.strictEqual(
    nodeBelongsToSnapshot(snapshotFrame, "session-2", 7),
    false,
  );
  assert.strictEqual(
    sameFrameLocation(
      {
        id: 306,
        name: "__futex_abstimed_wait_common64",
        instructionPointerReference: "0x00007FFFF7C98D71",
      },
      {
        id: 999,
        name: "__futex_abstimed_wait_common64",
        instructionPointerReference: "0x7ffff7c98d71",
      },
    ),
    true,
  );
  assert.strictEqual(
    sameFrameLocation(firstFrame.frame, {
      ...firstFrame.frame,
      line: 7,
    }),
    false,
  );
  const disassembly = renderDisassembly(
    {
      id: 307,
      name: "__futex_abstimed_wait_common64",
      instructionPointerReference: "0x7FFFF7C98D71",
    },
    [
      {
        address: "0x7FFFF7C98D6F",
        instruction: "syscall",
        instructionBytes: "0F 05",
      },
      {
        address: "0x7FFFF7C98D71",
        instruction: "movl %r13d, %edi",
        instructionBytes: "44 89 EF",
        line: 57,
        location: { name: "futex-internal.c" },
      },
    ],
  );
  assert.match(disassembly, /=> 0x7FFFF7C98D71/);
  assert.match(disassembly, /futex-internal\.c:57/);
  const fallbackCalls: Array<{ command: string; args: unknown }> = [];
  const fallbackContent = await nativeFrameContent(
    {
      async customRequest(command: string, args?: unknown): Promise<unknown> {
        fallbackCalls.push({ command, args });
        if (command === "source") {
          throw new Error("Invalid source reference");
        }
        if (command === "disassemble") {
          return {
            instructions: [
              {
                address: "0x72B7D1BA2D71",
                instruction: "retq",
                instructionBytes: "C3",
              },
            ],
          };
        }
        throw new Error(`unexpected command: ${command}`);
      },
    },
    {
      id: 308,
      name: "native frame",
      source: { sourceReference: 1000 },
      instructionPointerReference: "0x72B7D1BA2D71",
    },
  );
  assert.match(fallbackContent ?? "", /retq/);
  assert.deepStrictEqual(
    fallbackCalls.map(({ command }) => command),
    ["source", "disassemble"],
  );
  assert.deepStrictEqual(fallbackCalls[1].args, {
    memoryReference: "0x72B7D1BA2D71",
    instructionOffset: 0,
    instructionCount: 24,
    resolveSymbols: true,
  });
  const unavailable = await nativeFrameContent(
    {
      async customRequest(): Promise<unknown> {
        throw new Error("Internal debugger error");
      },
    },
    {
      id: 309,
      name: "unreadable native frame",
      source: { sourceReference: 1001 },
      instructionPointerReference: "0x72B7D1BA2D71",
    },
  );
  assert.strictEqual(unavailable, undefined);
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
