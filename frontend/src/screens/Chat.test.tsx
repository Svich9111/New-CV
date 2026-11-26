/**
 * A4, задача 2.2 — экран чата с личным агентом (ФТ-05; ТЗ §2 п.4, п.7, §4.2, §8, §9.3, §10) на заглушке API
 * по контракту. Тексты выдуманные.
 */
import { act, screen, waitFor, within } from "@testing-library/react";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { FakeServer, crisis, delta, done, errorEvent, gate } from "../test/fakeApi";
import { heading, renderApp, type Rendered } from "../test/renderApp";

function pairedServer(): FakeServer {
  const server = new FakeServer().onboarded().pendingCouple();
  server.partnerAccepts();
  return server;
}

async function openChat(server = pairedServer()): Promise<Rendered> {
  const r = renderApp({ server });
  await heading("Вы пара");
  await r.pressMain();
  await heading("Твой агент");
  await screen.findByRole("textbox", { name: "Сообщение агенту" });
  await waitFor(() => expect(screen.queryByText("Загружаем переписку…")).toBeNull());
  return r;
}

const input = () => screen.getByRole("textbox", { name: "Сообщение агенту" }) as HTMLTextAreaElement;
const sendBtn = () => screen.getByRole("button", { name: "Отправить агенту" });
const list = () => screen.getByRole("list", { name: "Переписка с агентом" });
const bubbles = () =>
  [...list().querySelectorAll<HTMLElement>(":scope > li")].filter((li) => !li.hasAttribute("data-typing") && !li.hasAttribute("data-checking"));
const texts = () => bubbles().map((li) => `${li.dataset.author}:${li.querySelector(".msg__text")?.textContent}`);
const live = () => document.querySelector("[data-live]") as HTMLElement;
const posts = (r: Rendered) => r.server.calls("POST", "/chat/messages");

async function type(r: Rendered, text: string) {
  await r.user.clear(input());
  await r.user.type(input(), text);
}

async function say(r: Rendered, text: string) {
  await type(r, text);
  await r.user.click(sendBtn());
}

describe("Вход в чат", () => {
  test("с главного экрана «Вы пара» — MainButton «Поговорить с агентом» и карточка; «Назад» — на главный", async () => {
    const r = renderApp({ server: pairedServer() });
    await heading("Вы пара");
    expect(r.tg!.main.text).toBe("Поговорить с агентом");
    await r.user.click(screen.getByRole("button", { name: /Поговорить с агентом/ }));
    await heading("Твой агент");
    expect(r.tg!.main.visible).toBe(false);
    expect(r.tg!.back.visible).toBe(true);
    await r.pressBack();
    await heading("Вы пара");
  });

  test("с экрана приглашения (пара ещё ждёт) — кнопка «Поговорить с агентом»", async () => {
    const r = renderApp({ server: new FakeServer().onboarded().pendingCouple() });
    await screen.findByText(/Приглашение уже создано/);
    await r.user.click(screen.getByRole("button", { name: "Поговорить с агентом" }));
    await heading("Твой агент");
  });

  test("на экране сказано, что разговор приватен; «Помощь» в закреплённом низу", async () => {
    const r = await openChat();
    expect(screen.getByText(/Этот разговор видишь только ты\. Партнёр и его агент его не видят/)).toBeInTheDocument();
    const help = screen.getByRole("button", { name: "Помощь и контакты служб поддержки" });
    expect(help.closest(".chat-bottom")).not.toBeNull();
    await r.user.click(help);
    await heading("Помощь");
  });
});

