import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";

/** Консоль в клиенте запрещена (initData и токены не должны туда попасть): любой вывод — провал теста. */
const methods = ["log", "info", "warn", "error", "debug"] as const;
const calls: string[] = [];

beforeEach(() => {
  calls.length = 0;
  for (const m of methods) {
    vi.spyOn(console, m).mockImplementation((...args: unknown[]) => {
      calls.push(`${m}: ${args.map(String).join(" ")}`);
    });
  }
});

afterEach(() => {
  cleanup();
  delete document.documentElement.dataset.theme;
  delete (window as { Telegram?: unknown }).Telegram;
  localStorage.clear();
  sessionStorage.clear();
  const out = calls.splice(0);
  expect(out, "вывод в консоль").toEqual([]);
});
