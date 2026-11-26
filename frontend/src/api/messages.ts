/**
 * Тексты ошибок для человека — на «ты», как весь интерфейс (ТЗ §2, тон). Сервер отдаёт `message`
 * на «вы»; клиент показывает свой текст по паре (операция, `code`), а для незнакомого кода — `message`
 * сервера. Ни одна ошибка не окрашивается красным (ТЗ §8): красный — только главное действие и близость.
 *
 * Коды контракта общие для разных путей (`not_found` отдают и приглашение, и пара, а в 2.x — черновик
 * и сообщения), поэтому общий текст нейтрален, а точный — в переопределении для операции.
 */

/** Операция клиента: определяет, какой текст получит общий код. */
export type Op =
  | "login"
  | "me"
  | "onboarding"
  | "couple"
  | "createCouple"
  | "reissueInvite"
  | "invite"
  | "acceptInvite"
  | "help"
  | "chatHistory"
  | "chatSend"
  | "memoryList"
  | "memoryDelete";

const INVITE_NOT_FOUND = "Приглашение не найдено. Попроси партнёра прислать новую ссылку.";

const BY_CODE: Record<string, string> = {
  network: "Нет связи с сервером. Проверь интернет и попробуй ещё раз.",
  timeout: "Сервер не ответил вовремя. Проверь интернет и попробуй ещё раз.",
  bad_response: "Сервер ответил непонятно. Попробуй ещё раз чуть позже.",
  invalid_init_data: "Не получилось подтвердить вход через Telegram. Закрой приложение и открой его снова.",
  unauthorized: "Сессия закончилась. Закрой приложение и открой его снова.",
  account_deleted: "Этот аккаунт удалён. Повторная регистрация появится позже.",
  validation_error: "Сервер не принял данные. Проверь их и попробуй ещё раз.",
  service_unavailable: "Сервис временно недоступен. Попробуй чуть позже.",
  onboarding_required: "Сначала нужно познакомиться: заполни поля и отметь согласия.",
  underage: "Сервис доступен только с 18 лет.",
  not_found: "Не нашли то, что нужно. Попробуй ещё раз.",
  already_in_couple: "У тебя уже есть пара. Одновременно можно состоять только в одной.",
  own_invite: "Это твоё приглашение — отправь ссылку партнёру.",
  couple_not_pending: "Пара уже не ждёт партнёра.",
  invite_expired: "Срок приглашения истёк. Попроси партнёра прислать новую ссылку.",
  invite_used: "Приглашение уже использовано или заменено новым. Попроси партнёра прислать новую ссылку.",
  conflict_retry: "Не получилось с первого раза. Попробуй ещё раз.",
  // Для путей 1.5 контракт 429 не описывает; это общий ограничитель частоты. Для чата (2.2) `rate_limited`
  // по контракту — исчерпан дневной лимит реплик: там нужен свой текст через переопределение операции.
  rate_limited: "Слишком много запросов подряд. Подожди немного и попробуй снова.",
  internal: "Что-то пошло не так. Попробуй ещё раз.",
  subscription_required: "Подписка закончилась. Контакты помощи доступны и без неё.",
  aborted: "Запрос остановлен.",
};

/**
 * Чат: тексты по коду события `error` и по сбою потока (ТЗ §10: понятное сообщение, реплика не теряется).
 * Сохранена ли реплика, экран говорит отдельно — по тому, что знает (200 пришёл или история её показала).
 */
const CHAT_SEND: Record<string, string> = {
  validation_error: "Сообщение пустое или длиннее 4000 символов — поправь и отправь снова.",
  conflict_retry: "Сообщение не сохранилось из-за сбоя. Отправь его ещё раз.",
  rate_limited: "На сегодня лимит сообщений агенту исчерпан.",
  // Под подписью любой ошибки `error` экран добавляет строку о помощи со 112 (АР-34 п.2): отказ может быть
  // репликой человека в кризисе, которую не распознали.
  refusal: "Агент не смог ответить на это сообщение. Можно повторить.",
  unavailable: "Агент сейчас недоступен. Попробуй ещё раз чуть позже.",
  quota: "Агент временно недоступен по техническим причинам. Попробуй позже.",
  bad_output: "Ответ агента пришёл с ошибкой. Попробуй ещё раз.",
  internal: "Что-то пошло не так, агент не ответил. Попробуй ещё раз.",
  bad_response: "Сервер ответил непонятно, агент не ответил. Попробуй ещё раз.",
  stream_broken: "Связь прервалась — ответ агента не дошёл.",
  stream_timeout: "Агент слишком долго молчит — соединение прервано.",
  aborted: "Ответ остановлен.",
  network: "Нет связи с сервером. Проверь интернет и попробуй ещё раз.",
  timeout: "Сервер не ответил вовремя. Проверь интернет и попробуй ещё раз.",
};

