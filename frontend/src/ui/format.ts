/** «28 ноября» — срок приглашения (дата в часовом поясе устройства). */
export function formatDay(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString("ru-RU", { day: "numeric", month: "long" });
}

/** «26 ноября, 19:42» — время сообщения в часовом поясе устройства; неверная дата — пустая строка. */
export function formatMoment(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const day = d.toLocaleDateString("ru-RU", { day: "numeric", month: "long" });
  const time = d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  return `${day}, ${time}`;
}
