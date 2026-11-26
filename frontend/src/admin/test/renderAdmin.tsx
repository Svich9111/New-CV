import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AdminApp } from "../AdminApp";
import { ADMIN_TOKEN, FakeAdminApi } from "./fakeAdminApi";

/**
 * Панель на заглушке. Глобальный `fetch` подменён отказом: клиент панели обязан ходить только через переданный
 * `fetch` заглушки, а она пишет каждый запрос в журнал (`expectOnlyAdminRequests`).
 */
export function renderAdmin(fake = new FakeAdminApi(), opts: { pollMs?: number } = {}) {
  const globalFetch = vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("global fetch is not allowed"));
  const user = userEvent.setup();
  const view = render(<AdminApp fetch={fake.fetch} pollMs={opts.pollMs ?? 60_000} />);
  return { fake, user, view, globalFetch };
}

export async function login(user: ReturnType<typeof userEvent.setup>, token = ADMIN_TOKEN) {
  await user.type(screen.getByLabelText("Токен администратора"), token);
  await user.click(screen.getByRole("button", { name: "Войти" }));
}

/** Критерий RoadMap (карточка 2.8, п.1): каждый запрос панели — к `/api/v1/admin/*`, других нет. */
export function expectOnlyAdminRequests(fake: FakeAdminApi, globalFetch?: { mock: { calls: unknown[] } }) {
  for (const r of fake.log) expect(r.path, `${r.method} ${r.path}`).toMatch(/^\/api\/v1\/admin\/kb\/documents(\/[0-9a-f-]{36})?$/);
  if (globalFetch) expect(globalFetch.mock.calls).toEqual([]);
}
