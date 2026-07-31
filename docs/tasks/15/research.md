# Research #15 — почему банят Claude-аккаунты Александра и как поднять Кешу

Дата: 2026-07-31. Все URL ниже открыты в этой сессии.

## Вопрос (Step 0)

- **Контекст:** клиент в РФ, 5 забаненных аккаунтов Claude подряд, схема «покупная NL-почта → регистрация с телефона под VPN NL → сразу оплата Pro → запуск Кеши с компа под VPN» → бан через 15–20 минут.
- **Проверяемая гипотеза (Максима):** причина — датацентровый IP (Marzban/VLESS на арендованном VPS), нужен резидентный выход.
- **Baseline:** альтернативные объяснения — geo-политика Anthropic, платёж, поведение сразу после регистрации.
- **Измеримый исход:** аккаунт живёт > 20 минут под нагрузкой Кеши.

## Гипотезы и фальсификаторы (Step 1)

| # | Гипотеза | Что бы её опровергло |
|---|---|---|
| H1 | Виноват датацентровый ASN | Наличие людей, годами работающих с VPS-IP без бана |
| H2 | Виновата **страна проживания** — РФ вне Supported Regions, и VPN этого не лечит | Наличие РФ в списке поддерживаемых стран |
| H3 | Виноват платёж (перекуп/крипто-карта/geo-mismatch) | Баны при чистой оплате своей картой |
| H4 | Виновата автоматизация (Agent SDK) с нового аккаунта | Официальное разрешение Agent SDK на подписке |

**Итог: H1 и H2 подтверждены и складываются, H3 подтверждена как усилитель, H4 ОПРОВЕРГНУТА.** Ни одна гипотеза не является единственной причиной — сработала комбинация.

---

## F1. Россия отсутствует в списке поддерживаемых стран — CONFIRMED

