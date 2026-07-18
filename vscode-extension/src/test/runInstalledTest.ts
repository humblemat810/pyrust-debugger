import * as path from "node:path";
import { runTests } from "@vscode/test-electron";

async function main(): Promise<void> {
  const extensionRoot = path.resolve(__dirname, "../..");
  const extensionDevelopmentPath = path.join(
    extensionRoot,
    "test-fixtures",
    "installed-harness",
  );
  const extensionTestsPath = path.resolve(
    __dirname,
    "./suite/installed-index",
  );
  const workspacePath = path.resolve(extensionRoot, "..");

  await runTests({
    version: "1.125.0",
    extensionDevelopmentPath,
    extensionTestsPath,
    reuseMachineInstall: true,
    launchArgs: [
      workspacePath,
      "--extensions-dir",
      "/root/.vscode-server/extensions",
      "--user-data-dir",
      "/tmp/pyrust-installed-smoke-user-data",
      "--disable-workspace-trust",
      "--skip-welcome",
      "--skip-release-notes",
      "--no-sandbox",
    ],
    extensionTestsEnv: {
      ...process.env,
      PYRUST_EXPECTED_EXTENSION_ROOT:
        "/root/.vscode-server/extensions/pyrust.pyrust-debugger-0.0.1",
    },
  });
}

main().catch((error: unknown) => {
  console.error(error);
  process.exitCode = 1;
});
