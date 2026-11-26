/**
 * A4, задача 1.5, раунд 4 — n-6 (docs/reviews/1.5-review-r3.md) и замечание A8 р.3 №3 (1.5-test-r3.md):
 * «Помощь», открытая на «Входим…», остаётся открытой после входа, «Назад» ведёт на первый экран (ТЗ §9.3);
 * сбой чтения токенов цвета в MainButton даёт кнопку в странице, а не падение экрана (ТЗ §8).
 */
import { act, screen } from "@testing-library/react";
import { FakeServer, PARTNER_TOKEN } from "../test/fakeApi";
import { fakeTelegram } from "../test/fakeTelegram";
import { heading, renderApp } from "../test/renderApp";

const helpLink = () => screen.getByRole("button", { name: "Помощь и контакты служб поддержки" });

/** Вход ждёт, пока тест его не отпустит. */
function holdLogin(server: FakeServer) {
  const base = server.fetch;
  let release: () => void = () => undefined;
  const gate = new Promise<void>((r) => (release = r));
  server.fetch = async (i, init) => {
    if (String(i).endsWith("/auth/telegram")) await gate;
    return base(i, init);
  };
  return () => act(async () => release());
}

describe("n-6: «Помощь» до окончания входа остаётся после входа (ТЗ §9.3)", () => {
  const cases: [string, () => FakeServer, string][] = [
    ["новый пользователь", () => new FakeServer(), "Твой личный агент"],
    ["прошёл онбординг, пары нет", () => new FakeServer().onboarded(), "Пригласи партнёра"],
  ];
  for (const [who, make, first] of cases) {
    test(`${who}: вход завершился — «Помощь» на месте, «Назад» (нативная) → «${first}»`, async () => {
      const server = make();
      const release = holdLogin(server);
      const r = renderApp({ server });
      await screen.findByText("Входим через Telegram…");
      await r.user.click(helpLink());
      expect(await heading("Помощь")).toBeInTheDocument();
      expect(await screen.findByRole("link", { name: "112" })).toHaveAttribute("href", "tel:112");
      await release();
      expect(server.calls("POST", "/auth/telegram")).toHaveLength(1);
      await act(async () => new Promise((res) => setTimeout(res, 20)));
      expect(screen.getByRole("heading", { level: 1, name: "Помощь" })).toBeInTheDocument();
      expect(screen.queryByRole("heading", { level: 1, name: first })).toBeNull();
      expect(await screen.findByRole("link", { name: "112" })).toHaveAttribute("href", "tel:112");
      // На «Помощи» нет ссылки на саму себя, а «Назад» видна.
      expect(screen.queryByRole("button", { name: "Помощь и контакты служб поддержки" })).toBeNull();
      expect(r.tg!.back.visible).toBe(true);
      await r.pressBack();
      expect(await heading(first)).toBeInTheDocument();
      // Первый экран — дно стека: «Назад» скрыта, «Помощь» снова доступна ссылкой.
      expect(r.tg!.back.visible).toBe(false);
      expect(helpLink()).toBeInTheDocument();
      await r.pressBack();
      expect(screen.getByRole("heading", { level: 1, name: first })).toBeInTheDocument();
    });
  }

  test("приглашение по startapp: «Помощь» на месте, «Назад» → «Игорь приглашает тебя»", async () => {
    const server = new FakeServer().partnerInvite();
    const release = holdLogin(server);
    const r = renderApp({ server, tg: fakeTelegram({ startParam: PARTNER_TOKEN }) });
    await screen.findByText("Входим через Telegram…");
    await r.user.click(helpLink());
    await heading("Помощь");
    await release();
    await act(async () => new Promise((res) => setTimeout(res, 20)));
    expect(screen.getByRole("heading", { level: 1, name: "Помощь" })).toBeInTheDocument();
    await r.pressBack();
    expect(await heading("Игорь приглашает тебя")).toBeInTheDocument();
  });

  test("кнопки в странице (нет нативной «Назад»): «‹ Назад» с «Помощи» после входа → первый экран", async () => {
    const tg = fakeTelegram({ version: "6.0" });
    const server = new FakeServer();
    const release = holdLogin(server);
    const r = renderApp({ server, tg });
    await screen.findByText("Входим через Telegram…");
    await r.user.click(helpLink());
    await heading("Помощь");
    await release();
    await act(async () => new Promise((res) => setTimeout(res, 20)));
    expect(screen.getByRole("heading", { level: 1, name: "Помощь" })).toBeInTheDocument();
    await r.user.click(screen.getByRole("button", { name: "‹ Назад" }));
    expect(await heading("Твой личный агент")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "‹ Назад" })).toBeNull();
  });

  test("«Помощь» закрыта до окончания входа — после входа первый экран, «Назад» нет (как прежде)", async () => {
    const server = new FakeServer();
    const release = holdLogin(server);
    const r = renderApp({ server });
    await screen.findByText("Входим через Telegram…");
    await r.user.click(helpLink());
    await heading("Помощь");
    await r.pressBack();
    await screen.findByText("Входим через Telegram…");
    await release();
    expect(await heading("Твой личный агент")).toBeInTheDocument();
    expect(r.tg!.back.visible).toBe(false);
  });
});

describe("A8 р.3 №3: сбой getComputedStyle в MainButton — кнопка в странице (ТЗ §8)", () => {
  test("токены цвета не читаются — нативная кнопка скрыта, экран с кнопкой в странице, запасного экрана нет", async () => {
    const real = globalThis.getComputedStyle.bind(globalThis);
    let on = true;
    vi.spyOn(globalThis, "getComputedStyle").mockImplementation((el: Element, p?: string | null) => {
      if (on && el === document.documentElement) throw new Error("getComputedStyle failure");
      return real(el, p);
    });
    const tg = fakeTelegram();
    renderApp({ tg });
    const h = await screen.findByRole("heading", { level: 1, name: "Твой личный агент", hidden: true });
    expect(h).toBeInTheDocument();
    expect(tg.main.visible).toBe(false);
    expect(document.querySelector('[data-screen="crashed"]')).toBeNull();
    expect(document.querySelector('[data-screen="fatal"]')).toBeNull();
    const next = screen.getByRole("button", { name: "Далее", hidden: true });
    expect(next).toHaveClass("main-button");
    on = false;
    await act(async () => next.click());
    expect(await heading("Что приватно")).toBeInTheDocument();
    // Отказ запомнен до конца сессии: на следующем экране тоже кнопка в странице, нативная не показана.
    expect(tg.main.visible).toBe(false);
    expect(screen.getByRole("button", { name: "Далее" })).toHaveClass("main-button");
  });
});
