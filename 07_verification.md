# 07. Верификация: гейты, идентичности, гигиена по волнам

**Статус:** протокол программы. **Дата:** 2026-10-20.
Правила дома обязательны на каждом гейте: Н9.4 (арифметика до единицы ДО объявления PASS; формы — потери, ENTRY, точки агрегации), И7 (сравнивать distinct work, а не счётчики), §2 AGENTS (pytest и живой прогон не смешивать), §1 (run-руты `/var/tmp`, umask 077, дистилляция), §3 (маскирование, не провоцировать secondary), §5 (3.11 sweep из песочницы перед деплоем), commit-first → доки цитируют хэш, staged-diff credscan VALUE+FORM перед каждым коммитом.

---

## 7.0 Волна 0 — проверки фактов (без кода)

| # | Проверка | Инструмент | Порог/ожидание |
|---|---|---|---|
| 0.1 | Предел bind-переменных SQLite на prod pyenv 3.11 | однострочник: `python -c "import sqlite3; print(sqlite3.sqlite_version)"` + бинарный поиск N в `IN (?,…)` | если 32 766 — C3-чанкинг обязателен ДО первого рестарта wide; если 250 000 — всё равно делаем (переносимость) |
| 0.2 | Диск прод-хоста | `df -h <workspace volume>` | ≥100 ГБ минимум, ≥500 ГБ комфортно (док 06) |
| 0.3 | RAM прод-хоста | `free -g` | MemoryMax 6G влезает с запасом |
| 0.4 | Код прод-копии | `git rev-parse HEAD` / diff против `fc7c948` | ≥ `c5468eb` (фикс Any); рекомендуется полный синк |
| 0.5 | Потребитель снэпшотов | подтверждение оператора (в репо ноль — проверено) | внешний потребитель отсутствует ⇒ дефолт False безопасен |
| 0.6 | Статус ротации PAT/сессий | сторона оператора (GitHub UI) | OPEN-1/2 дока 08 |
| 0.7 | (web, волна 6) blackbird `{failed,count,mode}` для `/regex/` + deduped page-size | один гейтовый зонд, 1 сессия, несколько запросов | калибровка partitions-арифметики до обязательств |

Выполняет оператор по чек-листу (доступа к прод-хосту у агента нет), вывод возвращается в `08_decisions.md`.

## 7.1 Волна 1 (F1 + C1-config + C3 + snapshots-off + юнит-черновик)

- **Suite:** RED→GREEN, новые пины: F1 pagination-fallback (пустой admission ⇒ страницы создаются, счётчик растёт), C3 chunk-граница (IN ≤1000, spy на decide-вызовах), snapshot-пины C1–C9 (док 04 §4.5). Ни один существующий пин не правится, кроме сознательных расширений.
- **Живой гейт:** 300 с fresh + restart-drill 2×120 с (протокол liveval/p26_gate.sh переиспользуется). **Идентичность v3 БЕЗ изменений** (новые счётчики аддитивны, точек агрегации не появляется): run1 `created = claims − share_suppr`, run2 `+ Σ recovery-enq`; баланс 0 обоих. Компараторы канонические: `Refine gen=21312 adm=144 refused[wire=21168] uniq_wires=144`, `Aggregation hits=111 misses=37 pairs=3`, links 3067-семейство, soft cross `skipped+claims ≈ discovered ± drift`.
- **Спецпроверки:** строки «Built snapshot» ОТСУТСТВУЮТ (snapshots off); stop-time без финальной сборки (сравнение с liveval 122 с); age-purge при max_age_hours 720 не стирает pending на drill-рестарте (строка purge-отчёта содержит 0 или только genuinely-старые).
- **Гигиена:** стандартная батарея (§7.6).

## 7.2 Волна 2 (plumbing: C2/C2b/C6/C8/F4/W1-W4/E1/C10)

- **Suite:** пины plumbing'а (config-ключ → ctor-параметр → поведение), W-предупреждения (advisory, негативные кейсы), F4 агрегированная строка (одна на родителя, счётчик точен), ротация decision-лога.
- **Живой гейт:** короткий soak 120 с — **поведение неизменно** (все флаги в прежних значениях; новые ключи в дефолтах): канонические компараторы байт-в-байт, новые поверхности рендерятся (`Share:` получает `full=` аларм-семантику, строка refusal-агрегата). Это волна наблюдаемости — гейт доказывает НЕйтральность.
- **Офлайн-дрель:** ledger cap искусственно занижен (tmp-конфиг) ⇒ fail-open + loud-once + `ledger_full` в строке; dedup_max_size занижен ⇒ eviction-счётчик.

## 7.3 Волна 3a (wire_coverage + registry-recovery)

