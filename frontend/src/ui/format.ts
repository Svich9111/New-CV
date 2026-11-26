/** «28 ноября» — срок приглашения (дата в часовом поясе устройства). */
export function formatDay(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString("ru-RU", { day: "numeric", month: "long" });
}
