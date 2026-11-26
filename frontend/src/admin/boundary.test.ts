/// <reference types="node" />
/**
 * Критерий RoadMap «в панели нет ни одного экрана с текстами переписок» (карточка 2.8, п.2; ТЗ §9.1 п.5; АР-52) —
 * статический запрет в исходниках `src/admin/**`. Исходник разбирается компилятором TypeScript (синтаксическое
 * дерево), а не регулярными выражениями: комментарии, строки, шаблоны и литералы регулярных выражений не путаются
 * (ревью A6 р.1, MAJOR-1).
 * - Импорт только своих модулей, сгенерированных типов контракта (`../api/schema`), общих стилей и плашки
 *   `../ui/Notice`; спецификатор — только строковый литерал (ни шаблона, ни выражения в `import()`), без
 *   `require`, `import.meta` (`glob`, `new URL(…, import.meta.url)`).
 * - Строки, похожие на путь, — по перечню разрешённого (allowlist): `/admin/…` и `/api/v1`; склейка вида
 *   `"/api/v1/" + seg` не проходит.
 * - Сетевые вызовы (`fetch(`) — только в `api.ts`; `XMLHttpRequest`, `sendBeacon`, `WebSocket`, `EventSource`,
 *   `BroadcastChannel`, `postMessage` — нигде.
 * - Ни хранилищ браузера, ни cookie, ни `window.name`, ни доступа `window[…]`/`document[…]` (токен — только в памяти
 *   вкладки); ни Telegram; ни `console`.
 * - Ни одного пользовательского типа; схемы контракта — только `KbDocument` и `Error`, пути — только `/admin/*`.
 * Каждое правило проверено на заведомо плохих примерах («страж падает»), см. конец файла.
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import ts from "typescript";

const root = __dirname;

function files(dir: string): string[] {
  return readdirSync(dir).flatMap((n) => {
    const p = join(dir, n);
    return statSync(p).isDirectory() ? files(p) : /\.(ts|tsx)$/.test(n) ? [p] : [];
  });
}

const all = files(root);
const isTest = (p: string) => /\.test\.tsx?$/.test(p);
/** Код панели (без тестов; заглушка и помощники тестов — тоже проверяются: им всё это тоже не нужно). */
const sources = all.filter((p) => !isTest(p));
const read = (p: string) => readFileSync(p, "utf8");

const ALLOWED_OUTSIDE = new Set(["../api/schema", "../ui/Notice", "../theme.css", "../app.css"]);
const ALLOWED_PACKAGES = new Set(["react", "react-dom/client", "@testing-library/react", "@testing-library/user-event"]);
/** Тестам сборки и статическим тестам нужны ещё node:*, vite и компилятор — коду панели нет. */
const TEST_ONLY_PACKAGE = (spec: string) => spec.startsWith("node:") || spec === "vite" || spec === "typescript";

/** Строки, похожие на путь: только префикс админ-API и сам корень `/api/v1`. */
const ALLOWED_PATH = /^\/(admin\/[a-z0-9/_{}-]*|api\/v1)$/;
const looksLikePath = (s: string) => /^\/[a-z]/i.test(s) && !/\s/.test(s);

const FORBIDDEN_TYPES = [
  "ChatMessage",
  "ChatHistory",
  "ChatSendRequest",
  "PartnerMessage",
  "CoupleMessage",
  "MemoryItem",
  "MemoryList",
  "MemoryType",
  "Draft",
  "DraftVariant",
  "UnavailableText",
  "ExportText",
  "Session",
  "Me",
];
const FORBIDDEN_IDENTIFIERS = new Set([
  ...FORBIDDEN_TYPES,
  // хранилища и cookie
  "localStorage",
  "sessionStorage",
  "indexedDB",
  "cookie",
  "cookieStore",
  "caches",
  // сетевые примитивы мимо клиента
  "XMLHttpRequest",
  "sendBeacon",
  "WebSocket",
  "EventSource",
  "BroadcastChannel",
  "postMessage",
  "importScripts",
  // Telegram и журнал
  "Telegram",
  "WebApp",
  "initData",
  "initDataUnsafe",
  "console",
  "eval",
  "require",
]);
const GLOBALS = new Set(["window", "document", "globalThis", "self", "navigator", "top", "parent"]);
const ALLOWED_SCHEMAS = new Set(["KbDocument", "Error"]);

const literalText = (n: ts.Node): string | null =>
  ts.isStringLiteral(n) || ts.isNoSubstitutionTemplateLiteral(n) ? n.text : null;

