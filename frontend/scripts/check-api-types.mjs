// Сгенерированные типы API (src/api/schema.d.ts) совпадают с контрактом api/openapi.yaml.
// Запускается в `npm run lint`; при расхождении: `npm run gen:api` (контракт меняет только A1).
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const dir = mkdtempSync(join(tmpdir(), "api-types-"));
try {
  const out = join(dir, "schema.d.ts");
  execFileSync(
    process.execPath,
    [join(root, "node_modules/openapi-typescript/bin/cli.js"), join(root, "../api/openapi.yaml"), "--default-non-nullable", "false", "-o", out],
    { stdio: "ignore" },
  );
  const fresh = readFileSync(out, "utf8");
  const committed = readFileSync(join(root, "src/api/schema.d.ts"), "utf8");
  if (fresh !== committed) {
    console.error("src/api/schema.d.ts устарел относительно api/openapi.yaml — выполните `npm run gen:api`.");
    process.exit(1);
  }
  console.log("Типы API соответствуют api/openapi.yaml.");
} finally {
  rmSync(dir, { recursive: true, force: true });
}
