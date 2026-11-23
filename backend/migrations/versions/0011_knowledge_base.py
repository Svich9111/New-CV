"""База знаний: исходный файл, индексация воркером, права ролей (задача 2.6, A1).

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-23 10:00:00

ТЗ §6.1 п.4, §6.4, §7, §9.1 п.5, §10 (фоновые задачи: индексация книг); ФТ-15; DECISIONS.md
АР-5, АР-24, АР-35, АР-39; BACKLOG Б-63, Б-65.

1. **Исходный файл — колонки ``kb_documents``** (не отдельная таблица: §7 — 17 таблиц, большой
   ``bytea`` хранится в TOAST и не читается при выборке остальных колонок):
   ``source_data bytea`` (до 20 МиБ, CHECK), ``source_format`` (``pdf``/``epub``/``docx``/``txt``),
   ``source_size``, ``source_sha256`` (32 байта; уникален среди материалов не в ``failed`` —
   повторная загрузка того же файла запрещена, после сбоя — разрешена). Исходник хранится
   только до конца индексации: ``kb_finish_index`` и ``kb_fail_index`` обнуляют его (CHECK:
   исходник есть только в ``uploaded``/``indexing``) — копия книги не лежит в БД дольше нужного.
   ``index_error`` — код причины сбоя (``^[a-z][a-z0-9_]{0,31}$``, без текста), ``indexed_at``;
   аренда воркера — ``index_lease_token``, ``index_lease_until``, ``index_attempts``.
   ``kb_chunks.ord`` — порядок фрагмента в материале (уникален в материале).
2. **Роли.** ``feelings_admin``: загрузка ``admin_kb_upload``, удаление ``admin_kb_delete`` (оба
   пишут ``audit_log`` без текстов, АР-24), список — SELECT колонок без исходника и аренды;
   прямых INSERT/UPDATE/DELETE на ``kb_documents`` и ``kb_chunks`` нет (фрагменты и векторы — не
   его, Б-63). ``feelings_worker``: ``kb_claim_index`` (``SKIP LOCKED``, аренда, попытки, догон
   после перезапуска), ``kb_read_source``, ``kb_add_chunk``, ``kb_finish_index``,
   ``kb_fail_index`` — все с проверкой токена аренды; векторы — ``kb_set_chunk_embedding``
   (0008); SELECT ``kb_chunks`` (без векторов) — для пересчёта векторов новой моделью.
   ``feelings_app``: поиск ``app_kb_nearest`` (0008); на ``kb_documents`` — SELECT колонок без
   исходника и аренды.
3. **Б-65** — замер ``app_kb_nearest`` с фильтрами на 15–20 тыс. фрагментов: без фильтра
   (HNSW) 50–65 мс, тема 9–12 мс, уровень 13–20 мс, тема и уровень 7 мс, ничего не найдено —
   7 мс (недобор HNSW → точный перебор отфильтрованных). Частичные индексы не нужны; функция
   0008 не меняется (АР-39).

Downgrade: функции, колонки, индексы и CHECK 0011 удаляются, права — как в 0010.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MAX_SOURCE_BYTES = 20 * 1024 * 1024  # 20 МиБ: лимит загрузки (контракт, 413)
FORMATS = "('pdf', 'epub', 'docx', 'txt')"
ERROR_CODE = "'^[a-z][a-z0-9_]{0,31}$'"


LEASED = """
        PERFORM 1 FROM public.kb_documents d
         WHERE d.id = p_document AND d.index_status = 'indexing'
           AND p_token IS NOT NULL AND d.index_lease_token = p_token
           FOR NO KEY UPDATE;"""


def _fill(template: str) -> str:
    """Подставить общие фрагменты (без f-строк: тексты функций — константы миграции)."""
    for name, value in (
        ("MAX", str(MAX_SOURCE_BYTES)),
        ("FORMATS", FORMATS),
        ("ERROR_CODE", ERROR_CODE),
        ("LEASED", LEASED),
    ):
        template = template.replace(f"@{name}@", value)
    return template


SCHEMA = tuple(
    _fill(s)
    for s in (
        "ALTER TABLE kb_documents ADD COLUMN source_data bytea",
        "ALTER TABLE kb_documents ADD COLUMN source_format varchar(8)",
        "ALTER TABLE kb_documents ADD COLUMN source_size integer",
        "ALTER TABLE kb_documents ADD COLUMN source_sha256 bytea",
        "ALTER TABLE kb_documents ADD COLUMN index_error varchar(32)",
        "ALTER TABLE kb_documents ADD COLUMN indexed_at timestamptz",
        "ALTER TABLE kb_documents ADD COLUMN index_lease_token uuid",
        "ALTER TABLE kb_documents ADD COLUMN index_lease_until timestamptz",
        "ALTER TABLE kb_documents ADD COLUMN index_attempts integer DEFAULT 0 NOT NULL",
        "ALTER TABLE kb_chunks ADD COLUMN ord integer",
        "ALTER TABLE kb_documents ADD CONSTRAINT ck_kb_documents_source_only_before_index "
        "CHECK (source_data IS NULL OR index_status IN ('uploaded', 'indexing'))",
        "ALTER TABLE kb_documents ADD CONSTRAINT ck_kb_documents_source_size "
        "CHECK (source_data IS NULL OR octet_length(source_data) BETWEEN 1 AND @MAX@)",
        "ALTER TABLE kb_documents ADD CONSTRAINT ck_kb_documents_source_format "
        "CHECK (source_format IS NULL OR source_format IN @FORMATS@)",
        "ALTER TABLE kb_documents ADD CONSTRAINT ck_kb_documents_sha256_len "
        "CHECK (source_sha256 IS NULL OR octet_length(source_sha256) = 32)",
        "ALTER TABLE kb_documents ADD CONSTRAINT ck_kb_documents_error_code "
        "CHECK (index_error IS NULL OR index_error ~ @ERROR_CODE@)",
        "ALTER TABLE kb_documents ADD CONSTRAINT ck_kb_documents_rights_basis_present "
        "CHECK (btrim(rights_basis) <> '')",
        "ALTER TABLE kb_documents ADD CONSTRAINT ck_kb_documents_attempts_nonnegative "
        "CHECK (index_attempts >= 0)",
        "ALTER TABLE kb_chunks ADD CONSTRAINT ck_kb_chunks_ord_nonnegative "
        "CHECK (ord IS NULL OR ord >= 0)",
        "CREATE UNIQUE INDEX uq_kb_documents_sha256_active ON kb_documents (source_sha256) "
        "WHERE source_sha256 IS NOT NULL AND index_status <> 'failed'",
        "CREATE UNIQUE INDEX uq_kb_chunks_document_ord ON kb_chunks (document_id, ord)",
        "CREATE INDEX ix_kb_documents_index_queue ON kb_documents (created_at, id) "
        "WHERE index_status IN ('uploaded', 'indexing')",
    )
)

ADMIN_UPLOAD = _fill(
    r"""
