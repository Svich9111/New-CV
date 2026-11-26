import { useCallback, useEffect, useRef, useState } from "react";
import { Loading, Notice } from "../ui/Notice";
import { AdminApiError, type AdminApi } from "./api";
import { formatDateTime, formatSize, formatTime } from "./format";
import { STATUS_TEXT, adminErrorText, indexErrorText } from "./texts";
import type { KbDocument } from "./types";
import { UploadForm } from "./UploadForm";

/** Пока есть материалы в `uploaded`/`indexing`, список перезапрашивается с этим шагом. */
export const POLL_MS = 5_000;

const isAuth = (e: unknown): e is AdminApiError => e instanceof AdminApiError && (e.status === 401 || e.status === 403);
const codeOf = (e: unknown) => (e instanceof AdminApiError ? e.code : "bad_response");

type RowState = { phase: "confirm" } | { phase: "deleting" } | { phase: "error"; text: string };

/**
 * База знаний (ФТ-15, ФТ-20, ТЗ §6.4): список материалов со статусом индексации подписью, загрузка и удаление
 * с подтверждением. Только метаданные — текстов книг и фрагментов в API нет.
 */
export function KbScreen({ api, onAuthError, pollMs = POLL_MS }: { api: AdminApi; onAuthError(code: string): void; pollMs?: number }) {
  const [docs, setDocs] = useState<KbDocument[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  const [rows, setRows] = useState<Record<string, RowState>>({});
  const [notice, setNotice] = useState<string | null>(null);
  const alive = useRef(true);
  const inFlight = useRef(false);
  /**
   * Номер изменения списка (принятая загрузка, удаление). Ответ `GET`, начатого до изменения, устарел: он затёр бы
   * только что загруженный материал или вернул удалённый. Такой ответ отбрасывается, и список запрашивается снова.
   */
  const changes = useRef(0);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const load = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    setLoading(true);
    try {
      let list: KbDocument[];
      for (;;) {
        const started = changes.current;
        list = await api.listDocuments();
        if (!alive.current) return;
        if (started === changes.current) break;
      }
      setDocs(list);
      setLoadError(null);
      setUpdatedAt(new Date());
    } catch (e) {
      if (!alive.current) return;
      if (isAuth(e)) {
        onAuthError(e.code);
        return;
      }
      setLoadError(adminErrorText(codeOf(e), "list"));
    } finally {
      inFlight.current = false;
      if (alive.current) setLoading(false);
    }
  }, [api, onAuthError]);

  useEffect(() => {
    void load();
  }, [load]);

  // Опрос, пока индексация не закончилась (и последний запрос прошёл без ошибки).
  const pending = docs?.some((d) => d.index_status === "uploaded" || d.index_status === "indexing") ?? false;
  useEffect(() => {
    if (!pending || loadError || loading) return;
    const t = setTimeout(() => void load(), pollMs);
    return () => clearTimeout(t);
  }, [pending, loadError, loading, load, pollMs]);

  const setRow = (id: string, s: RowState | null) =>
    setRows((prev) => {
      const next = { ...prev };
      if (s) next[id] = s;
      else delete next[id];
      return next;
    });

  const remove = async (doc: KbDocument) => {
    if (rows[doc.id]?.phase === "deleting") return;
    setRow(doc.id, { phase: "deleting" });
    try {
      const r = await api.deleteDocument(doc.id);
      if (!alive.current) return;
      changes.current += 1;
      setDocs((prev) => prev?.filter((d) => d.id !== doc.id) ?? prev);
      setRow(doc.id, null);
      setNotice(r === "deleted" ? `Материал «${doc.title}» удалён вместе с фрагментами.` : `Материал «${doc.title}» уже был удалён.`);
    } catch (e) {
      if (!alive.current) return;
      if (isAuth(e)) {
        onAuthError(e.code);
        return;
      }
      setRow(doc.id, { phase: "error", text: `Не удалось удалить. ${adminErrorText(codeOf(e), "delete")}` });
    }
  };

  const onUploaded = (doc: KbDocument) => {
    changes.current += 1;
    setNotice(null);
    setDocs((prev) => [doc, ...(prev ?? []).filter((d) => d.id !== doc.id)]);
  };

  return (
    <>
      <UploadForm api={api} onUploaded={onUploaded} onAuthError={onAuthError} />

      <section aria-labelledby="kb-list-title">
        <div className="admin-toolbar">
          <h2 id="kb-list-title">Материалы</h2>
          <button type="button" className="btn btn--ghost" aria-disabled={loading || undefined} onClick={() => void load()}>
            {loading ? "Обновляем…" : "Обновить"}
          </button>
        </div>
        <p className="muted small" role="status">
          {updatedAt ? `Список обновлён в ${formatTime(updatedAt)}.` : ""}
          {pending && !loadError ? " Идёт индексация — список обновляется сам." : ""}
        </p>
        {notice && <Notice>{notice}</Notice>}
        {loadError && <Notice kind="error">{loadError}</Notice>}
        {docs === null && !loadError && <Loading>Загружаем список материалов…</Loading>}
        {docs?.length === 0 && <p className="muted">Материалов пока нет.</p>}
        {docs && docs.length > 0 && (
          <div className="admin-table-wrap">
            <table className="admin-table">
              <thead>
                <tr>
                  <th scope="col">Название</th>
                  <th scope="col">Автор</th>
                  <th scope="col">Подход</th>
                  <th scope="col">Основание прав</th>
                  <th scope="col">Формат</th>
                  <th scope="col">Размер</th>
                  <th scope="col">Статус</th>
                  <th scope="col">Загружен</th>
                  <th scope="col">Проиндексирован</th>
                  <th scope="col">
                    <span className="visually-hidden">Действия</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {docs.map((d) => (
                  <DocRow
                    key={d.id}
                    doc={d}
                    state={rows[d.id]}
                    onAsk={() => setRow(d.id, { phase: "confirm" })}
                    onCancel={() => setRow(d.id, null)}
                    onConfirm={() => void remove(d)}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  );
}

function DocRow({
  doc,
  state,
  onAsk,
  onCancel,
  onConfirm,
}: {
  doc: KbDocument;
  state: RowState | undefined;
  onAsk(): void;
  onCancel(): void;
  onConfirm(): void;
}) {
  return (
    <tr data-testid={`kb-row-${doc.id}`}>
      <th scope="row" className="admin-table__title">
        {doc.title}
      </th>
      <td>{doc.author ?? "—"}</td>
      <td>{doc.approach ?? "—"}</td>
      <td className="admin-table__rights">{doc.rights_basis}</td>
      <td>{doc.source_format ? doc.source_format.toUpperCase() : "—"}</td>
      <td className="admin-table__num">{formatSize(doc.source_size)}</td>
      <td>
        <span className={`admin-status admin-status--${doc.index_status}`}>
          <span className="admin-status__mark" aria-hidden="true">
            {doc.index_status === "indexed" ? "✓" : doc.index_status === "failed" ? "!" : "…"}
          </span>
          {STATUS_TEXT[doc.index_status]}
        </span>
        {doc.index_status === "failed" && (
          <p className="small admin-status__reason">
            Причина: {indexErrorText(doc.index_error)}
            {doc.index_error && <code className="admin-code"> {doc.index_error}</code>}
          </p>
        )}
      </td>
      <td>{formatDateTime(doc.created_at)}</td>
      <td>{formatDateTime(doc.indexed_at)}</td>
      <td className="admin-table__actions">
        {state?.phase === "confirm" ? (
          <div role="group" aria-label={`Подтверждение удаления «${doc.title}»`} className="admin-confirm">
            <p className="small">Удалить «{doc.title}» вместе с фрагментами? Это не отменить.</p>
            <button type="button" className="btn" onClick={onConfirm}>
              Удалить навсегда
            </button>
            <button type="button" className="btn btn--ghost" onClick={onCancel}>
              Отмена
            </button>
          </div>
        ) : state?.phase === "deleting" ? (
          <button type="button" className="btn btn--ghost" aria-disabled="true">
            Удаляем…
          </button>
        ) : (
          <>
            <button type="button" className="btn btn--ghost" onClick={onAsk} aria-label={`Удалить «${doc.title}»`}>
              Удалить
            </button>
            {state?.phase === "error" && (
              <p className="small admin-field-error" role="alert">
                <span aria-hidden="true">! </span>
                {state.text}
              </p>
            )}
          </>
        )}
      </td>
    </tr>
  );
}
