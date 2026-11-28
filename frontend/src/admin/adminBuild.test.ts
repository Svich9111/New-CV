// @vitest-environment node
/// <reference types="node" />
/**
 * Сборка (карточка 2.8; критерий RoadMap п.2; ТЗ §9.1 п.5; АР-52): два входа — Mini App (`index.html`) и админ-панель
 * (`admin.html`). Собирается как `npm run build` (`NODE_ENV=production`; под vitest он `test`, и JSX собирался бы в
 * отладочном виде с путями файлов — проверялась бы не та сборка), результат берётся из памяти (`RollupOutput`,
 * `write: false`).
 * - Граф чанков — от входа `admin.html` по `imports` и `dynamicImports` (ленивые экраны в HTML не попадают).
 * - Состав **каждого** достижимого чанка, общего с Mini App тоже, — по `chunk.moduleIds` (ревью A6 р.2, MAJOR-1):
 *   только `admin.html`, `src/admin/**` (без тестов и `src/admin/test/`), `src/ui/Notice.tsx`, общие стили, React
 *   (`react`, `react-dom`, `scheduler`) и служебные модули Vite. Модуль Mini App, подключённый переэкспортом через
 *   разрешённый модуль или файл `.js`, попадает в состав чанка — страж падает.
 * - В каждом достижимом чанке строки-пути — только из перечня разрешённого (`/admin/…`, `/api/v1`), нет SDK Telegram,
 *   хранилищ, отладочной сборки и путей файлов.
 * - Mini App не тянет ни одного модуля панели ни сразу, ни лениво.
 * Каждое правило состава проверено мутацией сборки через плагин (`transform`/`resolveId`/`load`), настоящие файлы не
 * меняются: см. «страж падает» в конце файла.
 */
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { build, type Plugin, type Rollup } from "vite";

const root = resolve(__dirname, "../..");
const ADMIN_HTML = join(root, "admin.html");
const INDEX_HTML = join(root, "index.html");
const ALLOWED_PATH = /^\/(admin\/[a-z0-9/_{}-]*|api\/v1)$/;

/** Служебные модули сборщика (виртуальные, `\0…`), без кода приложения. */
const VIRTUAL = new Set(["vite/modulepreload-polyfill.js", "vite/preload-helper.js", "commonjsHelpers.js"]);

/** Модуль, из которого может состоять сборка панели (id модуля Rollup: абсолютный путь или `\0…` с `?commonjs-…`). */
function allowedModule(id: string): boolean {
  const virtual = id.startsWith("\0");
  const path = (virtual ? id.slice(1) : id).replace(/\?.*$/, "");
  if (virtual && VIRTUAL.has(path)) return true;
  if (/\/node_modules\/(react|react-dom|scheduler)\/[^?]*$/.test(path)) return true;
  if (virtual) return false;
  if (path === ADMIN_HTML) return true;
  if (path === join(root, "src/ui/Notice.tsx")) return true;
  // Б-105 (A8-0021-3): строгие проверки дат — чистые функции без импортов, сети и хранилищ; что он тянет, проверяет
  // этот же перечень (любой другой модуль `src/api/*` — красный).
  if (path === join(root, "src/api/datetime.ts")) return true;
  if (path === join(root, "src/theme.css") || path === join(root, "src/app.css")) return true;
  const admin = join(root, "src/admin") + "/";
  return path.startsWith(admin) && !path.startsWith(join(admin, "test") + "/") && !/\.test\.[a-z]+$/.test(path) && /\.(ts|tsx|css)$/.test(path);
}

type Chunk = Rollup.OutputChunk;
type Output = (Rollup.OutputChunk | Rollup.OutputAsset)[];

function reach(chunks: Map<string, Chunk>, entry: Chunk): Chunk[] {
  const seen = new Map<string, Chunk>();
  const walk = (c: Chunk | undefined) => {
    if (!c || seen.has(c.fileName)) return;
    seen.set(c.fileName, c);
    for (const f of [...c.imports, ...c.dynamicImports]) walk(chunks.get(f));
  };
  walk(entry);
  return [...seen.values()];
}

