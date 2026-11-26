// @vitest-environment node
/// <reference types="node" />
/**
 * A8, задача 2.8, раунд 1 — критерий RoadMap «в панели нет ни одного экрана с текстами переписок» (пп.1–2; ТЗ §9.1
 * п.5; АР-52), более строгие стражи, чем `boundary.test.ts`/`adminBuild.test.ts` (ср. ревью A6 р.1, MAJOR-1):
 * - исходники разбираются своим сканером (строки и шаблоны — отдельно, комментарии и регулярные выражения не мешают);
 * - строки путей в коде панели — по перечню разрешённого (allowlist), а не запрещённого;
 * - сетевые примитивы (`fetch`, XHR, `sendBeacon`, `WebSocket`, `EventSource`) — только в `api.ts`;
 * - нет обходов загрузки модулей (`import.meta.glob`, `require`, нелитеральный `import()`, `new URL(…, import.meta.url)`)
 *   и хранилищ в обход проверки (`document[…]`, `window.name`, `cookieStore`);
 * - сборка: обход графа чанков по манифесту Vite от входа `admin.html`, включая динамические импорты; в каждом чанке
 *   панели строки путей — только `/admin/…` и `/api/v1`; Mini App не тянет ни одного чанка панели.
 */
import { mkdtempSync, readFileSync, readdirSync, rmSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, relative, resolve } from "node:path";
import { build } from "vite";

const root = __dirname;
const front = resolve(root, "../..");

function files(dir: string): string[] {
  return readdirSync(dir).flatMap((n) => {
    const p = join(dir, n);
    return statSync(p).isDirectory() ? files(p) : /\.(ts|tsx)$/.test(n) ? [p] : [];
  });
}
const sources = files(root).filter((p) => !/\.test\.tsx?$/.test(p) && !p.includes(`${join("admin", "test")}`));

/**
 * Сканер исходника (без зависимостей; `typescript` в тестах панели не разрешён `boundary.test.ts`): комментарии
 * выброшены, строковые и шаблонные литералы — отдельно (`strings`), в коде на их месте `""` (шаблон с подстановкой —
 * `TPL`); литералы регулярных выражений пропускаются. Пунктуация в `code` отделена пробелами.
 */