CREATE FUNCTION admin_kb_upload(p_title text, p_author text, p_approach text,
                                p_rights_basis text, p_format text, p_source bytea)
    RETURNS uuid
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    DECLARE v_id uuid; v_sha bytea;
    BEGIN
        IF p_title IS NULL OR btrim(p_title) = '' OR length(p_title) > 300
           OR length(p_author) > 200 OR length(p_approach) > 100
           OR p_rights_basis IS NULL OR btrim(p_rights_basis) = ''
           OR p_format IS NULL OR p_format NOT IN @FORMATS@
           OR p_source IS NULL OR octet_length(p_source) = 0 THEN
            RAISE EXCEPTION 'bad document' USING ERRCODE = '22023';
        END IF;
        IF octet_length(p_source) > @MAX@ THEN
            RAISE EXCEPTION 'source too large' USING ERRCODE = 'FK001';
        END IF;
        v_sha := sha256(p_source);
        BEGIN
            INSERT INTO public.kb_documents (title, author, approach, rights_basis, source_data,
                                             source_format, source_size, source_sha256)
            VALUES (btrim(p_title), NULLIF(btrim(p_author), ''), NULLIF(btrim(p_approach), ''),
                    btrim(p_rights_basis), p_source, p_format, octet_length(p_source), v_sha)
            RETURNING id INTO v_id;
        EXCEPTION WHEN unique_violation THEN
            RAISE EXCEPTION 'duplicate source' USING ERRCODE = 'FK002';
        END;
        -- АР-24: журнал — функцией, без текстов; администратор не пользователь (actor NULL).
        INSERT INTO public.audit_log (actor_id, action, object_type, object_id)
        VALUES (NULL, 'kb.upload', 'kb_document', v_id);
        RETURN v_id;
    END $f$
"""
)

ADMIN_DELETE = r"""
CREATE FUNCTION admin_kb_delete(p_document uuid) RETURNS boolean
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    BEGIN
        -- Фрагменты и векторы — каскадом; идущая индексация теряет документ (её завершение
        -- вернёт false).
        DELETE FROM public.kb_documents WHERE id = p_document;
        IF NOT FOUND THEN RETURN false; END IF;
        INSERT INTO public.audit_log (actor_id, action, object_type, object_id)
        VALUES (NULL, 'kb.delete', 'kb_document', p_document);
        RETURN true;
    END $f$
