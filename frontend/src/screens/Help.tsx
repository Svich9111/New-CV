import { useEffect, useState } from "react";
import type { Api } from "../api/client";
import type { HelpContact } from "../api/types";
import { Heading } from "../ui/Heading";
import { Loading, Notice } from "../ui/Notice";

/**
 * Запасной контакт, если список с сервера не загрузился: помощь нужна «всегда» (ТЗ §9.3), даже без
 * связи с сервером. Совпадает с непроверенным списком сервера (АР-33): только 112.
 */
const FALLBACK: HelpContact[] = [
  {
    name: "Единый номер экстренных служб",
    phone: "112",
    description: "Если есть угроза жизни или здоровью — тебе или кому-то рядом. Звонок бесплатный.",
    round_the_clock: true,
  },
];

function tel(phone: string): string {
  return "tel:" + phone.replace(/[^\d+]/g, "");
}

/** Помощь (ТЗ §8, §9.3, §9.4): контакты служб, правила, документы. Доступна без входа и без подписки. */
export function Help({ api }: { api: Api }) {
  const [contacts, setContacts] = useState<HelpContact[] | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    api.helpContacts().then(
      (list) => alive && setContacts(list.length ? list : FALLBACK),
      () => {
        if (!alive) return;
        setFailed(true);
        setContacts(FALLBACK);
      },
    );
    return () => {
      alive = false;
    };
  }, [api]);

  return (
    <section data-screen="help">
      <Heading>Помощь</Heading>
      <div className="card card--soft">
        <b>«Чувства AI» не заменяет психолога.</b>
        <p className="small">
          Сервис не оказывает психологических и медицинских услуг и не ставит диагнозов. Если тяжело справляться —
          обратись к специалисту.
        </p>
      </div>

      <h2>Если тебе или кому-то рядом опасно</h2>
      {contacts === null && <Loading>Загружаем контакты…</Loading>}
      {failed && <Notice>Не получилось загрузить полный список. Вот номер, по которому помогут всегда.</Notice>}
      {contacts && (
        <ul className="contacts">
          {contacts.map((c) => (
            <li key={c.name + c.phone} className="contact">
              <div>{c.name}</div>
              <a className="contact__phone" href={tel(c.phone)}>
                {c.phone}
              </a>
              <div className="small muted">
                {c.description}
                {c.round_the_clock ? " Круглосуточно." : ""}
              </div>
            </li>
          ))}
        </ul>
      )}
      <p className="small muted">Контакты помощи доступны всегда, даже без подписки.</p>

      <h2>Правила</h2>
      <ul className="small rules">
        <li>Агент помогает сказать о чувствах — он не адвокат и не судья.</li>
        <li>Ничего не уходит партнёру без твоего «Отправить».</li>
        <li>Агент партнёра может смягчить форму, но не смысл; оригинал виден по запросу.</li>
        <li>Твой разговор с агентом не видят ни партнёр, ни его агент.</li>
        <li>Ты видишь, что агент о тебе помнит, и можешь удалить любую запись.</li>
        <li>При угрозах и насилии агент ничего не смягчает и подскажет, куда обратиться.</li>
      </ul>

      <h2>Документы</h2>
      <p className="small muted">
        Пользовательское соглашение, политика обработки данных и согласие на обработку чувствительных сведений
        готовятся юристом и появятся здесь до запуска.
      </p>
    </section>
  );
}
