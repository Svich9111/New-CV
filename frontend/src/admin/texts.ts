/**
 * Тексты панели для администратора. Ошибка показывается своим текстом по коду контракта; `message` сервера не
 * выводится (незнакомый код — нейтральный текст с кодом). Цветом ошибки не выделяются (ТЗ §8).
 */
import type { KbIndexStatus } from "./types";

export type AdminOp = "list" | "upload" | "delete";

const BY_CODE: Record<string, string> = {
  network: "Нет связи с сервером. Проверьте сеть и повторите.",
  timeout: "Сервер не ответил вовремя. Повторите.",
  bad_response: "Сервер ответил не по контракту. Повторите позже.",
  unauthorized: "Токен не принят. Введите токен администратора заново.",
  admin_only: "Нужен токен администратора, а не вход через Telegram.",
  forbidden: "Доступ запрещён.",
  server_error: "Сбой на сервере. Повторите позже.",
  service_unavailable: "Сервис временно недоступен. Повторите позже.",
  duplicate_document:
    "Этот файл уже загружен. Загрузить его снова можно, только если прежняя индексация закончилась сбоем.",
  file_too_large: "Файл больше 20 МиБ — сервер его не принимает.",
  unsupported_format:
    "Сервер не распознал формат. Нужен настоящий PDF, EPUB, DOCX или TXT: проверяется содержимое файла, а не только расширение.",
  validation_error:
    "Сервер не принял форму. Проверьте: файл не пустой, название до 300 символов, автор до 200, подход до 100, основание прав заполнено.",
  not_found: "Материал не найден.",
};

const BY_OP: Partial<Record<AdminOp, Record<string, string>>> = {
  list: { not_found: "Список материалов не найден на сервере. Проверьте адрес панели." },
};

export function adminErrorText(code: string, op: AdminOp): string {
  return BY_OP[op]?.[code] ?? BY_CODE[code] ?? `Сервер ответил ошибкой (код «${code}»). Повторите позже.`;
}

/** Статус индексации — подписью, не цветом (ТЗ §8). */
export const STATUS_TEXT: Record<KbIndexStatus, string> = {
  uploaded: "Загружен, ждёт индексации",
  indexing: "Индексируется",
  indexed: "Проиндексирован",
  failed: "Сбой индексации",
};

/** Коды причины сбоя индексации (`index_error`; перечень — воркер A3: `kb/parse.py`, `workers/kb.py`). */
const INDEX_ERROR_TEXT: Record<string, string> = {
  too_large: "Текст файла слишком большой для разбора.",
  too_many_pages: "Слишком много страниц (больше 2000).",
  too_many_chunks: "Материал даёт слишком много фрагментов — разделите его на части.",
  zip_bomb: "Файл распаковывается в подозрительно большой объём — разбор остановлен.",
  encrypted: "Файл зашифрован или защищён паролем.",
  corrupt: "Файл повреждён или не читается.",
  empty_text: "В файле не найден текст (например, скан без текстового слоя).",
  timeout: "Разбор занял слишком много времени.",
  unsupported_format: "Формат файла не поддерживается.",
  attempts_exhausted: "Индексация не удалась после нескольких попыток.",
};

export function indexErrorText(code: string | null | undefined): string {
  if (!code) return "Причина не указана.";
  return INDEX_ERROR_TEXT[code] ?? `Неизвестная причина (код «${code}»).`;
}
