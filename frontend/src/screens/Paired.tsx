import { useEffect, useState } from "react";
import { useApp } from "../app/store";
import { Heading } from "../ui/Heading";
import { Heart } from "../ui/Notice";

/** Пара создана (ФТ-03). Главный экран с чатом — следующие задачи (2.2); здесь — статус пары. */
export function Paired() {
  const { api, me } = useApp();
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
      <p className="small muted">Скоро здесь появится чат с твоим агентом.</p>
    </section>
  );
}
