import { useEffect, useState } from "react";
import { useApp } from "../app/store";
import { FEED_PAGE } from "./Messages";
import { MainButton } from "../telegram/MainButton";
import { Heading } from "../ui/Heading";
import { Heart } from "../ui/Notice";

/** «1 непрочитанное», «3 непрочитанных», «нет новых» — число словами, не только кружком (ТЗ §8). */
export function unreadText(n: number): string {
  if (n === 0) return "Новых сообщений нет";
  const mod10 = n % 10;
  const mod100 = n % 100;
  const word = mod10 === 1 && mod100 !== 11 ? "непрочитанное" : "непрочитанных";
  return `${n} ${word}`;
}

type Unread = { kind: "loading" } | { kind: "ready"; count: number; more: boolean } | { kind: "error" };

/**
 * Главный экран пары (ТЗ §8 «Главный»): статус пары, вход в чат с агентом (2.2) и в ленту «Сообщения пары» с числом
 * непрочитанных полученных (3.4) — по первой странице ленты: полученные со статусом `delivered`.
 * Вход в «Что агент помнит» (2.5) — здесь, пока нет экрана «Профиль и пара».
 */
export function Paired() {
  const { api, me, push } = useApp();
  const [partner, setPartner] = useState<string | null>(me.partner_name ?? null);
  const [unread, setUnread] = useState<Unread>({ kind: "loading" });

  useEffect(() => {
    let alive = true;
    api.coupleMessages({ limit: FEED_PAGE }).then(
      (p) => {
        if (!alive) return;
        const count = p.items.filter((m) => m.direction === "incoming" && m.status === "delivered").length;
        // Все сообщения первой страницы — новые, а дальше есть ещё: «30 и больше».
        setUnread({ kind: "ready", count, more: count === p.items.length && !!p.next_cursor });
      },
      () => alive && setUnread({ kind: "error" }),
    );
    return () => {
      alive = false;
    };
  }, [api]);

  useEffect(() => {
    let alive = true;
    api.couple().then(
      (c) => {
        const other = c?.members.find((m) => !m.is_me);
        if (alive && other?.name) setPartner(other.name);
      },
      () => undefined,
    );
    return () => {
      alive = false;
    };
  }, [api]);

  return (
    <section data-screen="paired">
      <Heading>Вы пара</Heading>
      <div className="card card--soft">
        <div className="status" data-status="active" role="status">
          <span className="status__mark" aria-hidden="true">
            <Heart />
          </span>
          <span>
            <b>Пара создана.</b> {partner ? `Ты и ${partner} теперь на связи.` : "Вы теперь на связи."}
          </span>
        </div>
      </div>
      <p className="small">
        Ваши личные разговоры с агентами друг другу не видны. Между вами передаются только сообщения, которые каждый
        сам отправит кнопкой «Отправить».
      </p>
      <button type="button" className="card card--link" onClick={() => push({ name: "chat" })}>
        <span className="card__title">Поговорить с агентом</span>
        <span className="small muted">
          Выговориться, разобраться в чувствах, подготовить сообщение. Этот разговор видишь только ты.
        </span>
      </button>
      <button type="button" className="card card--link" data-unread={unread.kind === "ready" ? unread.count : undefined} onClick={() => push({ name: "messages" })}>
        <span className="card__title">Сообщения пары</span>
        <span className="small" data-unread-text>
          {unread.kind === "loading" && <span className="muted">Проверяем новые сообщения…</span>}
          {unread.kind === "error" && <span className="muted">Не получилось проверить новые сообщения. Открой ленту.</span>}
          {unread.kind === "ready" && (
            <span className={unread.count > 0 ? "unread" : "muted"}>
              {unread.count > 0 && <Heart />} {unreadText(unread.count)}
              {unread.more ? " и больше" : ""}
            </span>
          )}
        </span>
      </button>
      <button type="button" className="card card--link" onClick={() => push({ name: "memory" })}>
        <span className="card__title">Что агент помнит</span>
        <span className="small muted">Записи, которые агент сохранил о тебе. Любую можно удалить. Партнёр их не видит.</span>
      </button>
      <MainButton text="Поговорить с агентом" onClick={() => push({ name: "chat" })} />
    </section>
  );
}