"""

CLAIM = r"""
CREATE FUNCTION kb_claim_index(p_lease interval, p_limit integer)
    RETURNS TABLE (document_id uuid, token uuid, attempts integer, source_format varchar,
                   source_size integer)
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    #variable_conflict use_column
    DECLARE
        v_lim integer := LEAST(GREATEST(COALESCE(p_limit, 1), 1), 20);
        r record;
    BEGIN
        IF p_lease IS NULL OR p_lease <= interval '0' OR p_lease > interval '1 day' THEN
            RAISE EXCEPTION 'bad lease' USING ERRCODE = '22023';
        END IF;
        -- Новые и брошенные (аренда истекла — воркер упал) материалы, старые первыми; строки,
        -- занятые другой копией воркера или удалением, пропускаются.
        FOR r IN
            SELECT d.id FROM public.kb_documents d
             WHERE (d.index_status = 'uploaded'
                    OR (d.index_status = 'indexing'
                        AND (d.index_lease_until IS NULL OR d.index_lease_until < now())))
               AND d.source_data IS NOT NULL
             ORDER BY d.created_at, d.id
             LIMIT v_lim
             FOR NO KEY UPDATE SKIP LOCKED
        LOOP
            -- Догон: фрагменты прерванной попытки (если воркер фиксировал их пачками) — заново.
            DELETE FROM public.kb_chunks c WHERE c.document_id = r.id;
            document_id := r.id;
            token := gen_random_uuid();
            UPDATE public.kb_documents d
               SET index_status = 'indexing', index_error = NULL, index_lease_token = token,
                   index_lease_until = now() + p_lease, index_attempts = d.index_attempts + 1
             WHERE d.id = r.id
            RETURNING d.index_attempts, d.source_format, d.source_size
                 INTO attempts, source_format, source_size;
            RETURN NEXT;
        END LOOP;
    END $f$
"""


READ_SOURCE = r"""
CREATE FUNCTION kb_read_source(p_document uuid, p_token uuid) RETURNS bytea
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    DECLARE v bytea;
    BEGIN
        -- Только по своей аренде: иначе NULL (материала нет, аренду перехватили).
        SELECT d.source_data INTO v FROM public.kb_documents d
         WHERE d.id = p_document AND d.index_status = 'indexing'
           AND p_token IS NOT NULL AND d.index_lease_token = p_token;
        RETURN v;
    END $f$
"""

ADD_CHUNK = _fill(
    r"""
CREATE FUNCTION kb_add_chunk(p_document uuid, p_token uuid, p_ord integer, p_text text,
                             p_topics text[], p_level integer) RETURNS uuid
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    DECLARE v_id uuid;
    BEGIN
        IF p_ord IS NULL OR p_ord < 0 OR p_text IS NULL OR btrim(p_text) = ''
           OR (p_level IS NOT NULL AND p_level NOT BETWEEN 1 AND 5)
           OR cardinality(p_topics) > 32 THEN
            RAISE EXCEPTION 'bad chunk' USING ERRCODE = '22023';
        END IF;@LEASED@
        IF NOT FOUND THEN RETURN NULL; END IF;
        INSERT INTO public.kb_chunks (document_id, ord, text, topic_tags, problem_level)
        VALUES (p_document, p_ord, p_text, COALESCE(p_topics, '{}')::varchar[], p_level)
        RETURNING id INTO v_id;
        RETURN v_id;
    END $f$
"""
)

FINISH = _fill(
    r"""
CREATE FUNCTION kb_finish_index(p_document uuid, p_token uuid) RETURNS boolean
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    BEGIN@LEASED@
        IF NOT FOUND THEN RETURN false; END IF;
        IF NOT EXISTS (SELECT 1 FROM public.kb_chunks c WHERE c.document_id = p_document) THEN
            RAISE EXCEPTION 'no chunks' USING ERRCODE = '22023';  -- пустой текст — kb_fail_index
        END IF;
        UPDATE public.kb_documents
           SET index_status = 'indexed', indexed_at = now(), index_error = NULL,
               source_data = NULL, index_lease_token = NULL, index_lease_until = NULL
         WHERE id = p_document;
        RETURN true;
    END $f$
"""
)

FAIL = _fill(
    r"""
CREATE FUNCTION kb_fail_index(p_document uuid, p_token uuid, p_reason text) RETURNS boolean
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    BEGIN
        IF p_reason IS NULL OR p_reason !~ @ERROR_CODE@ THEN
            RAISE EXCEPTION 'bad reason' USING ERRCODE = '22023';
        END IF;@LEASED@
        IF NOT FOUND THEN RETURN false; END IF;
        DELETE FROM public.kb_chunks WHERE document_id = p_document;
        UPDATE public.kb_documents
           SET index_status = 'failed', index_error = p_reason, source_data = NULL,
               index_lease_token = NULL, index_lease_until = NULL
         WHERE id = p_document;
        RETURN true;
    END $f$
