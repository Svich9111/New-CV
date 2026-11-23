"""Поиск по смыслу в БД: память агента и база знаний (задача 2.0v, A1).

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-22 12:00:00

ТЗ §6.1 п.3–4, §6.3, §6.4, §7, §9.2 п.1, §10; DECISIONS.md АР-5, АР-35; BACKLOG Б-55.

Что добавляется:

- колонка ``embedding_model varchar(128)`` в ``memory_items`` и ``kb_chunks`` — какой моделью
  сделан вектор (АР-5: векторы разных моделей несравнимы). CHECK ``*_model_has_vector``: имя
  модели без вектора не хранится (удаление записи памяти обнуляет и вектор, и модель; CHECK
  ``deleted_has_no_content`` из 0001 не меняется). Вектор без модели (старые строки до 0008)
  допустим, но поиском не находится — до пересчёта;
- индексы HNSW (``vector_cosine_ops``) на ``memory_items.embedding`` и ``kb_chunks.embedding``;
- ``embedding_arg(text, text) RETURNS vector`` — разбор и проверка вектора запроса (не SECURITY
  DEFINER, EXECUTE ни у кого: вызывается только из функций ниже от имени их владельца);
- ``app_memory_nearest(p_query text, p_model text, p_limit integer)`` — ближайшие записи памяти
  своего агента (``feelings_app``);
- ``app_set_memory_embedding(p_item uuid, p_vec text, p_model text) RETURNS boolean`` — вектор
  своей неудалённой записи с текстом (``feelings_app``);
- ``app_kb_nearest(p_query text, p_model text, p_limit integer, p_approach text, p_topics text[],
  p_level integer)`` — фрагменты базы знаний проиндексированных материалов (``feelings_app``);
- ``kb_set_chunk_embedding(p_chunk uuid, p_vec text, p_model text) RETURNS boolean`` — вектор
  фрагмента (``feelings_worker``, воркер индексации 2.6).

Вектор передаётся текстом pgvector ``[x1,...,x1024]`` и приводится внутри функции: у ролей
приложения нет EXECUTE на функции pgvector (``REVOKE ALL ON ALL FUNCTIONS … FROM PUBLIC``, 0001),
поэтому ``CAST(:v AS vector)`` под ``feelings_app`` даёт ``permission denied for function
vector_in`` (проверено). Текущая модель — параметр ``p_model`` (имя модели эмбеддера, которым
сделан вектор запроса), а не настройка БД: вектор запроса и векторы строк сравнимы, только если
модель одна, и знает её тот, кто посчитал вектор (АР-35).

Downgrade удаляет функции, индексы, CHECK и колонки; векторы остаются.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# app_memory_nearest: при не более чем 1000 записях своего агента поиск — точный перебор (без
# HNSW). pgvector 0.6 фильтрует результат индекса после обхода графа (``hnsw.ef_search``
# кандидатов на всю таблицу): при поиске по одному агенту из многих индекс почти ничего не
# вернёт. Перебор по индексу ``agent_id`` для такого объёма — миллисекунды и без потери полноты
# (АР-35).

SCHEMA = (
    "ALTER TABLE memory_items ADD COLUMN embedding_model varchar(128)",
    "ALTER TABLE kb_chunks ADD COLUMN embedding_model varchar(128)",
    "ALTER TABLE memory_items ADD CONSTRAINT ck_memory_items_model_has_vector "
    "CHECK (embedding_model IS NULL OR (embedding IS NOT NULL AND embedding_model <> ''))",
    "ALTER TABLE kb_chunks ADD CONSTRAINT ck_kb_chunks_model_has_vector "
    "CHECK (embedding_model IS NULL OR (embedding IS NOT NULL AND embedding_model <> ''))",
    "CREATE INDEX ix_memory_items_embedding_hnsw ON memory_items "
    "USING hnsw (embedding vector_cosine_ops)",
    "CREATE INDEX ix_kb_chunks_embedding_hnsw ON kb_chunks USING hnsw (embedding vector_cosine_ops)",
)

EMBEDDING_ARG = r"""
CREATE FUNCTION embedding_arg(p_vec text, p_model text) RETURNS vector
    LANGUAGE plpgsql IMMUTABLE SET search_path = public, pg_temp
    AS $f$
    DECLARE v public.vector;
    BEGIN
        IF p_model IS NULL OR p_model = '' OR length(p_model) > 128 THEN
            RAISE EXCEPTION 'bad embedding model' USING ERRCODE = '22023';
        END IF;
        IF p_vec IS NULL THEN
            RAISE EXCEPTION 'no embedding' USING ERRCODE = '22023';
        END IF;
        -- Текст ошибки pgvector содержит сам вектор (производное от личного текста):
        -- наружу и в журнал сервера — только код без значения.
        BEGIN
            v := p_vec::public.vector(1024);
        EXCEPTION WHEN OTHERS THEN
            RAISE EXCEPTION 'bad embedding' USING ERRCODE = '22023';
        END;
        IF public.vector_norm(v) = 0 THEN
            RAISE EXCEPTION 'zero embedding' USING ERRCODE = '22023';
        END IF;
        RETURN v;
    END $f$
