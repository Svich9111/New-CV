// @vitest-environment node
/// <reference types="node" />
/**
 * Сборка (карточка 2.8; критерий RoadMap п.2; АР-52): два входа — Mini App (`index.html`) и админ-панель
 * (`admin.html`). Собирается как `npm run build` (`NODE_ENV=production`; под vitest он `test`, и JSX собирался бы в
 * отладочном виде с путями файлов — проверялась бы не та сборка). Граф чанков — по манифесту Vite, от входа
 * `admin.html`, включая динамические импорты (ленивые экраны в HTML не попадают): в **каждом** достижимом чанке
 * строки-пути — только из перечня разрешённого (`/admin/…`, `/api/v1`), нет SDK Telegram; модули — только свои,
 * `src/ui/Notice` и React. Mini App не тянет ни одного модуля панели ни сразу, ни лениво.
 */
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { build } from "vite";

const root = resolve(__dirname, "../..");
const ALLOWED_PATH = /^\/(admin\/[a-z0-9/_{}-]*|api\/v1)$/;
/** Модули, из которых может состоять сборка панели (ключи/`src` манифеста). */
const ALLOWED_MODULE = (src: string) =>
  src === "admin.html" ||
  src.startsWith("src/admin/") ||
  src === "src/ui/Notice.tsx" ||
  /^src\/(theme|app)\.css$/.test(src) ||
  /(^|\/)node_modules\/(react|react-dom|scheduler)\//.test(src) ||
  src.startsWith("_"); // общий чанк без своего исходника (`_app-….js`) — его содержимое проверяется ниже

interface ManifestChunk {
  file: string;
  src?: string;
  imports?: string[];
  dynamicImports?: string[];
  css?: string[];
}

function reach(manifest: Record<string, ManifestChunk>, entry: string): Set<string> {
  const seen = new Set<string>();
  const walk = (k: string) => {
    if (seen.has(k) || !manifest[k]) return;
    seen.add(k);
    for (const i of [...(manifest[k].imports ?? []), ...(manifest[k].dynamicImports ?? [])]) walk(i);
  };
  walk(entry);
  return seen;
}

test("vite build (production): все чанки панели по манифесту — только /admin/*; Mini App без кода панели", async () => {
  const outDir = mkdtempSync(join(tmpdir(), "feelings-admin-"));
  const env = process.env.NODE_ENV;
  process.env.NODE_ENV = "production";
  try {
    await build({
      root,
      mode: "production",
      configFile: join(root, "vite.config.ts"),
      logLevel: "silent",
      build: { outDir, emptyOutDir: true, write: true, manifest: "manifest.json" },
    });
    const manifest = JSON.parse(readFileSync(join(outDir, "manifest.json"), "utf8")) as Record<string, ManifestChunk>;
    expect(Object.keys(manifest)).toEqual(expect.arrayContaining(["admin.html", "index.html"]));

    // Страница панели: без SDK Telegram, без встроенных скриптов и комментариев (A6 р.1, MINOR-6).
    const page = readFileSync(join(outDir, "admin.html"), "utf8");
    expect(page).not.toMatch(/telegram\.org|telegram-web-app|sdk-state/);
    expect(page).not.toMatch(/<!--/);
    for (const m of page.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g)) {
      expect(m[1]).toMatch(/\ssrc="/);
      expect(m[2].trim()).toBe("");
    }

    const adminKeys = reach(manifest, "admin.html");
    for (const k of adminKeys) expect(ALLOWED_MODULE(manifest[k].src ?? k), `модуль панели: ${k}`).toBe(true);
    const adminJs = [...adminKeys].map((k) => manifest[k].file).filter((f) => f.endsWith(".js"));
    expect(adminJs.length).toBeGreaterThan(0);
    let sawAdminPath = false;
    for (const f of adminJs) {
      const js = readFileSync(join(outDir, f), "utf8");
      expect(js, `${f}: отладочная сборка`).not.toContain("jsxDEV");
      expect(js, `${f}: пути файлов`).not.toContain(root);
      for (const m of js.matchAll(/["'`](\/[a-z][a-z0-9_/{}.-]*)/gi)) expect(m[1], `${f}: «${m[1]}»`).toMatch(ALLOWED_PATH);
      for (const m of js.matchAll(/\/api\/v1\/[a-z]+/g)) expect(m[0], `${f}`).toBe("/api/v1/admin");
      expect(js, f).not.toMatch(/telegram-web-app|initData|Telegram\.WebApp|localStorage|sessionStorage/);
      if (js.includes('"/admin/kb/documents')) sawAdminPath = true;
    }
    expect(sawAdminPath, "путь /admin/kb/documents найден в чанках панели (проверка не пустая)").toBe(true);

    // Mini App: ни одного модуля src/admin (ни общим чанком, ни лениво) и ни одного пути панели.
    const appKeys = reach(manifest, "index.html");
    expect([...appKeys].map((k) => manifest[k].src ?? k).filter((s) => s.includes("src/admin/"))).toEqual([]);
    for (const k of appKeys) {
      const f = manifest[k].file;
      if (f.endsWith(".js")) expect(readFileSync(join(outDir, f), "utf8"), f).not.toContain("/admin/kb/documents");
    }
  } finally {
    process.env.NODE_ENV = env;
    rmSync(outDir, { recursive: true, force: true });
  }
}, 120_000);

test("перечень разрешённых путей сборки — не пустой страж", () => {
  for (const ok of ["/admin/", "/admin/kb/documents", "/api/v1"]) expect(ok).toMatch(ALLOWED_PATH);
  for (const bad of ["/api/v1/", "/api/v1/memory", "/memory", "/chat/messages", "/auth/telegram", "/admin"]) expect(bad).not.toMatch(ALLOWED_PATH);
});