async function buildAll(plugins: Plugin[] = []): Promise<Output> {
  const outDir = mkdtempSync(join(tmpdir(), "feelings-admin-"));
  const env = process.env.NODE_ENV;
  process.env.NODE_ENV = "production";
  try {
    const res = await build({
      root,
      mode: "production",
      configFile: join(root, "vite.config.ts"),
      logLevel: "silent",
      plugins,
      build: { outDir, emptyOutDir: true, write: false },
    });
    const outs = (Array.isArray(res) ? res : [res]) as Rollup.RollupOutput[];
    return outs.flatMap((o) => o.output);
  } finally {
    process.env.NODE_ENV = env;
    rmSync(outDir, { recursive: true, force: true });
  }
}

/** Нарушения в собранном выводе; пустой список — сборка чистая. */
function findings(output: Output): string[] {
  const out: string[] = [];
  const chunks = new Map(output.filter((o): o is Chunk => o.type === "chunk").map((c) => [c.fileName, c]));
  const entry = (html: string) => [...chunks.values()].find((c) => c.isEntry && c.facadeModuleId === html);
  const admin = entry(ADMIN_HTML);
  const app = entry(INDEX_HTML);
  if (!admin || !app) return ["нет входа admin.html или index.html"];

  // Страница панели: без SDK Telegram, без встроенных скриптов и комментариев (A6 р.1, MINOR-6).
  const pageAsset = output.find((o) => o.type === "asset" && o.fileName === "admin.html") as Rollup.OutputAsset | undefined;
  const page = pageAsset ? String(pageAsset.source) : "";
  if (!page) out.push("нет admin.html в выводе");
  if (/telegram\.org|telegram-web-app|sdk-state/.test(page)) out.push("admin.html: Telegram SDK");
  if (/<!--/.test(page)) out.push("admin.html: комментарий");
  for (const m of page.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g))
    if (!/\ssrc="/.test(m[1]) || m[2].trim() !== "") out.push("admin.html: встроенный скрипт");

  let sawAdminPath = false;
  let sawAdminModule = false;
  for (const c of reach(chunks, admin)) {
    for (const id of c.moduleIds) {
      if (!allowedModule(id)) out.push(`${c.fileName}: модуль ${id.replace("\0", "\\0").replace(root + "/", "")}`);
      if (id === join(root, "src/admin/main.tsx")) sawAdminModule = true;
    }
    const js = c.code;
    if (js.includes("jsxDEV")) out.push(`${c.fileName}: отладочная сборка`);
    if (js.includes(root)) out.push(`${c.fileName}: пути файлов`);
    for (const m of js.matchAll(/["'`](\/[a-z][a-z0-9_/{}.-]*)/gi)) if (!ALLOWED_PATH.test(m[1])) out.push(`${c.fileName}: «${m[1]}»`);
    for (const m of js.matchAll(/\/api\/v1\/[a-z]+/g)) if (m[0] !== "/api/v1/admin") out.push(`${c.fileName}: «${m[0]}»`);
    const bad = js.match(/telegram-web-app|initData|Telegram\.WebApp|localStorage|sessionStorage/);
    if (bad) out.push(`${c.fileName}: «${bad[0]}»`);
    if (js.includes('"/admin/kb/documents')) sawAdminPath = true;
  }
  if (!sawAdminModule) out.push("src/admin/main.tsx не найден в чанках панели (проверка пустая)");
  if (!sawAdminPath) out.push("путь /admin/kb/documents не найден в чанках панели (проверка пустая)");

  // Mini App: ни одного модуля src/admin (ни общим чанком, ни лениво) и ни одного пути панели.
  for (const c of reach(chunks, app)) {
    for (const id of c.moduleIds) if (id.includes("/src/admin/")) out.push(`Mini App ${c.fileName}: модуль панели ${id}`);
    if (c.code.includes("/admin/kb/documents")) out.push(`Mini App ${c.fileName}: путь панели`);
  }
  return out;
}

test("vite build (production): состав и содержимое каждого чанка панели — только разрешённое; Mini App без кода панели", async () => {
  expect(findings(await buildAll())).toEqual([]);
}, 120_000);

test("перечень разрешённых путей сборки — не пустой страж", () => {
  for (const ok of ["/admin/", "/admin/kb/documents", "/api/v1"]) expect(ok).toMatch(ALLOWED_PATH);
  for (const bad of ["/api/v1/", "/api/v1/memory", "/memory", "/chat/messages", "/auth/telegram", "/admin"]) expect(bad).not.toMatch(ALLOWED_PATH);
});

test("перечень модулей сборки — не пустой страж", () => {
  for (const ok of ["src/admin/api.ts", "src/admin/admin.css", "src/ui/Notice.tsx", "src/api/datetime.ts", "src/theme.css", "admin.html"]) expect(allowedModule(join(root, ok)), ok).toBe(true);
  for (const ok of ["\0vite/preload-helper.js", "\0/x/node_modules/react/index.js?commonjs-module", "/x/node_modules/scheduler/index.js"])
    expect(allowedModule(ok), ok).toBe(true);
  for (const bad of ["src/api/chat.ts", "src/api/client.ts", "src/api/datetime.test.ts", "src/api/datetime.a8.test.ts", "src/ui/format.ts", "src/admin/zzz.js", "src/admin/test/fakeAdminApi.ts", "src/admin/api.test.ts", "src/main.tsx"])
    expect(allowedModule(join(root, bad)), bad).toBe(false);
  for (const bad of ["\0/x/src/api/chat.ts?commonjs-proxy", "/x/node_modules/@telegram-apps/sdk/index.js", "\0other-virtual.js"]) expect(allowedModule(bad), bad).toBe(false);
});

/**
 * Мутации сборки (ревью A6 р.2, MAJOR-1: m10, m11, m6): плагин дописывает код в модуль при сборке или подставляет
 * несуществующий файл — настоящие исходники не меняются.
 */
const MAIN = join(root, "src/admin/main.tsx");
const NOTICE = join(root, "src/ui/Notice.tsx");
const DATETIME = join(root, "src/api/datetime.ts");
const append = (file: string, code: string): Plugin => ({
  name: `mut-append-${file}`,
  enforce: "pre",
  transform: (src, id) => (id === file ? { code: `${src}\n${code}\n`, map: null } : null),
});
/** Виртуальный файл `src/admin/<name>` (например `.js`), подключённый из `main.tsx`. */
const virtualAdminFile = (name: string, code: string): Plugin => {
  const id = join(root, "src/admin", name);
  return {
    name: `mut-file-${name}`,
    enforce: "pre",
    resolveId: (spec, importer) => (spec === `./${name}` && importer === MAIN ? id : null),
    load: (i) => (i === id ? code : null),
  };
};
const LEAK = 'if (location.hash === "#x") (window as unknown as { a: unknown }).a';

describe("страж сборки падает (ревью A6 р.2, MAJOR-1)", () => {
  test.each<[string, Plugin[], RegExp]>([
    [
      "m10: модуль Mini App переэкспортом через Notice (статический импорт)",
      [append(NOTICE, 'export { SseParser, parseEvent } from "../api/chat";'), append(MAIN, `import { parseEvent } from "../ui/Notice";\n${LEAK} = parseEvent;`)],
      /модуль src\/api\/chat\.ts/,
    ],
    [
      "m11: то же лениво — import(\"../ui/Notice\")",
      [append(NOTICE, 'export { SseParser, parseEvent } from "../api/chat";'), append(MAIN, `if (location.hash === "#x") void import("../ui/Notice").then((m) => ((window as unknown as { a: unknown }).a = m.parseEvent));`)],
      /модуль src\/api\/chat\.ts/,
    ],
    [
      "m6: файл .js в src/admin переэкспортирует модуль Mini App",
      [virtualAdminFile("zzz.js", 'export { SseParser } from "../api/chat.ts";'), append(MAIN, `import { SseParser } from "./zzz.js";\n${LEAK} = new SseParser();`)],
      /модуль src\/api\/chat\.ts/,
    ],
    [
      "Б-105: модуль Mini App переэкспортом через datetime.ts",
      [append(DATETIME, 'export { SseParser } from "./chat";'), append(MAIN, `import { SseParser } from "../api/datetime";\n${LEAK} = SseParser;`)],
      /модуль src\/api\/chat\.ts/,
    ],
    [
      "общий модуль ui вне перечня (src/ui/format.ts через Notice)",
      [append(NOTICE, 'export * as fmt from "./format";'), append(MAIN, `import { fmt } from "../ui/Notice";\n${LEAK} = fmt;`)],
      /модуль src\/ui\/format\.ts/,
    ],
  ])("%s", async (_name, plugins, expected) => {
    const found = findings(await buildAll(plugins));
    expect(found.join("\n")).toMatch(expected);
  }, 120_000);
});
