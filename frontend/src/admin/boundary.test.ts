/// <reference types="node" />
/**
 * Критерий RoadMap «в панели нет ни одного экрана с текстами переписок» (карточка 2.8, п.2; ТЗ §9.1 п.5; АР-52) —
 * статический запрет в исходниках `src/admin/**`:
 * - импорт только своих модулей, сгенерированных типов контракта (`../api/schema`), общих стилей и плашки
 *   `../ui/Notice` — ни клиента Mini App (`src/api/client.ts`, `types.ts`, `chat.ts`, `memory.ts`, `messages.ts`), ни
 *   Telegram SDK, ни экранов приложения;
 * - ни одного пользовательского типа (`ChatMessage`, `PartnerMessage`, `MemoryItem`, `Draft` и родственных);
 * - схемы контракта — только `KbDocument` и `Error` (и тело `POST /admin/kb/documents`); пути — только `/admin/*`;
 * - ни хранилищ браузера, ни cookie (токен — только в памяти вкладки), ни `window.Telegram`.
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";

const root = __dirname;

function files(dir: string): string[] {
  return readdirSync(dir).flatMap((n) => {
    const p = join(dir, n);
    return statSync(p).isDirectory() ? files(p) : /\.(ts|tsx)$/.test(n) ? [p] : [];
  });
}

const all = files(root);
const isTest = (p: string) => /\.test\.tsx?$/.test(p);
/** Код панели (без тестов; заглушка и помощники тестов — тоже проверяются: они импортируют только своё). */
const sources = all.filter((p) => !isTest(p));
const read = (p: string) => readFileSync(p, "utf8");
const stripComments = (s: string) => s.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:"'`])\/\/.*$/gm, "$1");

function imports(src: string): string[] {
  return [...src.matchAll(/(?:import|export)\s[^"'`;]*?from\s*["']([^"']+)["']|import\s*["']([^"']+)["']|import\(\s*["']([^"']+)["']\s*\)/g)].map(
    (m) => m[1] ?? m[2] ?? m[3],
  );
}

const ALLOWED_OUTSIDE = new Set(["../api/schema", "../ui/Notice", "../theme.css", "../app.css"]);
const ALLOWED_PACKAGES = new Set(["react", "react-dom/client", "@testing-library/react", "@testing-library/user-event"]);

test("исходники панели найдены (проверка не пустая)", () => {
  const names = sources.map((p) => relative(root, p));
  for (const n of ["api.ts", "AdminApp.tsx", "KbScreen.tsx", "UploadForm.tsx", "main.tsx", "test/fakeAdminApi.ts"]) expect(names).toContain(n);
});

test.each(all.map((p) => [relative(root, p), p]))("импорты %s — только свои, контракт, стили, Notice", (_, p) => {
  for (const spec of imports(read(p))) {
    if (spec.startsWith("./") || spec.startsWith("../")) {
      // Свои модули: ./x и ../x изнутри test/ (в пределах src/admin).
      const resolved = relative(root, join(p, "..", spec));
      const inside = !resolved.startsWith("..");
      expect(inside || ALLOWED_OUTSIDE.has(spec), `${spec}`).toBe(true);
    } else {
      // Тестам сборки и статическим тестам нужны ещё node:* и vite — коду панели нет.
      const testOnly = isTest(p) && (spec.startsWith("node:") || spec === "vite");
      expect(ALLOWED_PACKAGES.has(spec) || testOnly, spec).toBe(true);
    }
  }
});

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

test.each(sources.map((p) => [relative(root, p), p]))("%s — без пользовательских типов, путей, хранилищ и Telegram", (_, p) => {
  const code = stripComments(read(p));
  for (const t of FORBIDDEN_TYPES) expect(code, t).not.toMatch(new RegExp(`\\b${t}\\b`));
  // Схемы контракта — только админские.
  for (const m of code.matchAll(/\["schemas"\]\["(\w+)"\]/g)) expect(["KbDocument", "Error"], m[1]).toContain(m[1]);
  for (const m of code.matchAll(/paths\["([^"]+)"\]/g)) expect(m[1]).toMatch(/^\/admin\//);
  expect(code).not.toMatch(/\b(localStorage|sessionStorage|indexedDB|document\.cookie|caches)\b/);
  expect(code).not.toMatch(/\.Telegram\b|\bTelegram\.WebApp|telegram-web-app|initData|telegram\//);
  expect(code).not.toMatch(/console\./);
  // Строки путей API — только /admin/*.
  for (const m of code.matchAll(/["'`](\/(?:api\/v1\/)?[a-z][\w/-]*)/g)) {
    const s = m[1];
    if (/^\/(api\/v1\/)?(chat|memory|drafts|couple|messages|auth|me|invites|onboarding|export|account|help)\b/.test(s))
      expect.fail(`пользовательский путь ${s} в ${relative(root, p)}`);
  }
});

test("тесты панели тоже не трогают пользовательские модули", () => {
  for (const p of all.filter(isTest)) {
    const code = stripComments(read(p));
    expect(code, relative(root, p)).not.toMatch(/from\s+["'](\.\.\/)+(api\/(client|types|chat|memory|messages)|telegram|screens|app|test\/fakeApi)/);
  }
});
