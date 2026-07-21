import * as fs from "node:fs";
import * as path from "node:path";
import * as vscode from "vscode";
import {
  focusFrame,
  focusThread,
  PyRustProcessTreeProvider,
} from "./processTree";

function workspaceRoot(session: vscode.DebugSession): string {
  const folder =
    session.workspaceFolder ?? vscode.workspace.workspaceFolders?.[0];
  if (!folder) {
    throw new Error("PyRust requires an open workspace folder");
  }
  return folder.uri.fsPath;
}

function expandWorkspace(value: string, root: string): string {
  return value.replaceAll("${workspaceFolder}", root);
}

function requireFile(label: string, value: string): string {
  if (!fs.existsSync(value) || !fs.statSync(value).isFile()) {
    throw new Error(`${label} does not exist: ${value}`);
  }
  return value;
}

function inheritedEnvironment(): Record<string, string> {
  const environment: Record<string, string> = {};
  for (const [name, value] of Object.entries(process.env)) {
    if (value !== undefined) {
      environment[name] = value;
    }
  }
  return environment;
}

class PyRustAdapterFactory
  implements vscode.DebugAdapterDescriptorFactory
{
  createDebugAdapterDescriptor(
    session: vscode.DebugSession,
  ): vscode.ProviderResult<vscode.DebugAdapterDescriptor> {
    const root = workspaceRoot(session);
    const settings = vscode.workspace.getConfiguration(
      "pyrust",
      session.workspaceFolder,
    );
    const python = requireFile(
      "PyRust Python executable",
      expandWorkspace(
        settings.get<string>("pythonPath") ??
          "${workspaceFolder}/.venv/bin/python",
        root,
      ),
    );
    const adapter = requireFile(
      "PyRust adapter",
      expandWorkspace(
        settings.get<string>("adapterPath") ??
          "${workspaceFolder}/prototype/adapter/__main__.py",
        root,
      ),
    );
    const codelldb =
      settings.get<string>("codelldbPath") || process.env.PYRUST_CODELLDB || "";
    const liblldb =
      settings.get<string>("liblldbPath") || process.env.PYRUST_LIBLLDB || "";
    if (Boolean(codelldb) !== Boolean(liblldb)) {
      throw new Error(
        "PyRust CodeLLDB adapter and liblldb paths must be configured together",
      );
    }

    const arguments_: string[] = [adapter];
    if (codelldb && liblldb) {
      arguments_.push(
        "--codelldb",
        requireFile("CodeLLDB adapter", expandWorkspace(codelldb, root)),
        "--liblldb",
        requireFile("CodeLLDB liblldb", expandWorkspace(liblldb, root)),
      );
    }

    return new vscode.DebugAdapterExecutable(python, arguments_, {
      cwd: root,
      env: inheritedEnvironment(),
    });
  }
}

class PyRustConfigurationProvider
  implements vscode.DebugConfigurationProvider
{
  resolveDebugConfiguration(
    folder: vscode.WorkspaceFolder | undefined,
    configuration: vscode.DebugConfiguration,
  ): vscode.ProviderResult<vscode.DebugConfiguration> {
    if (!configuration.type && !configuration.request && !configuration.name) {
      configuration.type = "pyrust";
      configuration.request = "launch";
      configuration.name = "PyRust: Python Outer";
      configuration.program = "${workspaceFolder}/.venv/bin/python";
      configuration.args = [
        "${workspaceFolder}/research/fixtures/python_outer/app.py",
      ];
    }
    configuration.cwd ??= folder?.uri.fsPath;
    configuration.args ??= [];
    configuration.terminal ??= "console";
    configuration.consoleMode ??= "evaluate";
    configuration.sourceLanguages ??= ["rust"];
    return configuration;
  }
}

export function activate(context: vscode.ExtensionContext): void {
  const processTree = new PyRustProcessTreeProvider();
  context.subscriptions.push(
    vscode.debug.registerDebugAdapterDescriptorFactory(
      "pyrust",
      new PyRustAdapterFactory(),
    ),
    vscode.debug.registerDebugConfigurationProvider(
      "pyrust",
      new PyRustConfigurationProvider(),
    ),
    vscode.window.registerTreeDataProvider("pyrustProcessTree", processTree),
    vscode.commands.registerCommand("pyrust.focusFrame", focusFrame),
    vscode.commands.registerCommand("pyrust.focusThread", focusThread),
    vscode.debug.onDidStartDebugSession((session) => {
      if (session.type === "pyrust") {
        void processTree.refresh(session);
      }
    }),
    vscode.debug.onDidTerminateDebugSession((session) => {
      processTree.clear(session);
    }),
    vscode.debug.registerDebugAdapterTrackerFactory("pyrust", {
      createDebugAdapterTracker(session) {
        return {
          onDidSendMessage(message: unknown) {
            const event = message as { type?: string; event?: string };
            if (
              event.type === "event" &&
              [
                "continued",
                "exited",
                "process",
                "stopped",
                "terminated",
                "thread",
              ].includes(event.event ?? "")
            ) {
              void processTree.refresh(session);
            }
          },
        };
      },
    }),
  );
}

export function deactivate(): void {}
