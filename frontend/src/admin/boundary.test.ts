/// <reference types="node" />
/**
 * Критерий RoadMap «в панели нет ни одного экрана с текстами переписок» (карточка 2.8, п.2; ТЗ §9.1 п.5; АР-52) —
 * статический запрет в исходниках `src/admin/**`. Исходник разбирается компилятором TypeScript (синтаксическое
 * дерево), а не регулярными выражениями: комментарии, строки, шаблоны и литералы регулярных выражений не путаются
 * (ревью A6 р.1, MAJOR-1).
 * - В `src/admin/` — только `.ts`, `.tsx`, `.css`; файлы `.js/.jsx/.mjs/.cjs/…` тоже разбираются (ревью A6 р.2,
 *   MAJOR-2), но уже само их наличие — нарушение.
 * - Импорт только своих модулей, сгенерированных типов контракта (`../api/schema`, только `import type`), общих
 *   стилей и плашки `../ui/Notice`; спецификатор — только строковый литерал (ни шаблона, ни выражения в `import()`),
 *   без `require`, `import.meta` (`glob`, `new URL(…, import.meta.url)`). Разрешённые модули вне `src/admin`
 *   проверяются по тем же правилам, транзитивно: всё, что они тянут, — тоже из перечня (ревью A6 р.2, MAJOR-1).
 * - Строки, похожие на путь, — по перечню разрешённого (allowlist): `/admin/…` и `/api/v1`; склейка вида
 *   `"/api/v1/" + seg` не проходит.
 * - `fetch` — только в `api.ts`: вне его запрещено любое обращение к глобальному `fetch` (вызов, псевдоним, свойство
 *   `window.fetch`, строка `"fetch"`), кроме типа `typeof fetch` и своего параметра с тем же именем (ревью A6 р.2,
 *   MINOR-1). `XMLHttpRequest`, `sendBeacon`, `WebSocket`, `EventSource`, `BroadcastChannel`, `postMessage`, `Reflect`,
 *   `Function`, `eval`, строковые `setTimeout`/`setInterval` — нигде.
 * - Глобальные объекты (`window`, `globalThis`, `self`, `document`, `navigator`, `top`, `parent`, `frames`) — только
 *   как `объект.свойство`: ни значением (присваивание, аргумент, `as`), ни `объект[…]`, ни `window.name`.
 * - Ни хранилищ браузера, ни cookie (токен — только в памяти вкладки); ни Telegram; ни `console`.
 * - Ни одного пользовательского типа; схемы контракта — только `KbDocument` и `Error`, пути — только `/admin/*`.
 * Помощники тестов (`src/admin/test/`) проверяются по тем же правилам, кроме двух: им можно подменять глобальный
 * `fetch` (`vi.spyOn(globalThis, "fetch")`) и `@testing-library/*`; в сборку панели они не попадают (`adminBuild`).
 * Каждое правило проверено на заведомо плохих примерах («страж падает»), см. конец файла.
 */
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { extname, join, relative, resolve } from "node:path";
import ts from "typescript";

const root = __dirname;
const srcDir = resolve(root, "..");

function files(dir: string): string[] {
  return readdirSync(dir).flatMap((n) => {
    const p = join(dir, n);
    return statSync(p).isDirectory() ? files(p) : [p];
  });
}

/** Расширения, которые компилятор может разобрать как код. */
const CODE_EXT = /\.(ts|tsx|mts|cts|js|jsx|mjs|cjs)$/;
/** Расширения, допустимые в `src/admin/`. */
const ALLOWED_EXT = /\.(ts|tsx|css)$/;

const all = files(root);
const code = all.filter((p) => CODE_EXT.test(p));
const isTest = (p: string) => /\.test\.[cm]?[jt]sx?$/.test(p);
const isHelper = (rel: string) => rel.startsWith("test/");
/** Код панели (без тестов; заглушка и помощники тестов — тоже проверяются: им всё это тоже не нужно). */
const sources = code.filter((p) => !isTest(p));
const read = (p: string) => readFileSync(p, "utf8");