"""

MEMORY_NEAREST = r"""
CREATE FUNCTION app_memory_nearest(p_query text, p_model text, p_limit integer)
    RETURNS TABLE (id uuid, type memory_type, source memory_source, event_date date,
                   confidence double precision, text_enc bytea, distance double precision)
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp
    SET hnsw.ef_search = 200 SET plan_cache_mode = force_custom_plan
    AS $f$
    #variable_conflict use_column
    DECLARE
        v_uid uuid := public.app_uid();
        v_lim integer := LEAST(GREATEST(COALESCE(p_limit, 1), 1), 50);
        v_q public.vector;
        v_agent uuid;
        v_total integer;
        v_ids uuid[];
    BEGIN
        IF v_uid IS NULL THEN RAISE EXCEPTION 'no user' USING ERRCODE = '42501'; END IF;
        PERFORM 1 FROM public.users u WHERE u.id = v_uid AND u.deleted_at IS NULL;
        IF NOT FOUND THEN RAISE EXCEPTION 'no user' USING ERRCODE = '42501'; END IF;
        v_q := public.embedding_arg(p_query, p_model);
        SELECT a.id INTO v_agent FROM public.agents a WHERE a.user_id = v_uid;
        IF NOT FOUND THEN RETURN; END IF;

        SELECT count(*) INTO v_total FROM public.memory_items m
         WHERE m.agent_id = v_agent AND m.deleted_at IS NULL AND m.text_enc IS NOT NULL
           AND m.embedding IS NOT NULL AND m.embedding_model = p_model;
        IF v_total = 0 THEN RETURN; END IF;

        IF v_total > 1000 THEN
            -- Индекс HNSW; если фильтр по агенту оставил меньше нужного — точный перебор.
            SELECT array_agg(c.id) INTO v_ids FROM (
                SELECT m.id FROM public.memory_items m
                 WHERE m.agent_id = v_agent AND m.deleted_at IS NULL AND m.text_enc IS NOT NULL
                   AND m.embedding IS NOT NULL AND m.embedding_model = p_model
                 ORDER BY m.embedding OPERATOR(public.<=>) v_q
                 LIMIT v_lim) c;
            IF COALESCE(cardinality(v_ids), 0) >= LEAST(v_lim, v_total) THEN
                RETURN QUERY
                SELECT m.id, m.type, m.source, m.event_date, m.confidence, m.text_enc,
                       m.embedding OPERATOR(public.<=>) v_q AS d
                  FROM public.memory_items m
                 WHERE m.id = ANY (v_ids) AND m.agent_id = v_agent
                 ORDER BY d, m.id;
                RETURN;
            END IF;
        END IF;

        -- Точный перебор записей своего агента (индекс agent_id); MATERIALIZED — чтобы
        -- планировщик не подменил перебор обходом HNSW по всей таблице.
        RETURN QUERY
        WITH c AS MATERIALIZED (
            SELECT m.id, m.embedding OPERATOR(public.<=>) v_q AS d FROM public.memory_items m
             WHERE m.agent_id = v_agent AND m.deleted_at IS NULL AND m.text_enc IS NOT NULL
               AND m.embedding IS NOT NULL AND m.embedding_model = p_model)
        SELECT m.id, m.type, m.source, m.event_date, m.confidence, m.text_enc, c.d
          FROM c JOIN public.memory_items m ON m.id = c.id
         ORDER BY c.d, c.id
         LIMIT v_lim;
    END $f$
