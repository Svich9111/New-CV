/// <reference types="node" />
/**
 * Страница `admin.html` (АР-52): отдельный вход, без Telegram SDK, без встроенных скриптов, обработчиков и стилей
 * (CSP), не индексируется. Стили панели — только токены `theme.css`; красный — только главное действие (ТЗ §8).
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";

const html = readFileSync(join(__dirname, "../../admin.html"), "utf8");
const css = readFileSync(join(__dirname, "admin.css"), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");

test("admin.html: свой модуль src/admin/main.tsx, без Telegram SDK и скриптов Mini App", () => {
  const scripts = [...html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g)];
  expect(scripts).toHaveLength(1);
  expect(scripts[0][1]).toContain('type="module"');
  expect(scripts[0][1]).toContain('src="/src/admin/main.tsx"');
  expect(scripts[0][2].trim()).toBe("");
  expect(html).not.toMatch(/telegram\.org|telegram-web-app|sdk-state/);
  expect(html).not.toContain("sdk-state.js");
  expect(html).not.toContain("/src/main.tsx");
});

test("admin.html: ни встроенных обработчиков, ни style; no-referrer и noindex", () => {
  expect(html).not.toMatch(/\son[a-z]+\s*=/i);
  expect(html).not.toMatch(/\sstyle\s*=/i);
  expect(html).not.toMatch(/<style\b/i);
  expect(html).toContain('<meta name="referrer" content="no-referrer" />');
  expect(html).toMatch(/<meta name="robots" content="noindex/);
});

test("admin.css — без литералов цвета, шрифта и радиуса, только var(--…)", () => {
  expect(css).not.toMatch(/#[0-9a-f]{3,8}\b/i);
  expect(css).not.toMatch(/\b(rgb|rgba|hsl|hsla)\(/i);
  expect(css).not.toMatch(/:\s*(white|black|red)\b/i);
  for (const m of css.matchAll(/(font-size|font-family|border-radius|color|background)\s*:\s*([^;]+);/g)) {
    const value = m[2].trim();
    expect(/var\(--/.test(value) || /^(transparent|inherit|50%|0|none)$/.test(value), `${m[1]}: ${value}`).toBe(true);
  }
});

test("красный акцент в панели — только главное действие (.admin-primary), не ошибки и не статусы", () => {
  const rules = [...css.matchAll(/([^{}]+)\{([^}]*)\}/g)].filter((m) => m[2].includes("var(--accent)"));
  expect(rules.map((m) => m[1].trim())).toEqual([".admin-primary"]);
});