/** Файлы `src/admin/**` с недопустимым расширением (ревью A6 р.2, MAJOR-2). */
const extensionViolations = (names: string[]) => names.filter((n) => !ALLOWED_EXT.test(n));

const SCHEMA = join(srcDir, "api/schema.d.ts");
/** Разрешённые модули вне `src/admin` (после разрешения пути). */
const ALLOWED_OUTSIDE = new Set([SCHEMA, join(srcDir, "ui/Notice.tsx"), join(srcDir, "theme.css"), join(srcDir, "app.css")]);
const CODE_PACKAGES = new Set(["react", "react-dom/client"]);
/** Помощникам тестов и тестам — ещё testing-library; тестам сборки и статическим тестам — node:*, vite, компилятор. */
const HELPER_PACKAGE = (spec: string) => spec.startsWith("@testing-library/");
const TEST_ONLY_PACKAGE = (spec: string) => spec.startsWith("node:") || spec === "vite" || spec === "typescript";

/** Путь модуля, как его разрешит сборщик (без псевдонимов: в `vite.config.ts` их нет). */
function resolveFile(abs: string): string | null {
  const cands = [abs, ...[".ts", ".tsx", ".d.ts", ".js", ".jsx", ".mjs", ".cjs"].map((e) => abs + e), ...["index.ts", "index.tsx", "index.js"].map((i) => join(abs, i))];
  return cands.find((c) => existsSync(c) && statSync(c).isFile()) ?? null;
}
const insideAdmin = (abs: string) => !relative(root, abs).startsWith("..");

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
  // обход статического разбора (ревью A6 р.2, MINOR-1)
  "Reflect",
  "Function",
  "eval",
  "require",
  // Telegram и журнал
  "Telegram",
  "WebApp",
  "initData",
  "initDataUnsafe",
  "console",
]);
const GLOBALS = new Set(["window", "document", "globalThis", "self", "navigator", "top", "parent", "frames"]);
const ALLOWED_SCHEMAS = new Set(["KbDocument", "Error"]);

const literalText = (n: ts.Node): string | null =>
  ts.isStringLiteral(n) || ts.isNoSubstitutionTemplateLiteral(n) ? n.text : null;

function scriptKind(file: string): ts.ScriptKind {
  const e = extname(file);
  return e === ".tsx" ? ts.ScriptKind.TSX : e === ".jsx" ? ts.ScriptKind.JSX : /^\.[cm]?js$/.test(e) ? ts.ScriptKind.JS : ts.ScriptKind.TS;
}

/** Узел внутри типа (`typeof fetch`, `typeof window`) — во время работы его нет. */
function inType(n: ts.Node): boolean {
  for (let p: ts.Node | undefined = n.parent; p; p = p.parent) {
    if (ts.isTypeNode(p)) return true;
    if (ts.isStatement(p) || ts.isSourceFile(p)) return false;
  }
  return false;
}

/** Идентификатор — имя объявления или имя свойства, а не обращение к значению. */
function isDeclarationName(n: ts.Identifier): boolean {
  const p = n.parent;
  return (
    ((ts.isParameter(p) || ts.isVariableDeclaration(p) || ts.isFunctionDeclaration(p) || ts.isPropertySignature(p) || ts.isPropertyDeclaration(p) || ts.isMethodSignature(p) || ts.isMethodDeclaration(p)) && p.name === n) ||
    (ts.isBindingElement(p) && (p.name === n || p.propertyName === n)) ||
    (ts.isPropertyAssignment(p) && p.name === n)
  );
}

