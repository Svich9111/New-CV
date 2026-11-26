import { useEffect, useRef, useState, type FormEvent } from "react";
import { Notice } from "../ui/Notice";
import { isValidToken } from "./api";
import { adminErrorText } from "./texts";

/**
 * Вход администратора: поле «Токен администратора». Токен уходит только в `onSubmit` (дальше — в памяти клиента);
 * у поля нет атрибута `name`, у формы нет `action`, так что даже без обработчика токен не попадёт ни в адрес, ни
 * в отправку формы. Автозаполнение выключено. Токен с символами, недопустимыми в заголовке (кириллица, пробел или
 * перевод строки внутри), отклоняется здесь же, без запроса: иначе браузер не отправит его и покажет «нет связи».
 */
export function Login({ notice, onSubmit }: { notice: { kind: "info" | "error"; text: string } | null; onSubmit(token: string): void }) {
  const [value, setValue] = useState("");
  const [problem, setProblem] = useState<"empty" | "chars" | null>(null);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => input.current?.focus(), []);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const token = value.trim();
    if (!token || !isValidToken(token)) {
      setProblem(token ? "chars" : "empty");
      if (token) setValue("");
      input.current?.focus();
      return;
    }
    setValue("");
    onSubmit(token);
  };

  return (
    <main className="admin-login">
      <h1>Админ-панель «Чувства AI»</h1>
      <p className="muted">
        Вход по токену администратора. Токен хранится только в памяти этой вкладки: после перезагрузки или закрытия
        вкладки его нужно ввести снова.
      </p>
      {problem === "chars" ? (
        <Notice kind="error">{adminErrorText("unauthorized", "list")}</Notice>
      ) : (
        notice && <Notice kind={notice.kind}>{notice.text}</Notice>
      )}
      <form onSubmit={submit} noValidate autoComplete="off">
        <div className="field">
          <label htmlFor="admin-token">Токен администратора</label>
          <input
            ref={input}
            id="admin-token"
            className="input"
            type="password"
            autoComplete="off"
            autoCapitalize="off"
            spellCheck={false}
            value={value}
            aria-invalid={problem ? true : undefined}
            aria-describedby={problem ? "admin-token-error" : undefined}
            onChange={(e) => {
              setValue(e.target.value);
              if (problem === "empty") setProblem(null);
            }}
          />
          {problem && (
            <p id="admin-token-error" className="admin-field-error small">
              <span aria-hidden="true">! </span>
              {problem === "empty"
                ? "Введите токен."
                : "Токен содержит недопустимые символы: только латинские буквы, цифры и знаки, без пробелов и кириллицы."}
            </p>
          )}
        </div>
        <button type="submit" className="admin-primary">
          Войти
        </button>
      </form>
    </main>
  );
}
