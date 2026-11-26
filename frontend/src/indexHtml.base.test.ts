// @vitest-environment node
/// <reference types="node" />
/**
 * n-7 (A6, р.3): путь к public/sdk-state.js идёт от `base` Vite. В index.html он записан от корня
 * (`/sdk-state.js`) — так Vite распознаёт файл из public/ и при сборке подставляет `base`. Тест собирает
 * приложение с `base` не в корне домена и проверяет, что скрипт отметки указывает туда, где лежит файл, —
 * рядом со сборкой и с тем же префиксом, что и модуль приложения.
 */
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { build } from "vite";

const root = resolve(__dirname, "..");

async function buildWith(base: string): Promise<{ html: string; outDir: string }> {
  const outDir = mkdtempSync(join(tmpdir(), "feelings-base-"));
  await build({
    root,
    base,
    configFile: join(root, "vite.config.ts"),
    logLevel: "silent",
    build: { outDir, emptyOutDir: true, write: true },
  });
  return { html: readFileSync(join(outDir, "index.html"), "utf8"), outDir };
}

for (const [base, prefix] of [
  ["/", "/"],
  ["/feelings/app/", "/feelings/app/"],
  ["./", "./"],
] as const) {
  test(`base "${base}": скрипт отметки — "${prefix}sdk-state.js", файл лежит в сборке, до SDK`, async () => {
    const { html, outDir } = await buildWith(base);
    try {
      const tags = [...html.matchAll(/<script\b[^>]*\ssrc="([^"]*sdk-state\.js)"[^>]*>/g)];
      expect(tags).toHaveLength(1);
      expect(tags[0][1]).toBe(`${prefix}sdk-state.js`);
      expect(existsSync(join(outDir, "sdk-state.js"))).toBe(true);
      // Отметка подключена до SDK и с тем же префиксом, что и собранный модуль приложения.
      expect(html.indexOf("sdk-state.js")).toBeLessThan(html.indexOf("telegram-web-app.js"));
      expect(html).toMatch(new RegExp(`<script type="module"[^>]*src="${prefix.replace(/[./]/g, "\\$&")}assets/`));
    } finally {
      rmSync(outDir, { recursive: true, force: true });
    }
  }, 60_000);
}
