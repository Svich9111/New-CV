import { useRef, useState, type FormEvent } from "react";
import { Notice } from "../ui/Notice";
import { AdminApiError, KB_FORMATS, MAX_FIELD_BYTES, MAX_FILE_BYTES, type AdminApi } from "./api";
import { extensionOf, formatSize, titleFromFileName } from "./format";
import { adminErrorText } from "./texts";
import type { KbDocument } from "./types";

/** Подходы для первого наполнения (ТЗ §6.4, [уточняется]) — подсказки, поле свободное. */
const APPROACHES = ["Ненасильственное общение", "Эмоционально-фокусированная терапия", "Метод Готтмана", "Теория привязанности"];

type Field = "file" | "title" | "author" | "approach" | "rights_basis";
type Errors = Partial<Record<Field, string>>;

const LIMITS = { title: 300, author: 200, approach: 100 } as const;

/** Длина в символах Unicode (как `len()` в pydantic и `length()` в PostgreSQL), а не в единицах UTF-16. */
const charCount = (s: string) => [...s].length;
const utf8Bytes = (s: string) => new TextEncoder().encode(s).length;
const TOO_LONG_BYTES = "слишком длинный текст: больше 64 КиБ (около 32 000 букв кириллицы) сервер не принимает.";

/**
 * Проверка до отправки (контракт `POST /admin/kb/documents`, `UploadFields` в `admin_kb.py`): файл, формат, размер,
 * обязательные поля, длины. Длины считаются у значения после `trim()` — именно оно уходит на сервер; каждое поле —
 * не больше 64 КиБ в UTF-8 (`max_part_size` сервера, иначе 400).
 */
export function validateUpload(v: { file: File | null; title: string; author: string; approach: string; rights_basis: string }): Errors {
  const errors: Errors = {};
  const title = v.title.trim();
  const author = v.author.trim();
  const approach = v.approach.trim();
  const rights = v.rights_basis.trim();
  if (!v.file) errors.file = "Выберите файл.";
  else if (!(KB_FORMATS as readonly string[]).includes(extensionOf(v.file.name)))
    errors.file = "Поддерживаются только PDF, EPUB, DOCX и TXT.";
  else if (v.file.size === 0) errors.file = "Файл пустой.";
  else if (v.file.size > MAX_FILE_BYTES) errors.file = `Файл больше 20 МиБ (${formatSize(v.file.size)}).`;
  if (!title) errors.title = "Укажите название.";
  else if (charCount(title) > LIMITS.title) errors.title = `Название — не больше ${LIMITS.title} символов.`;
  if (charCount(author) > LIMITS.author) errors.author = `Автор — не больше ${LIMITS.author} символов.`;
  if (charCount(approach) > LIMITS.approach) errors.approach = `Подход — не больше ${LIMITS.approach} символов.`;
  if (!rights)
    errors.rights_basis = "Укажите основание прав на использование материала — без него загрузка невозможна (ТЗ §6.4).";
  else if (utf8Bytes(rights) > MAX_FIELD_BYTES) errors.rights_basis = `Основание прав: ${TOO_LONG_BYTES}`;
  // Название, автор и подход по пределу символов укладываются в 64 КиБ; проверка — на случай смены пределов.
  for (const [f, value] of [["title", title], ["author", author], ["approach", approach]] as const)
    if (!errors[f] && utf8Bytes(value) > MAX_FIELD_BYTES) errors[f] = `Поле: ${TOO_LONG_BYTES}`;
  return errors;
}

const ORDER: Field[] = ["file", "title", "author", "approach", "rights_basis"];

