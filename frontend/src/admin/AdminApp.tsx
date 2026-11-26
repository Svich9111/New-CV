import { useCallback, useState } from "react";
import { AdminApi } from "./api";
import { KbScreen } from "./KbScreen";
import { Login } from "./Login";
import { adminErrorText } from "./texts";

export interface AdminAppProps {
  fetch?: typeof fetch;
  baseUrl?: string;
  pollMs?: number;
}

type LoginNotice = { kind: "info" | "error"; text: string } | null;

/**
 * Админ-панель (ФТ-20, АР-52): отдельная страница `admin.html`, не маршрут Mini App. Сейчас — только база знаний;
 * версии промптов и метрики ждут решений оператора (АР-53, АР-54). Экранов с текстами переписок, памяти,
 * черновиков и личного чата нет и не будет (ТЗ §9.1 п.5).
 *
 * Токен живёт только в объекте клиента в состоянии React: «Выйти», 401 и 403 его забывают (клиент выбрасывается).
 */
export function AdminApp({ fetch, baseUrl, pollMs }: AdminAppProps) {
  const [api, setApi] = useState<AdminApi | null>(null);
  const [notice, setNotice] = useState<LoginNotice>(null);

  const onAuthError = useCallback((code: string) => {
    setApi(null);
    setNotice({ kind: "error", text: adminErrorText(code === "admin_only" ? "admin_only" : code === "forbidden" ? "forbidden" : "unauthorized", "list") });
  }, []);

  if (!api) {
    return (
      <Login
        notice={notice}
        onSubmit={(token) => {
          setNotice(null);
          setApi(new AdminApi({ token, fetch, baseUrl }));
        }}
      />
    );
  }

  return (
    <div className="admin">
      <header className="admin-header">
        <div>
          <p className="admin-header__name">Админ-панель «Чувства AI»</p>
          <h1>База знаний</h1>
        </div>
        <button
          type="button"
          className="btn btn--ghost"
          onClick={() => {
            setApi(null);
            setNotice({ kind: "info", text: "Вы вышли. Токен забыт — для входа введите его снова." });
          }}
        >
          Выйти
        </button>
      </header>
      <p className="muted small">
        В панели только справочные материалы. Текстов переписок, памяти агентов и личных чатов здесь нет — у
        администратора нет к ним доступа (ТЗ §9.1). Версии промптов и метрики появятся после решений оператора.
      </p>
      <main>
        <KbScreen api={api} onAuthError={onAuthError} pollMs={pollMs} />
      </main>
    </div>
  );
}
