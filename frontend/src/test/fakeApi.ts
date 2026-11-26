import type { ApiErrorBody, Couple, HelpContact, InviteCreated, InvitePreview, Me, OnboardingRequest, Session } from "../api/types";
import { INIT_DATA } from "./fakeTelegram";

/**
 * Заглушка сервера по контракту `api/openapi.yaml` (пути, коды, схемы — через сгенерированные типы).
 * Один пользователь «я» и выдуманный партнёр; данные выдуманные.
 */

export interface Logged {
  method: string;
  path: string;
  auth: string | null;
  body: unknown;
}

interface Injected {
  status: number;
  body?: unknown;
  headers?: Record<string, string>;
  times: number;
}

interface InviteRow {
  inviter_name: string | null;
  expires_at: string;
  status: "active" | "used" | "expired" | "revoked";
  own: boolean;
}

export const TOKEN_A = "A".repeat(43);
export const TOKEN_B = "B".repeat(43);
export const PARTNER_TOKEN = "Pk3_xY-".padEnd(43, "q");
const EXPIRES = "2026-12-03T12:00:00Z";

export const CONTACTS: HelpContact[] = [
  {
    name: "Единый номер экстренных служб",
    phone: "112",
    description: "Если есть угроза жизни или здоровью.",
    round_the_clock: true,
  },
];

function err(code: string, message: string): ApiErrorBody {
  return { code, message };
}

export class FakeServer {
  me: Me = {
    id: "00000000-0000-4000-8000-000000000001",
    name: null,
    address_form: null,
    partner_name: null,
    timezone: "Europe/Moscow",
    onboarding_completed: false,
    has_active_couple: false,
  };
  couple: Couple | null = null;
  invites = new Map<string, InviteRow>();
  log: Logged[] = [];
  token: string | null = null;
  private tokenSeq = 0;
  private inviteSeq = 0;
  private injected = new Map<string, Injected>();

  /** Пользователь уже прошёл онбординг. */
  onboarded(): this {
    Object.assign(this.me, { name: "Аня", address_form: "Аня", partner_name: "Игорь", onboarding_completed: true });
    return this;
  }

  /** Входящее приглашение от партнёра. */
  partnerInvite(status: InviteRow["status"] = "active", inviter: string | null = "Игорь"): this {
    this.invites.set(PARTNER_TOKEN, { inviter_name: inviter, expires_at: EXPIRES, status, own: false });
    return this;
  }

  /** Партнёр принял моё приглашение (сделал это на своём телефоне). */
  partnerAccepts(): void {
    if (!this.couple) throw new Error("no couple");
    this.couple = {
      ...this.couple,
      status: "active",
      members: [...this.couple.members, { role: "partner", name: "Игорь", is_me: false }],
    };
    this.me.has_active_couple = true;
  }

  pendingCouple(): this {
    this.couple = { id: "c0000000-0000-4000-8000-000000000001", status: "pending", members: [{ role: "initiator", name: this.me.name ?? null, is_me: true }] };
    return this;
  }

  /** Следующие `times` запросов `method path` получат этот ответ. */
  inject(method: string, path: string, status: number, body?: unknown, headers?: Record<string, string>, times = 1): this {
    this.injected.set(`${method} ${path}`, { status, body, headers, times });
    return this;
  }

  expireSession(): void {
    this.token = null;
  }

  calls(method: string, path: string): Logged[] {
    return this.log.filter((l) => l.method === method && l.path === path);
  }

  fetch = async (input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> => {
    const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url, "http://app.test");
    const method = (init.method ?? "GET").toUpperCase();
    const path = url.pathname.replace(/^\/api\/v1/, "");
    const headers = new Headers(init.headers);
    const body = typeof init.body === "string" ? (JSON.parse(init.body) as unknown) : undefined;
    this.log.push({ method, path, auth: headers.get("Authorization"), body });

    const inj = this.injected.get(`${method} ${path}`);
    if (inj) {
      if (--inj.times <= 0) this.injected.delete(`${method} ${path}`);
      return json(inj.status, inj.body, inj.headers);
    }
    return this.route(method, path, headers.get("Authorization"), body);
  };

