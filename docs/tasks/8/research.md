# RAG апгрейд #8 — выбор модели (research + бенчмарк)

**Задача:** Кеша переехала на Contabo (8GB RAM, ~6GB free). Раньше на Timeweb (2.9GB) стояла компромиссная **e5-small int8** (качество 4.3/5, OOM не давал большего). Теперь RAM позволяет — ставим лучшую модель для русского семантического поиска.

**Кандидаты по задаче:** e5-large int8 (фаворит из прошлого ресёрча), bge-m3, что-то новее под ruMTEB.

---

## TL;DR — ВЕРДИКТ

🏆 **Победитель: `bge-m3` (int8 ONNX, single-file) — репо `AlpEge/bge-m3-onnx-int8`**

| Критерий | e5-small (текущая) | e5-large-int8 | **bge-m3-int8** |
|----------|--------------------|--------------|-----------------|
| **Разделение сигнал/шум** (avg margin cos_rel−cos_dist) | +0.055 | +0.063 | **+0.237** 🏆 |
| **Худший случай** (min margin) | +0.028 | +0.039 | **+0.180** 🏆 |
| recall@5 (677 msgs, 6 контр-запросов) | 4/6 | 4/6 | 4/6 (насыщен) |
| dim | 384 | 1024 | 1024 |
| Модель на диске | 118MB | 561MB | 570MB |
| **Процесс RSS** (весь python+ort) | 665MB | 1466MB | **1826MB** |
| Query latency (поиск) | 3ms | 18ms | **22ms** |
| Passage latency (индексация, фон) | 43ms | 382ms | **577ms** |
| Пулинг | MEAN | MEAN | **CLS** |
| E5-префиксы query:/passage: | да | да | **нет** |

**Почему bge-m3, а не e5-large (прошлый фаворит):** оба int8 влезают в RAM с запасом. Но bge-m3 **в ~4x лучше разделяет релевантное от мусора** на абстрактных русских запросах. У E5 известная анизотропия — все косинусы сжаты в 0.75–0.85 (даже мусор 0.78), margin крошечный. bge-m3 растягивает 0.27–0.74, margin в разы больше → **надёжнее ранжирование на худших кейсах** (еда/КБЖУ, психология — там где и были жалобы 1.5–2/5). RAM-цена (+360MB против e5-large) на 8GB несущественна.

---

## Метод бенчмарка

- **Данные:** боевой `messages.db` с Contabo (677 непустых сообщений), read-only копия в `/tmp/rag-bench/` — прод vec.db НЕ тронут.
- **Идентичный пайплайн** для всех моделей: chunking (>1200 симв), hybrid vec+FTS5, RRF fusion, prefix-expansion. Меняется ТОЛЬКО embedding-модель. Скрипты: `/tmp/rag-bench/{bench,cosine,rss,lat}.py`.
- **Паритет окружения:** локально fastembed 0.8.0 + Python 3.12 = как на VPS. VPS CPU = AVX2 (не AVX512-VNNI).
- **Две метрики:**
  1. `recall@5` через полный пайплайн (in-memory vec+fts) — но на 677 msgs **насыщается** (все модели 4/6, 2 «промаха» = дефекты ground-truth/чанкинга, не модели).
  2. **cosine separation margin** = `cos(query, релевантное) − cos(query, дистрактор)` на 5 абстрактных триплетах реальным языком юзера — **вот это дискриминирует модели**. Именно этим прошлый ресёрч ловил провал MiniLM.

## Измерения — cosine margin (главный сигнал)

```
e5-small:      avg +0.055  min +0.028   (rel 0.84–0.87, dis 0.78–0.82 — узко)
e5-large-int8: avg +0.063  min +0.039   (rel 0.80–0.85, dis 0.75–0.77 — узко)
bge-m3-int8:   avg +0.237  min +0.180   (rel 0.47–0.74, dis 0.27–0.33 — широко)
```

Все триплеты у всех моделей `margin > 0` (никто не путает сигнал с шумом на топ-1). Но **ширина запаса** решает устойчивость на реальных зашумлённых запросах: bge-m3 min-margin +0.180 против e5 +0.03 — на порядок надёжнее там, где косинусы плывут.

Триплеты (query → релевантное vs дистрактор), реальные формулировки юзера:
- «ссора с девушкой» → «Катя заебала, расстаться» vs «макароны с курицей»
- «настройки AI промпт» → «настройки модели opus max_turns effort» vs «затычка для носа на озоне»
- «что ел КБЖУ» → «крыло картошку 2 яйца макароны молока» vs «проблемы с VPN»
- «парсер маркетплейса» → «Ozon парсер скрейпинг товаров» vs «психолог схемотерапия»
- «психологическое состояние» → «схемотерапия домини три схемы» vs «деплой seedon упал»

## RAM бюджет (VPS Contabo, замерено)

- Всего 7941MB, **available 6192MB** (2.1GB в reclaimable buff/cache).
- Текущий бот (e5-small): 665MB. Все kesha-процессы (бот+CLI+MCP): **1279MB**.
- Проекция с bge-m3-int8 (delta +1160MB к процессу бота):
  - kesha total ≈ **2440MB** + будущий Ozon Playwright (~350MB) ≈ **2790MB**
  - Headroom против available ≈ **3.4GB** ✅ комфортно, без риска OOM.
- Для сравнения: старый Timeweb был 2.9GB total — bge-m3 туда бы НЕ влез (отсюда исторический OOM). На Contabo — влезает с запасом.