**Тир: primary source (открыт).** [anthropic.com/supported-countries](https://www.anthropic.com/supported-countries) — России в списке нет. Нидерланды, Германия, Казахстан, Грузия, Армения, Сербия, Турция — есть. Для Украины отдельная оговорка про оккупированные территории.

Consumer ToS ([anthropic.com/legal/terms](https://anthropic.com/legal/terms), §3) прямо подчиняет доступ «Supported Regions Policy»:

> "You may access and use our Services only in compliance with our Terms, including our Acceptable Use Policy, the policy governing the countries and regions Anthropic currently supports ("Supported Regions Policy")"

**Это ключевой сдвиг диагноза.** Гипотеза Максима (H1) верна, но она — *вторая* по важности. Первая: аккаунт из неподдерживаемой страны нарушает ToS сам по себе, независимо от качества IP. VPN не делает использование легитимным — он лишь маскирует его.

**Важное следствие для честности гайда:** это значит, что гарантии не существует в принципе. Мы снижаем вероятность срабатывания антифрода, но не устраняем базовое несоответствие.

### Что НЕ подтвердилось
В ToS **нет явного пункта**, запрещающего VPN/прокси для обхода гео-ограничений (проверено запросом по тексту). Утверждения «Anthropic прямо запрещает VPN» в блогах — вольный пересказ. Честная формулировка: запрещено *использование из неподдерживаемого региона*, а не *VPN как технология*.

## F2. Датацентровый ASN — реальный фактор риска, но детали от вендоров ненадёжны — LIKELY

**Тир: multi-secondary + понимание механики; НЕ измерено.**

Механика Cloudflare (перед Anthropic стоит именно он): первым делом — репутация IP по ASN, до всякого JS. Датацентровые ASN (AWS, GCP, Hetzner, OVH, DigitalOcean) — низкий базовый траст; резидентные ISP — базовый; мобильные — высший ([proxies.sx](https://www.proxies.sx/blog/why-cloudflare-blocks-residential-proxies-mobile-ips-difference), [torchproxies](https://torchproxies.com/datacenter-vs-residential-proxies-2026/)).

⚠️ **Counter-evidence и предвзятость источников.** Практически все источники по этой теме — блоги продавцов прокси (QuarkIP, IPOASIS, NSTBrowser, AoxVPN, ProxyHorizon, apiyi, qcode). У них прямой коммерческий интерес в выводе «покупай резидентный IP». Конкретные проценты успеха (60–90% / 20–40%) я считаю **маркетинговыми, не проверенными**.

⚠️ **Прямое контр-свидетельство H1:** в тех же источниках встречается рекомендация для Claude Code на VPS использовать **стабильный датацентровый IP без VPN**, и сообщение оператора о месяцах работы без проблем. Это согласуется с нашим собственным фактом: **Кеша Максима крутится на Contabo (датацентровый IP, Франция) и живёт.** То есть датацентровый IP сам по себе НЕ приговор.

**Отсюда уточнённый вывод:** решает не «датацентр vs резидент» в вакууме, а **стабильность и связность**. Хуже всего — прыгающий VPN и рассинхрон «IP одной страны / карта другой / почта третьей». Постоянный один IP в поддерживаемой стране работает и на VPS.

## F3. Свежий аккаунт + мгновенный платёж + сразу автоматизация — CONFIRMED как рисковый паттерн

**Тир: multi-secondary, согласуются между собой + косвенно первичный (Transparency Hub упоминает автоматические детекты).**

Совпадающие у независимых источников сигналы: смена/сбой платёжного метода, несколько аккаунтов на одной карте (описывается как zero-tolerance), 24/7 запросы без пауз, RPS выше человеческого, регистрация с датацентрового IP. Баны сразу после апгрейда плана или смены платёжки — частая жалоба ([autonomee.ai](https://autonomee.ai/blog/claude-code-account-suspended-banned-safe-usage/), [knightli.com](https://knightli.com/en/2026/05/09/claude-account-suspension-code-limit-guide/)).

**Схема Александра собирала почти все флаги одновременно:** купленная (уже «засвеченная») почта, датацентровый IP, платёж в первые минуты жизни аккаунта, затем немедленная API-нагрузка от бота. Бан за 15–20 минут — это ровно профиль автоматического риск-скоринга, а не ручной модерации.

## F4. Agent SDK на подписке — РАЗРЕШЁН, отдельного кредитного пула НЕТ — CONFIRMED (и это опровергает H4)

**Тир: primary source (открыт).** [support.claude.com — Use the Claude Agent SDK with your Claude plan](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan):

> "We're pausing the changes to Claude Agent SDK usage described below. For now, nothing has changed: Claude Agent SDK, `claude -p`, and third-party app usage still draw from your subscription's usage limits."
> "The previously announced monthly credit ... isn't available."

**Здесь я едва не ошибся, и это стоит зафиксировать.** Первый поиск уверенно выдал: «с 15 июня 2026 Agent SDK уходит из подписки в отдельный пул $20/$100/$200». Это было объявлено 14 мая 2026 — и **отменено в день вступления в силу**. Если бы я не открыл первоисточник, весь раздел про тариф был бы неверным.

Следствие: Кеша на подписке — легитимный сценарий, отдельно платить за SDK не нужно. Утверждения блогов «с апреля 2026 подписка не покрывает сторонние харнессы» — про OAuth-токены в чужих харнессах (OpenCode, Cline, Roo), а не про собственный Agent SDK.

## F5. Лимиты тарифов и потребление Кеши — цифры приблизительные

**Primary (открыт):** [claude.com/pricing](https://claude.com/pricing) — Pro $20/мес ($17 при годовой), Max «5x or 20x more usage than Pro», от $100/мес. Точные числа Anthropic **не публикует**; официальная статья про Pro/Max ограничивается фразой «usage limits are shared across Claude and Claude Code».

**Secondary (оценки, не гарантия):** ~45 промптов / 5 ч на Pro, ~225 на Max 5x, ~900 на Max 20x; 6 мая 2026 5-часовые лимиты удвоены; поверх — недельные потолки ([morphllm](https://www.morphllm.com/claude-code-usage-limits), [truefoundry](https://www.truefoundry.com/blog/claude-code-limits-explained)). Мера — токены, не сообщения. Пул общий с веб-чатом.

**Замеры по коду репозитория (тир: прямое чтение исходников):**
- `config.py:18` — `MODEL = "claude-opus-5"` по умолчанию. Opus — самый дорогой по лимитам.
- `config.py:23` — `AUTO_COMPACT_PCT = 95`.
- `chat_state.py:30` — `PREVENTIVE_COMPACT_MIN_CTX = 20.0`, `PREVENTIVE_IDLE_MINUTES = 55`. Превентивный компакт = дополнительный вызов модели в простое.
- `reminders.py` — тип `urgent_llm` будит модель по расписанию **без участия пользователя**.
- `rag.py` — RAG-поиск локальный (bge-m3), лимиты Anthropic не тратит. Но найденные чанки идут в контекст → удорожают каждый ход.

**Вывод (обоснованный, но не измеренный на живом аккаунте — UNCERTAIN в части точных цифр):** персональный ассистент с Opus по умолчанию, длинным системным промптом, RAG-контекстом, авто-компактами и фоновыми напоминаниями — это не «45 коротких промптов в чате». Один ход Кеши весит кратно больше чата. Pro реалистичен только при переводе на Sonnet и лёгком использовании; для Opus-ассистента базой следует считать Max 5x.

## F6. Оплата из РФ — CONFIRMED в части «российские карты не проходят»

**Тир: multi-secondary, единодушны.** Карты российских банков (Visa/MC/МИР) не принимаются с 2022. Рабочие пути: зарубежная виртуальная карта с пополнением по СБП; посредник, оплачивающий **на ваш** аккаунт; готовый/общий аккаунт.

**Про «ForgetShop, Claude Pro 1990 ₽, БЕЗ ГАРАНТИИ НА БАН».** Официально Pro = $20 (~1650 ₽). 1990 ₽ ≈ цена подписки + комиссия. Категории принципиально разные, и надпись «без гарантии» — честное признание, что продаётся третья:
1. посредник даёт реквизиты карты, вы платите сами на свой аккаунт — контроль у вас;
2. посредник заходит в ваш аккаунт — надо менять пароль и включать 2FA после;
3. **готовый/общий аккаунт — почта не ваша**, восстановление через поддержку невозможно, лимиты делятся, переписки видны другим, продавец может сменить пароль.

Дополнительный риск: крипто-карты и виртуалки с криптобирж чаще отклоняются Stripe и отмечены в жалобах как часть триггера бана. ToS §13: при расторжении за существенное нарушение **возврата средств не будет** —

> "If we terminate your access to the Services due to a material breach of these Terms and you have a Subscription: you will not be entitled to any refund"

Это делает дешёвый общий аккаунт особенно плохой сделкой: деньги не возвращаются.

## F7. KYC у хостеров — CONFIRMED

**Тир: primary (справка Contabo).** [help.contabo.com — Why do I need to verify my purchase?](https://help.contabo.com/en/support/solutions/articles/103000348466-why-do-i-need-to-verify-my-purchase-) — требуются скан паспорта/прав/ID **и** счёт за коммуналку/телефон с именем и адресом. Проверка запускается **после оплаты**.

Важные детали:
- Проверка **не универсальна** — она fraud-score-driven. Рассинхрон (страна прокси ≠ страна карты) её провоцирует. Давние клиенты сообщают, что их не просили.
- Заказ **на компанию** часто закрывает вопрос корпоративными документами (сообщения на LowEndTalk по Contabo, OVH, Hetzner, Netcup).
- Замазанные/водянознаковые сканы **отклоняют**.
- **Hetzner больше не запасной путь:** с марта 2026 в регистрацию встроен iDenfy — госдокумент + селфи с проверкой живости ([financialcontent](https://markets.financialcontent.com/ms.intelvalue/article/marketersmedia-2026-3-20-idenfy-teams-up-with-hetzner-to-improve-kyc-conversions-through-its-ai-powered-identity-verification-solution)).

**Без-KYC провайдеры существуют легально** (ExtraVM, Cloudzy, AnubizHost, ISHosting, PQ.Hosting и др.), оплата криптой/картой.
⚠️ **Counter-evidence, который вендоры не афишируют:** часть «без-KYC» хостеров принимает крипту через шлюзы (CoinGate, BVNK), у которых **свой KYC**. Проверять надо платёжный шлюз, а не лендинг. И «нет KYC» ≠ анонимность: IP входа в панель логируется.

## F8. Кеша на Windows — требования из репозитория

**Тир: прямое чтение файлов.** `README.md:120–124` — Python 3.11+, ffmpeg (для видеокружков). `requirements.txt` — aiogram≥3.28, claude-agent-sdk≥0.2.128, sqlite-vec, fastembed≥0.8, watchfiles. Плюс сам `claude` CLI (Node) и `.env` с `TELEGRAM_BOT_TOKEN`. Есть `setup_wizard.py` для интерактивной настройки и `kesha-bot.service` (systemd-юнит — под Linux).

Практические следствия для WSL2 (гипотеза на основе состава зависимостей, **не проверено запуском на Windows-машине — UNCERTAIN**):
- fastembed тянет ONNX-модель bge-m3; по замерам из CLAUDE.md бот с ней держит RSS ~1.25 ГБ → WSL2 надо ограничивать/выделять память осознанно.
- systemd в WSL2 доступен не по умолчанию (нужен `systemd=true` в `/etc/wsl.conf`).
- **Бот живёт, только пока включён компьютер и запущен WSL.** Напоминания и `urgent_llm` при выключенном ноуте не сработают. Это принципиальный минус против VPS, и его надо назвать прямо.

---

## Ответ на исходный вопрос

Причина банов — **не один фактор, а стек**, и главный из них не тот, что предполагался:

1. **РФ вне Supported Regions** (F1, primary) — базовое нарушение, VPN его не устраняет.
2. **Датацентровый прыгающий VPN** (F2) — усилитель, но стабильный DC-IP сам по себе рабочий (Кеша Максима на Contabo — живое доказательство).
3. **Свежий аккаунт → платёж за минуты → сразу бот** (F3) — классический профиль автоматического риск-скоринга.
4. **Покупная почта и мутный платёж** (F3, F6).

Гипотеза Максима подтвердилась частично и требует уточнения: важна **стабильность и связность гео-сигналов**, а не «резидентный IP любой ценой».

## Риски и края

- Гарантии нет и быть не может — при базовом несоответствии региона любой аккаунт остаётся уязвим. Обещать «больше не забанят» в гайде **нельзя**.
- Точные лимиты не публикуются → цифры по тарифам подавать как ориентиры со ссылкой на `/usage`.
- Все вендорские проценты по прокси — маркетинг, в гайд не тащить.
- KYC у Contabo обходить подделкой документов недопустимо; варианта два — настоящие документы или другой хостер.

## Источники (открыты в этой сессии)

1. https://www.anthropic.com/supported-countries
2. https://anthropic.com/legal/terms
3. https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan
4. https://support.claude.com/en/articles/11145838-using-claude-code-with-your-pro-or-max-plan
5. https://claude.com/pricing
6. https://help.contabo.com/en/support/solutions/articles/103000348466-why-do-i-need-to-verify-my-purchase-
7. https://www.morphllm.com/claude-code-usage-limits
8. https://www.truefoundry.com/blog/claude-code-limits-explained
9. https://autonomee.ai/blog/claude-code-account-suspended-banned-safe-usage/ (вендорский, тир 4)
10. https://knightli.com/en/2026/05/09/claude-account-suspension-code-limit-guide/ (вендорский, тир 4)
11. https://www.proxies.sx/blog/why-cloudflare-blocks-residential-proxies-mobile-ips-difference (вендорский, тир 4)
12. https://torchproxies.com/datacenter-vs-residential-proxies-2026/ (вендорский, тир 4)
13. https://github.com/anthropics/claude-code/issues/51583 (кейс бана при корпоративном VPN)
14. https://markets.financialcontent.com/ms.intelvalue/article/marketersmedia-2026-3-20-idenfy-teams-up-with-hetzner-to-improve-kyc-conversions-through-its-ai-powered-identity-verification-solution
15. https://vc.ru/exnode/2915289-oplata-claude-iz-rossii (обзор, тир 4)
16. https://habr.com/ru/companies/alpinadigital/articles/1049150/ (разбор банов, тир 4)

Локальные (тир 1 — прямое чтение): `config.py`, `chat_state.py`, `compact.py`, `reminders.py`, `requirements.txt`, `README.md`.
