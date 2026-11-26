import { useEffect, useId, useState } from "react";
import { ApiError, isRetryable } from "../api/client";
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
  // Черновик формы хранится в сессии: уход в «Помощь» (экран сам туда отправляет) и «Назад» его не стирают.
  const draft = app.onboardingDraft;
  const [name, setName] = useState(draft?.name ?? app.me.name ?? tg.firstName.slice(0, MAX));
  const [addressForm, setAddressForm] = useState(draft?.addressForm ?? app.me.address_form ?? "");
  const [partnerName, setPartnerName] = useState(
    draft?.partnerName ?? app.me.partner_name ?? pending?.inviterName?.slice(0, MAX) ?? "",
  );
  const [checks, setChecks] = useState<Record<Consent, boolean>>(
    draft?.checks ?? { consent_terms: false, consent_data: false, consent_sensitive: false, age_confirmed: false },
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /**
   * Онбординг сохранён, а принять приглашение не удалось — режим хранится в сессии (`pendingAccept.failure`
   * ставится только после успешного `PUT /me/onboarding`), поэтому переживает уход в «Помощь» и «Назад» (n-1).
   * Повтор только принимает приглашение (согласия второй раз не отправляются); `retry` — помогает ли повтор
   * (сеть, 5xx, 429), иначе остаётся одно действие — «Продолжить без приглашения».
   */
  const acceptFailed = pending?.failure ?? null;
  const shownError = error ?? (acceptFailed && !busy ? acceptFailed.text : null);

  const { setOnboardingDraft } = app;
  useEffect(() => {
    setOnboardingDraft({ name, addressForm, partnerName, checks });
  }, [name, addressForm, partnerName, checks, setOnboardingDraft]);

  const fields = [name, addressForm, partnerName].map((v) => v.trim());
  const fieldsOk = fields.every((v) => v.length > 0 && v.length <= MAX);
  const checksOk = CONSENTS.every((c) => checks[c.key]);
  const valid = fieldsOk && checksOk;

  const consentsOk = CONSENTS.every((c) => c.key === "age_confirmed" || checks[c.key]);
  // Подсказка про возраст — нейтральная: она сообщает правило, а не просит поставить отметку
  // (АР-14: форма ответа «мне нет 18» — за оператором).
  const todo = [!fieldsOk && "заполни все три поля", !consentsOk && "отметь все три согласия"].filter(Boolean);
  const hint = valid
    ? null
    : [todo.length ? `Чтобы продолжить, ${todo.join(" и ")}.` : "", checks.age_confirmed ? "" : "Сервис только для взрослых 18+."]
        .filter(Boolean)
        .join(" ");

  async function accept(saved: Me, token: string) {
    try {
      await app.api.acceptInvite(token);
      app.setPendingAccept(null);
      app.setOnboardingDraft(null);
      app.setMe({ ...saved, has_active_couple: true });
      tg.notify("success");
      app.reset({ name: "paired" });
    } catch (e) {
      setBusy(false);
      tg.notify("error");
      app.setPendingAccept({ token, inviterName: pending?.inviterName ?? null, failure: { text: message(e), retry: isRetryable(e) } });
    }
  }

  async function retryAccept() {
    if (!acceptFailed || !pending || busy) return;
    setBusy(true);
    setError(null);
    await accept(app.me, pending.token);
  }

  async function submit() {
    if (!valid || busy || acceptFailed) return;
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
        app.setOnboardingDraft(null);
        app.reset({ name: "underage" });
        return;
      }
      tg.notify("error");
      setError(message(e));
      return;
    }

    if (pending) {
      await accept(saved, pending.token);
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
    app.setOnboardingDraft(null);
    tg.notify("success");
    app.reset({ name: "invite" });
  }

  function continueWithoutInvite() {
    app.setPendingAccept(null);
    app.setOnboardingDraft(null);
    app.reset({ name: "invite" });
  }

  return (
    <section data-screen="onboarding">
      <Heading>Давай познакомимся</Heading>
      <p className="muted">Это нужно агенту, чтобы говорить с тобой по-человечески. Номер телефона не нужен.</p>
      {!acceptFailed && (
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
      )}
      <Notice>
        Сервис не оказывает психологических и медицинских услуг. Тексты правил и согласий готовит юрист — они
        появятся в разделе «Помощь» до запуска.
      </Notice>
      {pending && !acceptFailed && (
        <p className="small muted">После этого шага {pending.inviterName ? `ты и ${pending.inviterName}` : "вы"} станете парой.</p>
      )}
      {shownError && <Notice kind="error">{shownError}</Notice>}
      {acceptFailed && (
        <>
          <p className="small muted">Знакомство сохранено — заполнять заново не нужно.</p>
          <button type="button" className="btn btn--block btn--ghost" onClick={continueWithoutInvite}>
            Продолжить без приглашения
          </button>
          {acceptFailed.retry && (
            <MainButton text="Принять приглашение снова" progress={busy} onClick={() => void retryAccept()} />
          )}
        </>
      )}
      {!acceptFailed && hint && (
        <p className="main-hint" role="status">
          {hint}
        </p>
      )}
      {!acceptFailed && <MainButton text="Продолжить" disabled={!valid} progress={busy} onClick={() => void submit()} />}
    </section>
  );
}
