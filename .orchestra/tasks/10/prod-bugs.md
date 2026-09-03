# Task #10 — prod bug fixes (post-deploy)

## Bug #1: session-limit retry loop (FIXED)

**Symptom:** Kesha получал «You've hit your session limit · resets 2:20pm (Europe/Berlin)» и
reconnect+retry'ил 2-3 раза — каждый ретрай ловил тот же лимит → спам reconnect'ов.

**Root cause:** `response_stream.py` `ct == "error"` handler: `if "session" in err.lower()` →
reconnect+retry. Session-limit содержит "session" → матчился → бесполезный reconnect (лимит тот же).

**Fix:** `_session_limit_reset(err)` детектор — распознаёт лимит (`hit your ... limit` /
`session limit` / `usage limit`) как НЕ-ретраимый, извлекает время сброса. При лимите → сообщить
юзеру «⏳ лимит сессии (сброс X:XX), жди» и `break` БЕЗ retry. Отличает от транзиентных
`No conversation found` / `process exited` / `connection reset` (те → reconnect как раньше).
Продублировано в outer `except Exception` (если лимит прилетит исключением, не yield).

**Files:** `response_stream.py` (детектор + 2 guard'а), `config.py` (STRINGS `session_limit` ru/en).
**Tests:** `tests/test_session_limit.py` — 6 тестов (лимит с/без reset, usage-лимит, транзиентные
не-лимиты, 'reset' substring не путается с лимитом).

## Bug #2: файлы не ищутся — NOT A BUG (уже пофикшено рестартом)

**Проверено на проде (Contabo):**
- git HEAD = `0b4d5f3 #10-fix` (role-guard fix задеплоен).
- vec.db: files=1298, vec_files=3071, file_chunks=3071, fts_files=3071 (консистентно), schema v8.
  Файловые эмбеддинги НА МЕСТЕ.
- **`rag.search()` вручную возвращает файлы:** «рекомпозиция» → 3 файла вкл. `recomposition-science.md`.
- **Точный путь тула (RO conn + role="user"):** «рекомпозиция силовые» → 4 файла, диалоги
  отфильтрованы по role=user. Работает.

**Вывод:** Kesha получил 0 файлов из поиска, сделанного ДО рестарта 13:57 CEST (fix закоммичен
13:56:59, бот рестартнул 13:57:25). После рестарта поиск файлов работает. Кода менять НЕ нужно.
