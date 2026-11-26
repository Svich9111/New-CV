/**
 * A8, задача 2.2, раунд 2 — экран чата на контракте идемпотентности АР-51: 409 «ответ ещё готовится» с ожиданием
 * и «Остановить», 422 под ключом, кризис из сверки, одинаковые реплики подряд, реплика с другого устройства.
 * ФТ-05; ТЗ §8 (состояние подписью), §9.3, §10; карточка 2.2, критерии 4–6. Тексты выдуманные.
 */
import { act, screen, waitFor, within } from "@testing-library/react";
import { CONTACTS, FakeServer, delta, done, gate } from "../test/fakeApi";
import { heading, renderApp, type Rendered } from "../test/renderApp";
import { newMessageKey } from "../chat/session";

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
const texts = () =>
  [...list().querySelectorAll<HTMLElement>(":scope > li")]
    .filter((li) => !li.hasAttribute("data-typing") && !li.hasAttribute("data-checking"))
    .map((li) => `${li.dataset.author}:${li.querySelector(".msg__text")?.textContent}`);
const posts = (r: Rendered) => r.server.calls("POST", "/chat/messages");
const keyOf = (body: unknown) => (body as { client_message_id?: string }).client_message_id;

async function say(r: Rendered, text: string) {
  await r.user.clear(input());
  await r.user.type(input(), text);
  await r.user.click(sendBtn());
}

/** Реплика «Раз» сохранена без ответа (обрыв после 200), на экране — «Повторить». */
async function unanswered(server: FakeServer): Promise<{ r: Rendered; id: string }> {
  server.reply({ steps: [delta("Нач")], end: "error" });
  const r = await openChat(server);
  await say(r, "Раз");
  await screen.findByRole("button", { name: "Повторить" });
  const id = server.chat.find((m) => m.author === "owner" && m.text === "Раз")!.id;
  return { r, id };
}

describe("A8 р.2 — 409 «ответ ещё готовится» на экране (АР-51 (в); ТЗ §8)", () => {
  test("ожидание: «Агент пишет…» и «Остановить»; «Остановить» — подпись «ещё готовится» и «Повторить», POST больше нет", async () => {
    const server = pairedServer();
    const { r, id } = await unanswered(server);
    server.leaseFor(id, 50);
    await r.user.click(screen.getByRole("button", { name: "Повторить" }));
    await waitFor(() => expect(posts(r)).toHaveLength(3));
    const stop = await screen.findByRole("button", { name: "Остановить" });
    expect(screen.getAllByText("Агент пишет…").length).toBeGreaterThan(0);
    expect(sendBtn).toThrow();
    r.advanceClock(1000); // за пределами защиты от второго касания после своей отправки (DOUBLE_TAP_MS)
    await r.user.click(stop);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Ответ на это сообщение ещё готовится. Повтори чуть позже — он придёт к этому же сообщению.");
    expect(within(alert).getByRole("button", { name: "Повторить" })).toBeEnabled();
    expect(screen.queryByText("Агент пишет…")).toBeNull();
    await new Promise((res) => setTimeout(res, 50));
    expect(posts(r)).toHaveLength(3);
    expect(texts()).toEqual(["owner:Раз"]);
  });

  test(
    "ожидание закончилось само (аренда снята за паузу 3 с) — ответ под той же репликой, ключ один",
    async () => {
      const server = pairedServer();
      const { r, id } = await unanswered(server);
      server.leaseFor(id, 2);
      await r.user.click(screen.getByRole("button", { name: "Повторить" }));
      await screen.findByRole("button", { name: "Остановить" });
      await waitFor(() => expect(texts()).toEqual(["owner:Раз", "agent:Слышу тебя."]), { timeout: 6000 });
      expect(screen.queryByRole("alert")).toBeNull();
      expect(new Set(posts(r).map((c) => keyOf(c.body))).size).toBe(1);
      expect(server.chat.filter((m) => m.author === "owner")).toHaveLength(1);
    },
    12_000,
  );
});

