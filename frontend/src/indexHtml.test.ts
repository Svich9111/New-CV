/// <reference types="node" />
/**
 * m-9 (A6, р.1): SDK Telegram не держит страницу пустой, если telegram.org недоступен — скрипт
 * асинхронный, а приложение ждёт его не дольше SDK_WAIT_MS (см. webapp.test.ts).
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { SDK_SCRIPT_ID } from "./telegram/webapp";

const html = readFileSync(join(__dirname, "../index.html"), "utf8");
const sdkTags = [...html.matchAll(/<script\b[^>]*telegram-web-app\.js[^>]*>/g)].map((m) => m[0]);

test("SDK подключён одним асинхронным скриптом с id и отметкой загрузки/ошибки", () => {
  expect(sdkTags).toHaveLength(1);
  const tag = sdkTags[0];
  expect(tag).toMatch(/\basync\b/);
  expect(tag).toContain(`id="${SDK_SCRIPT_ID}"`);
  expect(tag).toContain('src="https://telegram.org/js/telegram-web-app.js"');
  expect(tag).toContain(`onload="this.dataset.state='loaded'"`);
  expect(tag).toContain(`onerror="this.dataset.state='failed'"`);
});

test("до загрузки приложения в #root есть текст, а не пустая страница", () => {
  expect(html).toMatch(/<div id="root">[\s\S]*Загружаем[\s\S]*<\/div>/);
});
