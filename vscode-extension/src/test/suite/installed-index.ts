import * as path from "node:path";
import * as vscode from "vscode";

import { runSmokeTest } from "./extension.test";

export async function run(): Promise<void> {
  const extension = vscode.extensions.getExtension(
    "pyrust.pyrust-debugger",
  );
  if (!extension) {
    throw new Error("installed PyRust extension was not discovered");
  }

  const expectedRoot = process.env.PYRUST_EXPECTED_EXTENSION_ROOT;
  if (!expectedRoot) {
    throw new Error("installed extension root was not configured");
  }
  if (
    path.resolve(extension.extensionPath) !== path.resolve(expectedRoot)
  ) {
    throw new Error(
      `PyRust loaded from ${extension.extensionPath}, expected ${expectedRoot}`,
    );
  }

  await runSmokeTest();
}
