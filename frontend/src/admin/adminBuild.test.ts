// @vitest-environment node
/// <reference types="node" />
/**
 * Сборка (карточка 2.8): два входа — Mini App (`index.html`) и админ-панель (`admin.html`). Код панели не тянет
 * Telegram SDK и клиент Mini App: в её скриптах нет пользовательских путей API и SDK Telegram, а Mini App не
 * содержит кода панели.
 */
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { build } from "vite";

const root = resolve(__dirname, "../..");

test("vite build: обе страницы; в скриптах панели — только /admin/*", async () => {
  const outDir = mkdtempSync(join(tmpdir(), "feelings-admin-"));
  try {
    await build({ root, configFile: join(root, "vite.config.ts"), logLevel: "silent", build: { outDir, emptyOutDir: true, write: true } });
    expect(existsSync(join(outDir, "index.html"))).toBe(true);
    expect(existsSync(join(outDir, "admin.html"))).toBe(true);
    const admin = readFileSync(join(outDir, "admin.html"), "utf8");
    const app = readFileSync(join(outDir, "index.html"), "utf8");
    expect(admin).not.toMatch(/telegram\.org|telegram-web-app|sdk-state/);

    const scripts = (page: string) =>
      [...page.matchAll(/<(?:script[^>]*\ssrc|link[^>]*rel="modulepreload"[^>]*\shref)="\/?([^"]+\.js)"/g)]
        .filter((m) => !/^[a-z]+:/.test(m[1])) // внешний SDK Telegram у Mini App — не из сборки
        .map((m) => readFileSync(join(outDir, m[1]), "utf8"));
    const adminJs = scripts(admin).join("\n");
    expect(adminJs.length).toBeGreaterThan(0);
    expect(adminJs).toContain("/admin/kb/documents");
    for (const userPath of ["/auth/telegram", "/chat/messages", "/memory", "/drafts", "/couple", "telegram-web-app", "initData"])
      expect(adminJs, userPath).not.toContain(userPath);

    const appJs = scripts(app).join("\n");
    expect(appJs).not.toContain("/admin/kb/documents");
  } finally {
    rmSync(outDir, { recursive: true, force: true });
  }
}, 120_000);