const BY_OP: Partial<Record<Op, Record<string, string>>> = {
  onboarding: { validation_error: "Проверь, что все поля заполнены — каждое не длиннее 64 символов." },
  invite: { not_found: INVITE_NOT_FOUND },
  acceptInvite: { not_found: INVITE_NOT_FOUND },
  // Ссылку выпускает сам инициатор — «попроси партнёра» здесь не к месту.
  reissueInvite: {
    not_found: "Пары пока нет. Создай новую ссылку-приглашение.",
    couple_not_pending: "Пара уже не ждёт партнёра — новая ссылка не нужна.",
  },
  createCouple: { couple_not_pending: "Пара уже не ждёт партнёра — новая ссылка не нужна." },
  chatHistory: {
    validation_error: "Не получилось загрузить более ранние сообщения. Попробуй ещё раз.",
    bad_response: "История пришла с ошибкой. Попробуй ещё раз чуть позже.",
  },
  chatSend: CHAT_SEND,
  memoryList: {
    bad_response: "Память пришла с ошибкой. Попробуй ещё раз чуть позже.",
    validation_error: "Не получилось загрузить память. Попробуй ещё раз.",
    internal: "Не получилось загрузить память: сбой на сервере. Попробуй ещё раз чуть позже.",
  },
  // Запись при этих ошибках не удалена — экран оставляет её на месте и предлагает повторить.
  memoryDelete: {
    conflict_retry: "Не получилось удалить, попробуй ещё раз.",
    internal: "Не получилось удалить: сбой на сервере. Запись на месте — попробуй ещё раз.",
    service_unavailable: "Не получилось удалить: сервис временно недоступен. Запись на месте — попробуй чуть позже.",
    network: "Нет связи с сервером — запись не удалена. Проверь интернет и попробуй ещё раз.",
    timeout: "Сервер не ответил вовремя — запись могла не удалиться. Попробуй ещё раз.",
    bad_response: "Сервер ответил непонятно — запись могла не удалиться. Попробуй ещё раз.",
  },
};

/** Срок ожидания из `Retry-After` по-человечески: секунды, минуты или часы (с округлением вверх). */
export function waitText(seconds: number): string {
  if (seconds < 60) return `${seconds} с.`;
  if (seconds < 3600) return `${Math.ceil(seconds / 60)} мин.`;
  return `${Math.ceil(seconds / 3600)} ч.`;
}

export function errorText(code: string, serverMessage?: string, retryAfter?: number, op?: Op): string {
  const base = (op && BY_OP[op]?.[code]) ?? BY_CODE[code] ?? serverMessage ?? BY_CODE.internal;
  if (code === "rate_limited" && retryAfter) return `${base.replace(/\.$/, "")} — через ${waitText(retryAfter)}`;
  return base;
}

/** Текст события `error` чата по коду; незнакомый код — как `internal` (контракт `ChatErrorCode`). */
export function chatErrorText(code: string): string {
  return CHAT_SEND[code] ?? CHAT_SEND.internal;
}

/** Дальше этого срока дата лимита пишется с годом, без «через N ч». */
const YEAR_MS = 366 * 86_400_000;

/**
 * Когда снова можно писать агенту после дневного лимита (429, `Retry-After`): время на часах устройства и
 * сколько ждать — «после 00:00 (через 3 ч.)», «завтра после 09:30 (через 12 ч.)». Срок дальше года — дата с
 * годом; срок за пределами дат (огромный `Retry-After`) — «позже», без «Invalid Date».
 */
export function limitUntilText(retryAfter: number, now: number = Date.now()): string {
  const at = new Date(now + retryAfter * 1000);
  if (!Number.isFinite(retryAfter) || retryAfter < 0 || Number.isNaN(at.getTime())) return "позже";
  const time = at.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  if (at.getTime() - now > YEAR_MS) {
    return `${at.toLocaleDateString("ru-RU", { day: "numeric", month: "long", year: "numeric" })} после ${time}`;
  }
  const today = new Date(now);
  const dayDiff = Math.round(
    (new Date(at.getFullYear(), at.getMonth(), at.getDate()).getTime() -
      new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime()) /
      86_400_000,
  );
  const day =
    dayDiff <= 0 ? "" : dayDiff === 1 ? "завтра " : `${at.toLocaleDateString("ru-RU", { day: "numeric", month: "long" })} `;
  return `${day}после ${time} (через ${waitText(retryAfter).replace(/\.$/, "")})`;
}
