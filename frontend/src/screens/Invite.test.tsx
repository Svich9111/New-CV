import { act, screen } from "@testing-library/react";
import { FakeServer, TOKEN_A, TOKEN_B } from "../test/fakeApi";
import { heading, renderApp } from "../test/renderApp";
import { POLL_MS } from "./Invite";

function onboarded() {
  return new FakeServer().onboarded();
}

describe("Приглашение — инициатор (ФТ-03, ТЗ §4.1 п.3, §8)", () => {
  test("пары нет: «Ссылки пока нет» → «Создать ссылку-приглашение» → ссылка и срок", async () => {
    const r = renderApp({ server: onboarded() });
    expect(await heading("Пригласи партнёра")).toBeInTheDocument();
    expect(await screen.findByText(/Ссылки пока нет/)).toBeInTheDocument();
    expect(r.tg!.main).toMatchObject({ visible: true, text: "Создать ссылку-приглашение", active: true });
    await r.pressMain();
    expect(await screen.findByLabelText("Ссылка-приглашение")).toHaveTextContent(`startapp=${TOKEN_A}`);
    expect(screen.getByText(/действует 7 дней — до 3 декабря/)).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Ссылка готова. Ещё не отправлена.");
    expect(r.tg!.main.text).toBe("Отправить в Telegram");
  });

  test("«Отправить в Telegram» открывает выбор чата; статус «Ждём партнёра» подписью", async () => {
    const r = renderApp({ server: onboarded() });
    await screen.findByText(/Ссылки пока нет/);
    await r.pressMain();
    await screen.findByLabelText("Ссылка-приглашение");
    await r.pressMain();
    expect(r.tg!.opened).toHaveLength(1);
    const share = new URL(r.tg!.opened[0]);
    expect(share.origin + share.pathname).toBe("https://t.me/share/url");
    expect(share.searchParams.get("url")).toBe(`https://t.me/test_bot?startapp=${TOKEN_A}`);
    expect(share.searchParams.get("text")).not.toMatch(/терап/i);
    expect(await screen.findByText("Ждём партнёра.")).toBeInTheDocument();
    expect(r.tg!.main.text).toBe("Отправить ещё раз в Telegram");
    // Отправка партнёру невозможна, пока пары нет (ТЗ §4.1 п.5).
    expect(screen.getByText(/Отправлять сообщения партнёру можно будет, когда пара создана/)).toBeInTheDocument();
  });

  test("«Скопировать ссылку» кладёт ссылку в буфер обмена", async () => {
    const r = renderApp({ server: onboarded() });
    await screen.findByText(/Ссылки пока нет/);
    await r.pressMain();
    await r.user.click(await screen.findByRole("button", { name: "Скопировать ссылку" }));
    expect(await screen.findByText("Ссылка скопирована.")).toBeInTheDocument();
    expect(await navigator.clipboard.readText()).toBe(`https://t.me/test_bot?startapp=${TOKEN_A}`);
  });

  test("«Новая ссылка» — POST /couple/invite, прежняя заменяется", async () => {
    const r = renderApp({ server: onboarded() });
    await screen.findByText(/Ссылки пока нет/);
    await r.pressMain();
    await screen.findByLabelText("Ссылка-приглашение");
    await r.user.click(screen.getByRole("button", { name: "Новая ссылка" }));
    expect(await screen.findByText(new RegExp(`startapp=${TOKEN_B}`))).toBeInTheDocument();
    expect(r.server.invites.get(TOKEN_A)!.status).toBe("revoked");
    expect(r.server.calls("POST", "/couple/invite")).toHaveLength(1);
  });

  test("приложение открыто заново, пара ждёт: ссылки в памяти нет — «Ждём партнёра» и «Получить новую ссылку»", async () => {
    const r = renderApp({ server: onboarded().pendingCouple() });
    expect(await screen.findByText(/Приглашение уже создано/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Ссылка-приглашение")).not.toBeInTheDocument();
    expect(r.tg!.main.text).toBe("Получить новую ссылку");
    await r.pressMain();
    expect(await screen.findByLabelText("Ссылка-приглашение")).toBeInTheDocument();
  });

  test("партнёр принял: кнопка «Проверить» → «Вы пара»", async () => {
    const r = renderApp({ server: onboarded().pendingCouple() });
    await screen.findByText(/Приглашение уже создано/);
    r.server.partnerAccepts();
    await r.user.click(screen.getByRole("button", { name: "Проверить, принято ли приглашение" }));
    expect(await heading("Вы пара")).toBeInTheDocument();
    expect(await screen.findByText(/Ты и Игорь теперь на связи/)).toBeInTheDocument();
    expect(r.tg!.main.visible).toBe(false);
    expect(r.tg!.haptics).toContain("notify:success");
  });

  test("ожидание: экран сам опрашивает пару раз в POLL_MS", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const r = renderApp({ server: onboarded().pendingCouple() });
      await screen.findByText(/Приглашение уже создано/);
      const before = r.server.calls("GET", "/couple").length;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(POLL_MS);
      });
      expect(r.server.calls("GET", "/couple").length).toBe(before + 1);
      r.server.partnerAccepts();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(POLL_MS);
      });
      expect(await heading("Вы пара")).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  test("возврат в Telegram (activated) — статус проверяется сразу", async () => {
    const r = renderApp({ server: onboarded().pendingCouple() });
    await screen.findByText(/Приглашение уже создано/);
    r.server.partnerAccepts();
    await act(async () => r.tg!.emit("activated"));
    expect(await heading("Вы пара")).toBeInTheDocument();
  });

  test("ошибка загрузки пары — причина подписью и «Попробовать снова»", async () => {
    const server = onboarded().inject("GET", "/couple", 500);
    const r = renderApp({ server });
    expect(await screen.findByRole("alert")).toHaveTextContent("Что-то пошло не так");
    expect(r.tg!.main.text).toBe("Попробовать снова");
    await r.pressMain();
    expect(await screen.findByText(/Ссылки пока нет/)).toBeInTheDocument();
  });

  test("409 already_in_couple при создании — экран перечитывает пару", async () => {
    const server = onboarded();
    const r = renderApp({ server });
    await screen.findByText(/Ссылки пока нет/);
    server.pendingCouple();
    await r.pressMain();
    expect(await screen.findByRole("alert")).toHaveTextContent("У тебя уже есть пара");
    expect(await screen.findByText(/Приглашение уже создано/)).toBeInTheDocument();
  });

  test("429 при выпуске ссылки — сколько ждать, подписью", async () => {
    const server = onboarded().inject("POST", "/couple", 429, { code: "rate_limited", message: "x" }, { "Retry-After": "20" });
    const r = renderApp({ server });
    await screen.findByText(/Ссылки пока нет/);
    await r.pressMain();
    expect(await screen.findByRole("alert")).toHaveTextContent("через 20 с");
  });
});
