# 02. Реестр критических исправлений (цель 1)

**Статус:** составлен по четырём аудитам 2026-10-18 (governor/admission A; web-readiness B; durability C; config/pins D) + личная верификация. **Дата:** 2026-10-20.
Полные нарративы аудитов дистиллированы в план-карточках и сжатых блоках сессии; этот реестр — единственный рабочий источник для волн 1–6. Severity: **BLOCKER** (ломает покрытие/данные на горизонте), **CONTRADICTION** (противоречит канону §0.2), **RISK**, **NOTE**.

---

## 2.1 Волна 1 — блокеры + config-only безопасность (малый код, быстрый выигрыш)

| ID | Sev | Где | Суть | Фикс | Откат |
|---|---|---|---|---|---|
| **F1** | **BLOCKER** | `stage/definition.py:416-465` | `if refine: … elif total > per_page:` — пустой admission (guard/бюджет отклонили всех детей) ⇒ родитель не делает НИЧЕГО, даже свои страницы 2–10; при широком клампе + голодании бюджета теряется целое поддерево корня (≤1000 ссылок/родитель) | Падение в пагинацию при пустом admission (~10 строк); счётчик `pagination_fallbacks`; семантика подтверждена оператором (Q4=@285@ п.4 — «оставить без исправлений» относится только к мёртвой зоне; F1 — отдельный блокер, в маршруте с самого начала) | revert коммита |
| **C1** | **BLOCKER** | `storage/task_queue.py:171-177` | Age-purge удаляет строки `created_at < now−24h` на КАЖДОМ старте (`_recover_on_open`, ctor :146), без фильтра state/attempts; defer сохраняет исходный `created_at` (`base.py:455-462`) ⇒ при чистом росте бэклога +5–8 строк/с после первых суток каждый рестарт стирает **430–690k строк**; с `Restart=on-failure` = петля уничтожения покрытия | **Config-only:** `queue.max_age_hours: 24 → 720` (валидатор требует только >0, `schemas.py:699-701`); belt-and-braces (волна 2, опционально): exempt `state='pending' AND attempts=0` в SQL purge | возврат числа |
| **C3** | **BLOCKER** | `manager/recovery.py:206` → `storage/gather_skip.py:294-304` | Весь per-provider список URL уходит в ОДИН `decide()` = один bind-var на хэш; предел dev-host SQLite **250 000** (замерено), upstream-дефолт **32 766**, prod pyenv НЕИЗВЕСТЕН (фаза 0); превышение → catch `:181-185` fail-open ВСЕ «unknown» ⇒ P26 тихо инвалидируется, весь накопленный шард-источник перезаходит в очередь на каждом рестарте | Чанкинг `decide()`/claim ≤ **1000** URL (search-сторона уже батчит ≤100); пин на чанк-границу; WARNING при числе чанков > 1 | revert |
| **C5-ядро** | **CONTRADICTION** | `storage/snapshot.py`, `strategies.py:324-338`, `pipeline.py:202-208` | Периодическая (300 с) и финальная (безусловная, `persistence.py:523-527`) пересборка ВСЕХ шардов в pretty-JSON; churn ≈27·t ГБ/ч (квадратичный), IO-доминанта к ~24 ч, SIGKILL-риск финальной сборки >4.6 ГБ; потребителей НОЛЬ; `snapshot_interval` — МЁРТВЫЙ КЛЮЧ (loader не парсит) | Ядро — флаг отключения + дефолт False; полная поверхность цели 3 — `04_snapshot_lifecycle.md` | флаг |
| **TimeoutStopSec** | RISK | `examples/harvester.service` | 300 с недостаточно при больших шардах (~65 с/ГБ на финальной сборке); после отключения снэпшотов остановка быстрая, но запас дешевле инцидента | Черновик юнита: `TimeoutStopSec=1800` (установка — root, сторона оператора) | — |

**Гейт волны 1:** suite RED→GREEN; живой гейт = 300 с fresh + restart-drill 2×120 с (протокол liveval); идентичность v3 БЕЗ изменений (новые счётчики аддитивны); компараторы канонические (`Refine gen=21312 adm=144 refused[wire=21168] uniq_wires=144`). Подробно — `07_verification.md`.

