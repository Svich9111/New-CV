/// <reference types="node" />
import { readFileSync } from "node:fs";
import { join } from "node:path";

// Файлы читаются с диска: в vitest импорт CSS (в том числе `?raw`) даёт пустую строку.
const read = (rel: string) => readFileSync(join(__dirname, rel), "utf8");
const theme = read("theme.css");
const app = read("app.css");
const proto = read("../prototype/styles.css");

const stripComments = (css: string) => css.replace(/\/\*[\s\S]*?\*\//g, "");

function block(css: string, selector: string): Record<string, string> {
  const re = new RegExp(selector.replace(/[[\]"()]/g, "\\$&") + "\\s*\\{([^}]*)\\}", "g");
  const out: Record<string, string> = {};
  for (const m of stripComments(css).matchAll(re))
    for (const d of m[1].matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) out[d[1]] = d[2].trim().toLowerCase();
  return out;
}

describe("Токены дизайна (ТЗ §8; смена дизайна после Г1 — правка theme.css)", () => {
  test("палитра §8: белый фон, текст #111111, акцент #E11D2E; тёмная — чёрный фон, белый текст, тот же акцент", () => {
    const light = block(theme, ":root");
    const dark = block(theme, ':root[data-theme="dark"]');
    expect(light["--bg"]).toBe("#ffffff");
    expect(light["--text"]).toBe("#111111");
    expect(light["--accent"]).toBe("#e11d2e");
    expect(dark["--bg"]).toBe("#000000");
    expect(dark["--text"]).toBe("#ffffff");
    expect(dark["--accent"]).toBeUndefined();
  });

  test("тёмная тема по data-theme и запасная по prefers-color-scheme задают одни и те же значения", () => {
    const dark = block(theme, ':root[data-theme="dark"]');
    const media = block(theme, ':root:not([data-theme="light"])');
    expect(media).toEqual(dark);
  });

  test("базовые токены совпадают с прототипом 0.6 (prototype/styles.css)", () => {
    for (const sel of [":root", ':root[data-theme="dark"]']) {
      const ours = block(theme, sel);
      const theirs = block(proto, sel);
      for (const k of ["--bg", "--bg-secondary", "--text", "--accent", "--radius", "--text-muted", "--line", "--disabled-bg", "--disabled-text", "--card-bg", "--on-accent"])
        if (k in ours || k in theirs) expect(ours[k], `${sel} ${k}`).toBe(theirs[k]);
    }
  });

  test("app.css без литералов цвета, шрифта и размера шрифта — только var(--…)", () => {
    const css = stripComments(app);
    expect(css).not.toMatch(/#[0-9a-f]{3,8}\b/i);
    expect(css).not.toMatch(/\b(rgb|rgba|hsl|hsla)\(/i);
    expect(css).not.toMatch(/:\s*(white|black|red)\b/i);
    for (const m of css.matchAll(/(font-size|font-family|border-radius|color|background)\s*:\s*([^;]+);/g)) {
      const value = m[2].trim();
      const ok = /var\(--/.test(value) || /^(transparent|inherit|50%|0|none)$/.test(value);
      expect(ok, `${m[1]}: ${value}`).toBe(true);
    }
  });

  test("красный акцент — только главная кнопка и сердце (знак близости), не ошибки", () => {
    const css = stripComments(app);
    const rules = [...css.matchAll(/([^{}]+)\{([^}]*)\}/g)].filter((m) => m[2].includes("var(--accent)"));
    const selectors = rules.map((m) => m[1].trim());
    expect(selectors).toEqual([".heart", ".main-button"]);
  });
});
