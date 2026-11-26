/**
 * A4, задача 3.4 — дополнение чата 2.2: кнопка «Подготовить сообщение» (ТЗ §8) и подсказка агента
 * (`done.suggest_draft`). Черновик создаётся только по нажатию; без активной пары кнопки нет. Тексты выдуманные.
 */
import { screen, waitFor } from "@testing-library/react";
import { FakeServer, delta, done, gate } from "../test/fakeApi";
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
  await waitFor(() => expect(screen.queryByText("Загружаем переписку…")).toBeNull());
  return r;
}

const prepare = () => screen.getByRole("button", { name: "Подготовить сообщение" });
const input = () => screen.getByRole("textbox", { name: "Сообщение агенту" });

async function say(r: Rendered, text: string) {
  await r.user.type(input(), text);
  await r.user.click(screen.getByRole("button", { name: "Отправить агенту" }));
}

test("активная пара — кнопка видна, не выделена; открытие чата черновик не создаёт", async () => {
  const r = await openChat();
  expect(prepare()).toHaveClass("btn--ghost");
  expect(prepare()).not.toHaveClass("btn--fill");
  expect(screen.queryByText("Агент предлагает подготовить сообщение партнёру.")).toBeNull();
  expect(r.server.calls("POST", "/drafts")).toHaveLength(0);
});

test("suggest_draft: true — кнопка выделена и подпись словами; новая реплика снимает выделение", async () => {
  const server = pairedServer().reply({ steps: [delta("Хочешь, подготовим сообщение?"), done(undefined, true)] });
  const r = await openChat(server);
  await say(r, "Обидно из-за ужина");
  expect(await screen.findByText("Агент предлагает подготовить сообщение партнёру.")).toBeInTheDocument();
  expect(prepare()).toHaveClass("btn--fill");
  // Выделение — цветом текста (чёрная кнопка), не красным: красный — только MainButton (§8).
  expect(r.tg!.main.visible).toBe(false);
  const g = gate();
  server.reply({ steps: [g.step, delta("Слышу."), done()] });
  await say(r, "Ещё одно");
  expect(screen.queryByText("Агент предлагает подготовить сообщение партнёру.")).toBeNull();
  // Пока агент отвечает, черновик не начать.
  expect(prepare()).toBeDisabled();
  g.open();
  await waitFor(() => expect(prepare()).toBeEnabled());
  expect(prepare()).not.toHaveClass("btn--fill");
  expect(r.server.calls("POST", "/drafts")).toHaveLength(0);
});

test("suggest_draft: false — кнопка не выделена", async () => {
  const server = pairedServer().reply({ steps: [delta("Слышу."), done(undefined, false)] });
  const r = await openChat(server);
  await say(r, "Привет");
  await screen.findByText("Слышу.");
  expect(prepare()).not.toHaveClass("btn--fill");
});

test("нажатие — экран черновика (POST /drafts один раз)", async () => {
  const server = pairedServer();
  server.addChat("owner", "Обидно");
  const r = await openChat(server);
  await r.user.click(prepare());
  await heading("Черновик");
  await screen.findByLabelText("Текст сообщения партнёру");
  expect(r.server.calls("POST", "/drafts")).toHaveLength(1);
});

test("пары нет (ждём партнёра) — кнопки нет, подпись «Пара ещё не создана»", async () => {
  const server = new FakeServer().onboarded().pendingCouple();
  const r = renderApp({ server });
  await screen.findByText(/Приглашение уже создано/);
  await r.user.click(screen.getByRole("button", { name: "Поговорить с агентом" }));
  await heading("Твой агент");
  expect(screen.queryByRole("button", { name: /Подготовить/ })).toBeNull();
  expect(screen.getByText(/Пара ещё не создана/)).toBeInTheDocument();
});