- **Suite:** пины дока 03 §3d (contract таблицы, TTL, patterns-смена, pages_done, чанки, миграция v6→v7 round-trip v7→v6→v7 офлайн в worktree без дефисов).
- **Идентичность ЭВОЛЮЦИОНИРУЕТ — пред-регистрация ДО гейта:** `created_search = claims − share_suppr − covered_suppr` (новый член); мягкая кросс-идентичность: `admitted + refused_wire_covered == post-guard population`. Компаратор `uniq_wires` больше не канон — регистрируется новая каноническая форма на shadow-прогоне.
- **Живой гейт (серия возобновления):** run1 300 с fresh (wire_coverage shadow → on: подавление 0 в shadow, счетчики растут) → run2 120 с РЕСТАРТ (тот же воркспейс): `refused_wire_covered ≈ admitted(run1)` для свежих бакетов, бюджет расходуется только на новые; **recovery из SQL**: строка READ-агрегата шардов больше не ENTRY-член идентичности — вместо неё `recovered_from_registry=N` (новая публикация, громкая); fallback-дрель: registry degraded (искусственно) ⇒ шард-путь + WARNING.
- **Drill потерь:** kill -9 mid-run ⇒ wire_coverage частично заполнена, незавершённые бакеты не подавляются (pages_done < needed) — возобновление корректно.

## 7.4 Волна 3b (search-share ledger, вариант A)

Протокол зеркалит R17 (три руки off/shadow/on по 300 с fresh + restart-drill):
- **Рука B (shadow)** доказательно инертна: создание задач байт-в-байт как A, `would_suppress > 0`.
- **Рука C (on):** HTTP-экономия измеряется ПО distinct wires исполненных (`Aggregation misses` + basket-счётчики `token_requests`), НЕ числом задач (И7); ожидание: misses × ~4 → ×1 на twin-множестве; инвариант 1: корзины пер-провайдер, владелец платит; **ротация владельца** (второй срез D-03.9): `token_requests` по провайдерам выравнивается ±10 %.
- Fan-out проверяется продуктом: ссылки каждого провайдера в ЕГО шарде, `link_coverage` на всех бенефициаров, check-задачи с regex бенефициара, early-stop observe на бенефициара.
- Идентичность: search-side created получает член `− search_share_suppr` (пред-регистрация); точка агрегации проверена: search-share и gather-share счетчики НЕ пересекаются (разные стадии создания) — доказательство дизъюнктности потоков до сложения (урок P26).

## 7.5 Волны 3c/4/5/6

- **3c (даты):** гейт промоушена `file_dating enabled/graphql/gathered`: `Dating:` fill растёт от 0.0000, `budget_stop` отсутствует (фоновый батчинг не имеет wall-clock бюджета), gather throughput в пределах run-to-run разброса (нейтральность), graphql refusals 0, drain батчера в graceful stop ≤ ограниченного. Компаратор D7-арифметики R17 (raw − 304 = dating denominator) заменяется на `filled ≤ batches×50` (батч-семантика).
- **4 (purge CLI):** офлайн-матрица T1–T13 (док 05) + живая дрель НА КОПИИ гейт-воркспейса (никогда прод): dry-run → apply → сверка счётчиков квитанции с фактическим состоянием → старт пайплайна после `links`-purge показывает re-gather (skip видит unknown) → старт после `all` = свежий воркспейс. Пин «нет значений секретов в stdout/receipt».
- **5 (wide enablement):** лестница СТупенями: шаг 1 clamp **1296** budget 200k (короткий soak: арифметика `gen = 4 корня × 1296 + …`, `adm ≈ 4×1296`, `uniq_wires` кратно 1296; F2 fairness: admitted по провайдерам ±10 %); шаг 2 clamp **46656** в серии прогонов (возобновление через wire_coverage: run2 `refused_wire_covered ≈ run1 admitted`); на каждом шаге новая каноническая Refine-строка регистрируется в этом доке; диск/RAM-наблюдение по доку 06 (du каждые 30 мин, RSS-сэмплер). F10-аннотация спеки едет с шагом 2.
- **6 (web):** предусловия (§2.4 дока 02) → зонд 0.7 → фиксы П-B/П-G/П-K′ каждый со своим пином и живой проверкой (П-B: контролируемая протухшая сессия — операторская, не провоцируем лимиты; П-G: только органический эпизод, AGENTS §3); flip-гигиена (transport в `_generate_id` ИЛИ drain очередей профилем `queues`); W1–W10 чек-лист (plan.md II.17.4) с живыми замерами; перемерить агрегацию для TTL 120 с (П-I/W10); dead-zone хвосты замеряются на web впервые (решение @287@ п.5).

## 7.6 Стандартная гигиеническая батарея (каждый живой гейт)

credscan по ЗНАЧЕНИЯМ (8 живых учётных значений, никогда не печатаются) + формам (`ghp_`/`github_pat_`/bare-48) по всем файлам evidence; banned signatures (13+: Traceback, IntegrityError, failed to open durable task queue, lost task during persistence, _enter_buffered_busy, Logging error, Fatal Python error, provider limiter starved, secondary rate limit, Rate limit exceeded (HTTP 429), Authentication failed (HTTP, queue is full, gather-skip decision failed); exit 0 / Healthy / Critical Alerts 0; `df /tmp` до и после (не растёт); `logs/`+`data/` репозитория пусты; staged-diff credscan перед коммитом; FAIL→CORRECTED narrative append-only (оригинал вердикта сохраняется).

## 7.7 Интерпретаторная дисциплина (AGENTS §5)

Каждая волна: AST-пин annotation-hygiene проходит автоматически (новый код в scan-охвате); перед любым деплоем на прод — 3.11 import sweep из песочной копии + `--validate` под 3.11 с конфигом по абсолютному пути.