describe("A8 р.2 — кризис из сверки на экране (ТЗ §9.3; карточка, критерий 5)", () => {
  test("обрыв до ответа, а сервер сохранил ответ протокола — «Помощь рядом» под ним, 112 и контакты, без «ошибки»", async () => {
    const server = pairedServer();
    const base = server.fetch;
    server.fetch = async (input, init = {}) => {
      const u = String(input);
      if ((init.method ?? "GET") === "GET" && u.includes("/chat/messages")) {
        const own = server.chat.find((m) => m.author === "owner" && m.text === "Мне не хочется жить");
        if (own && !server.answerTo(own.id)) server.addChat("agent", "Ответ протокола.", undefined, { reply_to: own.id, crisis: true });
      }
      return base(input, init);
    };
    const r = await openChat(server);
    server.reply({ dropResponse: true });
    await say(r, "Мне не хочется жить");
    const block = await screen.findByRole("region", { name: "Контакты помощи" });
    expect(block.closest("li")?.querySelector(".msg__text")?.textContent).toBe("Ответ протокола.");
    expect(within(block).getByRole("link", { name: "112" })).toHaveAttribute("href", "tel:112");
    await waitFor(() => expect(within(block).getAllByRole("link").length).toBeGreaterThanOrEqual(CONTACTS.length));
    expect(screen.queryByRole("alert")).toBeNull();
    expect(texts()).toEqual(["owner:Мне не хочется жить", "agent:Ответ протокола."]);
    expect(r.tg!.haptics).not.toContain("notify:error");
  });
});

describe("A8 р.2 — одинаковые реплики подряд и другое устройство на экране", () => {
  test("«да» ×3 (ответ, нет связи, обрыв с сохранённым ответом) — три пузыря, одна ошибка «Не отправлено», ответы на месте", async () => {
    const server = pairedServer();
    const r = await openChat(server);
    await say(r, "да");
    await waitFor(() => expect(texts()).toEqual(["owner:да", "agent:Слышу тебя."]));
    server.failNetwork("POST", "/chat/messages");
    await say(r, "да");
    await screen.findByRole("button", { name: "Отправить ещё раз" });
    server.reply({ dropResponse: "answered" });
    await say(r, "да");
    await waitFor(() =>
      expect(texts()).toEqual(["owner:да", "agent:Слышу тебя.", "owner:да", "owner:да", "agent:Ответ, который не дошёл."]),
    );
    const alerts = screen.getAllByRole("alert");
    expect(alerts).toHaveLength(1);
    expect(alerts[0]).toHaveTextContent("Не отправлено.");
    // Повтор старого «да» после нового не предлагается как действие — кнопка неактивна или пропала.
    const stale = within(alerts[0]).queryByRole("button");
    if (stale) {
      await r.user.click(stale);
      await new Promise((res) => setTimeout(res, 30));
    }
    expect(posts(r)).toHaveLength(3);
  });

  test("с телефона пришло такое же «Привет» с ответом, своё не дошло — два пузыря, ответ под телефонным, своё «Не отправлено»", async () => {
    const server = pairedServer();
    const r = await openChat(server);
    const other = server.addChat("owner", "Привет", undefined, { client_message_id: newMessageKey() });
    server.addChat("agent", "Ответ телефону", undefined, { reply_to: other.id });
    server.failNetwork("POST", "/chat/messages");
    await say(r, "Привет");
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Не отправлено.");
    expect(texts()).toEqual(["owner:Привет", "agent:Ответ телефону", "owner:Привет"]);
    const g = gate();
    server.reply({ steps: [delta("Мне "), g.step, delta("ответ."), done()] });
    await r.user.click(within(alert).getByRole("button", { name: "Отправить ещё раз" }));
    await act(async () => g.open());
    await waitFor(() => expect(texts()).toEqual(["owner:Привет", "agent:Ответ телефону", "owner:Привет", "agent:Мне ответ."]));
  });
});