/** Нарушения правил панели в одном исходнике (`file` — путь относительно `src/admin`). */
function violations(file: string, text: string, opts: { test?: boolean } = {}): string[] {
  const out: string[] = [];
  const kind = file.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  const sf = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, kind);
  const at = (n: ts.Node) => `${file}:${sf.getLineAndCharacterOfPosition(n.getStart(sf)).line + 1}`;

  const checkImport = (spec: string, n: ts.Node) => {
    if (spec.startsWith("./") || spec.startsWith("../")) {
      const inside = !relative(root, join(root, file, "..", spec)).startsWith("..");
      if (!inside && !ALLOWED_OUTSIDE.has(spec)) out.push(`${at(n)}: импорт «${spec}» вне src/admin`);
    } else if (!ALLOWED_PACKAGES.has(spec) && !(opts.test && TEST_ONLY_PACKAGE(spec))) {
      out.push(`${at(n)}: пакет «${spec}»`);
    }
  };

  const visit = (n: ts.Node) => {
    // Импорты: только литерал.
    if ((ts.isImportDeclaration(n) || ts.isExportDeclaration(n)) && n.moduleSpecifier) {
      const s = literalText(n.moduleSpecifier);
      if (s === null) out.push(`${at(n)}: нелитеральный спецификатор`);
      else checkImport(s, n);
    }
    if (ts.isImportEqualsDeclaration(n)) out.push(`${at(n)}: import = require`);
    if (ts.isCallExpression(n) && n.expression.kind === ts.SyntaxKind.ImportKeyword) {
      const a = n.arguments[0];
      if (n.arguments.length !== 1 || !ts.isStringLiteral(a)) out.push(`${at(n)}: import() с нелитеральным аргументом`);
      else checkImport(a.text, n);
    }
    if (ts.isImportTypeNode(n)) {
      const a = n.argument;
      if (ts.isLiteralTypeNode(a) && ts.isStringLiteral(a.literal)) checkImport(a.literal.text, n);
      else out.push(`${at(n)}: import("…") в типе с нелитеральным аргументом`);
    }
    if (ts.isMetaProperty(n) && n.keywordToken === ts.SyntaxKind.ImportKeyword) out.push(`${at(n)}: import.meta`);

    // Строки и части шаблонов: пути — по allowlist.
    const pieces: string[] = [];
    const lit = literalText(n);
    if (lit !== null) pieces.push(lit);
    if (ts.isTemplateHead(n) || ts.isTemplateMiddle(n) || ts.isTemplateTail(n)) pieces.push(n.text);
    for (const s of pieces) {
      if (looksLikePath(s) && !ALLOWED_PATH.test(s)) out.push(`${at(n)}: строка-путь «${s}»`);
      if (/\/api\/v1\/(?!admin\/)/.test(s)) out.push(`${at(n)}: «${s}» — путь API не /admin/*`);
      if (/telegram\.org|telegram-web-app/i.test(s)) out.push(`${at(n)}: Telegram SDK`);
    }

    // Идентификаторы.
    if (ts.isIdentifier(n) && FORBIDDEN_IDENTIFIERS.has(n.text)) out.push(`${at(n)}: «${n.text}»`);
    if (ts.isPropertyAccessExpression(n) && ts.isIdentifier(n.expression) && GLOBALS.has(n.expression.text) && n.name.text === "name")
      out.push(`${at(n)}: ${n.expression.text}.name`);
    if (ts.isElementAccessExpression(n) && ts.isIdentifier(n.expression) && GLOBALS.has(n.expression.text))
      out.push(`${at(n)}: ${n.expression.text}[…]`);

    // Сетевой вызов — только в api.ts.
    if (ts.isCallExpression(n) && file !== "api.ts") {
      const e = n.expression;
      const name = ts.isIdentifier(e) ? e.text : ts.isPropertyAccessExpression(e) ? e.name.text : ts.isElementAccessExpression(e) ? literalText(e.argumentExpression) : null;
      if (name === "fetch") out.push(`${at(n)}: fetch() вне api.ts`);
    }

    // Схемы и пути контракта в типах: components["schemas"]["X"], paths["/…"].
    if (ts.isIndexedAccessTypeNode(n) && ts.isLiteralTypeNode(n.indexType) && ts.isStringLiteral(n.indexType.literal)) {
      const key = n.indexType.literal.text;
      const obj = n.objectType;
      if (ts.isIndexedAccessTypeNode(obj) && ts.isLiteralTypeNode(obj.indexType) && ts.isStringLiteral(obj.indexType.literal) && obj.indexType.literal.text === "schemas" && !ALLOWED_SCHEMAS.has(key))
        out.push(`${at(n)}: схема «${key}»`);
      if (ts.isTypeReferenceNode(obj) && ts.isIdentifier(obj.typeName) && obj.typeName.text === "paths" && !key.startsWith("/admin/"))
        out.push(`${at(n)}: путь контракта «${key}»`);
    }
    ts.forEachChild(n, visit);
  };
  visit(sf);
  return out;
}

