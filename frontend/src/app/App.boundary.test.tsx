/**
 * D4 (A8, р.1): граница ошибок. Экран, упавший при отрисовке, не оставляет пустую страницу:
 * причина подписью, «Помощь» с 112 и перезапуск.
 */
import { screen } from "@testing-library/react";
import { fakeTelegram } from "../test/fakeTelegram";
import { heading, renderApp } from "../test/renderApp";

const crash = vi.hoisted(() => ({ on: false, help: false, helpRenders: 0 }));

vi.mock("../screens/Welcome", async (importOriginal) => {
  const real = await importOriginal<typeof import("../screens/Welcome")>();
  return {
    Welcome: (props: { slide: number }) => {
      if (crash.on) throw new Error("отрисовка упала");
      return real.Welcome(props);
    },
  };
});

// «Помощь», которая падает при отрисовке, — чтобы проверить, что запасной экран не зацикливается (A8-1.5-D6).
vi.mock("../screens/Help", async (importOriginal) => {
  const real = await importOriginal<typeof import("../screens/Help")>();
  return {
    ...real,
    Help: (props: Parameters<typeof real.Help>[0]) => {
      if (crash.help) {
        crash.helpRenders++;
        throw new Error("помощь упала");
      }
      return real.Help(props);
    },
  };
});

afterEach(() => {
  crash.on = false;
  crash.help = false;
  crash.helpRenders = 0;
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

test("D6: нативные кнопки отказали — запасной экран с кнопками в странице, «Перезапустить» входит", async () => {
  crash.on = true;
  const tg = fakeTelegram();
  const boom = () => {
    throw new Error("SDK failure");
  };
  tg.webApp.MainButton.show = boom;
  tg.webApp.BackButton.show = boom;
  const r = renderApp({ tg });
  expect(await heading("Что-то пошло не так")).toBeInTheDocument();
  expect(tg.main.visible).toBe(false);
  await r.user.click(screen.getByRole("button", { name: "Помощь и контакты служб поддержки" }));
  expect(await screen.findByRole("link", { name: "112" })).toHaveAttribute("href", "tel:112");
  await r.user.click(screen.getByRole("button", { name: "‹ Назад" }));
  expect(await heading("Что-то пошло не так")).toBeInTheDocument();
  crash.on = false;
  await r.user.click(screen.getByRole("button", { name: "Перезапустить" }));
  expect(await heading("Твой личный агент")).toBeInTheDocument();
});

test("D6: упал и запасной экран — статичный минимум с текстом и tel:112, без цикла; «Перезапустить» входит", async () => {
  crash.on = true;
  crash.help = true;
  const r = renderApp();
  expect(await heading("Что-то пошло не так")).toBeInTheDocument();
  await r.user.click(screen.getByRole("button", { name: "Помощь и контакты служб поддержки" }));
  expect(await screen.findByText(/Приложение не может показать экран/)).toBeInTheDocument();
  const link = screen.getByRole("link", { name: "112" });
  expect(link).toHaveAttribute("href", "tel:112");
  expect(document.querySelector('[data-screen="fatal"]')).not.toBeNull();
  // Конечное число попыток отрисовки (React сам повторяет упавшую отрисовку несколько раз) — и оно не растёт.
  const renders = crash.helpRenders;
  expect(renders).toBeLessThanOrEqual(10);
  await new Promise((res) => setTimeout(res, 100));
  expect(crash.helpRenders).toBe(renders);
  expect(screen.getByRole("link", { name: "112" })).toBe(link);
  crash.on = false;
  crash.help = false;
  const logins = r.server.calls("POST", "/auth/telegram").length;
  await r.user.click(screen.getByRole("button", { name: "Перезапустить" }));
  expect(await heading("Твой личный агент")).toBeInTheDocument();
  expect(r.server.calls("POST", "/auth/telegram")).toHaveLength(logins + 1);
});
