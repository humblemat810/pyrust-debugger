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
    origin?: string;
    sourceReference?: number;
  };
  instructionPointerReference?: string;
}

export type ProcessTreeNode = (
  | { kind: "process"; process: PyRustProcess }
  | { kind: "thread"; process: PyRustProcess; thread: PyRustThread }
  | {
      kind: "frame";
      process: PyRustProcess;
      thread: PyRustThread;
      frame: PyRustStackFrame;
      frameIndex: number;
    }
) & {
  sessionId?: string;
  generation?: number;
};

export interface PyRustDisassembledInstruction {
  address: string;
  instruction: string;
  instructionBytes?: string;
  symbol?: string;
  line?: number;
  location?: {
    name?: string;
    path?: string;
  };
}

export interface PyRustDebugRequester {
  customRequest(command: string, args?: unknown): Thenable<unknown>;
}

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
  return frames.map((frame, frameIndex) => ({
    kind: "frame",
    process,
    thread,
    frame,
    frameIndex,
  }));
}

export type FrameOpenTarget = "source" | "disassembly" | "unavailable";

export function frameOpenTarget(frame: PyRustStackFrame): FrameOpenTarget {
  if (frame.source?.path && frame.line) {
    return "source";
  }
  if (
    isPositiveInteger(frame.source?.sourceReference) ||
    hasInstructionPointer(frame)
  ) {
    return "disassembly";
  }
  return "unavailable";
}

export function sameFrameLocation(
  expected: PyRustStackFrame,
  current: PyRustStackFrame,
): boolean {
  if (
    expected.instructionPointerReference &&
    current.instructionPointerReference
  ) {
    return (
      normalizeAddress(expected.instructionPointerReference) ===
      normalizeAddress(current.instructionPointerReference)
    );
  }
  if (
    expected.source?.path &&
    current.source?.path &&
    expected.line &&
    current.line
  ) {
    return (
      expected.source.path === current.source.path &&
      expected.line === current.line &&
      expected.name === current.name
    );
  }
  return expected.name === current.name;
}

export function renderDisassembly(
  frame: PyRustStackFrame,
  instructions: PyRustDisassembledInstruction[],
): string {
  const instructionPointer = frame.instructionPointerReference ?? "";
  const currentAddress = normalizeAddress(instructionPointer);
  const body = instructions.map((instruction) => {
    const marker =
      normalizeAddress(instruction.address) === currentAddress ? "=>" : "  ";
    const bytes = (instruction.instructionBytes ?? "").trim().padEnd(30);
    const locationName =
      instruction.location?.path ?? instruction.location?.name ?? "";
    const location = locationName
      ? ` ; ${locationName}${instruction.line ? `:${instruction.line}` : ""}`
      : "";
    return `${marker} ${instruction.address.padEnd(18)} ${bytes} ${instruction.instruction}${location}`;
  });
  return [
    "; PyRust Process Tree disassembly",
    `; Frame: ${frame.source?.name ?? frame.name}`,
    `; Instruction pointer: ${instructionPointer || "unavailable"}`,
    "",
    ...body,
  ].join("\n");
}

export function nodeBelongsToSnapshot(
  node: ProcessTreeNode,
  sessionId: string,
  generation: number,
): boolean {
  return node.sessionId === sessionId && node.generation === generation;
}