  private route(method: string, path: string, auth: string | null, body: unknown): Response {
    if (method === "POST" && path === "/auth/telegram") {
      const b = body as { init_data?: unknown };
      if (b?.init_data !== INIT_DATA) return json(401, err("invalid_init_data", "Не удалось подтвердить вход через Telegram"));
      this.token = `test-session-${++this.tokenSeq}`;
      const s: Session = { access_token: this.token, expires_in: 3600, me: { ...this.me } };
      return json(200, s);
    }
    if (method === "GET" && path === "/help/contacts") return json(200, CONTACTS);

    if (!this.token || auth !== `Bearer ${this.token}`) return json(401, err("unauthorized", "Нет сессии"));

    if (method === "GET" && path === "/me") return json(200, { ...this.me });
    if (method === "PUT" && path === "/me/onboarding") return this.onboarding(body as OnboardingRequest);

    const inv = path.match(/^\/invites\/([^/]+)(\/accept)?$/);
    if (inv && method === "GET" && !inv[2]) return this.preview(decodeURIComponent(inv[1]));
    if (inv && method === "POST" && inv[2]) {
      if (!this.me.onboarding_completed) return json(403, err("onboarding_required", "Сначала завершите знакомство с сервисом"));
      return this.accept(decodeURIComponent(inv[1]));
    }

    if (!this.me.onboarding_completed) return json(403, err("onboarding_required", "Сначала завершите знакомство с сервисом"));
    if (method === "GET" && path === "/couple") return this.couple ? json(200, this.couple) : json(404, err("not_found", "Не найдено"));
    if (method === "POST" && path === "/couple") {
      if (this.couple) return json(409, err("already_in_couple", "У вас уже есть пара"));
      this.pendingCouple();
      return json(201, this.issue());
    }
    if (method === "POST" && path === "/couple/invite") {
      if (!this.couple) return json(404, err("not_found", "Не найдено"));
      if (this.couple.status !== "pending") return json(409, err("couple_not_pending", "Пара уже не ожидает партнёра"));
      return json(201, this.issue());
    }
    return json(404, err("not_found", "Не найдено"));
  }

  private onboarding(b: OnboardingRequest): Response {
    const okText = (v: unknown) => typeof v === "string" && v.length >= 1 && v.length <= 64;
    if (!okText(b.name) || !okText(b.address_form) || !okText(b.partner_name)) return json(422, err("validation_error", "Проверьте данные"));
    if (b.consent_terms !== true || b.consent_data !== true || b.consent_sensitive !== true) return json(422, err("validation_error", "Проверьте данные"));
    if (b.age_confirmed !== true) {
      this.me.onboarding_completed = false;
      return json(403, err("underage", "Сервис доступен только с 18 лет"));
    }
    Object.assign(this.me, { name: b.name, address_form: b.address_form, partner_name: b.partner_name, onboarding_completed: true });
    if (b.timezone) this.me.timezone = b.timezone;
    return json(200, { ...this.me });
  }

  private issue(): InviteCreated {
    for (const row of this.invites.values()) if (row.own && row.status === "active") row.status = "revoked";
    const token = ++this.inviteSeq === 1 ? TOKEN_A : TOKEN_B;
    this.invites.set(token, { inviter_name: this.me.name ?? null, expires_at: EXPIRES, status: "active", own: true });
    return { couple_id: this.couple!.id, link: `https://t.me/test_bot?startapp=${token}`, expires_at: EXPIRES };
  }

  private preview(token: string): Response {
    const row = this.invites.get(token);
    if (!row) return json(404, err("not_found", "Не найдено"));
    if (row.status === "expired") return json(410, err("invite_expired", "Срок приглашения истёк — попросите новую ссылку"));
    if (row.status !== "active") return json(410, err("invite_used", "Приглашение уже использовано — попросите новую ссылку"));
    const p: InvitePreview = { inviter_name: row.inviter_name, expires_at: row.expires_at };
    return json(200, p);
  }

  private accept(token: string): Response {
    const row = this.invites.get(token);
    if (!row) return json(404, err("not_found", "Не найдено"));
    if (row.status === "expired") return json(410, err("invite_expired", "Срок приглашения истёк — попросите новую ссылку"));
    if (row.status !== "active") return json(410, err("invite_used", "Приглашение уже использовано — попросите новую ссылку"));
    if (row.own) return json(409, err("own_invite", "Это ваше приглашение — отправьте ссылку партнёру"));
    if (this.couple) return json(409, err("already_in_couple", "У вас уже есть пара"));
    row.status = "used";
    this.couple = {
      id: "c0000000-0000-4000-8000-000000000002",
      status: "active",
      members: [
        { role: "initiator", name: row.inviter_name, is_me: false },
        { role: "partner", name: this.me.name ?? null, is_me: true },
      ],
    };
    this.me.has_active_couple = true;
    return json(200, this.couple);
  }
}

export function json(status: number, body?: unknown, headers: Record<string, string> = {}): Response {
  if (status === 204 || body === undefined) return new Response(null, { status, headers });
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });
}