function scanText(text: string): { code: string; strings: string[] } {
  const out: string[] = [];
  const strings: string[] = [];
  const braces: number[] = []; // глубина фигурных скобок, на которой открыта подстановка шаблона
  let depth = 0;
  let i = 0;
  const lastSignificant = () => {
    for (let j = out.length - 1; j >= 0; j--) {
      const t = out[j].trim();
      if (t) return t;
    }
    return "";
  };
  const readTemplate = () => {
    // i — сразу после «`» или после «}», закрывшего подстановку
    let buf = "";
    while (i < text.length) {
      const c = text[i];
      if (c === "\\") {
        buf += text[i + 1];
        i += 2;
      } else if (c === "`") {
        i += 1;
        strings.push(buf);
        out.push(' "" ');
        return;
      } else if (c === "$" && text[i + 1] === "{") {
        i += 2;
        strings.push(buf);
        out.push(" TPL ");
        braces.push(depth);
        depth += 1;
        return;
      } else {
        buf += c;
        i += 1;
      }
    }
  };
  while (i < text.length) {
    const c = text[i];
    const n = text[i + 1];
    if (c === "/" && n === "/") {
      while (i < text.length && text[i] !== "\n") i += 1;
    } else if (c === "/" && n === "*") {
      const e = text.indexOf("*/", i + 2);
      i = e < 0 ? text.length : e + 2;
    } else if (c === '"' || c === "'") {
      let buf = "";
      i += 1;
      while (i < text.length && text[i] !== c) {
        if (text[i] === "\\") {
          buf += text[i + 1];
          i += 2;
        } else buf += text[i++];
      }
      i += 1;
      strings.push(buf);
      out.push(' "" ');
    } else if (c === "`") {
      i += 1;
      readTemplate();
    } else if (c === "/" && (/^[(,=:[!&|?{};+\-*%<>~^]?$/.test(lastSignificant().slice(-1)) || /\b(return|typeof|case)$/.test(lastSignificant()))) {
      // литерал регулярного выражения
      i += 1;
      let cls = false;
      while (i < text.length && (cls || text[i] !== "/")) {
        if (text[i] === "\\") i += 1;
        else if (text[i] === "[") cls = true;
        else if (text[i] === "]") cls = false;
        i += 1;
      }
      i += 1;
      while (/[a-z]/i.test(text[i] ?? "")) i += 1;
      out.push(" RE ");
    } else if (c === "{") {
      depth += 1;
      out.push(c);
      i += 1;
    } else if (c === "}") {
      depth -= 1;
      i += 1;
      if (braces.length && braces[braces.length - 1] === depth) {
        braces.pop();
        readTemplate();
      } else out.push(c);
    } else {
      out.push(c);
      i += 1;
    }
  }
  const code = out.join("").replace(/([^\w\s$"])/g, " $1 ").replace(/\s+/g, " ");
  return { code, strings };
}
const scan = (path: string) => scanText(readFileSync(path, "utf8"));

test("исходники панели найдены сканером", () => {
  const names = sources.map((p) => relative(root, p));
  expect(names).toEqual(expect.arrayContaining(["api.ts", "AdminApp.tsx", "KbScreen.tsx", "UploadForm.tsx", "main.tsx", "texts.ts"]));
});

/** Разрешённые строки, похожие на путь: только префикс админ-API (и сам корень `/api/v1`). */
const ALLOWED_PATH = /^\/(admin\/[a-z0-9/_{}-]*|admin\/|api\/v1)$/;

test.each(sources.map((p) => [relative(root, p), p]))("%s — строки путей только /admin/* и /api/v1 (allowlist)", (_n, p) => {
  for (const s of scan(p).strings) {
    if (/^\/[a-z]/i.test(s) && !/\s/.test(s)) expect(s, `строка «${s}»`).toMatch(ALLOWED_PATH);
  }
});

test.each(sources.map((p) => [relative(root, p), p]))("%s — сетевые примитивы только в api.ts, без обходов загрузки и хранилищ", (_n, p) => {
  const { code } = scan(p);
  if (relative(root, p) !== "api.ts") expect(code).not.toMatch(/\bfetch\s*\(/);
  expect(code).not.toMatch(/\b(XMLHttpRequest|sendBeacon|WebSocket|EventSource|cookieStore|BroadcastChannel|postMessage)\b/);
  expect(code).not.toMatch(/import \. meta \. glob|\brequire\s*\(|new URL \(.*import \. meta/);
  // динамический import — только со строковым литералом
  for (const m of code.matchAll(/\bimport \( (\S+)/g)) expect(m[1], "import() с нелитеральным аргументом").toBe('""');
  expect(code).not.toMatch(/\b(document|window|globalThis|self) \[/);
  expect(code).not.toMatch(/\bwindow \. name\b|\blocalStorage\b|\bsessionStorage\b|\bindexedDB\b|\bcookie\b/);
});

test("сканер видит то, что пропускает регулярное выражение boundary.test.ts (проверка стража)", () => {
  const { code, strings } = scanText(
    [
      'const s = "a//"; localStorage.setItem("t", s);',
      'const p = "/api/v1/" + "memory";',
      "void import(`../api/client`);",
      "void import(`../api/${'client'}`);",
      "const n = window.name;",
      "const re = /^\\/admin\\/[\"']x/; // fetch(\"/memory\")",
      "/* localStorage */ const t = `a ${`b ${1}`} c`;",
    ].join("\n"),
  );
  expect(code).toMatch(/\blocalStorage\b/);
  expect(code).toMatch(/\bwindow \. name\b/);
  expect(strings).toEqual(expect.arrayContaining(["/api/v1/", "../api/client", "../api/", "a ", "b ", " c"]));
  expect([...code.matchAll(/\bimport \( (\S+)/g)].map((m) => m[1])).toEqual(['""', "TPL"]);
  expect(code.match(/localStorage/g)).toHaveLength(1); // из комментария — нет
  expect(code).not.toMatch(/fetch/); // комментарий после регулярного выражения отброшен
  expect("/api/v1/").not.toMatch(ALLOWED_PATH);
});

interface ManifestChunk {
  file: string;
  isEntry?: boolean;
  imports?: string[];
  dynamicImports?: string[];
  src?: string;
}

test("сборка: все чанки, достижимые из admin.html (в т.ч. динамические), — только пути /admin/* и /api/v1", async () => {
  const outDir = mkdtempSync(join(tmpdir(), "a8-admin-build-"));
  // Как `npm run build`: под vitest NODE_ENV=test, и JSX собирается в отладочном виде (с путями файлов).
  const env = process.env.NODE_ENV;
  process.env.NODE_ENV = "production";
  try {
    await build({
      root: front,
      configFile: join(front, "vite.config.ts"),
      logLevel: "silent",
      build: { outDir, emptyOutDir: true, write: true, manifest: "manifest.json" },
    });
    const manifest = JSON.parse(readFileSync(join(outDir, "manifest.json"), "utf8")) as Record<string, ManifestChunk>;
    const reach = (entry: string) => {
      const seen = new Set<string>();
      const walk = (k: string) => {
        if (seen.has(k)) return;
        seen.add(k);
        for (const i of [...(manifest[k].imports ?? []), ...(manifest[k].dynamicImports ?? [])]) walk(i);
      };
      walk(entry);
      return seen;
    };
    expect(Object.keys(manifest)).toEqual(expect.arrayContaining(["admin.html", "index.html"]));
    const adminKeys = reach("admin.html");
    const appKeys = reach("index.html");
    const adminFiles = [...adminKeys].map((k) => manifest[k].file).filter((f) => f.endsWith(".js"));
    expect(adminFiles.length).toBeGreaterThan(0);

    for (const f of adminFiles) {
      const js = readFileSync(join(outDir, f), "utf8");
      for (const m of js.matchAll(/["'`](\/[a-z][a-z0-9_/{}-]*)/gi)) expect(m[1], `${f}: «${m[1]}»`).toMatch(ALLOWED_PATH);
      expect(js).not.toMatch(/telegram-web-app|initData|Telegram\.WebApp/);
    }
    // Mini App не содержит модулей панели (ни как общий чанк, ни лениво)
    const appSrc = [...appKeys].map((k) => manifest[k].src ?? k);
    expect(appSrc.filter((s) => s.includes("src/admin/"))).toEqual([]);
    for (const k of appKeys) {
      const f = manifest[k].file;
      if (f.endsWith(".js")) expect(readFileSync(join(outDir, f), "utf8"), f).not.toContain("/admin/kb/documents");
    }
  } finally {
    process.env.NODE_ENV = env;
    rmSync(outDir, { recursive: true, force: true });
  }
}, 120_000);
