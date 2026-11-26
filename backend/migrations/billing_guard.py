"""Страж роли платежей ``feelings_billing`` (АР-50 п.7; ревью 4.4 э.1 р.2 MAJOR-1, A8-4.4-D3;
р.3 N1, A8-4.4-D4, R5, R6, R8).

Роли общие для кластера: ``feelings_billing`` может появиться до миграции или измениться после
неё (другая база того же кластера, эксперимент, ошибочный ``GRANT``). Граница «функции бота —
только бот» держится не на самой роли, а на том, **кто может ею стать** и **кто может это
выдать**. Страж останавливает миграцию (``RAISE EXCEPTION``, транзакция откатывается), если:

1. у роли есть вход, ``INHERIT`` или особые права (``SUPERUSER``/``CREATEROLE``/``CREATEDB``/
   ``REPLICATION``/``BYPASSRLS``) — как в 0019;
2. сама ``feelings_billing`` — член любой роли (её права расширились бы правами той роли);
3. ``feelings_app``/``feelings_api``/``feelings_worker``/``feelings_admin`` — члены
   ``feelings_billing`` в любом виде, в том числе через вложенные роли и только с ``ADMIN``
   (``pg_has_role(…, 'MEMBER')``): с ``ADMIN`` роль выдала бы членство себе;
4. стать ``feelings_billing`` (``SET``) или получить её права (``USAGE``), с учётом вложенности,
   может кто-то, кроме суперпользователей и роли входа бота ``feelings_billing_login`` (под это
   правило попадают и роли входа из ``infra/db/roles.sql``, и владелец схемы, и посторонние);
5. роль входа бота ``feelings_billing_login``, если она есть, — суперпользователь, член
   какой-либо роли, кроме ``feelings_billing`` (её член только бот, Б-97), или имеет ``INHERIT``/
   ``CREATEROLE``/``CREATEDB``/``REPLICATION``/``BYPASSRLS`` (Б-97: ``LOGIN NOINHERIT``; R5);
6. у ``feelings_billing`` в текущей базе есть права или объекты сверх ожидаемых (R6): ожидаются
   только ``EXECUTE`` на четыре функции бота ``app_bot_payment_*`` и ``USAGE`` на схему
   ``public`` (без права передачи) и, при отзыве ``CONNECT`` у ``PUBLIC``, ``CONNECT`` к базе.
   Любые права на таблицы, столбцы, последовательности, другие функции, схемы, типы, языки,
   внешние серверы, большие объекты, параметры сервера, права по умолчанию, а также владение
   объектами — остановка (п.7 «без прав на таблицы»);
7. **ADMIN** (N1, A8-4.4-D4): членство в ``feelings_billing`` или в ``feelings_billing_login``
   в любом виде, в том числе только с ``ADMIN`` и унаследованное от группы, есть у кого-то,
   кроме суперпользователей, самой роли входа бота (в ``feelings_billing``), владельца схемы
   ``public`` (``nspowner``; если это ``pg_database_owner`` — владелец базы ``datdba``) и роли,
   исполняющей миграции (``current_user``). Так владелец получает созданную им роль (``ADMIN``
   без ``SET``/``INHERIT``, Б-97); владелец с ``SET``/``INHERIT`` по-прежнему остановлен правилом 4;
8. у роли входа бота есть ``ADMIN OPTION`` на ``feelings_billing`` (бот выдал бы роль платежей
   сессии пользователя).

Правила 1–6 — ``GUARD_SQL`` (кто **уже** может действовать как ``feelings_billing`` и что ей
доступно), правила 7–8 — ``ADMIN_GUARD_SQL`` (кто может это изменить без суперпользователя).
Миграции исполняют оба одним блоком — ``MIGRATION_GUARD_SQL``.

Роль входа бота отличается **по имени**: ``feelings_billing_login`` (фиксировано, как
``feelings_api``; переменной имени, как у ролей входа воркера и администратора, у неё нет —
BACKLOG Б-97). Суперпользователи проходят любую проверку членства (так устроен PostgreSQL) и
доверены по определению.

Страж исполняет миграция 0020 и ``migrations/env.py`` при **каждом** запуске Alembic — и
``upgrade``, и ``downgrade`` (до миграций, в той же транзакции): испорченная после выкладки роль
остановит следующий запуск на стенде, в том числе аварийный откат (порядок действий —
``docs/ADMIN.md``). Роли нет — проверять нечего (миграции до 0019)."""

