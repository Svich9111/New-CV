/// <reference types="node" />
/**
 * m-9 (A6, р.1): SDK Telegram не держит страницу пустой, если telegram.org недоступен — скрипт
 * асинхронный, а приложение ждёт его не дольше SDK_WAIT_MS (см. webapp.test.ts).
 * n-3 (A6, р.2): в разметке нет встроенных обработчиков и стилей — отметку загрузки SDK ставит внешний
 * public/sdk-state.js, стиль заглушки #root лежит в app.css (CSP без 'unsafe-inline' и 'unsafe-hashes').
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { SDK_SCRIPT_ID, sdkScriptState } from "./telegram/webapp";

const html = readFileSync(join(__dirname, "../index.html"), "utf8");
const stateJs = readFileSync(join(__dirname, "../public/sdk-state.js"), "utf8");
const sdkTags = [...html.matchAll(/<script\b[^>]*telegram-web-app\.js[^>]*>/g)].map((m) => m[0]);

test("SDK подключён одним асинхронным скриптом с id", () => {
  expect(sdkTags).toHaveLength(1);
  const tag = sdkTags[0];
  expect(tag).toMatch(/\basync\b/);
  expect(tag).toContain(`id="${SDK_SCRIPT_ID}"`);
  expect(tag).toContain('src="https://telegram.org/js/telegram-web-app.js"');
});

test("n-3: ни встроенных обработчиков on*, ни атрибутов style, ни встроенных скриптов", () => {
  expect(html).not.toMatch(/\son[a-z]+\s*=/i);
  expect(html).not.toMatch(/\sstyle\s*=/i);
  expect(html).not.toMatch(/<style\b/i);
  for (const m of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g)) {
    expect(m[1]).toMatch(/\ssrc=/);
    expect(m[2].trim()).toBe("");
  }
});

test("n-3: отметка загрузки — внешний обычный скрипт, подключённый до SDK", () => {
  const helper = html.indexOf('<script src="/sdk-state.js"></script>');
  expect(helper).toBeGreaterThan(-1);
  expect(helper).toBeLessThan(html.indexOf("telegram-web-app.js"));
  expect(stateJs).toContain(`"${SDK_SCRIPT_ID}"`);
});

describe("public/sdk-state.js", () => {
  let script: HTMLScriptElement;
  beforeAll(() => {
    // Тот же код, что исполнит браузер: слушатели в фазе захвата на document.
    new Function(stateJs)();
  });
  beforeEach(() => {
    script = document.createElement("script");
    script.id = SDK_SCRIPT_ID;
    document.head.appendChild(script);
  });
  afterEach(() => script.remove());

  for (const [event, state] of [
    ["load", "loaded"],
    ["error", "failed"],
  ] as const) {
    test(`событие ${event} у скрипта SDK (не всплывает) → data-state="${state}"`, () => {
      expect(sdkScriptState(window)).toBe("pending");
      script.dispatchEvent(new Event(event, { bubbles: false }));
      expect(script.dataset.state).toBe(state);
      expect(sdkScriptState(window)).toBe(state);
    });
  }

  test("события других элементов не трогает", () => {
    const img = document.createElement("img");
    document.body.appendChild(img);
    img.dispatchEvent(new Event("error"));
    expect(img.dataset.state).toBeUndefined();
    expect(script.dataset.state).toBeUndefined();
    img.remove();
  });
});

test("до загрузки приложения в #root есть текст, а стиль заглушки — в app.css", () => {
  expect(html).toMatch(/<div id="root"><p class="boot-placeholder">[\s\S]*Загружаем[\s\S]*<\/div>/);
  const css = readFileSync(join(__dirname, "app.css"), "utf8");
  expect(css).toMatch(/\.boot-placeholder\s*\{[^}]*padding/);
});
