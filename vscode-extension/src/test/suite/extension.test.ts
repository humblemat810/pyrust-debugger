import * as assert from "node:assert";
import * as path from "node:path";
import * as vscode from "vscode";

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

export async function runSmokeTest(): Promise<void> {
  const extension = vscode.extensions.getExtension(
    "pyrust.pyrust-debugger",
  );
  if (!extension) {
    throw new Error("PyRust extension was not discovered");
  }
  await extension.activate();

  const folder = vscode.workspace.workspaceFolders?.[0];
  if (!folder) {
    throw new Error("extension test requires the repository workspace");
  }
  const initialized = new Promise<void>((resolve) => {
    vscode.debug.registerDebugAdapterTrackerFactory("pyrust", {
      createDebugAdapterTracker() {
        return {
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
    sourceLanguages: ["rust"],
  });
  assert.strictEqual(launched, true, "VS Code rejected the pyrust launch");

  const session = await withTimeout(started, 20_000, "debug session start");
  assert.strictEqual(session.type, "pyrust");
  await withTimeout(initialized, 20_000, "adapter initialization");
  await withTimeout(terminated, 30_000, "debug session termination");
}
