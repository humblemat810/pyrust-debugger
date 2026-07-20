import * as vscode from "vscode";

export interface PyRustThread {
  threadId: number;
  name: string;
  isStopped: boolean;
}

export interface PyRustProcess {
  processId: number;
  parentProcessId?: number;
  label: string;
  role: string;
  isActive: boolean;
  isStopped: boolean;
  threads: PyRustThread[];
}

export interface PyRustProcessTree {
  processes: PyRustProcess[];
}

export type ProcessTreeNode =
  | { kind: "process"; process: PyRustProcess }
  | { kind: "thread"; process: PyRustProcess; thread: PyRustThread };

export function rootProcesses(tree: PyRustProcessTree): PyRustProcess[] {
  const known = new Set(tree.processes.map((process) => process.processId));
  return tree.processes.filter(
    (process) =>
      process.parentProcessId === undefined ||
      !known.has(process.parentProcessId),
  );
}

export function childProcesses(
  tree: PyRustProcessTree,
  processId: number,
): PyRustProcess[] {
  return tree.processes.filter(
    (process) => process.parentProcessId === processId,
  );
}

export class PyRustProcessTreeProvider
  implements vscode.TreeDataProvider<ProcessTreeNode>
{
  private readonly changedEmitter = new vscode.EventEmitter<
    ProcessTreeNode | undefined
  >();
  readonly onDidChangeTreeData = this.changedEmitter.event;
  private tree: PyRustProcessTree = { processes: [] };
  private session: vscode.DebugSession | undefined;

  async refresh(session?: vscode.DebugSession): Promise<void> {
    if (session?.type === "pyrust") {
      this.session = session;
    }
    if (!this.session) {
      this.tree = { processes: [] };
      this.changedEmitter.fire(undefined);
      return;
    }
    try {
      const result = (await this.session.customRequest(
        "pyrust/processTree",
      )) as PyRustProcessTree;
      this.tree = isProcessTree(result) ? result : { processes: [] };
    } catch {
      this.tree = { processes: [] };
    }
    this.changedEmitter.fire(undefined);
  }

  clear(session?: vscode.DebugSession): void {
    if (!session || session === this.session) {
      this.session = undefined;
      this.tree = { processes: [] };
      this.changedEmitter.fire(undefined);
    }
  }

  getTreeItem(node: ProcessTreeNode): vscode.TreeItem {
    if (node.kind === "process") {
      const item = new vscode.TreeItem(
        `${node.process.label} (pid ${node.process.processId})`,
        vscode.TreeItemCollapsibleState.Expanded,
      );
      item.description = [
        node.process.role,
        node.process.isStopped ? "stopped" : "running",
      ].join(" | ");
      item.contextValue = "pyrustProcess";
      item.iconPath = new vscode.ThemeIcon(
        node.process.isStopped ? "debug-pause" : "vm",
      );
      return item;
    }

    const item = new vscode.TreeItem(
      `${node.thread.name} (tid ${node.thread.threadId})`,
      vscode.TreeItemCollapsibleState.None,
    );
    item.description = node.thread.isStopped ? "stopped" : "running";
    item.contextValue = "pyrustThread";
    item.iconPath = new vscode.ThemeIcon(
      node.thread.isStopped ? "debug-pause" : "debug-stackframe",
    );
    item.command = {
      command: "pyrust.focusThread",
      title: "Focus PyRust Thread",
      arguments: [node],
    };
    return item;
  }

  getChildren(node?: ProcessTreeNode): ProcessTreeNode[] {
    if (!node) {
      return rootProcesses(this.tree).map((process) => ({
        kind: "process",
        process,
      }));
    }
    if (node.kind === "thread") {
      return [];
    }
    const threads: ProcessTreeNode[] = node.process.threads.map((thread) => ({
      kind: "thread",
      process: node.process,
      thread,
    }));
    const children: ProcessTreeNode[] = childProcesses(
      this.tree,
      node.process.processId,
    ).map((process) => ({ kind: "process", process }));
    return [...threads, ...children];
  }
}

export async function focusThread(node: ProcessTreeNode): Promise<void> {
  if (node.kind !== "thread") {
    return;
  }
  const session = vscode.debug.activeDebugSession;
  if (!session || session.type !== "pyrust") {
    void vscode.window.showWarningMessage(
      "Start a PyRust debug session before focusing a process-tree thread.",
    );
    return;
  }
  const response = (await session.customRequest("stackTrace", {
    threadId: node.thread.threadId,
    startFrame: 0,
    levels: 1,
  })) as {
    stackFrames?: Array<{
      name?: string;
      line?: number;
      source?: { path?: string };
    }>;
  };
  const frame = response.stackFrames?.[0];
  const sourcePath = frame?.source?.path;
  if (sourcePath && frame?.line) {
    const document = await vscode.workspace.openTextDocument(sourcePath);
    await vscode.window.showTextDocument(document, {
      selection: new vscode.Range(frame.line - 1, 0, frame.line - 1, 0),
      preserveFocus: false,
    });
  }
  void vscode.window.showInformationMessage(
    `Focused PyRust thread ${node.thread.threadId} in ${node.process.label}.`,
  );
}

function isProcessTree(value: unknown): value is PyRustProcessTree {
  if (!value || typeof value !== "object") {
    return false;
  }
  const processes = (value as { processes?: unknown }).processes;
  return Array.isArray(processes);
}
