import * as path from "node:path";
import { runTests } from "@vscode/test-electron";

async function main(): Promise<void> {
  const extensionDevelopmentPath = path.resolve(__dirname, "../../");
  const extensionTestsPath = path.resolve(__dirname, "./suite/index");
  const workspacePath = path.resolve(extensionDevelopmentPath, "..");

  await runTests({
    version: "1.125.0",
    extensionDevelopmentPath,
    extensionTestsPath,
    launchArgs: [
      workspacePath,
      "--disable-workspace-trust",
      "--skip-welcome",
      "--skip-release-notes",
      "--no-sandbox",
    ],
    extensionTestsEnv: process.env,
  });
}

main().catch((error: unknown) => {
  console.error(error);
  process.exitCode = 1;
});
