import { screen } from "@testing-library/react";
import { FakeServer, PARTNER_TOKEN } from "../test/fakeApi";
import { fakeTelegram } from "../test/fakeTelegram";
import { heading, renderApp, type Rendered } from "../test/renderApp";

function partner(server = new FakeServer().partnerInvite(), startParam = PARTNER_TOKEN): Rendered {
  return renderApp({ server, tg: fakeTelegram({ startParam, firstName: "Игорь" }) });
}

async function fillOnboarding(r: Rendered) {
  await r.user.type(screen.getByLabelText("Как к тебе обращаться"), "Игорь");
  for (const cb of screen.getAllByRole("checkbox")) await r.user.click(cb);
}

describe("Приглашение — партнёр по ссылке startapp (ФТ-03, ТЗ §4.1 п.4)", () => {
  test("до онбординга видно, кто приглашает, и срок; больше ничего", async () => {
    const r = partner();
    expect(await heading("Игорь приглашает тебя")).toBeInTheDocument();
    expect(screen.getByText(/Приглашение действует до 3 декабря/)).toBeInTheDocument();
    expect(screen.getByText(/Он не видит разговоров партнёра/)).toBeInTheDocument();
    expect(r.server.calls("GET", `/invites/${PARTNER_TOKEN}`)).toHaveLength(1);
    expect(r.tg!.main).toMatchObject({ visible: true, text: "Принять приглашение" });
  });

  test("«Принять» → приветствие → онбординг (имя партнёра подставлено) → POST accept → «Вы пара»", async () => {
    const r = partner();
    await heading("Игорь приглашает тебя");
    await r.pressMain();
    expect(await heading("Твой личный агент")).toBeInTheDocument();
    expect(r.server.calls("POST", `/invites/${PARTNER_TOKEN}/accept`)).toHaveLength(0);
    await r.pressMain();
    await r.pressMain();
    await r.pressMain();
    expect(await heading("Давай познакомимся")).toBeInTheDocument();
    expect(screen.getByLabelText("Имя партнёра")).toHaveValue("Игорь");
    expect(screen.getByText(/После этого шага ты и Игорь станете парой/)).toBeInTheDocument();
    await fillOnboarding(r);
    await r.pressMain();
    expect(await heading("Вы пара")).toBeInTheDocument();
    expect(r.server.calls("POST", `/invites/${PARTNER_TOKEN}/accept`)).toHaveLength(1);
    expect(r.server.calls("POST", "/couple")).toHaveLength(0);
    expect(r.tg!.back.visible).toBe(false);
  });

  test("«Назад» с приветствия возвращает к приглашению", async () => {
    const r = partner();
    await heading("Игорь приглашает тебя");
    await r.pressMain();
    await heading("Твой личный агент");
    await r.pressBack();
    expect(await heading("Игорь приглашает тебя")).toBeInTheDocument();
  });

  test("онбординг уже пройден — «Принять» сразу принимает", async () => {
    const r = partner(new FakeServer().onboarded().partnerInvite());
    await heading("Игорь приглашает тебя");
    await r.pressMain();
    expect(await heading("Вы пара")).toBeInTheDocument();
  });

  test("срок истёк (410 invite_expired) — причина подписью и «Продолжить без приглашения»", async () => {
    const r = partner(new FakeServer().partnerInvite("expired"));
    expect(await heading("Приглашение не работает")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Срок приглашения истёк");
    expect(r.tg!.main.visible).toBe(false);
    await r.user.click(screen.getByRole("button", { name: "Продолжить без приглашения" }));
    expect(await heading("Твой личный агент")).toBeInTheDocument();
  });

  test("использовано (410 invite_used)", async () => {
    partner(new FakeServer().partnerInvite("used"));
    expect(await screen.findByRole("alert")).toHaveTextContent("Приглашение уже использовано");
  });

  test("не найдено (404)", async () => {
    partner(new FakeServer());
    expect(await screen.findByRole("alert")).toHaveTextContent("Приглашение не найдено");
  });

  test("токен неверного формата — без запроса к серверу", async () => {
    const r = partner(new FakeServer(), "../../me");
    expect(await screen.findByRole("alert")).toHaveTextContent("Приглашение не найдено");
    expect(r.server.log.filter((l) => l.path.startsWith("/invites"))).toHaveLength(0);
  });

  test("сеть недоступна при проверке — «Попробовать снова»", async () => {
    const server = new FakeServer().partnerInvite();
    server.inject("GET", `/invites/${PARTNER_TOKEN}`, 503, { code: "service_unavailable", message: "x" });
    const r = partner(server);
    expect(await screen.findByRole("alert")).toHaveTextContent("Сервис временно недоступен");
    expect(r.tg!.main.text).toBe("Попробовать снова");
    await r.pressMain();
    expect(await heading("Игорь приглашает тебя")).toBeInTheDocument();
  });

  test("уже есть пара (409) после онбординга — причина подписью и выход без приглашения", async () => {
    const server = new FakeServer().partnerInvite();
    server.inject("POST", `/invites/${PARTNER_TOKEN}/accept`, 409, { code: "already_in_couple", message: "x" });
    const r = partner(server);
    await heading("Игорь приглашает тебя");
    await r.pressMain();
    await r.pressMain();
    await r.pressMain();
    await r.pressMain();
    await heading("Давай познакомимся");
    await fillOnboarding(r);
    await r.pressMain();
    expect(await screen.findByRole("alert")).toHaveTextContent("У тебя уже есть пара");
    await r.user.click(screen.getByRole("button", { name: "Продолжить без приглашения" }));
    expect(await heading("Пригласи партнёра")).toBeInTheDocument();
  });

  test("своё приглашение (409 own_invite) у вошедшего — подсказка отправить партнёру", async () => {
    const server = new FakeServer().onboarded().partnerInvite();
    server.inject("POST", `/invites/${PARTNER_TOKEN}/accept`, 409, { code: "own_invite", message: "x" });
    const r = partner(server);
    await heading("Игорь приглашает тебя");
    await r.pressMain();
    expect(await screen.findByRole("alert")).toHaveTextContent("Это твоё приглашение — отправь ссылку партнёру");
  });

  test("имя пригласившего неизвестно — нейтральный заголовок", async () => {
    partner(new FakeServer().partnerInvite("active", null));
    expect(await heading("Тебя приглашают в «Чувства AI»")).toBeInTheDocument();
  });
});
