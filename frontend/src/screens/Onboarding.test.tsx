import { act, screen } from "@testing-library/react";
import { FakeServer } from "../test/fakeApi";
import { heading, renderApp, type Rendered } from "../test/renderApp";

async function openOnboarding(server = new FakeServer()): Promise<Rendered> {
  const r = renderApp({ server });
  await heading("Твой личный агент");
  await r.pressMain();
  await r.pressMain();
  await r.pressMain();
  await heading("Давай познакомимся");
  return r;
}

const CONSENTS = [
  "Принимаю правила сервиса",
  "Даю согласие на обработку персональных данных",
  "Даю отдельное согласие на обработку сведений о чувствах и отношениях",
  "Мне исполнилось 18 лет",
];

async function fill(r: Rendered, opts: { skip?: string } = {}) {
  const name = screen.getByLabelText("Твоё имя");
  await r.user.clear(name);
  await r.user.type(name, "Аня");
  await r.user.type(screen.getByLabelText("Как к тебе обращаться"), "Аня");
  await r.user.type(screen.getByLabelText("Имя партнёра"), "Игорь");
  for (const c of CONSENTS) if (c !== opts.skip) await r.user.click(screen.getByRole("checkbox", { name: c }));
}

describe("Онбординг (ФТ-02, ТЗ §4.1 п.2)", () => {
  test("поля и согласия подписаны; имя подставлено из Telegram", async () => {
    await openOnboarding();
    expect(screen.getByLabelText("Твоё имя")).toHaveValue("Аня");
    expect(screen.getByLabelText("Как к тебе обращаться")).toHaveValue("");
    expect(screen.getByRole("group", { name: "Согласия" })).toBeInTheDocument();
    for (const c of CONSENTS) expect(screen.getByRole("checkbox", { name: c })).not.toBeChecked();
    expect(screen.getByText(/не оказывает психологических и медицинских услуг/)).toBeInTheDocument();
  });

  test("MainButton недоступна, пока не заполнены поля и не отмечены все четыре пункта; причина подписью", async () => {
    const r = await openOnboarding();
    expect(r.tg!.main).toMatchObject({ visible: true, text: "Продолжить", active: false });
    expect(screen.getByRole("status")).toHaveTextContent("заполни поля и отметь все четыре пункта");
    await r.pressMain();
    expect(r.server.calls("PUT", "/me/onboarding")).toHaveLength(0);
  });

  for (const missing of CONSENTS) {
    test(`без пункта «${missing}» продолжить нельзя`, async () => {
      const r = await openOnboarding();
      await fill(r, { skip: missing });
      expect(r.tg!.main.active).toBe(false);
      expect(screen.getByRole("status")).toHaveTextContent("отметь все четыре пункта");
      await r.pressMain();
      expect(r.server.calls("PUT", "/me/onboarding")).toHaveLength(0);
    });
  }

  test("пустое поле (только пробелы) — продолжить нельзя", async () => {
    const r = await openOnboarding();
    await fill(r);
    const partner = screen.getByLabelText("Имя партнёра");
    await r.user.clear(partner);
    await r.user.type(partner, "   ");
    expect(r.tg!.main.active).toBe(false);
    expect(screen.getByRole("status")).toHaveTextContent("заполни все три поля");
  });

  test("поля ограничены 64 символами (контракт OnboardingRequest)", async () => {
    await openOnboarding();
    for (const l of ["Твоё имя", "Как к тебе обращаться", "Имя партнёра"])
      expect(screen.getByLabelText(l)).toHaveAttribute("maxLength", "64");
  });

  test("всё заполнено → PUT /me/onboarding по контракту → POST /couple → ссылка-приглашение", async () => {
    const r = await openOnboarding();
    await fill(r);
    expect(r.tg!.main.active).toBe(true);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    await r.pressMain();
    expect(await heading("Пригласи партнёра")).toBeInTheDocument();
    const [put] = r.server.calls("PUT", "/me/onboarding");
    expect(put.body).toMatchObject({
      name: "Аня",
      address_form: "Аня",
      partner_name: "Игорь",
      consent_terms: true,
      consent_data: true,
      consent_sensitive: true,
      age_confirmed: true,
    });
    expect(typeof (put.body as { timezone?: string }).timezone).toBe("string");
    expect(r.server.calls("POST", "/couple")).toHaveLength(1);
    expect(screen.getByLabelText("Ссылка-приглашение")).toHaveTextContent("https://t.me/test_bot?startapp=");
    // Вернуться в пройденный онбординг кнопкой «Назад» нельзя.
    expect(r.tg!.back.visible).toBe(false);
    expect(r.tg!.haptics).toContain("notify:success");
  });

  test("ошибка сервера — текст ошибки подписью, данные формы сохраняются, повтор проходит", async () => {
    const server = new FakeServer().inject("PUT", "/me/onboarding", 500);
    const r = await openOnboarding(server);
    await fill(r);
    await r.pressMain();
    expect(await screen.findByRole("alert")).toHaveTextContent("Ошибка: Что-то пошло не так");
    expect(screen.getByLabelText("Имя партнёра")).toHaveValue("Игорь");
    expect(r.tg!.haptics).toContain("notify:error");
    await r.pressMain();
    expect(await heading("Пригласи партнёра")).toBeInTheDocument();
  });

  test("ответ 403 underage — экран «только с 18 лет»", async () => {
    const server = new FakeServer().inject("PUT", "/me/onboarding", 403, {
      code: "underage",
      message: "Сервис доступен только с 18 лет",
    });
    const r = await openOnboarding(server);
    await fill(r);
    await r.pressMain();
    expect(await heading("Сервис доступен только с 18 лет")).toBeInTheDocument();
    expect(r.tg!.main.visible).toBe(false);
    expect(screen.getByRole("button", { name: /Помощь/ })).toBeInTheDocument();
  });

  test("выпуск ссылки недоступен (503) — онбординг сохранён, на экране приглашения причина и «Создать ссылку»", async () => {
    const server = new FakeServer().inject("POST", "/couple", 503, {
      code: "service_unavailable",
      message: "Приглашения временно недоступны",
    });
    const r = await openOnboarding(server);
    await fill(r);
    await r.pressMain();
    expect(await heading("Пригласи партнёра")).toBeInTheDocument();
    expect(await screen.findByRole("alert")).toHaveTextContent("Сервис временно недоступен");
    expect(await screen.findByText(/Ссылки пока нет/)).toBeInTheDocument();
    expect(r.tg!.main.text).toBe("Создать ссылку-приглашение");
    await r.pressMain();
    expect(await screen.findByLabelText("Ссылка-приглашение")).toBeInTheDocument();
  });

  test("двойное нажатие «Продолжить» — один запрос", async () => {
    const r = await openOnboarding();
    await fill(r);
    await act(async () => {
      r.tg!.main.click();
      r.tg!.main.click();
    });
    await heading("Пригласи партнёра");
    expect(r.server.calls("PUT", "/me/onboarding")).toHaveLength(1);
    expect(r.server.calls("POST", "/couple")).toHaveLength(1);
  });
});