BILLING_ROLE = "feelings_billing"
BILLING_LOGIN = "feelings_billing_login"
APP_ROLES = ("feelings_app", "feelings_api", "feelings_worker", "feelings_admin")
BOT_FUNCTIONS = (
    "app_bot_payment_precheck",
    "app_bot_payment_credit",
    "app_bot_payment_refunded",
    "app_bot_payment_lookup",
)

ATTRS_MESSAGE = "роль feelings_billing уже есть и имеет вход, INHERIT или особые права"

_HEAD = r"""
DO $guard$
DECLARE
    v_billing oid := to_regrole('feelings_billing');
    v_login oid := to_regrole('feelings_billing_login');
    v_owner oid;
    v_runner oid := (SELECT r.oid FROM pg_roles r WHERE r.rolname = current_user);
    v_bad text;
BEGIN
    IF v_billing IS NULL THEN
        RETURN;
    END IF;
    -- Владелец схемы public; в PG ≥ 15 это pg_database_owner — тогда владелец базы.
    SELECT CASE WHEN n.nspowner = to_regrole('pg_database_owner') THEN d.datdba
                ELSE n.nspowner END INTO v_owner
      FROM pg_namespace n, pg_database d
     WHERE n.nspname = 'public' AND d.datname = current_database();
"""

_TAIL = r"""
END $guard$
"""

_MEMBERSHIP = r"""
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
        IF (SELECT l.rolsuper FROM pg_roles l WHERE l.oid = v_login) THEN
            RAISE EXCEPTION 'роль входа бота feelings_billing_login входит в другие роли: '
                'она суперпользователь (входит во все роли)';
        END IF;
        SELECT string_agg(g.rolname, ', ' ORDER BY g.rolname) INTO v_bad
          FROM pg_roles g
         WHERE g.oid NOT IN (v_login, v_billing) AND pg_has_role(v_login, g.oid, 'MEMBER');
        IF v_bad IS NOT NULL THEN
            RAISE EXCEPTION 'роль входа бота feelings_billing_login входит в другие роли: %', v_bad;
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles l WHERE l.oid = v_login
                   AND (l.rolinherit OR l.rolcreaterole OR l.rolcreatedb OR l.rolreplication
                        OR l.rolbypassrls)) THEN
            RAISE EXCEPTION 'роль входа бота feelings_billing_login имеет INHERIT или особые права '
                '(CREATEROLE/CREATEDB/REPLICATION/BYPASSRLS)';
        END IF;
    END IF;
"""

