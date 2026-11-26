/**
 * Вход администратора (карточка 2.8): токен — только в памяти вкладки; 401 — снова поле токена; 403 `admin_only` —
 * «Нужен токен администратора, а не вход через Telegram»; «Выйти» — забыть токен. Токен не попадает в журнал,
 * консоль (проверяет общий `src/test/setup.ts`), адрес, хранилища, cookie.
 */
import { screen, waitFor } from "@testing-library/react";
import { ADMIN_TOKEN, FakeAdminApi, USER_SESSION_TOKEN, makeDoc } from "./test/fakeAdminApi";
import { expectOnlyAdminRequests, login, renderAdmin } from "./test/renderAdmin";

function expectTokenNowhere(token: string) {
  const storages = [localStorage, sessionStorage];
  for (const s of storages) {
    for (let i = 0; i < s.length; i++) {
      const k = s.key(i)!;
      expect(k + (s.getItem(k) ?? "")).not.toContain(token);
    }
  }
  expect(document.cookie).not.toContain(token);
  expect(window.location.href).not.toContain(token);
  expect(window.history.state === null || !JSON.stringify(window.history.state).includes(token)).toBe(true);
  expect(document.documentElement.outerHTML).not.toContain(token);
}

test("без токена — только поле входа и ни одного запроса", () => {
  const { fake, globalFetch } = renderAdmin();
  expect(screen.getByRole("heading", { level: 1, name: "Админ-панель «Чувства AI»" })).toBeInTheDocument();
  const input = screen.getByLabelText("Токен администратора");
  expect(input).toHaveAttribute("type", "password");
  expect(input).toHaveAttribute("autocomplete", "off");
  // Без name поле не уйдёт ни в отправку формы, ни в адрес, даже если обработчик не сработает.
  expect(input).not.toHaveAttribute("name");
  expect(input.closest("form")).not.toHaveAttribute("action");
  expect(fake.log).toEqual([]);
  expect(globalFetch).not.toHaveBeenCalled();
});

test("пустой токен — подпись под полем, запроса нет", async () => {
  const { fake, user } = renderAdmin();
  await user.click(screen.getByRole("button", { name: "Войти" }));
  expect(screen.getByText("Введите токен.")).toBeInTheDocument();
  expect(screen.getByLabelText("Токен администратора")).toHaveAttribute("aria-invalid", "true");
  expect(fake.log).toEqual([]);
});

test("вход: токен уходит только заголовком Authorization к /admin/*, без cookie; нигде не хранится", async () => {
  const fake = new FakeAdminApi();
  fake.docs = [makeDoc({ title: "Книга А" })];
  const setItem = vi.spyOn(Storage.prototype, "setItem");
  const { user, globalFetch } = renderAdmin(fake);
  await login(user);
  expect(await screen.findByRole("rowheader", { name: "Книга А" })).toBeInTheDocument();
  expect(fake.log).toHaveLength(1);
  expect(fake.log[0]).toMatchObject({ method: "GET", path: "/api/v1/admin/kb/documents", query: "", auth: `Bearer ${ADMIN_TOKEN}`, credentials: "omit" });
  expect(setItem).not.toHaveBeenCalled();
  expectTokenNowhere(ADMIN_TOKEN);
  expectOnlyAdminRequests(fake, globalFetch);
});

test("401 — снова поле токена с понятной причиной; токен забыт (следующий вход — с новым)", async () => {
  const { fake, user, globalFetch } = renderAdmin();
  await login(user, "wrong-token-value");
  expect(await screen.findByLabelText("Токен администратора")).toHaveValue("");
  expect(screen.getByRole("alert")).toHaveTextContent("Токен не принят. Введите токен администратора заново.");
  await login(user);
  expect(await screen.findByRole("heading", { level: 1, name: "База знаний" })).toBeInTheDocument();
  expect(fake.log.map((l) => l.auth)).toEqual(["Bearer wrong-token-value", `Bearer ${ADMIN_TOKEN}`]);
  expectOnlyAdminRequests(fake, globalFetch);
});

test("403 admin_only (вход сессией Mini App) — «Нужен токен администратора, а не вход через Telegram»", async () => {
  const { fake, user, globalFetch } = renderAdmin();
  await login(user, USER_SESSION_TOKEN);
  expect(await screen.findByRole("alert")).toHaveTextContent("Нужен токен администратора, а не вход через Telegram.");
  expect(screen.getByLabelText("Токен администратора")).toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "База знаний" })).not.toBeInTheDocument();
  expectTokenNowhere(USER_SESSION_TOKEN);
  expectOnlyAdminRequests(fake, globalFetch);
});

test("401 посреди работы (токен отозван) — поле токена, следующий запрос без старого токена не уходит", async () => {
  const fake = new FakeAdminApi();
  fake.docs = [makeDoc()];
  const { user } = renderAdmin(fake);
  await login(user);
  await screen.findByRole("heading", { level: 1, name: "База знаний" });
  fake.inject("GET", "/admin/kb/documents", { status: 401, body: { code: "unauthorized", message: "x" } });
  await user.click(screen.getByRole("button", { name: "Обновить" }));
  expect(await screen.findByLabelText("Токен администратора")).toBeInTheDocument();
  expect(screen.getByRole("alert")).toHaveTextContent("Токен не принят");
  const before = fake.log.length;
  await new Promise((r) => setTimeout(r, 20));
  expect(fake.log).toHaveLength(before);
});

test("«Выйти» — токен забыт: снова поле входа, запросов со старым токеном больше нет", async () => {
  const fake = new FakeAdminApi();
  fake.docs = [makeDoc({ index_status: "indexing", indexed_at: null })];
  const { user, globalFetch } = renderAdmin(fake, { pollMs: 10 });
  await login(user);
  await screen.findByRole("heading", { level: 1, name: "База знаний" });
  await user.click(screen.getByRole("button", { name: "Выйти" }));
  expect(screen.getByLabelText("Токен администратора")).toHaveValue("");
  expect(screen.getByText(/Вы вышли\. Токен забыт/)).toBeInTheDocument();
  const before = fake.log.length;
  await new Promise((r) => setTimeout(r, 60)); // опрос индексации остановлен вместе с экраном
  expect(fake.log).toHaveLength(before);
  expectTokenNowhere(ADMIN_TOKEN);
  expectOnlyAdminRequests(fake, globalFetch);
});

test("токен не попадает в DOM и не виден в поле после входа", async () => {
  const fake = new FakeAdminApi();
  const { user } = renderAdmin(fake);
  await login(user);
  await waitFor(() => expect(fake.log).toHaveLength(1));
  expect(await screen.findByText("Материалов пока нет.")).toBeInTheDocument();
  expect(document.body.innerHTML).not.toContain(ADMIN_TOKEN);
});
