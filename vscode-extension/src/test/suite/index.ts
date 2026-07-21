import { runProcessTreeModelTest, runSmokeTest } from "./extension.test";

export async function run(): Promise<void> {
  await runProcessTreeModelTest();
  await runSmokeTest();
}
