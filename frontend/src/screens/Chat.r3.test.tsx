/**
 * A4, задача 2.2, раунд 3 — экран чата: «Повторить» у реплики под лимитом после окна (MINOR-N5), реплика из
 * пробелов по правилам сервера не отправляется (A8-2.2-D6). ФТ-05; ТЗ §8, §10; АР-51. Тексты выдуманные.
 */
import { screen, waitFor, within } from "@testing-library/react";
import { FakeServer } from "../test/fakeApi";
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
const texts = () =>
  [...list().querySelectorAll<HTMLElement>(":scope > li")]
    .filter((li) => !li.hasAttribute("data-typing") && !li.hasAttribute("data-checking"))
    .map((li) => `${li.dataset.author}:${li.querySelector(".msg__text")?.textContent}`);
const keyOf = (body: unknown) => (body as { client_message_id?: string }).client_message_id;

describe("Лимит 429: «Повторить» после окна (MINOR-N5)", () => {
  test("до окна — подпись без повтора; окно кончилось — «Повторить», тот же ключ, ответ под той же репликой", async () => {
    const server = pairedServer();
    server.reply({ status: 429, body: { code: "rate_limited", message: "x" }, headers: { "Retry-After": "1" } });
    const r = await openChat(server);
    await r.user.type(input(), "Ещё вопрос");
    await r.user.click(sendBtn());
    await screen.findByText("Сохранено. Ответа пока нет — лимит на сегодня.");
    expect(screen.queryByRole("button", { name: "Повторить" })).toBeNull();
    const retry = await screen.findByRole("button", { name: "Повторить" }, { timeout: 3000 });
    const bubble = retry.closest("li") as HTMLElement;
    expect(within(bubble).getByText(/лимит закончился, можно повторить/)).toBeInTheDocument();
    await r.user.click(retry);
    await waitFor(() => expect(texts()).toEqual(["owner:Ещё вопрос", "agent:Слышу тебя."]));
    const keys = server.calls("POST", "/chat/messages").map((c) => keyOf(c.body));
    expect(keys).toHaveLength(2);
    expect(keys[1]).toBe(keys[0]);
    expect(server.chat.filter((m) => m.author === "owner")).toHaveLength(1);
    expect(screen.queryByText(/На сегодня лимит сообщений агенту исчерпан/)).toBeNull();
    expect(screen.queryByRole("button", { name: "Повторить" })).toBeNull();
  });
});

describe("Пустая реплика по правилам сервера (A8-2.2-D6)", () => {
  test("U+0085 и U+001F — кнопка «Отправить» недоступна, Enter ничего не шлёт", async () => {
    const r = await openChat();
    await r.user.type(input(), "\u0085\u001f");
    expect(input().value).toBe("\u0085\u001f");
    expect(sendBtn()).toBeDisabled();
    await r.user.type(input(), "{Enter}");
    expect(postsOf(r)).toHaveLength(0);
  });
});

const postsOf = (r: Rendered) => r.server.calls("POST", "/chat/messages");
