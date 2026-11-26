/**
 * Типы админ-панели — только схемы путей `/admin/*` из сгенерированного `schema.d.ts` (контракт меняет A1).
 * Пользовательские типы и клиент Mini App сюда не импортируются (АР-52; проверка — `boundary.test.ts`).
 */
import type { components, paths } from "../api/schema";

type Upload = NonNullable<paths["/admin/kb/documents"]["post"]["requestBody"]>["content"]["multipart/form-data"];

export type KbDocument = components["schemas"]["KbDocument"];
export type KbIndexStatus = KbDocument["index_status"];
export type KbFormat = NonNullable<KbDocument["source_format"]>;

/** Поля формы загрузки, кроме файла (по контракту). */
export type KbUploadFields = Omit<Upload, "file">;