/** Только импорты (для тестов: им можно document.cookie и хранилища — они проверяют, что токена там нет). */
function importViolations(file: string, text: string): string[] {
  return violations(file, text, { test: true }).filter((v) => /импорт|пакет|нелитеральн|import/.test(v));
}

test("исходники панели найдены (проверка не пустая)", () => {
  const names = sources.map((p) => relative(root, p));
  for (const n of ["api.ts", "AdminApp.tsx", "KbScreen.tsx", "UploadForm.tsx", "Login.tsx", "texts.ts", "main.tsx", "test/fakeAdminApi.ts"])
    expect(names).toContain(n);
});

test.each(sources.map((p) => [relative(root, p), p]))("%s — правила панели соблюдены", (name, p) => {
  expect(violations(name, read(p))).toEqual([]);
});

test.each(all.filter(isTest).map((p) => [relative(root, p), p]))("тест %s — импорты только свои, контракт, стили, Notice", (name, p) => {
  expect(importViolations(name, read(p))).toEqual([]);
});

describe("страж падает на нарушениях (ревью A6 р.1, MAJOR-1; A8 р.1, замечание 1)", () => {
  test.each([
    ["хранилище после «a//» в строке", 'const s = "a//"; localStorage.setItem("t", s);', /localStorage/],
    ["склейка пользовательского пути", 'const p = "/api/v1/" + "memory";', /строка-путь «\/api\/v1\/»/],
    ["путь не из перечня", 'const p = "/memory";', /строка-путь «\/memory»/],
    ["путь API внутри адреса", 'const p = "https://x.test/api/v1/chat/messages";', /не \/admin/],
    ["путь в шаблоне", "const p = `/invites/${id}`;", /строка-путь «\/invites\/»/],
    ["шаблонный import()", "void import(`../api/client`);", /нелитеральным/],
    ["import() с выражением", 'const m = "../api/client"; void import(m);', /нелитеральным/],
    ["статический импорт клиента Mini App", 'import { api } from "../api/client";', /вне src\/admin/],
    ["импорт типа клиента Mini App", 'type T = import("../api/types").MemoryItem;', /вне src\/admin/],
    ["import.meta.glob", 'const m = import.meta.glob("../screens/*.tsx");', /import\.meta/],
    ["new URL(…, import.meta.url)", 'const u = new URL("../api/client.ts", import.meta.url);', /import\.meta/],
    ["require", 'const c = require("../api/client");', /require/],
    ["fetch вне api.ts", 'void fetch("/admin/kb/documents");', /fetch\(\) вне api\.ts/],
    ["globalThis.fetch вне api.ts", 'void globalThis.fetch("/admin/x");', /fetch\(\) вне api\.ts/],
    ["sendBeacon", 'navigator.sendBeacon("/admin/x", "t");', /sendBeacon/],
    ["XMLHttpRequest", "const x = new XMLHttpRequest();", /XMLHttpRequest/],
    ["WebSocket", 'const w = new WebSocket("wss://x");', /WebSocket/],
    ["EventSource", 'const e = new EventSource("/admin/x");', /EventSource/],
    ["window.name", "window.name = token;", /window\.name/],
    ['document["cookie"]', 'document["cookie"] = "t=1";', /document\[…\]/],
    ["cookieStore", 'void cookieStore.set("t", "1");', /cookieStore/],
    ["Telegram", "const t = window.Telegram;", /Telegram/],
    ["console", 'console.log("t");', /console/],
    ["пользовательский тип", "let m: MemoryItem | null = null;", /MemoryItem/],
    ["чужая схема контракта", 'type X = components["schemas"]["ChatMessage"];', /схема «ChatMessage»/],
    ["чужой путь контракта", 'type X = paths["/memory"];', /путь контракта «\/memory»/],
    ["чужой пакет", 'import x from "@telegram-apps/sdk";', /пакет/],
  ])("%s", (_name, snippet, expected) => {
    const found = violations("zzz.tsx", snippet);
    expect(found.join("\n")).toMatch(expected);
  });

  test("комментарии и регулярные выражения не дают ложных срабатываний и не прячут код", () => {
    const ok = [
      "// localStorage, fetch(\"/memory\")",
      "/* window.name */",
      'const re = /^\\/memory\\/["\']x/; // fetch("/chat")',
      'const a = "a//b"; const t = `x ${"/admin/kb/documents"} y`;',
      "const n = <p>Текст: «/не путь» и ссылка</p>;",
    ].join("\n");
    expect(violations("zzz.tsx", ok)).toEqual([]);
    expect(violations("zzz.tsx", 'const re = /x/; localStorage.clear();').join()).toMatch(/localStorage/);
  });

  test("в api.ts fetch разрешён, остальное — нет", () => {
    expect(violations("api.ts", 'void fetch("/api/v1");')).toEqual([]);
    expect(violations("api.ts", 'void fetch("/api/v1/me");').join()).toMatch(/строка-путь/);
  });
});