export function UploadForm({
  api,
  onUploaded,
  onAuthError,
}: {
  api: AdminApi;
  onUploaded(doc: KbDocument): void;
  onAuthError(code: string): void;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState("");
  const [author, setAuthor] = useState("");
  const [approach, setApproach] = useState("");
  const [rights, setRights] = useState("");
  const [errors, setErrors] = useState<Errors>({});
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ kind: "info" | "error"; text: string } | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const form = useRef<HTMLFormElement>(null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (busy) return;
    const found = validateUpload({ file, title, author, approach, rights_basis: rights });
    setErrors(found);
    setResult(null);
    const first = ORDER.find((f) => found[f]);
    if (first) {
      form.current?.querySelector<HTMLElement>(`#kb-${first}`)?.focus();
      return;
    }
    setBusy(true);
    try {
      const doc = await api.uploadDocument({
        file: file!,
        title: title.trim(),
        author: author.trim() || undefined,
        approach: approach.trim() || undefined,
        rights_basis: rights.trim(),
      });
      setResult({ kind: "info", text: `Материал «${doc.title}» принят. Индексация идёт в фоне — статус обновится в списке.` });
      setFile(null);
      setTitle("");
      setAuthor("");
      setApproach("");
      setRights("");
      if (fileInput.current) fileInput.current.value = "";
      onUploaded(doc);
    } catch (err) {
      const code = err instanceof AdminApiError ? err.code : "bad_response";
      if (err instanceof AdminApiError && (err.status === 401 || err.status === 403)) {
        onAuthError(code);
        return;
      }
      setResult({ kind: "error", text: adminErrorText(code, "upload") });
    } finally {
      setBusy(false);
    }
  };

  const describe = (f: Field, hint?: string) => [errors[f] ? `kb-${f}-error` : "", hint ?? ""].filter(Boolean).join(" ") || undefined;
  const error = (f: Field) =>
    errors[f] ? (
      <p id={`kb-${f}-error`} className="admin-field-error small">
        <span aria-hidden="true">! </span>
        {errors[f]}
      </p>
    ) : null;

  return (
    <section aria-labelledby="kb-upload-title" className="card">
      <h2 id="kb-upload-title">Загрузить материал</h2>
      <p className="muted small">
        Только материалы, на использование которых есть права (ТЗ §6.4). Тексты книг и фрагментов в панели не
        показываются.
      </p>
      <form ref={form} onSubmit={submit} noValidate>
        <div className="field">
          <label htmlFor="kb-file">Файл</label>
          <input
            ref={fileInput}
            id="kb-file"
            type="file"
            accept=".pdf,.epub,.docx,.txt"
            aria-invalid={errors.file ? true : undefined}
            aria-describedby={describe("file", "kb-file-hint")}
            onChange={(e) => {
              const f = e.target.files?.[0] ?? null;
              setFile(f);
              setErrors((prev) => ({ ...prev, file: undefined }));
              if (f && !title.trim()) setTitle(titleFromFileName(f.name));
            }}
          />
          <p id="kb-file-hint" className="muted small">
            PDF, EPUB, DOCX или TXT, не больше 20 МиБ.{file ? ` Выбран: ${file.name}, ${formatSize(file.size)}.` : ""}
          </p>
          {error("file")}
        </div>
        <div className="field">
          <label htmlFor="kb-title">Название</label>
          <input
            id="kb-title"
            className="input"
            value={title}
            aria-invalid={errors.title ? true : undefined}
            aria-describedby={describe("title")}
            onChange={(e) => setTitle(e.target.value)}
          />
          {error("title")}
        </div>
        <div className="admin-form-row">
          <div className="field">
            <label htmlFor="kb-author">Автор (необязательно)</label>
            <input
              id="kb-author"
              className="input"
              value={author}
              aria-invalid={errors.author ? true : undefined}
              aria-describedby={describe("author")}
              onChange={(e) => setAuthor(e.target.value)}
            />
            {error("author")}
          </div>
          <div className="field">
            <label htmlFor="kb-approach">Подход (необязательно)</label>
            <input
              id="kb-approach"
              className="input"
              list="kb-approaches"
              value={approach}
              aria-invalid={errors.approach ? true : undefined}
              aria-describedby={describe("approach")}
              onChange={(e) => setApproach(e.target.value)}
            />
            <datalist id="kb-approaches">
              {APPROACHES.map((a) => (
                <option key={a} value={a} />
              ))}
            </datalist>
            {error("approach")}
          </div>
        </div>
        <div className="field">
          <label htmlFor="kb-rights_basis">Основание прав (обязательно)</label>
          <textarea
            id="kb-rights_basis"
            className="input"
            rows={2}
            value={rights}
            aria-required="true"
            aria-invalid={errors.rights_basis ? true : undefined}
            aria-describedby={describe("rights_basis", "kb-rights-hint")}
            onChange={(e) => setRights(e.target.value)}
          />
          <p id="kb-rights-hint" className="muted small">
            Например: договор с издательством, лицензия, разрешение автора, общественное достояние.
          </p>
          {error("rights_basis")}
        </div>
        {result && <Notice kind={result.kind}>{result.text}</Notice>}
        <button type="submit" className="admin-primary" aria-disabled={busy || undefined}>
          {busy ? "Загружаем…" : "Загрузить"}
        </button>
      </form>
    </section>
  );
}