"""

SET_MEMORY_EMBEDDING = r"""
CREATE FUNCTION app_set_memory_embedding(p_item uuid, p_vec text, p_model text) RETURNS boolean
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    DECLARE
        v_uid uuid := public.app_uid();
        v public.vector;
    BEGIN
        IF v_uid IS NULL THEN RAISE EXCEPTION 'no user' USING ERRCODE = '42501'; END IF;
        PERFORM 1 FROM public.users u WHERE u.id = v_uid AND u.deleted_at IS NULL;
        IF NOT FOUND THEN RAISE EXCEPTION 'no user' USING ERRCODE = '42501'; END IF;
        v := public.embedding_arg(p_vec, p_model);
        -- Чужая, удалённая, без текста или несуществующая запись — false (одинаково).
        UPDATE public.memory_items m SET embedding = v, embedding_model = p_model
          FROM public.agents a
         WHERE m.id = p_item AND a.id = m.agent_id AND a.user_id = v_uid
           AND m.deleted_at IS NULL AND m.text_enc IS NOT NULL;
        RETURN FOUND;
    END $f$
"""

KB_NEAREST = r"""
CREATE FUNCTION app_kb_nearest(p_query text, p_model text, p_limit integer,
                               p_approach text, p_topics text[], p_level integer)
    RETURNS TABLE (id uuid, document_id uuid, text text, approach varchar,
                   topic_tags varchar[], problem_level smallint, distance double precision)
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp
    SET hnsw.ef_search = 200 SET plan_cache_mode = force_custom_plan
    AS $f$
    #variable_conflict use_column
    DECLARE
        v_uid uuid := public.app_uid();
        v_lim integer := LEAST(GREATEST(COALESCE(p_limit, 1), 1), 20);
        v_topics varchar[] := CASE WHEN cardinality(p_topics) > 0 THEN p_topics::varchar[] END;
        v_q public.vector;
        v_docs uuid[];
        v_ids uuid[];
    BEGIN
        IF v_uid IS NULL THEN RAISE EXCEPTION 'no user' USING ERRCODE = '42501'; END IF;
        PERFORM 1 FROM public.users u WHERE u.id = v_uid AND u.deleted_at IS NULL;
        IF NOT FOUND THEN RAISE EXCEPTION 'no user' USING ERRCODE = '42501'; END IF;
        v_q := public.embedding_arg(p_query, p_model);
        IF p_level IS NOT NULL AND p_level NOT BETWEEN 1 AND 5 THEN
            RAISE EXCEPTION 'bad level' USING ERRCODE = '22023';
        END IF;
        -- Материалы отбираются заранее (их немного): иначе соединение с kb_documents уводит
        -- планировщик от индекса HNSW к перебору фрагментов по документам.
        SELECT array_agg(d.id) INTO v_docs FROM public.kb_documents d
         WHERE d.index_status = 'indexed' AND (p_approach IS NULL OR d.approach = p_approach);
        IF v_docs IS NULL THEN RETURN; END IF;

        -- Индекс HNSW; фильтры по меткам применяются после обхода графа (pgvector 0.6), поэтому
        -- при недоборе — точный перебор отфильтрованных фрагментов.
        SELECT array_agg(x.id) INTO v_ids FROM (
            SELECT c.id FROM public.kb_chunks c
             WHERE c.embedding IS NOT NULL AND c.embedding_model = p_model
               AND c.document_id = ANY (v_docs)
               AND (v_topics IS NULL OR c.topic_tags && v_topics)
               AND (p_level IS NULL OR c.problem_level = p_level)
             ORDER BY c.embedding OPERATOR(public.<=>) v_q
             LIMIT v_lim) x;
        IF COALESCE(cardinality(v_ids), 0) < v_lim THEN
            -- Точный перебор; MATERIALIZED — чтобы планировщик не вернулся к обходу HNSW.
            WITH x AS MATERIALIZED (
                SELECT c.id, c.embedding OPERATOR(public.<=>) v_q AS dist FROM public.kb_chunks c
                 WHERE c.embedding IS NOT NULL AND c.embedding_model = p_model
                   AND c.document_id = ANY (v_docs)
                   AND (v_topics IS NULL OR c.topic_tags && v_topics)
                   AND (p_level IS NULL OR c.problem_level = p_level))
            SELECT array_agg(y.id) INTO v_ids
              FROM (SELECT x.id FROM x ORDER BY x.dist, x.id LIMIT v_lim) y;
        END IF;

        RETURN QUERY
        SELECT c.id, c.document_id, c.text, d.approach, c.topic_tags, c.problem_level,
               c.embedding OPERATOR(public.<=>) v_q AS dist
          FROM public.kb_chunks c JOIN public.kb_documents d ON d.id = c.document_id
         WHERE c.id = ANY (COALESCE(v_ids, '{}'))
         ORDER BY dist, c.id;
    END $f$