## 2.2 Волна 2 — plumbing и наблюдаемость (подготовка к масштабу 10⁶)

| ID | Sev | Где | Суть | Фикс |
|---|---|---|---|---|
| **C2** | **CONTRADICTION** | `manager/gather_share.py:36,:61`; `pipeline.py:99` | Cap леджера 50 000 зашит константой; outstanding ≈ БЭКЛОГ gather (claim при создании, take при завершении) ⇒ при широком режиме cap достигается за ~1.7–2.8 ч и sharing умирает ровно когда нужнее всего (канон §0.2.2); запись ~500–550 Б ⇒ 10⁶ ≈ 0.5 ГБ |Expose `gather.share_ledger_max_tracked` (ctor-параметр УЖЕ есть) через schema/loader/validator + pipeline wiring; аларм `ledger_full > 0` в строке `Share:`; сайзинг под горизонт (10⁶ ≈ 0.5 ГБ против MemoryMax 6G — влезает) |
| **C2b** | RISK | `gather_share.py:77,:128-134` | `_shadow_seen` в shadow-режиме без границ (~330 Б/ключ) | Тот же cap на shadow-множество |
| **C6/F5** | RISK | `stage/base.py:167,:197,:443-451`; `pipeline.py:374-385` | `dedup_max_size` 100k по умолчанию, НИКОГДА не передаётся, комментарий врёт («LRU» — на деле FIFO без promotion); при 10⁶ задач гейт забывает всё кроме последних 100k | Plumb per-stage через конфиг; gather → 500k–1M (≈85–170 МБ/стадия — в пределах плато) |
| **C8** | NOTE | `storage/gather_skip.py:443-450` | Decision-JSONL без ротации (~250 Б/skip ≈ 0.5 ГБ/100 ч) | Ротация по размеру/возрасту (образец logrotate-дисциплины) или сэмплирование |
| **F4** | RISK | `search/refine_governor.py:345-351` | Спека ТРЕБУЕТ per-child WARNING на budget-отказ ⇒ при клампе 46656 один голодающий корень = 46k строк (реинцидент 56 048 строк) | Агрегированная строка на родителя (образец wire-collapse D7) + правка спеки (§1.4 доков) |
| **W1–W4** | NOTE | `config/validator.py` | Нет предупреждений о рассогласовании ручек | W1 `budget < providers × clamp`; W2 ledger cap < proxy бэклога; W3 age-gate < горизонта; W4 clamp > 46656 (конверп генератора) — все WARNING-класс (advisory, дом-стиль) |
| **E1** | NOTE | `config/loader.py:316-332`, validator | Секция `persistence` НЕ валидируется вовсе; `snapshot_interval` мёртв | `_validate_persistence_config` (позитивы) + parse новых ключей цели 3 (`04_snapshot_lifecycle.md`) |
| **C10** | NOTE | `manager/queue.py:747-760,:769-807` | Мёртвый класс `GracefulShutdown` со своими signal-хендлерами, никогда не инстанцируется | Удалить (или документировать) — косметика, низкий приоритет внутри волны |

## 2.3 Волна 3 — структурные (переносятся в `03_sql_integration.md`)

| ID | Sev | Куда | Суть одной строкой |
|---|---|---|---|
| **C4** | RISK (blocker-adjacent) | 3a-recovery | Recovery материализует 1–3 ГБ transient при 10⁶–10⁷ URL (per-line json.loads, seen-set per-file, SkipDecision ~1 КБ) ⇒ OOM на старте → рестарт → петля C1; фикс = streaming + восстановление из SQL (док 03) |
| **F3** | **CONTRADICTION** (канон §0.2.2) | 3b | Twin-wires разнесены на ~46k FIFO-строк ≈ 26 ч ≫ TTL 300 с; LRU 64 МиБ ≈ 200–600 страниц ⇒ каждый distinct wire стоит 4 HTTP (184k вместо 46k); фикс = search-side share ledger с fan-out (вариант A, док 03) |
| **F2** | **CONTRADICTION** | волна 5 | Глобальный FCFS-бюджет + FIFO + threads.search=1 + посев в порядке конфига ⇒ при широком клампе провайдер 1 съедает всё (точный реплей монополизации 2026-09-29); фикс = пропорциональный per-provider бюджет (trailing kwarg сохраняет запиненную сигнатуру `test_rfg_governor.py:10-11`) + ротация владельца в search-share (баланс корзин) |

