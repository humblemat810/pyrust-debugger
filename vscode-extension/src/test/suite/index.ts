import { runProcessTreeModelTest, runSmokeTest } from "./extension.test";

export async function run(): Promise<void> {
  runProcessTreeModelTest();
  await runSmokeTest();
}
