/**
 * Экран «База знаний» (карточка 2.8, ФТ-15, ФТ-20, ТЗ §6.4): список со статусом индексации подписью и причиной
 * сбоя понятным текстом; «Обновить» и опрос; загрузка с обязательным основанием прав и проверкой размера и формата
 * до отправки; понятный текст на каждый код ответа; удаление с подтверждением. После каждого теста — журнал
 * заглушки: только `/api/v1/admin/*` (критерий RoadMap).
 */
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { MAX_FIELD_BYTES, MAX_FILE_BYTES } from "./api";
import { adminErrorText } from "./texts";
import { FakeAdminApi, deferred, makeDoc } from "./test/fakeAdminApi";
import { expectOnlyAdminRequests, login, renderAdmin } from "./test/renderAdmin";

let current: { fake: FakeAdminApi; globalFetch: { mock: { calls: unknown[] } } } | null = null;
afterEach(() => {
  if (current) expectOnlyAdminRequests(current.fake, current.globalFetch);
  current = null;
});

async function open(docs = [makeDoc()], pollMs?: number) {
  const fake = new FakeAdminApi();
  fake.docs = docs;
  const r = renderAdmin(fake, { pollMs });
  current = r;
  await login(r.user);
  await screen.findByRole("heading", { level: 1, name: "База знаний" });
  await waitFor(() => expect(screen.queryByText("Загружаем список материалов…")).not.toBeInTheDocument());
  return r;
}

const row = (title: string) => screen.getByRole("rowheader", { name: title }).closest("tr")!;

function pick(file: File) {
  fireEvent.change(screen.getByLabelText("Файл"), { target: { files: [file] } });
}

function sized(name: string, size: number): File {
  const f = new File(["x"], name);
  Object.defineProperty(f, "size", { value: size });
  return f;
}

const posts = (fake: FakeAdminApi) => fake.requests("POST");

