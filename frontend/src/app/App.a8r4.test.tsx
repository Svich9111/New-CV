/**
 * A8, задача 1.5, раунд 4 — n-6: «Помощь», открытая на «Входим…», при отказе входа тоже остаётся,
 * «Назад» ведёт на причину с «Попробовать снова»; повтор входит без лишних запросов (ТЗ §9.3, ФТ-01).
 */
import { act, screen } from "@testing-library/react";
import { FakeServer } from "../test/fakeApi";
import { heading, renderApp } from "../test/renderApp";

const helpLink = () => screen.getByRole("button", { name: "Помощь и контакты служб поддержки" });

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

test("A8 р.4: вход упал (503), пока открыта «Помощь» — «Помощь» на месте, «Назад» → причина, «Попробовать снова» входит", async () => {
  const server = new FakeServer().inject("POST", "/auth/telegram", 503, { code: "service_unavailable", message: "x" });
  const release = holdLogin(server);
  const r = renderApp({ server });
  await screen.findByText("Входим через Telegram…");
  await r.user.click(helpLink());
  expect(await heading("Помощь")).toBeInTheDocument();
  await release();
  await act(async () => new Promise((res) => setTimeout(res, 20)));
  expect(server.calls("POST", "/auth/telegram")).toHaveLength(1);
  expect(screen.getByRole("heading", { level: 1, name: "Помощь" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "112" })).toHaveAttribute("href", "tel:112");
  await r.pressBack();
  expect(await screen.findByText(/Сервис временно недоступен/)).toBeInTheDocument();
  expect(screen.queryByRole("heading", { level: 1, name: "Помощь" })).toBeNull();
  expect(r.tg!.main).toMatchObject({ visible: true, text: "Попробовать снова" });
  await act(async () => r.tg!.main.click());
  expect(await heading("Твой личный агент")).toBeInTheDocument();
  expect(server.calls("POST", "/auth/telegram")).toHaveLength(2);
  // После входа стек с нуля: «Помощь» не открывается снова, «Назад» нет.
  expect(r.tg!.back.visible).toBe(false);
});
