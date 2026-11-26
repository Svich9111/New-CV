import { screen } from "@testing-library/react";
import { FakeServer } from "../test/fakeApi";
import { heading, renderApp } from "../test/renderApp";

describe("Помощь (ТЗ §9.3, §9.4) — доступна всегда", () => {
  test("контакты с сервера, ссылки tel:, «не заменяет психолога»", async () => {
    const r = renderApp();
    await heading("Твой личный агент");
    await r.user.click(screen.getByRole("button", { name: /Помощь/ }));
    expect(await screen.findByRole("link", { name: "112" })).toHaveAttribute("href", "tel:112");
    expect(screen.getByText("«Чувства AI» не заменяет психолога.")).toBeInTheDocument();
    expect(screen.getByText(/Контакты помощи доступны всегда/)).toBeInTheDocument();
  });

  test("сервер недоступен — запасной номер 112 и пояснение", async () => {
    const server = new FakeServer().inject("GET", "/help/contacts", 500);
    const r = renderApp({ server });
    await heading("Твой личный агент");
    await r.user.click(screen.getByRole("button", { name: /Помощь/ }));
    expect(await screen.findByRole("link", { name: "112" })).toBeInTheDocument();
    expect(screen.getByText(/Не получилось загрузить полный список/)).toBeInTheDocument();
  });

  test("доступна и когда вход не удался", async () => {
    const server = new FakeServer().inject("POST", "/auth/telegram", 401, { code: "invalid_init_data", message: "x" });
    const r = renderApp({ server });
    await heading("Не получилось войти");
    await r.user.click(screen.getByRole("button", { name: /Помощь/ }));
    expect(await heading("Помощь")).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "112" })).toBeInTheDocument();
    await r.pressBack();
    expect(await heading("Не получилось войти")).toBeInTheDocument();
  });

  test("тексты не обещают терапию", async () => {
    const r = renderApp();
    await heading("Твой личный агент");
    await r.user.click(screen.getByRole("button", { name: /Помощь/ }));
    await screen.findByRole("link", { name: "112" });
    // Принцип 7 (ТЗ §2): продукт не терапия — интерфейс не называет себя терапией и не обещает лечения.
    expect(document.body.textContent).not.toMatch(/терапи|лечени|вылеч/i);
  });
});
