import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import {
  runTests,
  runVSCodeCommand,
} from "@vscode/test-electron";

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
  const extensionsDir = path.join(
    os.tmpdir(),
    "pyrust-installed-smoke-extensions",
  );
  const userDataDir = path.join(
    os.tmpdir(),
    "pyrust-installed-smoke-user-data",
  );
  const installedRoot = path.join(
    extensionsDir,
    "pyrust.pyrust-debugger-0.0.5",
  );
  const vsix = path.join(extensionRoot, "pyrust-debugger.vsix");

  fs.rmSync(extensionsDir, { recursive: true, force: true });
  fs.rmSync(userDataDir, { recursive: true, force: true });

  try {
    await runVSCodeCommand(
      [
        "--install-extension",
        vsix,
        "--force",
        "--extensions-dir",
        extensionsDir,
        "--user-data-dir",
        userDataDir,
      ],
      {
        version: "1.125.0",
        reuseMachineInstall: true,
      },
    );

    await runTests({
      version: "1.125.0",
      extensionDevelopmentPath,
      extensionTestsPath,
      reuseMachineInstall: true,
      launchArgs: [
        workspacePath,
        "--extensions-dir",
        extensionsDir,
        "--user-data-dir",
        userDataDir,
        "--disable-workspace-trust",
        "--skip-welcome",
        "--skip-release-notes",
        "--no-sandbox",
      ],
      extensionTestsEnv: {
        ...process.env,
        PYRUST_EXPECTED_EXTENSION_ROOT: installedRoot,
      },
    });
  } finally {
    fs.rmSync(extensionsDir, { recursive: true, force: true });
    fs.rmSync(userDataDir, { recursive: true, force: true });
  }
}

main().catch((error: unknown) => {
  console.error(error);
  process.exitCode = 1;
});