"""

KB_SET_EMBEDDING = r"""
CREATE FUNCTION kb_set_chunk_embedding(p_chunk uuid, p_vec text, p_model text) RETURNS boolean
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    DECLARE v public.vector := public.embedding_arg(p_vec, p_model);
    BEGIN
        UPDATE public.kb_chunks SET embedding = v, embedding_model = p_model WHERE id = p_chunk;
        RETURN FOUND;
    END $f$
"""

APP_SIGNATURES = (
    "app_memory_nearest(text, text, integer)",
    "app_set_memory_embedding(uuid, text, text)",
    "app_kb_nearest(text, text, integer, text, text[], integer)",
)
WORKER_SIGNATURES = ("kb_set_chunk_embedding(uuid, text, text)",)

GRANTS = (
    "REVOKE ALL ON FUNCTION embedding_arg(text, text) FROM PUBLIC",
    *(f"REVOKE ALL ON FUNCTION {sig} FROM PUBLIC" for sig in APP_SIGNATURES + WORKER_SIGNATURES),
    *(f"GRANT EXECUTE ON FUNCTION {sig} TO feelings_app" for sig in APP_SIGNATURES),
    *(f"GRANT EXECUTE ON FUNCTION {sig} TO feelings_worker" for sig in WORKER_SIGNATURES),
)


def upgrade() -> None:
    # По одной команде: asyncpg не выполняет несколько команд в одном запросе.
    for stmt in SCHEMA:
        op.execute(stmt)
    for fn in (EMBEDDING_ARG, MEMORY_NEAREST, SET_MEMORY_EMBEDDING, KB_NEAREST, KB_SET_EMBEDDING):
        op.execute(fn)
    for stmt in GRANTS:
        op.execute(stmt)


def downgrade() -> None:
    for sig in reversed(APP_SIGNATURES + WORKER_SIGNATURES):
        op.execute(f"DROP FUNCTION IF EXISTS {sig}")
    op.execute("DROP FUNCTION IF EXISTS embedding_arg(text, text)")
    op.execute("DROP INDEX IF EXISTS ix_kb_chunks_embedding_hnsw")
    op.execute("DROP INDEX IF EXISTS ix_memory_items_embedding_hnsw")
    op.execute("ALTER TABLE kb_chunks DROP CONSTRAINT IF EXISTS ck_kb_chunks_model_has_vector")
    op.execute(
        "ALTER TABLE memory_items DROP CONSTRAINT IF EXISTS ck_memory_items_model_has_vector"
    )
    op.execute("ALTER TABLE kb_chunks DROP COLUMN IF EXISTS embedding_model")
    op.execute("ALTER TABLE memory_items DROP COLUMN IF EXISTS embedding_model")