"""
)

ADMIN_SIGNATURES = (
    "admin_kb_upload(text, text, text, text, text, bytea)",
    "admin_kb_delete(uuid)",
)
WORKER_SIGNATURES = (
    "kb_claim_index(interval, integer)",
    "kb_read_source(uuid, uuid)",
    "kb_add_chunk(uuid, uuid, integer, text, text[], integer)",
    "kb_finish_index(uuid, uuid)",
    "kb_fail_index(uuid, uuid, text)",
)

DOC_PUBLIC = "id, title, author, approach, index_status, created_at"
DOC_ADMIN = (
    "id, title, author, approach, rights_basis, index_status, created_at, source_format, "
    "source_size, source_sha256, index_error, indexed_at, index_attempts"
)

GRANTS = (
    *(f"REVOKE ALL ON FUNCTION {sig} FROM PUBLIC" for sig in ADMIN_SIGNATURES + WORKER_SIGNATURES),
    *(f"GRANT EXECUTE ON FUNCTION {sig} TO feelings_admin" for sig in ADMIN_SIGNATURES),
    *(f"GRANT EXECUTE ON FUNCTION {sig} TO feelings_worker" for sig in WORKER_SIGNATURES),
    # Администратор: список без исходника и аренды; запись — только функциями.
    "REVOKE ALL ON kb_documents FROM feelings_admin",
    f"GRANT SELECT ({DOC_ADMIN}) ON kb_documents TO feelings_admin",
    "REVOKE INSERT, UPDATE, DELETE ON kb_chunks FROM feelings_admin",
    # Приложение: метаданные материалов без исходника (поиск — app_kb_nearest).
    "REVOKE ALL ON kb_documents FROM feelings_app",
    f"GRANT SELECT ({DOC_PUBLIC}) ON kb_documents TO feelings_app",
    # Воркер: тексты фрагментов (не личные данные) — для пересчёта векторов новой моделью.
    "GRANT SELECT (id, document_id, ord, text, embedding_model) ON kb_chunks TO feelings_worker",
    f"GRANT SELECT ({DOC_PUBLIC}) ON kb_documents TO feelings_worker",
)

DOWNGRADE_GRANTS = (
    "REVOKE ALL ON kb_documents FROM feelings_worker",
    "REVOKE ALL ON kb_chunks FROM feelings_worker",
    "REVOKE ALL ON kb_documents FROM feelings_app",
    "GRANT SELECT ON kb_documents TO feelings_app",
    "REVOKE ALL ON kb_documents FROM feelings_admin",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON kb_documents TO feelings_admin",
    # Как в 0009: INSERT/UPDATE по колонкам без векторов, DELETE по таблице.
    "GRANT INSERT (id, document_id, text, topic_tags, problem_level) ON kb_chunks "
    "TO feelings_admin",
    "GRANT UPDATE (document_id, text, topic_tags, problem_level) ON kb_chunks TO feelings_admin",
    "GRANT DELETE ON kb_chunks TO feelings_admin",
)


def upgrade() -> None:
    for stmt in SCHEMA:
        op.execute(stmt)
    for fn in (ADMIN_UPLOAD, ADMIN_DELETE, CLAIM, READ_SOURCE, ADD_CHUNK, FINISH, FAIL):
        op.execute(fn)
    for stmt in GRANTS:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE_GRANTS:
        op.execute(stmt)
    for sig in reversed(ADMIN_SIGNATURES + WORKER_SIGNATURES):
        op.execute(f"DROP FUNCTION IF EXISTS {sig}")
    op.execute("DROP INDEX IF EXISTS ix_kb_documents_index_queue")
    op.execute("DROP INDEX IF EXISTS uq_kb_chunks_document_ord")
    op.execute("DROP INDEX IF EXISTS uq_kb_documents_sha256_active")
    op.execute("ALTER TABLE kb_chunks DROP COLUMN IF EXISTS ord")
    op.execute(
        "ALTER TABLE kb_documents DROP CONSTRAINT IF EXISTS ck_kb_documents_rights_basis_present"
    )
    for column in (
        "index_attempts",
        "index_lease_until",
        "index_lease_token",
        "indexed_at",
        "index_error",
        "source_sha256",
        "source_size",
        "source_format",
        "source_data",
    ):
        # Колонки уходят вместе со своими CHECK.
        op.execute(f"ALTER TABLE kb_documents DROP COLUMN IF EXISTS {column}")
