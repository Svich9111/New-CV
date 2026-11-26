"""Страж роли платежей ``feelings_billing`` (АР-50 п.7; ревью 4.4 э.1 р.2 MAJOR-1, A8-4.4-D3).

Роли общие для кластера: ``feelings_billing`` может появиться до миграции или измениться после
неё (другая база того же кластера, эксперимент, ошибочный ``GRANT``). Граница «функции бота —
только бот» держится не на самой роли, а на том, **кто может ею стать**. Страж останавливает
миграцию (``RAISE EXCEPTION``, транзакция откатывается), если:

1. у роли есть вход, ``INHERIT`` или особые права (``SUPERUSER``/``CREATEROLE``/``CREATEDB``/
   ``REPLICATION``/``BYPASSRLS``) — как в 0019;
2. сама ``feelings_billing`` — член любой роли (её права расширились бы правами той роли);
3. ``feelings_app``/``feelings_api``/``feelings_worker``/``feelings_admin`` — члены
   ``feelings_billing`` в любом виде, в том числе через вложенные роли и только с ``ADMIN``
   (``pg_has_role(…, 'MEMBER')``): с ``ADMIN`` роль выдала бы членство себе;
4. стать ``feelings_billing`` (``SET``) или получить её права (``USAGE``), с учётом вложенности,
   может кто-то, кроме суперпользователей и роли входа бота ``feelings_billing_login``. Под это
   правило попадают и роли входа из ``infra/db/roles.sql`` (воркер, администратор), и владелец
   схемы, и любые посторонние роли. ``ADMIN`` без ``SET``/``INHERIT`` (так владелец схемы получает
   созданную им роль, BACKLOG Б-97) разрешён: это доверие владельцу схемы, как к любой групповой
   роли (A8 R3);
5. роль входа бота ``feelings_billing_login``, если она есть, — суперпользователь или член
   какой-либо роли, кроме ``feelings_billing`` (её член только бот, Б-97).

Роль входа бота отличается **по имени**: ``feelings_billing_login`` (фиксировано, как
``feelings_api``; переменной имени, как у ролей входа воркера и администратора, у неё нет —
BACKLOG Б-97). Суперпользователи проходят любую проверку членства (так устроен PostgreSQL) и
доверены по определению.

Страж исполняет миграция 0020 и ``migrations/env.py`` при **каждом** запуске Alembic (до
миграций, в той же транзакции): испорченная после выкладки роль остановит следующий
``alembic upgrade`` на стенде. Роли нет — проверять нечего (миграции до 0019)."""

BILLING_ROLE = "feelings_billing"
BILLING_LOGIN = "feelings_billing_login"
APP_ROLES = ("feelings_app", "feelings_api", "feelings_worker", "feelings_admin")

ATTRS_MESSAGE = "роль feelings_billing уже есть и имеет вход, INHERIT или особые права"

GUARD_SQL = r"""
DO $guard$
DECLARE
    v_billing oid := to_regrole('feelings_billing');
    v_login oid := to_regrole('feelings_billing_login');
    v_bad text;
BEGIN
    IF v_billing IS NULL THEN
        RETURN;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles r WHERE r.oid = v_billing
               AND (r.rolcanlogin OR r.rolinherit OR r.rolsuper OR r.rolcreaterole
                    OR r.rolcreatedb OR r.rolreplication OR r.rolbypassrls)) THEN
        RAISE EXCEPTION 'роль feelings_billing уже есть и имеет вход, INHERIT или особые права';
    END IF;
    SELECT string_agg(g.rolname, ', ' ORDER BY g.rolname) INTO v_bad
      FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid
     WHERE m.member = v_billing;
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION 'роль feelings_billing — член других ролей: %', v_bad;
    END IF;
    SELECT string_agg(r.rolname, ', ' ORDER BY r.rolname) INTO v_bad
      FROM pg_roles r
     WHERE r.rolname IN ('feelings_app', 'feelings_api', 'feelings_worker', 'feelings_admin')
       AND NOT r.rolsuper AND pg_has_role(r.oid, v_billing, 'MEMBER');
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION 'в роль feelings_billing входят роли приложения: %', v_bad;
    END IF;
    SELECT string_agg(r.rolname, ', ' ORDER BY r.rolname) INTO v_bad
      FROM pg_roles r
     WHERE r.oid <> v_billing AND r.oid IS DISTINCT FROM v_login AND NOT r.rolsuper
       AND (pg_has_role(r.oid, v_billing, 'SET') OR pg_has_role(r.oid, v_billing, 'USAGE'));
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION 'стать feelings_billing может не только роль входа бота: %', v_bad;
    END IF;
    IF v_login IS NOT NULL THEN
        SELECT string_agg(g.rolname, ', ' ORDER BY g.rolname) INTO v_bad
          FROM pg_roles g
         WHERE g.oid NOT IN (v_login, v_billing)
           AND ((SELECT l.rolsuper FROM pg_roles l WHERE l.oid = v_login)
                OR pg_has_role(v_login, g.oid, 'MEMBER'));
        IF v_bad IS NOT NULL THEN
            RAISE EXCEPTION 'роль входа бота feelings_billing_login входит в другие роли: %', v_bad;
        END IF;
    END IF;
END $guard$
"""
