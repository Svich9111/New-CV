/**
 * A4, задача 2.2, исправления раунда 1, часть 2 — экран чата на контракте идемпотентности АР-51: кризис из
 * истории (MINOR-3), одинаковые реплики подряд, 422 под тем же ключом, `accepted` без id. Тексты выдуманные.
 */
import { screen, waitFor, within } from "@testing-library/react";
import { FakeServer, errorEvent } from "../test/fakeApi";
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
const list = () => screen.getByRole("list", { name: "Переписка с агентом" });
const texts = () =>
  [...list().querySelectorAll<HTMLElement>(":scope > li")]
    .filter((li) => !li.hasAttribute("data-typing") && !li.hasAttribute("data-checking"))
    .map((li) => `${li.dataset.author}:${li.querySelector(".msg__text")?.textContent}`);

async function say(r: Rendered, text: string) {
  await r.user.clear(input());
  await r.user.type(input(), text);
  await r.user.click(screen.getByRole("button", { name: "Отправить агенту" }));
}

describe("Кризисный ответ из истории (MINOR-3, ТЗ §9.3 «всегда»)", () => {
  test("при открытии чата под ответом с crisis: true — «Помощь рядом», номера ссылками и 112", async () => {
    const server = pairedServer();
    const o = server.addChat("owner", "Мне не хочется жить", undefined, { client_message_id: "11111111-2222-4333-8444-555555555555" });
    server.addChat("agent", "Ответ протокола.", undefined, { reply_to: o.id, crisis: true });
    server.addChat("owner", "Спасибо", undefined, { client_message_id: "21111111-2222-4333-8444-555555555555" });
    await openChat(server);
    const block = await screen.findByRole("region", { name: "Контакты помощи" });
    expect(block.closest("li")?.querySelector(".msg__text")?.textContent).toBe("Ответ протокола.");
    expect(within(block).getByRole("link", { name: "112" })).toHaveAttribute("href", "tel:112");
    expect(screen.getAllByRole("region", { name: "Контакты помощи" })).toHaveLength(1);
  });

  test("контакты не загрузились — блок с 112 всё равно под ответом", async () => {
    const server = pairedServer();
    server.addChat("agent", "Ответ протокола.", undefined, { crisis: true });
    server.inject("GET", "/help/contacts", 503, { code: "service_unavailable", message: "x" });
    await openChat(server);
    const block = await screen.findByRole("region", { name: "Контакты помощи" });
    await waitFor(() => expect(server.calls("GET", "/help/contacts")).toHaveLength(1));
    expect(within(block).getByRole("link", { name: "112" })).toHaveAttribute("href", "tel:112");
  });

  test("ответ, сохранённый до миграции 0018 (crisis: false), — обычная реплика; «Помощь» внизу есть", async () => {
    const server = pairedServer();
    server.addChat("agent", "Старый ответ.");
    await openChat(server);
    expect(screen.queryByRole("region", { name: "Контакты помощи" })).toBeNull();
    expect(screen.getByRole("button", { name: "Помощь и контакты служб поддержки" })).toBeInTheDocument();
  });
});

describe("Одинаковые реплики подряд (АР-51)", () => {
  test("«да» дважды с лимитом между — два пузыря, а не один", async () => {
    const server = pairedServer();
    server.reply({ status: 429, body: { code: "rate_limited", message: "x" }, headers: { "Retry-After": "60" } });
    const r = await openChat(server);
    await say(r, "да");
    await screen.findByText(/лимит сообщений агенту исчерпан/);
    await say(r, "да");
    await waitFor(() => expect(texts()).toEqual(["owner:да", "owner:да", "agent:Слышу тебя."]));
  });
});

describe("Ошибки под ключом", () => {
  test("422 — понятный текст и «Отправить ещё раз»; повтор с новым ключом", async () => {
    const server = pairedServer();
    server.inject("POST", "/chat/messages", 422, { code: "validation_error", message: "x" });
    const r = await openChat(server);
    await say(r, "Раз");
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Сервер не принял это сообщение. Отправь его ещё раз — оно уйдёт как новое. Не отправлено.");
    await r.user.click(within(alert).getByRole("button", { name: "Отправить ещё раз" }));
    await waitFor(() => expect(texts()).toEqual(["owner:Раз", "agent:Слышу тебя."]));
    const keys = server.calls("POST", "/chat/messages").map((c) => (c.body as { client_message_id: string }).client_message_id);
    expect(keys[1]).not.toBe(keys[0]);
  });

  test("accepted без id, затем error — «Не отправлено» со строкой о помощи (112)", async () => {
    const server = pairedServer();
    server.reply({ ownerNotSaved: true, steps: [errorEvent("internal")] });
    const r = await openChat(server);
    await say(r, "Раз");
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/Сообщение не сохранилось\. Не отправлено\./);
    expect(within(alert).getByRole("link", { name: "112" })).toHaveAttribute("href", "tel:112");
    expect(within(alert).getByRole("button", { name: "Отправить ещё раз" })).toBeInTheDocument();
  });
});
