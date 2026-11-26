import { useId, useState } from "react";
import { ApiError } from "../api/client";
import { errorText } from "../api/messages";
import type { Me } from "../api/types";
import { useApp } from "../app/store";
import { MainButton } from "../telegram/MainButton";
import { useTelegram } from "../telegram/context";
import { Heading } from "../ui/Heading";
import { Notice } from "../ui/Notice";

const MAX = 64;

type Consent = "consent_terms" | "consent_data" | "consent_sensitive" | "age_confirmed";

const CONSENTS: { key: Consent; label: string }[] = [
  { key: "consent_terms", label: "Принимаю правила сервиса" },
  { key: "consent_data", label: "Даю согласие на обработку персональных данных" },
  { key: "consent_sensitive", label: "Даю отдельное согласие на обработку сведений о чувствах и отношениях" },
  { key: "age_confirmed", label: "Мне исполнилось 18 лет" },
];

function deviceTimezone(): string | undefined {
  try {
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    return tz && tz.length <= MAX ? tz : undefined;
  } catch {
    return undefined;
  }
}

function message(e: unknown): string {
  return e instanceof ApiError ? e.message : errorText("internal");
}

/**
 * Онбординг (ФТ-02, ТЗ §4.1 п.2): имя, обращение, имя партнёра, три согласия и 18+.
 * «Продолжить» недоступна, пока не заполнены поля и не отмечены все четыре пункта.
 */
export function Onboarding() {
  const app = useApp();
  const tg = useTelegram();
  const ids = useId();
  const pending = app.pendingAccept;
  const [name, setName] = useState(app.me.name ?? tg.firstName.slice(0, MAX));
  const [addressForm, setAddressForm] = useState(app.me.address_form ?? "");
  const [partnerName, setPartnerName] = useState(app.me.partner_name ?? pending?.inviterName?.slice(0, MAX) ?? "");
  const [checks, setChecks] = useState<Record<Consent, boolean>>({
    consent_terms: false,
    consent_data: false,
    consent_sensitive: false,
    age_confirmed: false,
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [acceptFailed, setAcceptFailed] = useState(false);

  const fields = [name, addressForm, partnerName].map((v) => v.trim());
  const fieldsOk = fields.every((v) => v.length > 0 && v.length <= MAX);
  const checksOk = CONSENTS.every((c) => checks[c.key]);
  const valid = fieldsOk && checksOk;

  const hint = valid
    ? null
    : !fieldsOk && !checksOk
      ? "Чтобы продолжить, заполни поля и отметь все четыре пункта."
      : !fieldsOk
        ? "Чтобы продолжить, заполни все три поля."
        : "Чтобы продолжить, отметь все четыре пункта.";

  async function submit() {
    if (!valid || busy) return;
    setBusy(true);
    setError(null);
    let saved: Me;
    try {
      saved = await app.api.saveOnboarding({
        name: fields[0],
        address_form: fields[1],
        partner_name: fields[2],
        timezone: deviceTimezone(),
        consent_terms: true,
        consent_data: true,
        consent_sensitive: true,
        age_confirmed: checks.age_confirmed,
      });
      app.setMe(saved);
    } catch (e) {
      setBusy(false);
      if (e instanceof ApiError && e.code === "underage") {
        app.reset({ name: "underage" });
        return;
      }
      tg.notify("error");
      setError(message(e));
      return;
    }

    if (pending) {
      try {
        await app.api.acceptInvite(pending.token);
        app.setPendingAccept(null);
        app.setMe({ ...saved, has_active_couple: true });
        tg.notify("success");
        app.reset({ name: "paired" });
      } catch (e) {
        setBusy(false);
        tg.notify("error");
        setError(message(e));
        setAcceptFailed(true);
      }
      return;
    }

    try {
      const invite = await app.api.createCouple();
      app.setInvite(invite);
      app.setShared(false);
      app.setInviteError(null);
    } catch (e) {
      // Пара уже есть (например, создана с другого устройства) — экран приглашения покажет её статус.
      app.setInviteError(e instanceof ApiError && e.code === "already_in_couple" ? null : message(e));
    }
    tg.notify("success");
    app.reset({ name: "invite" });
  }

  function continueWithoutInvite() {
    app.setPendingAccept(null);
    app.reset({ name: "invite" });
  }

  return (
    <section data-screen="onboarding">
      <Heading>Давай познакомимся</Heading>
      <p className="muted">Это нужно агенту, чтобы говорить с тобой по-человечески. Номер телефона не нужен.</p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
        noValidate
      >
        <div className="field">
          <label htmlFor={`${ids}-name`}>Твоё имя</label>
          <input
            className="input"
            id={`${ids}-name`}
            value={name}
            maxLength={MAX}
            autoComplete="off"
            required
            onChange={(e) => setName(e.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor={`${ids}-call`}>Как к тебе обращаться</label>
          <input
            className="input"
            id={`${ids}-call`}
            value={addressForm}
            maxLength={MAX}
            autoComplete="off"
            required
            placeholder="Например: Аня, на «ты»"
            onChange={(e) => setAddressForm(e.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor={`${ids}-partner`}>Имя партнёра</label>
          <input
            className="input"
            id={`${ids}-partner`}
            value={partnerName}
            maxLength={MAX}
            autoComplete="off"
            required
            onChange={(e) => setPartnerName(e.target.value)}
          />
        </div>
        <fieldset className="field consents">
          <legend className="label">Согласия</legend>
          {CONSENTS.map((c) => (
            <label key={c.key} className="check">
              <input
                type="checkbox"
                checked={checks[c.key]}
                onChange={(e) => setChecks((prev) => ({ ...prev, [c.key]: e.target.checked }))}
              />
              <span>{c.label}</span>
            </label>
          ))}
        </fieldset>
      </form>
      <Notice>
        Сервис не оказывает психологических и медицинских услуг. Тексты правил и согласий готовит юрист — они
        появятся в разделе «Помощь» до запуска.
      </Notice>
      {pending && !acceptFailed && (
        <p className="small muted">После этого шага {pending.inviterName ? `ты и ${pending.inviterName}` : "вы"} станете парой.</p>
      )}
      {error && <Notice kind="error">{error}</Notice>}
      {acceptFailed && (
        <button type="button" className="btn btn--block btn--ghost" onClick={continueWithoutInvite}>
          Продолжить без приглашения
        </button>
      )}
      {hint && (
        <p className="main-hint" role="status">
          {hint}
        </p>
      )}
      <MainButton text="Продолжить" disabled={!valid} progress={busy} onClick={() => void submit()} />
    </section>
  );
}
