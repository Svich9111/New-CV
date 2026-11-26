/**
 * D4 (A8, р.1): граница ошибок. Экран, упавший при отрисовке, не оставляет пустую страницу:
 * причина подписью, «Помощь» с 112 и перезапуск.
 */
import { screen } from "@testing-library/react";
import { heading, renderApp } from "../test/renderApp";

const crash = vi.hoisted(() => ({ on: false }));

vi.mock("../screens/Welcome", async (importOriginal) => {
  const real = await importOriginal<typeof import("../screens/Welcome")>();
  return {
    Welcome: (props: { slide: number }) => {
      if (crash.on) throw new Error("отрисовка упала");
      return real.Welcome(props);
    },
  };
});

beforeEach(() => {
  // React сам пишет в консоль о пойманной границей ошибке; в продуктовом коде консоль не используется.
  vi.spyOn(console, "error").mockImplementation(() => undefined);
});

test("экран упал — «Что-то пошло не так», «Помощь» с 112, «Перезапустить» входит заново", async () => {
  crash.on = true;
  const r = renderApp();
  expect(await heading("Что-то пошло не так")).toBeInTheDocument();
  expect(screen.getByRole("alert")).toHaveTextContent("Экран не открылся из-за ошибки в приложении");
  expect(screen.getByText(/звони 112/)).toBeInTheDocument();
  expect(r.tg!.main).toMatchObject({ visible: true, text: "Перезапустить" });

  await r.user.click(screen.getByRole("button", { name: "Помощь и контакты служб поддержки" }));
  expect(await screen.findByRole("link", { name: "112" })).toHaveAttribute("href", "tel:112");
  await r.pressBack();
  expect(await heading("Что-то пошло не так")).toBeInTheDocument();

  crash.on = false;
  const logins = r.server.calls("POST", "/auth/telegram").length;
  await r.pressMain();
  expect(await heading("Твой личный агент")).toBeInTheDocument();
  expect(r.server.calls("POST", "/auth/telegram")).toHaveLength(logins + 1);
});