## Latency (замерено, AVX2-класс CPU)

- **Query (поиск, юзер ждёт):** bge-m3 22ms — незаметно (было 3ms на e5-small).
- **Passage (индексация, фоновый rag_executor):** bge-m3 577ms/chunk vs e5-small 43ms — ~13x, НО асинхронно, не блокирует ответ бота. На 2 юзера невидимо.
- **Backfill 677 msgs:** ~6–8 мин однократно на VPS. RAG временно неполный во время backfill — по задаче допустимо (простой RAG, не бота).

---

## КРИТИЧЕСКИЕ находки для имплементации

1. **⚠️ Модель ОБЯЗАНА быть single-file `model_quantized.onnx`.** FastEmbed 0.8.0 + onnxruntime в этом окружении **падает** на моделях с внешним `model.onnx_data` (fp32 split-weights): `External data path escapes model directory`. Так упали и нативный `intfloat/multilingual-e5-large` (fp32), и `BAAI/bge-m3` (fp32). Работают только self-contained int8-репо (`keisuke-miyako/...e5-large`, `AlpEge/bge-m3-onnx-int8`).
2. **⚠️ MODEL_NAME НЕ должен совпадать с нативным именем FastEmbed.** Если `MODEL_NAME="intfloat/multilingual-e5-large"` — guard `if MODEL_NAME not in supported_models` пропускает `add_custom_model`, и FastEmbed берёт нативный fp32-репо (→ падение из п.1). Решение: MODEL_NAME = имя кастомного репо (`AlpEge/bge-m3-onnx-int8`), тогда регистрация срабатывает.
3. **⚠️ bge-m3 ≠ E5 по конфигу embeddera:**
   - Пулинг **CLS** (не MEAN). В `add_custom_model(pooling=PoolingType.CLS)`.
   - **НЕТ** префиксов `query: `/`passage: `. Текущий код добавляет их по `if "e5" in MODEL_NAME` — для bge-m3 этот branch НЕ сработает (в имени нет "e5"), т.е. префиксы не добавятся автоматически. Проверить что условие корректно исключает bge-m3.
   - Нормализация True (как E5).
4. **int8 bge-m3 quality caveat:** есть репорты что int8-квантизация bge-m3 даёт заметный дрейф векторов vs fp32 (форумы HF). НО: (а) fp32 у нас не грузится вообще (п.1), (б) наш замер margin +0.237 сделан именно на int8-репо и он всё равно кардинально лучше E5 — квантизация не убила преимущество.

## Файлы под изменение (для плана)

| Файл | Изменение |
|------|-----------|
| `rag.py` | MODEL_NAME/MODEL_HF/MODEL_FILE → bge-m3-int8; DIM 384→1024; пулинг CLS в add_custom_model; убрать/скорректировать E5-префиксную ветку; SCHEMA_VERSION 6→7 (дроп+ребилд) |
| `tests/test_rag.py` | обновить DIM, проверки; префиксная логика |

## Риски / edge cases

- **Ребилд индекса:** SCHEMA_VERSION 6→7 дропнет vec.db (dim 384≠1024) → backfill переэмбедит 677 msgs на bge-m3. `messages.db` НЕ трогается (только read-only join). Простой RAG ~6-8 мин.
- **CLS-пулинг + отсутствие префиксов** — если перепутать (оставить MEAN или добавить E5-префиксы к bge) — качество просядет молча. Явно протестировать после деплоя на контр-запросах.
- **Холодная загрузка** модели ~10-15с (lazy, не блокирует старт бота).
- **Chunking-артефакт** (еда/КБЖУ промах): контент в длинном голосовом-чанке размывается — общий для всех моделей, отдельная тема, не блокер этой задачи.
- **anti-паттерн порога:** НЕ вводить абсолютный порог cosine. bge-m3 косинусы ниже E5 (0.47 для релевантного) — порог бы всё зарезал. Только top-K + RRF-ранги (уже так).

## Confidence

- **Выбор bge-m3-int8: CONFIRMED** — измерено на боевых данных, 4x margin, RAM/latency в бюджете, single-file оннх грузится (проверено EXIT 0).
- **RAM-бюджет: CONFIRMED** — замеры RSS локально + боевой footprint с VPS, паритет окружения.
- **int8 quality vs fp32: LIKELY достаточно** — fp32 недоступен для прямого сравнения в нашем стеке, но int8 всё равно бьёт E5 с запасом.

## Источники

- [AlpEge/bge-m3-onnx-int8 (single-file model_quantized.onnx, 570MB)](https://huggingface.co/AlpEge/bge-m3-onnx-int8)
- [keisuke-miyako/multilingual-e5-large-onnx-int8 (561MB, протестирован)](https://huggingface.co/keisuke-miyako/multilingual-e5-large-onnx-int8)
- [HF forum: int8 bge-m3 vector drift vs fp32](https://discuss.huggingface.co/t/difference-in-the-vector-generated-by-the-int8-quantized-model-vs-base-onnx-model/164270)
- [ruMTEB — Russian embedding benchmark (bge-m3 и e5-large топ non-instruct)](https://arxiv.org/abs/2408.12503)
- Прошлый ресёрч: `docs/tasks/rag-memory-v2/research.md` (E5 vs MiniLM, анизотропия, chunking, FTS5)
- Собственные прогоны: `/tmp/rag-bench/` (bench.py recall, cosine.py margin, rss.py, lat.py), fastembed 0.8.0, sqlite-vec, боевой messages.db 677 msgs.
```