describe("список материалов", () => {
  test("поля: название, автор, подход, основание прав, формат, размер, статус подписью, даты", async () => {
    await open([
      makeDoc({ title: "Книга А", author: null, approach: null, source_format: "epub", source_size: 2048, indexed_at: null, index_status: "indexing" }),
      makeDoc({ title: "Книга Б", rights_basis: "Договор № 1 (выдуманный)" }),
    ]);
    const a = within(row("Книга А"));
    expect(a.getAllByText("—").length).toBeGreaterThanOrEqual(3); // автор, подход, дата индексации
    expect(a.getByText("EPUB")).toBeInTheDocument();
    expect(a.getByText("2 КиБ")).toBeInTheDocument();
    expect(a.getByText("Индексируется")).toBeInTheDocument();
    const b = within(row("Книга Б"));
    expect(b.getByText("И. Иванова")).toBeInTheDocument();
    expect(b.getByText("Ненасильственное общение")).toBeInTheDocument();
    expect(b.getByText("Договор № 1 (выдуманный)")).toBeInTheDocument();
    expect(b.getByText("PDF")).toBeInTheDocument();
    expect(b.getByText("1,5 МиБ")).toBeInTheDocument();
    expect(b.getByText("Проиндексирован")).toBeInTheDocument();
    expect(b.getAllByText(/20 ноября 2026/, { selector: "td" })).toHaveLength(2); // загружен, проиндексирован
  });

  test.each([
    ["uploaded", "Загружен, ждёт индексации"],
    ["indexing", "Индексируется"],
    ["indexed", "Проиндексирован"],
    ["failed", "Сбой индексации"],
  ] as const)("статус %s — подписью «%s», а не только цветом", async (status, text) => {
    await open([makeDoc({ title: "Т", index_status: status, index_error: status === "failed" ? "corrupt" : null })]);
    expect(within(row("Т")).getByText(text)).toBeInTheDocument();
  });

  test.each([
    ["encrypted", "Файл зашифрован или защищён паролем."],
    ["empty_text", "В файле не найден текст (например, скан без текстового слоя)."],
    ["zip_bomb", "Файл распаковывается в подозрительно большой объём — разбор остановлен."],
    ["attempts_exhausted", "Индексация не удалась после нескольких попыток."],
    ["too_many_chunks", "Материал даёт слишком много фрагментов — разделите его на части."],
    ["brand_new_code", "Неизвестная причина (код «brand_new_code»)."],
  ])("сбой %s — понятная причина и код", async (code, text) => {
    await open([makeDoc({ title: "Т", index_status: "failed", index_error: code, indexed_at: null })]);
    const r = within(row("Т"));
    expect(r.getByText(`Причина: ${text}`, { exact: false })).toBeInTheDocument();
    expect(r.getByText(code)).toBeInTheDocument();
  });

  test("пустой список — подпись", async () => {
    await open([]);
    expect(screen.getByText("Материалов пока нет.")).toBeInTheDocument();
  });

  test("неизвестные поля ответа (например, текст) не попадают на экран", async () => {
    const fake = new FakeAdminApi();
    const doc = { ...makeDoc({ title: "Т" }), text: "СЕКРЕТНЫЙ ТЕКСТ ФРАГМЕНТА", chunks: ["ФРАГМЕНТ"] };
    fake.inject("GET", "/admin/kb/documents", { status: 200, body: [doc] });
    const r = renderAdmin(fake);
    current = r;
    await login(r.user);
    await screen.findByRole("rowheader", { name: "Т" });
    expect(document.body.textContent).not.toMatch(/СЕКРЕТНЫЙ|ФРАГМЕНТ/);
  });

  test("ответ не по контракту — ошибка подписью, ни одной строки", async () => {
    const fake = new FakeAdminApi();
    fake.inject("GET", "/admin/kb/documents", { status: 200, body: [{ ...makeDoc(), index_status: "done" }] });
    const r = renderAdmin(fake);
    current = r;
    await login(r.user);
    expect(await screen.findByRole("alert")).toHaveTextContent("Сервер ответил не по контракту");
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  test("нет связи — подпись; «Обновить» запрашивает снова", async () => {
    const fake = new FakeAdminApi();
    fake.docs = [makeDoc({ title: "Т" })];
    fake.inject("GET", "/admin/kb/documents", { status: 0, network: true });
    const r = renderAdmin(fake);
    current = r;
    await login(r.user);
    expect(await screen.findByRole("alert")).toHaveTextContent("Нет связи с сервером");
    await r.user.click(screen.getByRole("button", { name: "Обновить" }));
    expect(await screen.findByRole("rowheader", { name: "Т" })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(fake.requests("GET")).toHaveLength(2);
  });

  test("500 — «Сбой на сервере», не текст сервера", async () => {
    const fake = new FakeAdminApi();
    fake.inject("GET", "/admin/kb/documents", { status: 500, raw: "Internal Server Error with details" });
    const r = renderAdmin(fake);
    current = r;
    await login(r.user);
    expect(await screen.findByRole("alert")).toHaveTextContent("Сбой на сервере. Повторите позже.");
    expect(document.body.textContent).not.toContain("details");
  });

  test("опрос: пока есть uploaded/indexing — список перезапрашивается; после indexed — нет", async () => {
    const d = makeDoc({ title: "Т", index_status: "uploaded", indexed_at: null });
    const { fake } = await open([d], 20);
    await waitFor(() => expect(fake.requests("GET").length).toBeGreaterThanOrEqual(2));
    fake.setStatus(d.id, "indexed");
    expect(await within(row("Т")).findByText("Проиндексирован")).toBeInTheDocument();
    const n = fake.requests("GET").length;
    await new Promise((r) => setTimeout(r, 80));
    expect(fake.requests("GET")).toHaveLength(n);
  });

  test("без незавершённой индексации опроса нет", async () => {
    const { fake } = await open([makeDoc()], 10);
    await new Promise((r) => setTimeout(r, 60));
    expect(fake.requests("GET")).toHaveLength(1);
  });
});

describe("загрузка", () => {
  async function fill(user: Awaited<ReturnType<typeof open>>["user"], over: { title?: string; rights?: string; author?: string; approach?: string } = {}) {
    const title = screen.getByLabelText("Название");
    await user.clear(title);
    if (over.title !== "") await user.type(title, over.title ?? "Выдуманная статья");
    if (over.author) await user.type(screen.getByLabelText("Автор (необязательно)"), over.author);
    if (over.approach) await user.type(screen.getByLabelText("Подход (необязательно)"), over.approach);
    if (over.rights !== "") await user.type(screen.getByLabelText("Основание прав (обязательно)"), over.rights ?? "Разрешение автора (выдуманное)");
  }
  const submit = (user: Awaited<ReturnType<typeof open>>["user"]) => user.click(screen.getByRole("button", { name: "Загрузить" }));

  test("успех 202: поля формы multipart, материал в списке «Загружен, ждёт индексации», форма очищена", async () => {
    const { fake, user } = await open([]);
    pick(new File(["%PDF-1.4 выдуманный"], "Статья.PDF", { type: "application/pdf" }));
    expect(screen.getByLabelText("Название")).toHaveValue("Статья"); // по имени файла
    await fill(user, { author: "П. Петров", approach: "Метод Готтмана" });
    await submit(user);
    expect(await screen.findByText(/Материал «Выдуманная статья» принят/)).toBeInTheDocument();
    expect(posts(fake)).toHaveLength(1);
    expect(posts(fake)[0]).toMatchObject({
      path: "/api/v1/admin/kb/documents",
      body: {
        file: { name: "Статья.PDF" },
        title: "Выдуманная статья",
        author: "П. Петров",
        approach: "Метод Готтмана",
        rights_basis: "Разрешение автора (выдуманное)",
      },
    });
    expect(within(row("Выдуманная статья")).getByText("Загружен, ждёт индексации")).toBeInTheDocument();
    expect(screen.getByLabelText("Название")).toHaveValue("");
    expect(screen.getByLabelText("Основание прав (обязательно)")).toHaveValue("");
  });

  test("необязательные поля пустые — не отправляются", async () => {
    const { fake, user } = await open([]);
    pick(new File(["текст"], "a.txt"));
    await fill(user);
    await submit(user);
    await screen.findByText(/принят/);
    expect(Object.keys(posts(fake)[0].body!)).toEqual(["file", "title", "rights_basis"]);
  });

  test("основание прав обязательно: без него запрос не уходит, подпись у поля", async () => {
    const { fake, user } = await open([]);
    pick(new File(["x"], "a.pdf"));
    await fill(user, { rights: "" });
    await submit(user);
    const field = screen.getByLabelText("Основание прав (обязательно)");
    expect(field).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByText(/Укажите основание прав на использование материала/)).toBeInTheDocument();
    expect(field).toHaveFocus();
    expect(posts(fake)).toHaveLength(0);
  });

  test("основание прав из одних пробелов — тоже нельзя", async () => {
    const { fake, user } = await open([]);
    pick(new File(["x"], "a.pdf"));
    await fill(user, { rights: "   " });
    await submit(user);
    expect(screen.getByText(/Укажите основание прав/)).toBeInTheDocument();
    expect(posts(fake)).toHaveLength(0);
  });

  test.each([
    ["без файла", null, "Выберите файл."],
    ["формат .doc", new File(["x"], "old.doc"), "Поддерживаются только PDF, EPUB, DOCX и TXT."],
    ["без расширения", new File(["x"], "README"), "Поддерживаются только PDF, EPUB, DOCX и TXT."],
    ["пустой файл", new File([], "a.pdf"), "Файл пустой."],
    ["больше 20 МиБ", sized("big.pdf", MAX_FILE_BYTES + 1), "Файл больше 20 МиБ (20 МиБ)."],
  ])("проверка до отправки: %s", async (_, file, text) => {
    const { fake, user } = await open([]);
    if (file) pick(file);
    await fill(user);
    await submit(user);
    expect(screen.getByText(text)).toBeInTheDocument();
    expect(screen.getByLabelText("Файл")).toHaveAttribute("aria-invalid", "true");
    expect(posts(fake)).toHaveLength(0);
  });

  test("ровно 20 МиБ — допустимо", async () => {
    const { fake, user } = await open([]);
    pick(sized("edge.epub", MAX_FILE_BYTES));
    await fill(user);
    await submit(user);
    await waitFor(() => expect(posts(fake)).toHaveLength(1));
  });

  test("название обязательно и не длиннее 300", async () => {
    const { fake, user } = await open([]);
    pick(new File(["x"], "a.pdf"));
    await fill(user, { title: "" });
    await submit(user);
    expect(screen.getByText("Укажите название.")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Название"), { target: { value: "я".repeat(301) } });
    await submit(user);
    expect(screen.getByText("Название — не больше 300 символов.")).toBeInTheDocument();
    expect(posts(fake)).toHaveLength(0);
  });

  test.each([
    [409, "duplicate_document", "Этот файл уже загружен."],
    [413, "file_too_large", "Файл больше 20 МиБ — сервер его не принимает."],
    [415, "unsupported_format", "Сервер не распознал формат. Нужен настоящий PDF, EPUB, DOCX или TXT"],
    [422, "validation_error", "Сервер не принял форму."],
    [500, "internal", "Сбой на сервере. Повторите позже."],
    [409, "conflict_retry", "Не получилось с первого раза: на сервере одновременно шло другое изменение. Повторите."],
    [400, "http_error", "Сервер не принял форму: одно из полей слишком длинное (больше 64 КиБ)."],
  ])("ответ %i %s — понятный текст, форма сохранена", async (status, code, text) => {
    const { fake, user } = await open([]);
    fake.inject("POST", "/admin/kb/documents", { status, body: { code, message: "серверный текст" } });
    pick(new File(["x"], "a.pdf"));
    await fill(user);
    await submit(user);
    expect(await screen.findByRole("alert")).toHaveTextContent(text);
    expect(screen.queryByText("серверный текст")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Название")).toHaveValue("Выдуманная статья");
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  test("длины — в символах Unicode после trim (как сервер): 300 «𝔸» и 300 букв с пробелами по краям — можно", async () => {
    const { fake } = await open([]);
    pick(new File(["x"], "a.pdf"));
    fireEvent.change(screen.getByLabelText("Основание прав (обязательно)"), { target: { value: "Лицензия" } });
    fireEvent.change(screen.getByLabelText("Название"), { target: { value: "𝔸".repeat(300) } });
    fireEvent.change(screen.getByLabelText("Автор (необязательно)"), { target: { value: `  ${"я".repeat(200)}  ` } });
    fireEvent.change(screen.getByLabelText("Подход (необязательно)"), { target: { value: "😀".repeat(100) } });
    fireEvent.submit(screen.getByRole("button", { name: "Загрузить" }).closest("form")!);
    await screen.findByText(/принят/);
    expect(posts(fake)[0].body).toMatchObject({ title: "𝔸".repeat(300), author: "я".repeat(200), approach: "😀".repeat(100) });
  });

  test("автор 201 символ и подход 101 — подпись у поля, запроса нет", async () => {
    const { fake } = await open([]);
    pick(new File(["x"], "a.pdf"));
    fireEvent.change(screen.getByLabelText("Название"), { target: { value: "Т" } });
    fireEvent.change(screen.getByLabelText("Основание прав (обязательно)"), { target: { value: "Лицензия" } });
    fireEvent.change(screen.getByLabelText("Автор (необязательно)"), { target: { value: "𝔸".repeat(201) } });
    fireEvent.change(screen.getByLabelText("Подход (необязательно)"), { target: { value: "я".repeat(101) } });
    fireEvent.submit(screen.getByRole("button", { name: "Загрузить" }).closest("form")!);
    expect(await screen.findByText("Автор — не больше 200 символов.")).toBeInTheDocument();
    expect(screen.getByText("Подход — не больше 100 символов.")).toBeInTheDocument();
    expect(posts(fake)).toHaveLength(0);
  });

  test("основание прав: предел сервера 64 КиБ в UTF-8 — ровно можно, на байт больше нельзя", async () => {
    const { fake } = await open([]);
    pick(new File(["x"], "a.pdf"));
    fireEvent.change(screen.getByLabelText("Название"), { target: { value: "Т" } });
    const rights = screen.getByLabelText("Основание прав (обязательно)");
    // «п» — 2 байта UTF-8: 32 768 букв = 65 536 байт, ровно предел; плюс «x» — 65 537.
    fireEvent.change(rights, { target: { value: "п".repeat(MAX_FIELD_BYTES / 2) + "x" } });
    fireEvent.submit(rights.closest("form")!);
    expect(await screen.findByText(/Основание прав: слишком длинный текст: больше 64 КиБ/)).toBeInTheDocument();
    expect(rights).toHaveAttribute("aria-invalid", "true");
    expect(rights).toHaveFocus();
    expect(posts(fake)).toHaveLength(0);
    fireEvent.change(rights, { target: { value: "п".repeat(MAX_FIELD_BYTES / 2) } });
    fireEvent.submit(rights.closest("form")!);
    await screen.findByText(/принят/);
    expect(posts(fake)).toHaveLength(1);
  });

  test("срок загрузки вышел — подсказка обновить список (сервер мог принять материал)", () => {
    expect(adminErrorText("timeout", "upload")).toBe(
      "Сервер не ответил вовремя. Материал мог быть принят — обновите список, прежде чем загружать снова.",
    );
    expect(adminErrorText("timeout", "list")).toBe("Сервер не ответил вовремя. Повторите.");
  });

  test("ответ списка, начатый до загрузки, не затирает принятый материал; опрос индексации не встаёт", async () => {
    const { fake } = await open([makeDoc({ title: "Старый" })], 30);
    const held = deferred();
    fake.inject("GET", "/admin/kb/documents", { status: 200, body: [...fake.docs], hold: held.hold });
    fireEvent.click(screen.getByRole("button", { name: "Обновить" }));
    pick(new File(["x"], "a.txt"));
    fireEvent.change(screen.getByLabelText("Название"), { target: { value: "Новый" } });
    fireEvent.change(screen.getByLabelText("Основание прав (обязательно)"), { target: { value: "Лицензия" } });
    fireEvent.submit(screen.getByRole("button", { name: "Загрузить" }).closest("form")!);
    await screen.findByText(/Материал «Новый» принят/);
    held.release();
    await waitFor(() => expect(screen.getByRole("button", { name: "Обновить" })).not.toHaveAttribute("aria-disabled"));
    expect(screen.getByRole("rowheader", { name: "Новый" })).toBeInTheDocument();
    const gets = fake.requests("GET").length;
    await waitFor(() => expect(fake.requests("GET").length).toBeGreaterThan(gets)); // «Новый» ждёт индексации — опрос идёт
    expect(screen.getByRole("rowheader", { name: "Новый" })).toBeInTheDocument();
  });

  test("413 от прокси без JSON — текст по статусу", async () => {
    const { fake, user } = await open([]);
    fake.inject("POST", "/admin/kb/documents", { status: 413, raw: "<html>Request Entity Too Large</html>" });
    pick(new File(["x"], "a.pdf"));
    await fill(user);
    await submit(user);
    expect(await screen.findByRole("alert")).toHaveTextContent("Файл больше 20 МиБ — сервер его не принимает.");
  });

  test("ответ 200 вместо 202 — не по контракту", async () => {
    const { fake, user } = await open([]);
    fake.inject("POST", "/admin/kb/documents", { status: 200, body: makeDoc() });
    pick(new File(["x"], "a.pdf"));
    await fill(user);
    await submit(user);
    expect(await screen.findByRole("alert")).toHaveTextContent("Сервер ответил не по контракту");
  });

  test("нет связи — текст, повтор отправляет снова", async () => {
    const { fake, user } = await open([]);
    fake.inject("POST", "/admin/kb/documents", { status: 0, network: true });
    pick(new File(["x"], "a.pdf"));
    await fill(user);
    await submit(user);
    expect(await screen.findByRole("alert")).toHaveTextContent("Нет связи с сервером");
    await submit(user);
    expect(await screen.findByText(/принят/)).toBeInTheDocument();
    expect(posts(fake)).toHaveLength(2);
  });

  test("двойное нажатие во время загрузки — один запрос; кнопка «Загружаем…»", async () => {
    const { fake, user } = await open([]);
    const d = deferred();
    fake.inject("POST", "/admin/kb/documents", { status: 202, body: makeDoc({ title: "Т", index_status: "uploaded" }), hold: d.hold });
    pick(new File(["x"], "a.pdf"));
    await fill(user);
    await submit(user);
    const busy = await screen.findByRole("button", { name: "Загружаем…" });
    expect(busy).toHaveAttribute("aria-disabled", "true");
    await user.click(busy);
    d.release();
    expect(await screen.findByText(/принят/)).toBeInTheDocument();
    expect(posts(fake)).toHaveLength(1);
  });

  test.each([
    [401, "unauthorized", "Токен не принят"],
    [403, "admin_only", "Нужен токен администратора, а не вход через Telegram."],
  ])("загрузка: %i — снова поле токена", async (status, code, text) => {
    const { fake, user } = await open([]);
    fake.inject("POST", "/admin/kb/documents", { status, body: { code, message: "x" } });
    pick(new File(["x"], "a.pdf"));
    await fill(user);
    await submit(user);
    expect(await screen.findByLabelText("Токен администратора")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(text);
  });
});

describe("удаление", () => {
  test("с подтверждением: «Отмена» — запроса нет; «Удалить навсегда» — DELETE, строка убрана, подпись", async () => {
    const d = makeDoc({ title: "Т" });
    const { fake, user } = await open([d, makeDoc({ title: "Другая" })]);
    await user.click(screen.getByRole("button", { name: "Удалить «Т»" }));
    expect(within(row("Т")).getByText("Удалить «Т» вместе с фрагментами? Это не отменить.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Отмена" }));
    expect(fake.requests("DELETE")).toHaveLength(0);
    await user.click(screen.getByRole("button", { name: "Удалить «Т»" }));
    await user.click(screen.getByRole("button", { name: "Удалить навсегда" }));
    expect(await screen.findByText("Материал «Т» удалён вместе с фрагментами.")).toBeInTheDocument();
    expect(fake.requests("DELETE")).toEqual([expect.objectContaining({ path: `/api/v1/admin/kb/documents/${d.id}`, query: "", body: null })]);
    expect(screen.queryByRole("rowheader", { name: "Т" })).not.toBeInTheDocument();
    expect(screen.getByRole("rowheader", { name: "Другая" })).toBeInTheDocument();
  });

  test("404 — материал уже был удалён: строка убрана, не ошибка", async () => {
    const d = makeDoc({ title: "Т" });
    const { fake, user } = await open([d]);
    fake.docs = [];
    await user.click(screen.getByRole("button", { name: "Удалить «Т»" }));
    await user.click(screen.getByRole("button", { name: "Удалить навсегда" }));
    expect(await screen.findByText("Материал «Т» уже был удалён.")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByRole("rowheader", { name: "Т" })).not.toBeInTheDocument();
  });

  test("сбой сервера — строка на месте, причина подписью", async () => {
    const d = makeDoc({ title: "Т" });
    const { fake, user } = await open([d]);
    fake.inject("DELETE", `/admin/kb/documents/${d.id}`, { status: 503, body: { code: "service_unavailable", message: "x" } });
    await user.click(screen.getByRole("button", { name: "Удалить «Т»" }));
    await user.click(screen.getByRole("button", { name: "Удалить навсегда" }));
    expect(await within(row("Т")).findByRole("alert")).toHaveTextContent("Не удалось удалить. Сервис временно недоступен.");
  });

  test("нажатие во время удаления — один запрос", async () => {
    const d = makeDoc({ title: "Т" });
    const { fake, user } = await open([d]);
    const hold = deferred();
    fake.inject("DELETE", `/admin/kb/documents/${d.id}`, { status: 204, hold: hold.hold });
    await user.click(screen.getByRole("button", { name: "Удалить «Т»" }));
    await user.click(screen.getByRole("button", { name: "Удалить навсегда" }));
    const busy = screen.getByRole("button", { name: "Удаляем…" });
    await user.click(busy);
    hold.release();
    await screen.findByText(/удалён вместе с фрагментами/);
    expect(fake.requests("DELETE")).toHaveLength(1);
  });

  test.each([
    [500, "internal", "Не удалось удалить. Сбой на сервере. Повторите позже."],
    [409, "conflict_retry", "Не удалось удалить. Не получилось с первого раза"],
  ])("удаление: %i %s — понятный текст, строка на месте", async (status, code, text) => {
    const d = makeDoc({ title: "Т" });
    const { fake, user } = await open([d]);
    fake.inject("DELETE", `/admin/kb/documents/${d.id}`, { status, body: { code, message: "серверный текст" } });
    await user.click(screen.getByRole("button", { name: "Удалить «Т»" }));
    await user.click(screen.getByRole("button", { name: "Удалить навсегда" }));
    expect(await within(row("Т")).findByRole("alert")).toHaveTextContent(text);
    expect(screen.queryByText("серверный текст")).not.toBeInTheDocument();
  });

  test("ответ списка, начатый до удаления, не возвращает удалённый материал", async () => {
    const d = makeDoc({ title: "Т" });
    const { fake, user } = await open([d, makeDoc({ title: "Другая" })]);
    const held = deferred();
    fake.inject("GET", "/admin/kb/documents", { status: 200, body: [...fake.docs], hold: held.hold });
    await user.click(screen.getByRole("button", { name: "Обновить" }));
    await user.click(screen.getByRole("button", { name: "Удалить «Т»" }));
    await user.click(screen.getByRole("button", { name: "Удалить навсегда" }));
    await screen.findByText("Материал «Т» удалён вместе с фрагментами.");
    held.release();
    await waitFor(() => expect(screen.getByRole("button", { name: "Обновить" })).not.toHaveAttribute("aria-disabled"));
    expect(screen.queryByRole("rowheader", { name: "Т" })).not.toBeInTheDocument();
    expect(screen.getByRole("rowheader", { name: "Другая" })).toBeInTheDocument();
    expect(fake.requests("GET")).toHaveLength(3); // вход, устаревший (отброшен), повтор
  });

  test("удаление: 403 admin_only — поле токена", async () => {
    const d = makeDoc({ title: "Т" });
    const { fake, user } = await open([d]);
    fake.inject("DELETE", `/admin/kb/documents/${d.id}`, { status: 403, body: { code: "admin_only", message: "x" } });
    await user.click(screen.getByRole("button", { name: "Удалить «Т»" }));
    await user.click(screen.getByRole("button", { name: "Удалить навсегда" }));
    expect(await screen.findByLabelText("Токен администратора")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Нужен токен администратора, а не вход через Telegram.");
  });
});

