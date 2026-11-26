/**
 * A8, задача 2.2, раунд 3 — экран чата: кнопка «Повторить» у реплики под лимитом (MINOR-N5) после того, как
 * человек написал следующую реплику; огромный `Retry-After` не запускает таймер дольше предела `setTimeout`.
 * ФТ-05; ТЗ §8 («состояние словами»), §10; АР-51. Тексты выдуманные.
 */
import { act, screen, waitFor, within } from "@testing-library/react";
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
const bubble = (text: string) =>
  [...list().querySelectorAll<HTMLElement>(":scope > li")].find((li) => li.querySelector(".msg__text")?.textContent === text)!;
const posts = (r: Rendered) => r.server.calls("POST", "/chat/messages");
const keyOf = (body: unknown) => (body as { client_message_id?: string }).client_message_id;

async function say(r: Rendered, text: string) {
  await r.user.type(input(), text);
  await r.user.click(sendBtn());
}

describe("A8 р.3 — MINOR-N5: реплика под лимитом, а человек написал дальше", () => {
  // Кнопка «Повторить» у прежней реплики под лимитом не должна быть «мёртвой»: либо её нет, либо она шлёт эту
  // реплику тем же ключом (ТЗ §8 — состояние и действие понятны; АР-51 — ответ на ту же реплику).
  // A8-2.2-D8 (MINOR, новое в 0c64a7f): `LimitedCaption` показывает «лимит закончился, можно повторить» и кнопку у
  // любой реплики под лимитом, а `ChatSession.retry` молча выходит, если это не последний обмен (`turn.key !== key`):
  // нажатие ничего не делает. До р.3 кнопки не было — тест на 9410ecc зелёный.
  test("A8-2.2-D8: после окна у прежней реплики «Повторить» либо нет, либо нажатие отправляет её тем же ключом", async () => {
    const server = pairedServer();
    server.reply({ status: 429, body: { code: "rate_limited", message: "x" }, headers: { "Retry-After": "1" } });
    const r = await openChat(server);
    await say(r, "Первая");
    await screen.findByText("Сохранено. Ответа пока нет — лимит на сегодня.");
    const firstKey = keyOf(posts(r)[0].body);
    await act(() => new Promise<void>((ok) => setTimeout(ok, 1200))); // окно кончилось
    await say(r, "Вторая");
    await waitFor(() => expect(within(bubble("Вторая").parentElement!).getByText("Слышу тебя.")).toBeInTheDocument());
    const retry = within(bubble("Первая")).queryByRole("button", { name: "Повторить" });
    if (retry === null) return;
    const before = posts(r).length;
    await r.user.click(retry);
    await waitFor(() => expect(posts(r).length).toBe(before + 1), { timeout: 1500 });
    expect(keyOf(posts(r)[before].body)).toBe(firstKey);
  });

  // A8-2.2-D9 (MINOR, было и до р.3): то же для «Не отправлено» / «Сообщение сохранено, ответа нет» — кнопка
  // повтора остаётся у прежней реплики после следующей, нажатие ничего не делает (на 9410ecc тоже красный).
  test("A8-2.2-D9: у прежней реплики «Не отправлено» после следующей реплики «Отправить ещё раз» либо нет, либо работает", async () => {
    const server = pairedServer();
    server.failNetwork("POST", "/chat/messages");
    const r = await openChat(server);
    await say(r, "Первая");
    await within(bubble("Первая")).findByText(/Не отправлено/);
    await say(r, "Вторая");
    await waitFor(() => expect(r.server.chat.filter((m) => m.author === "agent")).toHaveLength(1));
    await waitFor(() => expect(screen.getByText("Слышу тебя.")).toBeInTheDocument());
    const retry = within(bubble("Первая")).queryByRole("button", { name: /Отправить ещё раз|Повторить|Проверить/ });
    if (retry === null) return;
    const before = posts(r).length + r.server.calls("GET", "/chat/messages").length;
    await r.user.click(retry);
    await waitFor(() => expect(posts(r).length + r.server.calls("GET", "/chat/messages").length).toBeGreaterThan(before), { timeout: 1500 });
  });
});

describe("A8 р.3 — MINOR-N5: огромный Retry-After", () => {
  test("таймер подписи не дольше 2^31−1 мс (иначе срабатывает сразу и крутит перерисовку), «Повторить» нет", async () => {
    const delays: number[] = [];
    const real = globalThis.setTimeout;
    const spy = vi.spyOn(globalThis, "setTimeout").mockImplementation(((fn: () => void, ms?: number, ...a: unknown[]) => {
      if (typeof ms === "number") delays.push(ms);
      return real(fn, ms, ...a);
    }) as typeof setTimeout);
    try {
      const server = pairedServer();
      server.reply({ status: 429, body: { code: "rate_limited", message: "x" }, headers: { "Retry-After": "99999999999999" } });
      const r = await openChat(server);
      await say(r, "Первая");
      await screen.findByText("Сохранено. Ответа пока нет — лимит на сегодня.");
      await new Promise((ok) => real(ok, 300));
      expect(delays.every((ms) => ms <= 2_147_483_647)).toBe(true);
      expect(screen.queryByRole("button", { name: "Повторить" })).toBeNull();
      expect(screen.queryByText(/Invalid Date|NaN/)).toBeNull();
      expect(posts(r)).toHaveLength(1);
    } finally {
      spy.mockRestore();
    }
  });
});