export async function nativeFrameContent(
  session: PyRustDebugRequester,
  frame: PyRustStackFrame,
): Promise<string | undefined> {
  const sourceReference = frame.source?.sourceReference;
  if (isPositiveInteger(sourceReference)) {
    try {
      const response = (await session.customRequest("source", {
        sourceReference,
      })) as { content?: unknown };
      if (typeof response.content === "string" && response.content.trim()) {
        return [
          "; PyRust Process Tree native source",
          `; Frame: ${frame.source?.name ?? frame.name}`,
          "",
          response.content,
        ].join("\n");
      }
    } catch {
      // A stop-scoped source reference may expire. Try the frame PC next.
    }
  }

  const instructionPointerReference = frame.instructionPointerReference;
  if (instructionPointerReference) {
    try {
      const response = (await session.customRequest("disassemble", {
        memoryReference: instructionPointerReference,
        instructionOffset: 0,
        instructionCount: 24,
        resolveSymbols: true,
      })) as { instructions?: unknown };
      const instructions = Array.isArray(response.instructions)
        ? response.instructions.filter(isDisassembledInstruction)
        : [];
      if (instructions.length > 0) {
        return renderDisassembly(frame, instructions);
      }
    } catch {
      // Some frame PCs are not readable instruction boundaries.
    }
  }

  return undefined;
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
  private generation = 0;
  private refreshVersion = 0;

  async refresh(session?: vscode.DebugSession): Promise<void> {
    if (session?.type === "pyrust" && session !== this.session) {
      this.session = session;
      this.generation += 1;
    }
    if (!this.session) {
      this.tree = { processes: [] };
      this.stackFrames.clear();
      this.changedEmitter.fire(undefined);
      return;
    }
    const activeSession = this.session;
    const version = ++this.refreshVersion;
    try {
      const result = (await activeSession.customRequest(
        "pyrust/processTree",
      )) as PyRustProcessTree;
      if (
        version !== this.refreshVersion ||
        activeSession !== this.session
      ) {
        return;
      }
      this.tree = isProcessTree(result) ? result : { processes: [] };
    } catch {
      if (
        version !== this.refreshVersion ||
        activeSession !== this.session
      ) {
        return;
      }
      this.tree = { processes: [] };
    }
    this.stackFrames.clear();
    this.changedEmitter.fire(undefined);
  }

  invalidate(session: vscode.DebugSession): void {
    if (session === this.session) {
      this.generation += 1;
      this.refreshVersion += 1;
      this.tree = { processes: [] };
      this.stackFrames.clear();
      this.changedEmitter.fire(undefined);
    }
  }

  clear(session?: vscode.DebugSession): void {
    if (!session || session === this.session) {
      this.generation += 1;
      this.refreshVersion += 1;
      this.session = undefined;
      this.tree = { processes: [] };
      this.stackFrames.clear();
      this.changedEmitter.fire(undefined);
    }
  }

  isCurrent(node: ProcessTreeNode, session: vscode.DebugSession): boolean {
    return (
      session === this.session &&
      nodeBelongsToSnapshot(node, session.id, this.generation)
    );
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
      return rootProcesses(this.tree).map((process) =>
        this.withContext({
          kind: "process",
          process,
        }),
      );
    }
    if (node.kind === "frame") {
      return [];
    }
    if (node.kind === "process") {
      return processTreeChildren(this.tree, node.process).map((child) =>
        this.withContext(child),
      );
    }
    if (!node.thread.isStopped) {
      return [];
    }
    const frames = await this.threadFrames(node);
    return stackFrameNodes(node.process, node.thread, frames).map((frame) =>
      this.withContext(frame),
    );
  }

  private withContext(node: ProcessTreeNode): ProcessTreeNode {
    return {
      ...node,
      sessionId: this.session?.id,
      generation: this.generation,
    };
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

  async open(
    session: vscode.DebugSession,
    frame: PyRustStackFrame | undefined,
  ): Promise<FrameOpenTarget> {
    const target = frame ? frameOpenTarget(frame) : "unavailable";
    if (target === "unavailable" || !frame) {
      return "unavailable";
    }

    if (target === "source") {
      const sourcePath = frame.source?.path;
      const line = frame.line;
      if (!sourcePath || !line) {
        return "unavailable";
      }
      const document = await vscode.workspace.openTextDocument(sourceUri(sourcePath));
      const range = new vscode.Range(line - 1, 0, line - 1, 0);
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
      return "source";
    }

    const content = await nativeFrameContent(session, frame);
    if (content) {
      await this.openAssemblyDocument(content);
      return "disassembly";
    }

    throw new Error(
      "No readable source or disassembly is available for this native frame.",
    );
  }

  private async openAssemblyDocument(content: string): Promise<void> {
    this.clear();
    const document = await vscode.workspace.openTextDocument({
      language: "asm",
      content,
    });
    await vscode.window.showTextDocument(document, {
      preserveFocus: false,
      preview: true,
    });
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
  session = vscode.debug.activeDebugSession,
  provider?: PyRustProcessTreeProvider,
): Promise<void> {
  if (!node || node.kind !== "thread") {
    return;
  }
  if (!session || session.type !== "pyrust") {
    void vscode.window.showWarningMessage(
      "Start a PyRust debug session before focusing a process-tree thread.",
    );
    return;
  }
  if (provider && !provider.isCurrent(node, session)) {
    void vscode.window.showWarningMessage(
      "This Process Tree thread is stale. Use the current tree entry.",
    );
    return;
  }
  try {
    const response = (await session.customRequest("stackTrace", {
      threadId: node.thread.threadId,
      startFrame: 0,
      levels: 1,
    })) as { stackFrames?: PyRustStackFrame[] };
    const frame = response.stackFrames?.find(isStackFrame);
    const target = await highlighter.open(session, frame);
    if (target === "unavailable") {
      void vscode.window.showWarningMessage(
        `PyRust thread ${node.thread.threadId} has no source or disassembly location.`,
      );
      return;
    }
    void vscode.window.showInformationMessage(
      target === "source"
        ? `Focused PyRust thread ${node.thread.threadId} in ${node.process.label}.`
        : `Opened CodeLLDB disassembly for PyRust thread ${node.thread.threadId}.`,
    );
  } catch (error) {
    void vscode.window.showWarningMessage(
      `Unable to focus PyRust thread ${node.thread.threadId}: ${errorMessage(error)}`,
    );
  }
}

export async function focusFrame(
  highlighter: PyRustFrameHighlighter,
  node?: ProcessTreeNode,
  session = vscode.debug.activeDebugSession,
  provider?: PyRustProcessTreeProvider,
): Promise<void> {
  if (!node || node.kind !== "frame") {
    return;
  }
  if (!session || session.type !== "pyrust") {
    void vscode.window.showWarningMessage(
      "Start a PyRust debug session before focusing a process-tree frame.",
    );
    return;
  }
  if (provider && !provider.isCurrent(node, session)) {
    void vscode.window.showWarningMessage(
      "This Process Tree frame is stale. Expand the current thread again.",
    );
    return;
  }
  try {
    const target = await highlighter.open(session, node.frame);
    if (target === "unavailable") {
      void vscode.window.showWarningMessage(
        "This PyRust frame has no source or CodeLLDB disassembly location.",
      );
    }
  } catch (error) {
    void vscode.window.showWarningMessage(
      `Unable to focus this PyRust frame: ${errorMessage(error)}`,
    );
  }
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

function isDisassembledInstruction(
  value: unknown,
): value is PyRustDisassembledInstruction {
  return Boolean(
    value &&
      typeof value === "object" &&
      typeof (value as { address?: unknown }).address === "string" &&
      typeof (value as { instruction?: unknown }).instruction === "string",
  );
}

function hasInstructionPointer(frame: PyRustStackFrame): boolean {
  return Boolean(frame.instructionPointerReference?.trim());
}

function isPositiveInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value > 0;
}

function normalizeAddress(address: string): string {
  return address.trim().toLowerCase().replace(/^0x0*/, "");
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
