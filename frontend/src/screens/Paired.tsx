import { useEffect, useState } from "react";
import { useApp } from "../app/store";
import { MainButton } from "../telegram/MainButton";
import { Heading } from "../ui/Heading";
import { Heart } from "../ui/Notice";

/**
 * Главный экран пары (ТЗ §8 «Главный»): статус пары и вход в чат с агентом (2.2). Непрочитанные сообщения
 * партнёра — задача 3.4.
 * Вход в «Что агент помнит» (2.5) — здесь, пока нет экрана «Профиль и пара».
 */
export function Paired() {
  const { api, me, push } = useApp();
  const [partner, setPartner] = useState<string | null>(me.partner_name ?? null);

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
      <button type="button" className="card card--link" onClick={() => push({ name: "memory" })}>
        <span className="card__title">Что агент помнит</span>
        <span className="small muted">Записи, которые агент сохранил о тебе. Любую можно удалить. Партнёр их не видит.</span>
      </button>
      <MainButton text="Поговорить с агентом" onClick={() => push({ name: "chat" })} />
    </section>
  );
}