# Права и объекты feelings_billing в текущей базе сверх ожидаемых (R6). aclexplode(NULL) пуст,
# права PUBLIC (grantee = 0) не относятся к роли и не проверяются.
_GRANTS = r"""
    SELECT string_agg(x, '; ' ORDER BY x) INTO v_bad FROM (
        SELECT format('%s ON %s', a.privilege_type, c.oid::regclass)
          FROM pg_class c, aclexplode(c.relacl) a WHERE a.grantee = v_billing
        UNION ALL
        SELECT format('%s (%I) ON %s', a.privilege_type, t.attname, c.oid::regclass)
          FROM pg_attribute t JOIN pg_class c ON c.oid = t.attrelid, aclexplode(t.attacl) a
         WHERE a.grantee = v_billing
        UNION ALL
        SELECT format('OWNER OF %s', c.oid::regclass) FROM pg_class c WHERE c.relowner = v_billing
        UNION ALL
        SELECT format('%s ON FUNCTION %s', a.privilege_type, p.oid::regprocedure)
          FROM pg_proc p, aclexplode(p.proacl) a
         WHERE a.grantee = v_billing
           AND NOT (a.privilege_type = 'EXECUTE' AND NOT a.is_grantable
                    AND p.pronamespace = 'public'::regnamespace
                    AND p.proname IN ('app_bot_payment_precheck', 'app_bot_payment_credit',
                                      'app_bot_payment_refunded', 'app_bot_payment_lookup'))
        UNION ALL
        SELECT format('OWNER OF FUNCTION %s', p.oid::regprocedure)
          FROM pg_proc p WHERE p.proowner = v_billing
        UNION ALL
        SELECT format('%s ON SCHEMA %I', a.privilege_type, n.nspname)
          FROM pg_namespace n, aclexplode(n.nspacl) a
         WHERE a.grantee = v_billing
           AND NOT (n.nspname = 'public' AND a.privilege_type = 'USAGE' AND NOT a.is_grantable)
        UNION ALL
        SELECT format('OWNER OF SCHEMA %I', n.nspname)
          FROM pg_namespace n WHERE n.nspowner = v_billing
        UNION ALL
        SELECT format('%s ON TYPE %s', a.privilege_type, t.oid::regtype)
          FROM pg_type t, aclexplode(t.typacl) a WHERE a.grantee = v_billing
        UNION ALL
        -- Типы строк и массивов принадлежат владельцу таблицы или типа — показаны там.
        SELECT format('OWNER OF TYPE %s', t.oid::regtype) FROM pg_type t
         WHERE t.typowner = v_billing AND t.typrelid = 0
           AND NOT EXISTS (SELECT 1 FROM pg_type e WHERE e.typarray = t.oid)
        UNION ALL
        SELECT format('%s ON LANGUAGE %I', a.privilege_type, l.lanname)
          FROM pg_language l, aclexplode(l.lanacl) a WHERE a.grantee = v_billing
        UNION ALL
        SELECT format('%s ON FOREIGN DATA WRAPPER %I', a.privilege_type, w.fdwname)
          FROM pg_foreign_data_wrapper w, aclexplode(w.fdwacl) a WHERE a.grantee = v_billing
        UNION ALL
        SELECT format('%s ON FOREIGN SERVER %I', a.privilege_type, s.srvname)
          FROM pg_foreign_server s, aclexplode(s.srvacl) a WHERE a.grantee = v_billing
        UNION ALL
        SELECT format('%s ON LARGE OBJECT %s', a.privilege_type, o.oid)
          FROM pg_largeobject_metadata o, aclexplode(o.lomacl) a WHERE a.grantee = v_billing
        UNION ALL
        SELECT format('OWNER OF LARGE OBJECT %s', o.oid)
          FROM pg_largeobject_metadata o WHERE o.lomowner = v_billing
        UNION ALL
        SELECT format('%s ON PARAMETER %s', a.privilege_type, pa.parname)
          FROM pg_parameter_acl pa, aclexplode(pa.paracl) a WHERE a.grantee = v_billing
        UNION ALL
        SELECT format('DEFAULT PRIVILEGES %s (%s)', a.privilege_type, d.defaclobjtype)
          FROM pg_default_acl d, aclexplode(d.defaclacl) a WHERE a.grantee = v_billing
        UNION ALL
        SELECT format('%s ON DATABASE %I', a.privilege_type, d.datname)
          FROM pg_database d, aclexplode(d.datacl) a
         WHERE d.datname = current_database() AND a.grantee = v_billing
           AND NOT (a.privilege_type = 'CONNECT' AND NOT a.is_grantable)
        UNION ALL
        SELECT format('OWNER OF DATABASE %I', d.datname)
          FROM pg_database d WHERE d.datname = current_database() AND d.datdba = v_billing
    ) s(x);
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION 'у роли feelings_billing лишние права или объекты: %', v_bad;
    END IF;
"""

_ADMIN = r"""
    SELECT string_agg(r.rolname, ', ' ORDER BY r.rolname) INTO v_bad
      FROM pg_roles r
     WHERE r.oid <> v_billing AND r.oid IS DISTINCT FROM v_login AND NOT r.rolsuper
       AND r.oid IS DISTINCT FROM v_owner AND r.oid IS DISTINCT FROM v_runner
       AND (pg_has_role(r.oid, v_billing, 'MEMBER')
            OR (v_login IS NOT NULL AND pg_has_role(r.oid, v_login, 'MEMBER')));
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION 'членство (в т.ч. только ADMIN) в feelings_billing или '
            'feelings_billing_login — не только у роли входа бота и владельца схемы: %', v_bad;
    END IF;
    IF v_login IS NOT NULL AND EXISTS (
        SELECT 1 FROM pg_auth_members m
         WHERE m.roleid = v_billing AND m.member = v_login AND m.admin_option) THEN
        RAISE EXCEPTION 'роль входа бота feelings_billing_login может выдавать feelings_billing '
            '(ADMIN OPTION)';
    END IF;
"""

# Правила 1–6: кто уже может действовать как feelings_billing и что ей доступно.
GUARD_SQL = _HEAD + _MEMBERSHIP + _GRANTS + _TAIL
# Правила 7–8: кто может выдать feelings_billing / роль входа бота без суперпользователя.
ADMIN_GUARD_SQL = _HEAD + _ADMIN + _TAIL
# Миграции (0020, env.py): все правила одним блоком.
MIGRATION_GUARD_SQL = _HEAD + _MEMBERSHIP + _GRANTS + _ADMIN + _TAIL