/** Имя связано объявлением в объемлющей области (параметр функции, переменная блока) — не глобальное. */
function declaredLocally(n: ts.Identifier): boolean {
  const name = n.text;
  const binds = (b: ts.BindingName): boolean =>
    ts.isIdentifier(b) ? b.text === name : b.elements.some((e) => !ts.isOmittedExpression(e) && binds(e.name));
  const inStatements = (ss: readonly ts.Statement[]) =>
    ss.some(
      (s) =>
        (ts.isVariableStatement(s) && s.declarationList.declarations.some((d) => binds(d.name))) ||
        ((ts.isFunctionDeclaration(s) || ts.isClassDeclaration(s)) && s.name?.text === name),
    );
  for (let p: ts.Node | undefined = n.parent; p; p = p.parent) {
    if (ts.isFunctionLike(p) && p.parameters.some((a) => binds(a.name))) return true;
    if ((ts.isBlock(p) || ts.isSourceFile(p) || ts.isModuleBlock(p)) && inStatements(p.statements)) return true;
    if ((ts.isForStatement(p) || ts.isForOfStatement(p) || ts.isForInStatement(p)) && p.initializer && ts.isVariableDeclarationList(p.initializer) && p.initializer.declarations.some((d) => binds(d.name)))
      return true;
    if (ts.isCatchClause(p) && p.variableDeclaration && binds(p.variableDeclaration.name)) return true;
  }
  return false;
}

/** Идентификатор — обращение к глобальному значению с этим именем. */
const globalReference = (n: ts.Identifier) => !inType(n) && !isDeclarationName(n) && !declaredLocally(n);

/** Импорт только типов (`import type`, `export type`, все элементы `type …`). */
function typeOnly(n: ts.ImportDeclaration | ts.ExportDeclaration): boolean {
  if (ts.isImportDeclaration(n)) {
    const c = n.importClause;
    if (!c) return false;
    if (c.isTypeOnly) return true;
    return !c.name && !!c.namedBindings && ts.isNamedImports(c.namedBindings) && c.namedBindings.elements.length > 0 && c.namedBindings.elements.every((e) => e.isTypeOnly);
  }
  if (n.isTypeOnly) return true;
  return !!n.exportClause && ts.isNamedExports(n.exportClause) && n.exportClause.elements.length > 0 && n.exportClause.elements.every((e) => e.isTypeOnly);
}

interface Opts {
  /** Файл теста: проверяются только импорты, разрешены пакеты тестов. */
  test?: boolean;
  /** Модули вне `src/admin`, на которые ссылается файл (собираются для транзитивной проверки). */
  outside?: Set<string>;
}