describe("История и курсор", () => {
  test("первая страница — от старых к новым; «Показать более ранние» — по курсору; в конце кнопки нет", async () => {
    const server = pairedServer().seedChat(35);
    const r = await openChat(server);
    expect(bubbles()).toHaveLength(30);
    expect(texts()[0]).toBe("agent:Реплика 6");
    expect(texts()[29]).toBe("owner:Реплика 35");
    await r.user.click(screen.getByRole("button", { name: "Показать более ранние сообщения" }));
    await waitFor(() => expect(bubbles()).toHaveLength(35));
    expect(texts()[0]).toBe("owner:Реплика 1");
    expect(screen.queryByRole("button", { name: "Показать более ранние сообщения" })).toBeNull();
    const q = server.calls("GET", "/chat/messages").map((c) => c.query);
    expect(q[0]).toBe("?limit=30");
    expect(q[1]).toMatch(/^\?limit=30&cursor=/);
  });

  test("422 по курсору (смена ключа сервера) — история с первой страницы, прежний курсор не повторяется", async () => {
    const server = pairedServer().seedChat(65);
    const r = await openChat(server);
    await r.user.click(screen.getByRole("button", { name: "Показать более ранние сообщения" }));
    await waitFor(() => expect(bubbles()).toHaveLength(60));
    server.cursorKey = 2; // ключ сменился: выданные курсоры больше не принимаются
    await r.user.click(screen.getByRole("button", { name: "Показать более ранние сообщения" }));
    expect(await screen.findByText(/Переписку пришлось загрузить заново/)).toBeInTheDocument();
    expect(bubbles()).toHaveLength(30);
    expect(texts()[29]).toBe("owner:Реплика 65");
    const q = server.calls("GET", "/chat/messages").map((c) => c.query);
    expect(q).toHaveLength(4);
    expect(q[3]).toBe("?limit=30"); // заново — без курсора
    // Дальше — по новому курсору, без 422.
    await r.user.click(screen.getByRole("button", { name: "Показать более ранние сообщения" }));
    await waitFor(() => expect(bubbles()).toHaveLength(60));
    const last = server.calls("GET", "/chat/messages").at(-1)!.query;
    expect(last).toMatch(/cursor=c\.2\./);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  test("ошибка загрузки — причина подписью и повтор; писать можно и без истории", async () => {
    const server = pairedServer();
    server.inject("GET", "/chat/messages", 503, { code: "service_unavailable", message: "x" });
    const r = renderApp({ server });
    await heading("Вы пара");
    await r.pressMain();
    expect(await screen.findByRole("alert")).toHaveTextContent(/Сервис временно недоступен/);
    await r.user.click(screen.getByRole("button", { name: "Загрузить переписку снова" }));
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  });

  test("ответ истории не по схеме — «История пришла с ошибкой», чужого автора на экране нет", async () => {
    const server = pairedServer();
    server.inject("GET", "/chat/messages", 200, {
      items: [{ id: "x1", author: "partner", text: "Секрет партнёра", created_at: "2026-11-20T10:00:00Z" }],
      next_cursor: null,
    });
    const r = renderApp({ server });
    await heading("Вы пара");
    await r.pressMain();
    expect(await screen.findByRole("alert")).toHaveTextContent(/История пришла с ошибкой/);
    expect(document.body.textContent).not.toContain("Секрет партнёра");
  });

  test("нерасшифрованный текст — подписью, без падения", async () => {
    const server = pairedServer();
    server.addChat("owner", { unavailable: true, reason: "unknown_key" });
    await openChat(server);
    expect(screen.getByText("Текст недоступен: его не удалось расшифровать.")).toBeInTheDocument();
  });
});

describe("Отправка и поток ответа", () => {
  test("«Агент пишет…», куски по мере прихода, итог; поле очищено; одна реплика на сервере", async () => {
    const server = pairedServer();
    const g = gate();
    server.reply({ steps: [g.step, delta("Слышу. "), delta("Что сейчас\nчувствуешь?"), done("a1")] });
    const r = await openChat(server);
    await say(r, "Мне тревожно");
    expect(input().value).toBe("");
    expect(within(list()).getByText("Агент пишет…")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Остановить" })).toBeInTheDocument();
    expect(live()).toHaveTextContent("Агент пишет…");
    await act(async () => g.open());
    await waitFor(() => expect(texts()).toEqual(["owner:Мне тревожно", "agent:Слышу. Что сейчас\nчувствуешь?"]));
    expect(screen.queryByText("Агент пишет…")).toBeNull();
    expect(sendBtn()).toBeDisabled(); // поле пустое
    expect(posts(r)).toHaveLength(1);
    expect(server.chat.map((m) => m.author)).toEqual(["owner", "agent"]);
  });

  test("целиком одним куском — тот же итог", async () => {
    const server = pairedServer();
    server.reply({ chunk: "whole", steps: [delta("Раз, "), delta("два."), done("a1")] });
    const r = await openChat(server);
    await say(r, "Привет");
    await waitFor(() => expect(texts()).toEqual(["owner:Привет", "agent:Раз, два."]));
  });

  test("aria-live: во время потока — «Агент пишет…», без кусков; после — ответ целиком; пузырь потока aria-busy", async () => {
    const server = pairedServer();
    const g = gate();
    server.reply({ steps: [delta("Первый кусок "), g.step, delta("второй."), done("a1")] });
    const r = await openChat(server);
    expect(live()).toHaveAttribute("aria-live", "polite");
    await say(r, "Привет");
    await waitFor(() => expect(texts()).toContain("agent:Первый кусок "));
    expect(live().textContent).toBe("Агент пишет…");
    expect(bubbles()[1]).toHaveAttribute("aria-busy", "true");
    expect(within(bubbles()[1]).getByText("Агент пишет…")).toBeInTheDocument();
    await act(async () => g.open());
    await waitFor(() => expect(live().textContent).toBe("Ответ агента: Первый кусок второй."));
    expect(bubbles()[1]).not.toHaveAttribute("aria-busy");
  });

  test("Enter — отправить, Shift+Enter — новая строка; пустое и из пробелов не уходит", async () => {
    const r = await openChat();
    await r.user.type(input(), "   ");
    expect(sendBtn()).toBeDisabled();
    await r.user.type(input(), "{Enter}");
    expect(posts(r)).toHaveLength(0);
    await r.user.clear(input());
    await r.user.type(input(), "Строка 1{Shift>}{Enter}{/Shift}Строка 2{Enter}");
    await waitFor(() => expect(posts(r)).toHaveLength(1));
    expect(posts(r)[0].body).toEqual({ text: "Строка 1\nСтрока 2" });
  });

  test("двойное нажатие (второе касание попадает в «Остановить» на том же месте) и Enter во время ответа — одна реплика, ответ не остановлен", async () => {
    const server = pairedServer();
    const g = gate();
    server.reply({ steps: [g.step, delta("Ок"), done()] });
    const r = await openChat(server);
    await type(r, "Один раз");
    const btn = sendBtn();
    await r.user.dblClick(btn);
    await r.user.type(input(), "ещё{Enter}");
    expect(posts(r)).toHaveLength(1);
    await act(async () => g.open());
    await waitFor(() => expect(texts()).toEqual(["owner:Один раз", "agent:Ок"]));
    expect(r.server.streamsAborted).toBe(0);
  });

  test("длина: не больше 4000 символов, счётчик с 3500", async () => {
    const r = await openChat();
    await act(async () => {
      const el = input();
      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!;
      setter.call(el, "я".repeat(4100));
      el.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(input().value).toHaveLength(4000);
    expect(input()).toHaveAttribute("maxlength", "4000");
    expect(screen.getByText("4000 из 4000 символов")).toBeInTheDocument();
    await r.user.click(sendBtn());
    await waitFor(() => expect(posts(r)).toHaveLength(1));
    expect((posts(r)[0].body as { text: string }).text).toHaveLength(4000);
  });

  test("уход в «Помощь» посреди ответа не обрывает поток; «Назад» — ответ на месте, ввод сохранён", async () => {
    const server = pairedServer();
    const g = gate();
    server.reply({ steps: [delta("Начало "), g.step, delta("конец."), done("a1")] });
    const r = await openChat(server);
    await say(r, "Мне плохо");
    await waitFor(() => expect(texts()).toContain("agent:Начало "));
    await r.user.type(input(), "недописанное");
    await r.user.click(screen.getByRole("button", { name: "Помощь и контакты служб поддержки" }));
    await heading("Помощь");
    await act(async () => g.open());
    await r.pressBack();
    await heading("Твой агент");
    await waitFor(() => expect(texts()).toEqual(["owner:Мне плохо", "agent:Начало конец."]));
    expect(input().value).toBe("недописанное");
    expect(server.streamsAborted).toBe(0);
    expect(server.calls("GET", "/chat/messages")).toHaveLength(1);
  });
});

describe("Отмена, обрыв и повтор без дубля реплики", () => {
  test("«Остановить»: поток закрыт, кусок помечен «остановлен и не сохранён»; «Повторить» — тот же пузырь", async () => {
    const server = pairedServer();
    const g = gate();
    server.reply({ steps: [delta("Недопис"), g.step, delta("ано"), done()] });
    const r = await openChat(server);
    await say(r, "Вопрос");
    await waitFor(() => expect(texts()).toContain("agent:Недопис"));
    r.advanceClock(1000); // не второе касание двойного нажатия
    await r.user.click(screen.getByRole("button", { name: "Остановить" }));
    expect(await screen.findByText("Ответ остановлен и не сохранён.")).toBeInTheDocument();
    expect(server.streamsAborted).toBe(1);
    expect(await screen.findByText(/Ответ остановлен\. Сообщение сохранено, ответа нет\./)).toBeInTheDocument();
    g.open();
    server.reply({ steps: [delta("Полный ответ."), done("a2")] });
    await r.user.click(screen.getByRole("button", { name: "Повторить" }));
    await waitFor(() => expect(texts()).toEqual(["owner:Вопрос", "agent:Полный ответ."]));
    expect(posts(r)).toHaveLength(2);
  });

  test("обрыв, а ответ агента сервер успел сохранить — показан он, реплика не отправляется снова", async () => {
    const server = pairedServer().seedChat(2);
    server.reply({ steps: [delta("Ответ "), { saveAgent: "Ответ целиком." }] }); // done не дошёл
    const r = await openChat(server);
    await say(r, "Как быть?");
    await waitFor(() => expect(texts()).toEqual(["owner:Реплика 1", "agent:Реплика 2", "owner:Как быть?", "agent:Ответ целиком."]));
    expect(screen.queryByRole("button", { name: /Повторить/ })).toBeNull();
    expect(posts(r)).toHaveLength(1);
    expect(live().textContent).toBe("Ответ агента: Ответ целиком.");
  });

  test("обрыв без ответа — «Повторить»; после повтора один пузырь, и после перечитывания истории тоже", async () => {
    const server = pairedServer();
    server.reply({ steps: [delta("Нача")] });
    const r = await openChat(server);
    await say(r, "Помоги");
    expect(await screen.findByText(/Связь прервалась — ответ агента не дошёл\. Сообщение сохранено, ответа нет\./)).toBeInTheDocument();
    expect(screen.getByText("Ответ прервался и не сохранён.")).toBeInTheDocument();
    server.reply({ steps: [delta("Вот ответ."), done("a1")] });
    await r.user.click(screen.getByRole("button", { name: "Повторить" }));
    await waitFor(() => expect(texts()).toEqual(["owner:Помоги", "agent:Вот ответ."]));
    expect(screen.queryByText("Ответ прервался и не сохранён.")).toBeNull();
    // Сервер хранит обе попытки (контракт); на экране — одна реплика, и при новом открытии тоже.
    expect(server.chat.map((m) => `${m.author}:${m.text}`)).toEqual(["owner:Помоги", "owner:Помоги", "agent:Вот ответ."]);
    await r.pressBack();
    await heading("Вы пара");
  });

  test("сверка после обрыва не дублирует прежние реплики этой сессии (их id клиенту не известен)", async () => {
    const server = pairedServer();
    server.reply({ steps: [delta("Первый ответ."), done("a1")] });
    server.reply({ status: 429, body: { code: "rate_limited", message: "x" }, headers: { "Retry-After": "60" } });
    server.reply({ steps: [delta("Нач"), { saveAgent: "Третий ответ." }] });
    const r = await openChat(server);
    await say(r, "Первая");
    await waitFor(() => expect(texts()).toEqual(["owner:Первая", "agent:Первый ответ."]));
    await say(r, "Вторая");
    await screen.findByText(/лимит сообщений агенту исчерпан/);
    await say(r, "Третья");
    await waitFor(() =>
      expect(texts()).toEqual(["owner:Первая", "agent:Первый ответ.", "owner:Вторая", "owner:Третья", "agent:Третий ответ."]),
    );
  });

  test("история с повторами одной реплики — один пузырь (при новом запуске)", async () => {
    const server = pairedServer();
    server.addChat("owner", "Помоги");
    server.addChat("owner", "Помоги");
    server.addChat("agent", "Вот ответ.");
    server.addChat("owner", "Другое");
    await openChat(server);
    expect(texts()).toEqual(["owner:Помоги", "agent:Вот ответ.", "owner:Другое"]);
  });

  test("нет связи до ответа, сервер реплику не получил — «Не отправлено», повтор безопасен", async () => {
    const server = pairedServer();
    server.failNetwork("POST", "/chat/messages");
    const r = await openChat(server);
    await say(r, "Тест");
    expect(await screen.findByText(/Нет связи с сервером.*Сообщение не сохранилось\. Не отправлено\./)).toBeInTheDocument();
    expect(server.chat).toHaveLength(0);
    await r.user.click(screen.getByRole("button", { name: "Отправить ещё раз" }));
    await waitFor(() => expect(texts()).toEqual(["owner:Тест", "agent:Слышу тебя."]));
    expect(server.chat.filter((m) => m.author === "owner")).toHaveLength(1);
  });

  test("связь оборвалась до ответа, а сервер всё сохранил — ответ из истории, повтора нет", async () => {
    const server = pairedServer();
    server.reply({ dropResponse: "answered" });
    const r = await openChat(server);
    await say(r, "Тест");
    await waitFor(() => expect(texts()).toEqual(["owner:Тест", "agent:Ответ, который не дошёл."]));
    expect(posts(r)).toHaveLength(1);
  });

  test("обрыв и история не читается — «Проверить и повторить»: сначала история, ответ есть — без отправки", async () => {
    const server = pairedServer();
    server.reply({ steps: [delta("Нач"), { saveAgent: "Сохранённый ответ." }] });
    const r = await openChat(server);
    server.failNetwork("GET", "/chat/messages");
    await say(r, "Тест");
    expect(await screen.findByText(/Не удалось проверить, дошло ли сообщение/)).toBeInTheDocument();
    await r.user.click(screen.getByRole("button", { name: "Проверить и повторить" }));
    await waitFor(() => expect(texts()).toEqual(["owner:Тест", "agent:Сохранённый ответ."]));
    expect(posts(r)).toHaveLength(1);
  });

  test("событие error — понятный текст, кусок прерван, реплика сохранена, повтор", async () => {
    const server = pairedServer();
    server.reply({ steps: [delta("Част"), errorEvent("unavailable")] });
    const r = await openChat(server);
    await say(r, "Тест");
    const alert = await screen.findByText(/Агент сейчас недоступен\. Попробуй ещё раз чуть позже\. Сообщение сохранено, ответа нет\./);
    expect(alert.closest("[role=alert]")).not.toBeNull();
    expect(screen.getByText("Ответ прервался и не сохранён.")).toBeInTheDocument();
    expect(r.tg!.haptics).toContain("notify:error");
    expect(server.calls("GET", "/chat/messages")).toHaveLength(1); // реплика сохранена по контракту — без перечитывания
    await r.user.click(screen.getByRole("button", { name: "Повторить" }));
    await waitFor(() => expect(texts()).toEqual(["owner:Тест", "agent:Слышу тебя."]));
  });

  test("409 conflict_retry дважды — «Не отправлено», сервер ничего не сохранил", async () => {
    const server = pairedServer();
    server.reply({ status: 409, body: { code: "conflict_retry", message: "x" } });
    server.reply({ status: 409, body: { code: "conflict_retry", message: "x" } });
    const r = await openChat(server);
    await say(r, "Тест");
    expect(await screen.findByText(/Сообщение не сохранилось из-за сбоя/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Отправить ещё раз" })).toBeInTheDocument();
    expect(server.chat).toHaveLength(0);
  });
});

describe("Кризисный ответ (ТЗ §9.3, принцип 7)", () => {
  const PROTOCOL = "Мне очень жаль, что тебе сейчас так.\nТы не обязана справляться одна. Позвони прямо сейчас.";

  test("текст протокола целиком, без смягчения; контакты помощи прямо под ответом, номера ссылками", async () => {
    const server = pairedServer();
    const contacts = [
      { name: "Единый номер экстренных служб", phone: "112", description: "Угроза жизни.", round_the_clock: true },
      { name: "Линия помощи (выдуманная)", phone: "8 800 000-00-00", description: "Психологическая помощь." },
    ];
    server.reply({ steps: [crisis(PROTOCOL, { contacts, id: "c1" })] });
    const r = await openChat(server);
    await say(r, "Мне не хочется жить");
    const block = await screen.findByRole("region", { name: "Контакты помощи" });
    expect(bubbles()[1].querySelector(".msg__text")!.textContent).toBe(PROTOCOL);
    expect(within(block).getByRole("link", { name: "112" })).toHaveAttribute("href", "tel:112");
    expect(within(block).getByRole("link", { name: "8 800 000-00-00" })).toHaveAttribute("href", "tel:88000000000");
    expect(live().textContent).toContain(PROTOCOL);
    await r.user.click(within(block).getByRole("button", { name: "Все контакты помощи" }));
    await heading("Помощь");
    await r.pressBack();
    expect(await screen.findByRole("region", { name: "Контакты помощи" })).toBeInTheDocument();
  });

  test("replace: true — показанные куски заменяются ответом протокола", async () => {
    const server = pairedServer();
    server.reply({ steps: [delta("Давай подберём слова"), crisis(PROTOCOL, { replace: true })] });
    const r = await openChat(server);
    await say(r, "Он меня бьёт");
    await screen.findByRole("region", { name: "Контакты помощи" });
    expect(texts()).toEqual(["owner:Он меня бьёт", `agent:${PROTOCOL}`]);
    expect(document.body.textContent).not.toContain("Давай подберём слова");
  });

  test("испорченное событие crisis — свой текст и 112 ссылкой; список без 112 — 112 добавлен", async () => {
    const server = pairedServer();
    server.reply({ steps: [{ event: "crisis", data: "{не json" }] });
    server.reply({ steps: [crisis(PROTOCOL, { contacts: [{ name: "Линия", phone: "8 800 111-11-11", description: "x" }] })] });
    const r = await openChat(server);
    await say(r, "Плохо");
    const first = await screen.findByRole("region", { name: "Контакты помощи" });
    expect(within(first).getByRole("link", { name: "112" })).toHaveAttribute("href", "tel:112");
    expect(within(list()).getByText(/обратись за помощью прямо сейчас/)).toBeInTheDocument();
    await say(r, "Очень плохо");
    await waitFor(() => expect(screen.getAllByRole("region", { name: "Контакты помощи" })).toHaveLength(2));
    const second = screen.getAllByRole("region", { name: "Контакты помощи" })[1];
    expect(within(second).getByRole("link", { name: "8 800 111-11-11" })).toBeInTheDocument();
    expect(within(second).getByRole("link", { name: "112" })).toBeInTheDocument();
  });

  test("кризис при исчерпанном лимите: сервер отвечает протоколом — клиент не блокирует отправку после 429", async () => {
    const server = pairedServer();
    server.reply({ status: 429, body: { code: "rate_limited", message: "x" }, headers: { "Retry-After": "7200" } });
    server.reply({ steps: [crisis(PROTOCOL)] });
    const r = await openChat(server);
    await say(r, "Привет");
    await screen.findByText(/лимит сообщений агенту исчерпан/);
    await say(r, "Мне страшно");
    expect(await screen.findByRole("region", { name: "Контакты помощи" })).toBeInTheDocument();
    expect(screen.queryByText(/лимит сообщений агенту исчерпан/)).toBeNull();
  });
});

describe("Дневной лимит (429)", () => {
  test("свой текст со временем по Retry-After, реплика сохранена, помощь упомянута; повтора нет", async () => {
    const server = pairedServer();
    server.reply({ status: 429, body: { code: "rate_limited", message: "На сегодня лимит…" }, headers: { "Retry-After": "10800" } });
    const r = await openChat(server);
    await say(r, "Ещё вопрос");
    const note = await screen.findByText(/На сегодня лимит сообщений агенту исчерпан\./);
    const box = note.closest(".note") as HTMLElement;
    expect(box).toHaveTextContent(/Агент снова ответит (завтра )?после \d\d:\d\d \(через 3 ч\)\./);
    expect(box).toHaveTextContent(/Твоё сообщение сохранено/);
    expect(box).toHaveTextContent(/контакты помощи доступны всегда/);
    expect(screen.getByText("Сохранено. Ответа пока нет — лимит на сегодня.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Повторить|Отправить ещё раз/ })).toBeNull();
    expect(server.chat.map((m) => m.text)).toEqual(["Ещё вопрос"]);
    expect(input()).not.toBeDisabled();
  });

  test("время по часам устройства: через 2 ч 30 мин — время конца окна и «через 3 ч»", async () => {
    const { limitUntilText } = await import("../api/messages");
    const now = new Date(2026, 10, 26, 21, 0, 0).getTime();
    expect(limitUntilText(9000, now)).toBe("после 23:30 (через 3 ч)");
    expect(limitUntilText(4 * 3600, now)).toBe("завтра после 01:00 (через 4 ч)");
    expect(limitUntilText(45, now)).toBe("после 21:00 (через 45 с)");
  });
});

describe("Безопасность и приватность", () => {
  test("XSS: разметка в ответе агента и в своей реплике — текст, а не элементы; переносы строк сохранены", async () => {
    const server = pairedServer();
    const evil = '<img src=x onerror="window.__xss=1"><script>window.__xss=2</script>\nвторая строка';
    server.reply({ steps: [delta(evil.slice(0, 10)), delta(evil.slice(10)), done()] });
    const r = await openChat(server);
    await say(r, "<b>жирный</b>");
    await waitFor(() => expect(bubbles()).toHaveLength(2));
    const li = list();
    expect(li.querySelector("img, script, b")).toBeNull();
    expect(bubbles()[0].querySelector(".msg__text")!.textContent).toBe("<b>жирный</b>");
    expect(bubbles()[1].querySelector(".msg__text")!.textContent).toBe(evil);
    expect((window as { __xss?: number }).__xss).toBeUndefined();
  });

  test("тексты не попадают в хранилища браузера, cookie и адрес; консоль молчит (setup)", async () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    const server = pairedServer();
    server.reply({ steps: [delta("Секретный ответ"), done()] });
    const href = window.location.href;
    const r = await openChat(server);
    await say(r, "Секретная реплика");
    await waitFor(() => expect(texts()).toContain("agent:Секретный ответ"));
    expect(setItem).not.toHaveBeenCalled();
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
    expect(document.cookie).toBe("");
    expect(window.location.href).toBe(href);
    expect(server.calls("POST", "/chat/messages")[0].query).toBe("");
  });

  test("в исходниках клиента нет dangerouslySetInnerHTML и innerHTML", () => {
    const root = join(__dirname, "..");
    const files: string[] = [];
    const walk = (dir: string) => {
      for (const f of readdirSync(dir)) {
        const p = join(dir, f);
        if (statSync(p).isDirectory()) walk(p);
        else if (/\.tsx?$/.test(f) && !/\.test\.tsx?$/.test(f)) files.push(p);
      }
    };
    walk(root);
    expect(files.length).toBeGreaterThan(10);
    for (const f of files) {
      const src = readFileSync(f, "utf8");
      expect(src, f).not.toMatch(/dangerouslySetInnerHTML|\.innerHTML\s*=|insertAdjacentHTML/);
    }
  });
});

describe("Нативные элементы и запасные кнопки", () => {
  test("вне нативной MainButton (Bot API без SDK-кнопки) — вход в чат кнопкой в странице", async () => {
    const r = renderApp({ server: pairedServer(), tg: null });
    expect(r.tg).toBeNull();
    // Вне Telegram — «Открой в Telegram»: чат недоступен без входа.
    expect(await heading("Открой в Telegram")).toBeInTheDocument();
  });

  test("MainButton отказала — на главном экране кнопка в странице открывает чат", async () => {
    const r = renderApp({ server: pairedServer() });
    await heading("Вы пара");
    r.tg!.webApp.MainButton.setText = () => {
      throw new Error("fake failure");
    };
    // Карточка-ссылка работает всегда, даже без нативной кнопки.
    await r.user.click(screen.getByRole("button", { name: /Поговорить с агентом/ }));
    await heading("Твой агент");
  });
});