## 2.4 Волна 6 — web-транспорт (заключительный этап, решение @287@ п.4: «включить, с живыми тестами и замерами»)

**Предусловия волны (до любого `use_api:false`):** II.18 L1 (права 600/700 — ждёт разрешения, OPEN-3) + L3-остаток (паттерн `user_session=` контекстный + пин словаря); фиксы П-B, П-G, П-K′; гигиена флипа (transport в queue identity или drain).

| ID | Sev | Где | Суть | Фикс |
|---|---|---|---|---|
| **П-B** | **CRITICAL blocker** | `client.py:2573-2578,:2667-2669,:440-441`; `tools/state.py:199-207` | Протухшая сессия: sign-in страница (HTTP 200, непустая) → `return ""` → TransientFetchError; ОТЯГОЩЕНИЕ: `mark_success` + early_release СНИМАЕТ cooldown — мёртвая сессия доказывает живость; shadow теряет задачу тихо; `_classify_credential_limit` (:710-724) не знает вида «expired» → ambiguous → ничего не охлаждается | На детекте sign-in: пометить учётку мёртвой + raise `GithubCredentialLimited` напрямую (plumbing существует :2998-2999, rotation catch `definition.py:342-346/:389-393`); подавить `mark_success` для этого контента. Effort S |
| **П-G** | **CRITICAL blocker** | `client.py:752-753,:173-175,:811-812`; `http_signals.py:28,:88-103` | Таксономия отказов на HTML структурно хуже: матчер из одной фразы → ambiguous; тело РЕЖЕТСЯ до 300 символов ДО `is_capacity_refusal` ⇒ abuse-маркер обрезан → 403 уходит в «Authentication failed» → transient; whole-pool secondary cooling (:687-701) практически НЕДОСТИЖИМ на web ⇒ нет тормоза на сотнях часов полноаккаунтных кук | Маркеры по полному контенту; необрезанный вход классификатора; повторный ambiguous на сессию → pool-cool. Effort M |
| **П-K′** | HIGH (blocker-adjacent) | `client.py:2869,:2911,:2917-2944` | `estimate_web_total` при любом сбое возвращает «консервативные» 20 ⇒ `refine=20>100` false И `pagination=20>20` false (`definition.py:406,:465`) → тихий коллапс к page-1-only. (Исходный П-K опровергнут: `get_total_num` :2828-2844 — мёртвый код, экспортирован, ноль вызовов) | Sentinel/типизированный `EstimateUnavailable` → paginate-anyway или DEFER, никогда «маленький total». Effort S |
| **Flip-гигиена** | BLOCKER | `definition.py:103-108` | Queue identity `search:{provider}:{query}:{page}:{regex}` БЕЗ транспорта ⇒ флип `use_api` при живых pending-строках подавляет web-близнецов как дубликаты | Transport в `_generate_id` ИЛИ процедура drain/purge очереди search перед флипом (профиль `queues` из дока 05 — синергия) |
| **П-J** | HIGH | `client.py:2672-2683,:3008-3028` | Нулевой parse ≡ «совпадений нет», ноль счётчиков; единственная tripwire дрейфа разметки | Счётчики `web_pages_parsed_zero/nonzero` + loud-once; ЕДЕТ ВМЕСТЕ с блокерами (это и есть монитор для П-B/П-K′/probe) |
| **П-D** | MEDIUM | `definition.py:315,:362`; `task_queue.py:446-458` | `while True` циклы ротации без дедлайна: k×(60+62)с может пересечь visibility 300 → sweep редоставляет claim без капа → дубль исполнения | Deadline-aware ротация (DEFER до ~visibility−margin); метрика p99 длительности задачи |
| **П-F** | MED-HIGH | `client.py:2563` vs `:2599/:2730`; `:2675/:2681/:3011/:3017` | Web без `s=indexed` (релевантность), `list(set())` недетерминирован, страницы — отдельные задачи минутами позже, возможно другой сессией → дрейф ⇒ дубли (поглощаются) + ТИХИЕ ПРОПУСКИ | Зонд поддержки `s=indexed`; мера прокси пропусков (duplicate rate + yield/total); записать как свойство платформы |
| **П-I** | LOW-MED | `aggregation.py:38,:357-366,:345-355,:363` | TTL web 120 с фиксирован при сохранении, hits не освежают; web хранит ПОЛНЫЙ HTML против 64 МиБ ≈ 40–120 страниц → eviction thrash; twin skew >120 с → ноль экономии | Shadow re-gate для web (W10); сайзинг `ttl_web_s` по измеренному skew; следить `evictions` |
| **П-H остаток** | MEDIUM | `tools/patterns.py:11-21` | Формы `ghp_/github_pat_/gho_` УЖЕ покрыты (коррекция ниже); пробел = `user_session=` (непрозрачные 48ch, НЕ по длине — камень №7 sha256=64hex) + пин словаря | Контекстный паттерн `user_session=[A-Za-z0-9_]+` + пин «каждая форма категории 1 маскируется каждым рендерером» |
| **Пробы** | — | — | Недоверяемые офлайн факты: blackbird `{failed,count,mode}` для `/regex/`; WEB_RESULTS_PER_PAGE=20 против 125 hrefs (regex исключает #L-фрагменты :2673 — возможна дедупликация до ~20 файлов, иначе spurious page tasks + 6.25× over-refinement) | Один гейтовый зонд (1 сессия, несколько запросов) ПЕРЕД обязательствами по partitions-арифметике |

Ёмкость web (справочно, B-аудит): потолок 2.0 req/с (4 сессии × 0.5) → 1200–3600 distinct queries/ч → 10⁶ wires ≈ **280–830 ч**; abuse-экспозиция 540k запросов/сессию за 300 ч; смерть сессии сегодня необнаружима (П-B).

## 2.5 Без действий / только наблюдение

| ID | Почему |
|---|---|
| F6 | Подтверждено: cache-hit НЕ потребляет корзину (`_materialize` мимо `real_fn`); потребляет строку очереди/processed-слот/ротацию токена до диспетчера. Микро-опция «агрегатор до get_token» — отложена |
| F7 | Floor-knob `min_partitions_per_refine ≥37` — **отложен решением оператора** (@287@ п.5) до web-волны; мёртвая зона остаётся собственностью платформы |
| F8 | Верхних границ в валидаторе нет — 46656/200000 проходят; действие не требуется (W4 — страховка от fat-finger) |
| F9 | Замерено: sort+guard при 46k ≈1.4 с CPU/родителя (wire_query 10.3 мкс), 15–20 МБ transient — пренебрежимо на горизонте |
| F10 | `coverage_estimate` при широком клампе = оптимистичная верхняя граница — аннотация спеки (едет с волной 5) |
| C7 | Registry writer queue-full drop → permanent degraded: направление fail-safe (re-gather), монитор `registry_metrics.dropped` |

## 2.6 Коррекции прежних записей (чтобы волны не гонялись за призраками)

1. **P14/II.18-L3 УСТАРЕЛ ЧАСТИЧНО:** `tools/patterns.py:11-21` уже покрывает `AIza/sk-/sk-proj-/anthropic/gsk_/stab_/ghp_{36}/github_pat_/gho_{36}` (проверено лично). Реальный остаток L3 = только `user_session=` контекстный паттерн + пин словаря (П-H остаток, волна 6).
2. **C9 ОПРОВЕРГНУТ лично:** консоль маскируется (все пять Formatter-подклассов fail-closed, `_redact_line` в `format()`); `provider/client.py` НЕ СУЩЕСТВУЕТ; печати заголовков в `provider/*.py` нет. Субагентская галлюцинация, снята до отчёта.
3. **П-K переформулирован:** `get_total_num` мёртв; живой дефект = `estimate_web_total` default-20 (П-K′).
4. **Drain gather:** 345–690k fetch/сутки (корзина 4.0/8, адаптив 0.1×–2×), не «~740k» однозначно.