/** Нарушения правил панели в одном исходнике (`file` — путь относительно `src/admin`, может быть `../ui/…`). */
function violations(file: string, text: string, opts: Opts = {}): string[] {
  const out: string[] = [];
  const helper = isHelper(file);
  const sf = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, scriptKind(file));
  const at = (n: ts.Node) => `${file}:${sf.getLineAndCharacterOfPosition(n.getStart(sf)).line + 1}`;

  const checkImport = (spec: string, n: ts.Node, onlyTypes: boolean) => {
    if (spec.startsWith("./") || spec.startsWith("../")) {
      const abs = join(root, file, "..", spec);
      if (insideAdmin(abs)) return;
      const target = resolveFile(abs);
      if (!target || !ALLOWED_OUTSIDE.has(target)) out.push(`${at(n)}: импорт «${spec}» вне src/admin`);
      else if (target === SCHEMA && !onlyTypes) out.push(`${at(n)}: «${spec}» — только import type`);
      else opts.outside?.add(target);
    } else if (!CODE_PACKAGES.has(spec) && !((helper || opts.test) && HELPER_PACKAGE(spec)) && !(opts.test && TEST_ONLY_PACKAGE(spec))) {
      out.push(`${at(n)}: пакет «${spec}»`);
    }
  };

  const visit = (n: ts.Node) => {
    // Импорты: только литерал.
    if ((ts.isImportDeclaration(n) || ts.isExportDeclaration(n)) && n.moduleSpecifier) {
      const s = literalText(n.moduleSpecifier);
      if (s === null) out.push(`${at(n)}: нелитеральный спецификатор`);
      else checkImport(s, n, typeOnly(n));
    }
    if (ts.isImportEqualsDeclaration(n)) out.push(`${at(n)}: import = require`);
    if (ts.isCallExpression(n) && n.expression.kind === ts.SyntaxKind.ImportKeyword) {
      const a = n.arguments[0];
      if (n.arguments.length !== 1 || !ts.isStringLiteral(a)) out.push(`${at(n)}: import() с нелитеральным аргументом`);
      else checkImport(a.text, n, false);
    }
    if (ts.isImportTypeNode(n)) {
      const a = n.argument;
      if (ts.isLiteralTypeNode(a) && ts.isStringLiteral(a.literal)) checkImport(a.literal.text, n, true);
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
      if (s.trim().toLowerCase() === "fetch" && file !== "api.ts" && !helper) out.push(`${at(n)}: строка «fetch» вне api.ts`);
    }

    // Идентификаторы.
    if (ts.isIdentifier(n) && FORBIDDEN_IDENTIFIERS.has(n.text)) out.push(`${at(n)}: «${n.text}»`);
    if (ts.isPropertyAccessExpression(n) && ts.isIdentifier(n.expression) && GLOBALS.has(n.expression.text) && n.name.text === "name")
      out.push(`${at(n)}: ${n.expression.text}.name`);
    if (ts.isElementAccessExpression(n) && ts.isIdentifier(n.expression) && GLOBALS.has(n.expression.text))
      out.push(`${at(n)}: ${n.expression.text}[…]`);

    if (ts.isIdentifier(n) && !helper) {
      // Глобальный объект — только как `объект.свойство`, не значением.
      const p = n.parent;
      const asObject = ts.isPropertyAccessExpression(p) && p.expression === n;
      const asName = ts.isPropertyAccessExpression(p) && p.name === n;
      if (GLOBALS.has(n.text) && !asObject && !asName && !ts.isElementAccessExpression(p) && globalReference(n))
        out.push(`${at(n)}: ${n.text} значением`);
      // Любое обращение к глобальному fetch вне api.ts; `x.fetch` — тоже (имя свойства не отличить от глобального).
      if (n.text === "fetch" && file !== "api.ts" && (asName || globalReference(n))) out.push(`${at(n)}: «fetch» вне api.ts`);
    }

    // Сетевой вызов — только в api.ts.
    if (ts.isCallExpression(n) && file !== "api.ts") {
      const e = n.expression;
      const name = ts.isIdentifier(e) ? e.text : ts.isPropertyAccessExpression(e) ? e.name.text : ts.isElementAccessExpression(e) ? literalText(e.argumentExpression) : null;
      if (name === "fetch") out.push(`${at(n)}: fetch() вне api.ts`);
      // Таймеры — только с функцией, не со строкой кода.
      if ((name === "setTimeout" || name === "setInterval") && !(n.arguments[0] && (ts.isArrowFunction(n.arguments[0]) || ts.isFunctionExpression(n.arguments[0]))))
        out.push(`${at(n)}: ${name} не с функцией`);
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

/**
 * Транзитивная проверка модулей вне `src/admin` (ревью A6 р.2, MAJOR-1, б): каждый разрешённый модуль, на который
 * ссылается панель, и всё, что тянет он сам, — по тем же правилам. Стили и сгенерированные типы не разбираются
 * (типы — только через `import type`, проверено в месте импорта).
 */
function outsideViolations(readFile: (p: string) => string = read): { visited: string[]; found: string[] } {
  const queue = new Set<string>();
  for (const p of sources) violations(relative(root, p), readFile(p), { outside: queue });
  const visited: string[] = [];
  const found: string[] = [];
  for (const target of queue) {
    visited.push(relative(srcDir, target));
    if (target === SCHEMA || !CODE_EXT.test(target)) continue;
    found.push(...violations(relative(root, target), readFile(target), { outside: queue }));
  }
  return { visited, found };
}

test("исходники панели найдены (проверка не пустая)", () => {
  const names = sources.map((p) => relative(root, p));
  for (const n of ["api.ts", "AdminApp.tsx", "KbScreen.tsx", "UploadForm.tsx", "Login.tsx", "texts.ts", "main.tsx", "test/fakeAdminApi.ts"])
    expect(names).toContain(n);
});

test("в src/admin только .ts, .tsx, .css (ревью A6 р.2, MAJOR-2)", () => {
  expect(extensionViolations(all.map((p) => relative(root, p)))).toEqual([]);
});

test.each(sources.map((p) => [relative(root, p), p]))("%s — правила панели соблюдены", (name, p) => {
  expect(violations(name, read(p))).toEqual([]);
});

test.each(code.filter(isTest).map((p) => [relative(root, p), p]))("тест %s — импорты только свои, контракт, стили, Notice", (name, p) => {
  expect(importViolations(name, read(p))).toEqual([]);
});

test("модули вне src/admin, которые тянет панель, — только из перечня и по тем же правилам (транзитивно)", () => {
  const { visited, found } = outsideViolations();
  expect(found).toEqual([]);
  expect(visited).toEqual(expect.arrayContaining(["ui/Notice.tsx", "api/schema.d.ts"]));
  for (const v of visited) expect(ALLOWED_OUTSIDE.has(join(srcDir, v)), v).toBe(true);
});

describe("страж падает на нарушениях (ревью A6 р.1, MAJOR-1; р.2, MAJOR-2, MINOR-1; A8 р.1, замечание 1)", () => {
  test.each([
    ["хранилище после «a//» в строке", 'const s = "a//"; localStorage.setItem("t", s);', /localStorage/],
    ["склейка пользовательского пути", 'const p = "/api/v1/" + "memory";', /строка-путь «\/api\/v1\/»/],
    ["путь не из перечня", 'const p = "/memory";', /строка-путь «\/memory»/],
    ["путь API внутри адреса", 'const p = "https://x.test/api/v1/chat/messages";', /не \/admin/],
    ["путь в шаблоне", "const p = `/invites/${id}`;", /строка-путь «\/invites\/»/],
    ["шаблонный import()", "void import(`../api/client`);", /нелитеральным/],
    ["import() с выражением", 'const m = "../api/client"; void import(m);', /нелитеральным/],
    ["статический импорт клиента Mini App", 'import { api } from "../api/client";', /вне src\/admin/],
    ["импорт модуля Mini App с расширением", 'export { SseParser } from "../api/chat.ts";', /вне src\/admin/],
    ["импорт общего модуля вне перечня", 'import { formatTime } from "../ui/format";', /вне src\/admin/],
    ["импорт типа клиента Mini App", 'type T = import("../api/types").MemoryItem;', /вне src\/admin/],
    ["схема контракта не как тип", 'import { paths } from "../api/schema";', /только import type/],
    ["import.meta.glob", 'const m = import.meta.glob("../screens/*.tsx");', /import\.meta/],
    ["new URL(…, import.meta.url)", 'const u = new URL("../api/client.ts", import.meta.url);', /import\.meta/],
    ["require", 'const c = require("../api/client");', /require/],
    ["fetch вне api.ts", 'void fetch("/admin/kb/documents");', /fetch\(\) вне api\.ts/],
    ["globalThis.fetch вне api.ts", 'void globalThis.fetch("/admin/x");', /fetch\(\) вне api\.ts/],
    ["псевдоним fetch (m1)", 'const f = fetch; void f(["", "api", "v1", "chat", "messages"].join("/"));', /«fetch» вне api\.ts/],
    ["fetch в объекте", "const o = { f: fetch };", /«fetch» вне api\.ts/],
    ["fetch в краткой записи вне области параметра", "function a({ fetch }: { fetch: unknown }) { return fetch; }\nconst o = { fetch };", /zzz\.tsx:2: «fetch» вне api\.ts/],
    ["window.fetch без вызова", "const f = window.fetch;", /«fetch» вне api\.ts/],
    ["строка «fetch» (m8)", 'const k = "fetch";', /строка «fetch»/],
    ["Reflect (m3)", 'const st = Reflect.get(o, ["local", "Storage"].join(""));', /«Reflect»/],
    ["Reflect.apply (m8)", "void Reflect.apply(f, o, []);", /«Reflect»/],
    ["конструктор Function", 'new Function("return 1")();', /«Function»/],
    ["eval", 'eval("1");', /«eval»/],
    ["setTimeout со строкой", 'setTimeout("x()", 1);', /setTimeout не с функцией/],
    ["setInterval с переменной", "const c = s; setInterval(c, 1);", /setInterval не с функцией/],
    ["globalThis значением", "const g = globalThis;", /globalThis значением/],
    ["window через as", "const w = window as unknown as Record<string, unknown>;", /window значением/],
    ["self аргументом", "Object.keys(self);", /self значением/],
    ["navigator значением", "const n = navigator;", /navigator значением/],
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
    ["testing-library в коде панели", 'import { render } from "@testing-library/react";', /пакет/],
  ])("%s", (_name, snippet, expected) => {
    const found = violations("zzz.tsx", snippet);
    expect(found.join("\n")).toMatch(expected);
  });

  test.each([
    ["zzz.js: fetch со склейкой через join (m9)", "zzz.js", 'export const leak = () => fetch(["", "api", "v1", "chat", "messages"].join("/"));', /fetch\(\) вне api\.ts/],
    ["zzz.js: переэкспорт клиента Mini App (m5)", "zzz.js", 'export { Api } from "../api/client.ts";', /вне src\/admin/],
    ["zzz.js: переэкспорт разбора чата (m6)", "zzz.js", 'export { SseParser } from "../api/chat.ts";', /вне src\/admin/],
    ["zzz.jsx: хранилище в JSX", "zzz.jsx", "export const N = () => <p>{localStorage.length}</p>;", /localStorage/],
    ["zzz.mjs: require", "zzz.mjs", 'const c = require("../api/client");', /require/],
    ["zzz.cjs: путь", "zzz.cjs", 'module.exports = "/memory";', /строка-путь/],
  ])("%s", (_name, file, snippet, expected) => {
    expect(violations(file, snippet).join("\n")).toMatch(expected);
  });

  test("файлы с расширением кроме .ts/.tsx/.css в src/admin — нарушение (m5, m6, m9)", () => {
    expect(extensionViolations(["zzz.js", "a/b.jsx", "c.mjs", "d.cjs", "e.mts", "f.json", "g.html", "x.ts", "y.tsx", "admin.css"])).toEqual(["zzz.js", "a/b.jsx", "c.mjs", "d.cjs", "e.mts", "f.json", "g.html"]);
  });

  test("транзитивно: Notice, переэкспортирующий модуль Mini App (m10, m11), или с fetch — нарушение", () => {
    const notice = join(srcDir, "ui/Notice.tsx");
    const withText = (extra: string) => (p: string) => (p === notice ? `${read(p)}\n${extra}\n` : read(p));
    expect(outsideViolations(withText('export { SseParser, parseEvent } from "../api/chat";')).found.join("\n")).toMatch(/\.\.\/ui\/Notice\.tsx:\d+: импорт «\.\.\/api\/chat» вне src\/admin/);
    expect(outsideViolations(withText('export * from "./format";')).found.join("\n")).toMatch(/импорт «\.\/format» вне src\/admin/);
    expect(outsideViolations(withText('export const leak = () => fetch("/api/v1/memory");')).found.join("\n")).toMatch(/fetch\(\) вне api\.ts/);
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

  test("без ложных срабатываний: свой параметр fetch, typeof fetch/window, document.getElementById, таймер с функцией", () => {
    const ok = [
      "interface P { fetch?: typeof fetch; w?: typeof window }",
      "export function A({ fetch, n }: P & { n: number }) { return make({ fetch, n }); }",
      "function b(fetch: typeof globalThis.fetch) { const o = { fetch }; return o; }",
      "const t = setTimeout(() => void 0, 5); clearTimeout(t);",
      'const el = document.getElementById("root");',
      'const e = new TypeError("Failed to fetch");',
    ].join("\n");
    expect(violations("zzz.tsx", ok)).toEqual([]);
  });

  test("в api.ts fetch разрешён, остальное — нет", () => {
    expect(violations("api.ts", 'void fetch("/api/v1");')).toEqual([]);
    expect(violations("api.ts", 'void fetch("/api/v1/me");').join()).toMatch(/строка-путь/);
    expect(violations("api.ts", "const g = globalThis;").join()).toMatch(/globalThis значением/);
  });
});
