import * as vscode from "vscode";

export interface PyRustThread {
  threadId: number;
  name: string;
  isStopped: boolean;
}

export interface PyRustProcess {
  processId: number;
  parentProcessId: number | null;
  label: string;
  role: string;
  command: string;
  isActive: boolean;
  isStopped: boolean;
  threads: PyRustThread[];
}

export interface PyRustProcessTree {
  processes: PyRustProcess[];
}

export interface PyRustStackFrame {
  id: number;
  name: string;
  line?: number;
  column?: number;
  source?: {
    name?: string;
    path?: string;
  };
}

export type ProcessTreeNode =
  | { kind: "process"; process: PyRustProcess }
  | { kind: "thread"; process: PyRustProcess; thread: PyRustThread }
  | {
      kind: "frame";
      process: PyRustProcess;
      thread: PyRustThread;
      frame: PyRustStackFrame;
    };

export function rootProcesses(tree: PyRustProcessTree): PyRustProcess[] {
  const known = new Set(tree.processes.map((process) => process.processId));
  return tree.processes.filter(
    (process) =>
      process.parentProcessId === null ||
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

export function processDescription(process: PyRustProcess): string {
  return [
    process.role,
    process.isStopped ? "stopped" : "running",
    `command: ${process.command}`,
  ].join(" | ");
}

export function processTreeChildren(
  tree: PyRustProcessTree,
  process: PyRustProcess,
): ProcessTreeNode[] {
  const threads: ProcessTreeNode[] = process.threads.map((thread) => ({
    kind: "thread",
    process,
    thread,
  }));
  const children: ProcessTreeNode[] = childProcesses(
    tree,
    process.processId,
  ).map((child) => ({ kind: "process", process: child }));
  return [...threads, ...children];
}

export function stackFrameNodes(
  process: PyRustProcess,
  thread: PyRustThread,
  frames: PyRustStackFrame[],
): ProcessTreeNode[] {
  return frames.map((frame) => ({ kind: "frame", process, thread, frame }));
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
  private readonly stackFrames = new Map<string, PyRustStackFrame[]>();

  async refresh(session?: vscode.DebugSession): Promise<void> {
    if (session?.type === "pyrust") {
      this.session = session;
    }
    if (!this.session) {
      this.tree = { processes: [] };
      this.stackFrames.clear();
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
    this.stackFrames.clear();
    this.changedEmitter.fire(undefined);
  }

  clear(session?: vscode.DebugSession): void {
    if (!session || session === this.session) {
      this.session = undefined;
      this.tree = { processes: [] };
      this.stackFrames.clear();
      this.changedEmitter.fire(undefined);
    }
  }

  getTreeItem(node: ProcessTreeNode): vscode.TreeItem {
    if (node.kind === "process") {
      const item = new vscode.TreeItem(
        `${node.process.label} (pid ${node.process.processId})`,
        vscode.TreeItemCollapsibleState.Expanded,
      );
      item.description = processDescription(node.process);
      item.tooltip = [
        node.process.role,
        `PID: ${node.process.processId}`,
        `State: ${node.process.isStopped ? "stopped" : "running"}`,
        `Command: ${node.process.command}`,
      ].join("\n");
      item.contextValue = "pyrustProcess";
      item.iconPath = new vscode.ThemeIcon(
        node.process.isStopped ? "debug-pause" : "vm",
      );
      return item;
    }

    if (node.kind === "thread") {
      const item = new vscode.TreeItem(
        `${node.thread.name} (tid ${node.thread.threadId})`,
        node.thread.isStopped
          ? vscode.TreeItemCollapsibleState.Collapsed
          : vscode.TreeItemCollapsibleState.None,
      );
      item.description = node.thread.isStopped ? "stopped" : "running";
      item.tooltip = [
        node.thread.name,
        `TID: ${node.thread.threadId}`,
        `State: ${node.thread.isStopped ? "stopped" : "running"}`,
        node.thread.isStopped
          ? "Expand to inspect the mixed Rust/Python stack."
          : "No stack is available while this thread is running.",
      ].join("\n");
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

    const sourceLabel = node.frame.source?.name ?? node.frame.source?.path;
    const location = sourceLabel && node.frame.line
      ? `${sourceLabel}:${node.frame.line}`
      : sourceLabel;
    const item = new vscode.TreeItem(
      node.frame.name,
      vscode.TreeItemCollapsibleState.None,
    );
    item.description = location;
    item.tooltip = [
      node.frame.name,
      node.frame.source?.path ?? "Source unavailable",
      node.frame.line ? `Line: ${node.frame.line}` : "Line unavailable",
    ].join("\n");
    item.contextValue = "pyrustFrame";
    item.iconPath = new vscode.ThemeIcon("debug-stackframe");
    item.command = {
      command: "pyrust.focusFrame",
      title: "Focus PyRust Stack Frame",
      arguments: [node],
    };
    return item;
  }

  async getChildren(node?: ProcessTreeNode): Promise<ProcessTreeNode[]> {
    if (!node) {
      return rootProcesses(this.tree).map((process) => ({
        kind: "process",
        process,
      }));
    }
    if (node.kind === "frame") {
      return [];
    }
    if (node.kind === "process") {
      return processTreeChildren(this.tree, node.process);
    }
    if (!node.thread.isStopped) {
      return [];
    }
    const frames = await this.threadFrames(node);
    return stackFrameNodes(node.process, node.thread, frames);
  }

  private async threadFrames(
    node: Extract<ProcessTreeNode, { kind: "thread" }>,
  ): Promise<PyRustStackFrame[]> {
    const key = `${node.process.processId}:${node.thread.threadId}`;
    const cached = this.stackFrames.get(key);
    if (cached) {
      return cached;
    }
    if (!this.session) {
      return [];
    }
    try {
      const response = (await this.session.customRequest("stackTrace", {
        threadId: node.thread.threadId,
        startFrame: 0,
        levels: 40,
      })) as { stackFrames?: PyRustStackFrame[] };
      const frames = Array.isArray(response.stackFrames)
        ? response.stackFrames.filter(isStackFrame)
        : [];
      this.stackFrames.set(key, frames);
      return frames;
    } catch {
      return [];
    }
  }
}

export class PyRustFrameHighlighter implements vscode.Disposable {
  private readonly decoration = vscode.window.createTextEditorDecorationType({
    isWholeLine: true,
    backgroundColor: "rgba(255, 193, 7, 0.22)",
    borderColor: "rgba(255, 193, 7, 0.92)",
    borderStyle: "solid",
    borderWidth: "1px 0",
    overviewRulerColor: "rgba(255, 193, 7, 0.92)",
    overviewRulerLane: vscode.OverviewRulerLane.Center,
    light: {
      backgroundColor: "rgba(255, 193, 7, 0.38)",
      borderColor: "rgba(130, 82, 0, 0.95)",
    },
    dark: {
      backgroundColor: "rgba(255, 193, 7, 0.28)",
      borderColor: "rgba(255, 214, 92, 0.95)",
    },
  });
  private editor: vscode.TextEditor | undefined;

  async open(frame: PyRustStackFrame | undefined): Promise<void> {
    const sourcePath = frame?.source?.path;
    if (!sourcePath || !frame?.line) {
      return;
    }
    const document = await vscode.workspace.openTextDocument(sourceUri(sourcePath));
    const range = new vscode.Range(frame.line - 1, 0, frame.line - 1, 0);
    const previousEditor = this.editor;
    const editor = await vscode.window.showTextDocument(document, {
      selection: range,
      preserveFocus: false,
    });
    if (previousEditor && previousEditor !== editor) {
      previousEditor.setDecorations(this.decoration, []);
    }
    editor.setDecorations(this.decoration, [range]);
    this.editor = editor;
  }

  clear(): void {
    this.editor?.setDecorations(this.decoration, []);
    this.editor = undefined;
  }

  dispose(): void {
    this.clear();
    this.decoration.dispose();
  }
}

export async function focusThread(
  highlighter: PyRustFrameHighlighter,
  node?: ProcessTreeNode,
): Promise<void> {
  if (!node || node.kind !== "thread") {
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
  })) as { stackFrames?: PyRustStackFrame[] };
  await highlighter.open(response.stackFrames?.[0]);
  void vscode.window.showInformationMessage(
    `Focused PyRust thread ${node.thread.threadId} in ${node.process.label}.`,
  );
}

export async function focusFrame(
  highlighter: PyRustFrameHighlighter,
  node?: ProcessTreeNode,
): Promise<void> {
  if (!node || node.kind !== "frame") {
    return;
  }
  await highlighter.open(node.frame);
}

export function sourceUri(sourcePath: string): vscode.Uri {
  const parsed = vscode.Uri.parse(sourcePath);
  return parsed.scheme ? parsed : vscode.Uri.file(sourcePath);
}

function isProcessTree(value: unknown): value is PyRustProcessTree {
  if (!value || typeof value !== "object") {
    return false;
  }
  const processes = (value as { processes?: unknown }).processes;
  return Array.isArray(processes);
}

function isStackFrame(value: unknown): value is PyRustStackFrame {
  return Boolean(
    value &&
      typeof value === "object" &&
      typeof (value as { id?: unknown }).id === "number" &&
      typeof (value as { name?: unknown }).name === "string",
  );
}
