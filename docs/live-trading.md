# Live: Polymarket CLOB V2 / pUSD

Проверено по официальной документации 13 сентября 2026 года:

- [Миграция V2](https://docs.polymarket.com/v2-migration)
- [Контракты](https://docs.polymarket.com/resources/contracts)
- [Python V2 SDK](https://github.com/Polymarket/py-clob-client-v2)
- [Session Keys](https://docs.polymarket.com/trading/session-keys)

Production: `https://clob.polymarket.com`. SDK: `py-clob-client-v2==1.1.0`.
Торговый collateral — pUSD, 6 знаков. Имена `LIVE_*_USDC` сохранены для
совместимости, но значения означают pUSD. V1 SDK больше не подходит.

| Назначение | Контракт Polygon |
| --- | --- |
| pUSD | `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB` |
| Exchange V2 | `0xE111180000d2663C0091e4f400237545B87B996B` |
| Neg Risk Exchange V2 | `0xe2222d279d744050d28e00520010520000310F59` |

Диагностика получает адреса из закреплённого SDK. Разрешение старому Exchange
не заменяет разрешение новому. Нулевой ответ V1 не доказывает отсутствие денег:
сначала проверьте V2, funder и тип кошелька; не пополняйте его на основании
старой диагностики.

## Кошелёк

`POLYMARKET_PRIVATE_KEY` — ключ подписанта; `POLYMARKET_FUNDER` — адрес,
где лежит pUSD. Не публикуйте ключ или .env.

| SIGNATURE_TYPE | Кошелёк |
| --- | --- |
| 0 | EOA: подписант и владелец средств — один адрес |
| 1 | Старый POLY_PROXY |
| 2 | Gnosis Safe |
| 3 | Deposit Wallet / POLY_1271 |

Тип определяется устройством кошелька, а не перебором до ненулевого баланса.
Вход через браузер/email сам по себе не определяет тип. V2 SDK формирует подписи
POLY_1271: funder должен быть Deposit Wallet, подписант — разрешён кошельком.

Session Key — отдельный авторизованный подписант Deposit Wallet. Это не Relayer
API key и не произвольный новый ключ. Авторизация сессии — отдельный процесс,
см. документацию Session Keys. Бот не создаёт и не авторизует сессии автоматически.
Поддержка формата 3 и ненулевой баланс не доказывают права конкретной сессии.
Ключ не нужно менять только из-за перехода на V2: L1/L2 auth при миграции сохранена.

## Обновить live на Ubuntu

Команды выполняются в каталоге отдельного live-экземпляра:

```bash
cd ~/polylive
docker compose -p polylive stop bot
git pull --ff-only
docker compose -p polylive build bot
```

Сохраните свой ключ, funder и Telegram token. В .env проверьте:

```ini
TRADING_MODE=live
LIVE_CONFIRM=I_UNDERSTAND_REAL_MONEY
POLYMARKET_CLOB=https://clob.polymarket.com
POLYGON_CHAIN_ID=137
LIVE_DRY_RUN=true
LIVE_ORDER_TYPE=FAK
LIVE_MAX_ORDER_USDC=2
```

`POLYMARKET_SIGNATURE_TYPE` задайте по таблице. Не переносите автоматически
старый тип 1/2 на Deposit Wallet. Проверка при работающем сервисе db:

```bash
docker compose -p polylive run --rm --no-deps bot python -m app.live_check
# Необязательно: добавить token ID исхода последним аргументом.
```

Проверка подписывает API-auth challenge/получает API credentials, но не отправляет
торговых ордеров и не меняет allowance. Поля: `pusd_balance`,
`collateral_contract`, `exchange_contract`, `neg_risk_exchange_contract`,
`exchange_allowance`, `blocking_problems`.
`ready_for_live` означает прохождение этих проверок; реальная подпись ордера
и on-chain settlement этим ещё не испытаны. Отчёт можно прислать без ключей.

Запуск dry-run:

```bash
docker compose -p polylive up -d --build bot
docker compose -p polylive logs --since=5m -f bot
```

Dry-run подписывает, но не публикует ордера. Для реальной торговли установите
`LIVE_DRY_RUN=false` и пересоздайте контейнер той же командой. Проверьте первый
fill и баланс на Polymarket. База при обновлении сохраняется.

## Новый экземпляр рядом с paper

```bash
git clone https://github.com/TakatoSey/polysey.git ~/polylive
cd ~/polylive
cp .env.example .env
nano .env
```

Нужны отдельный Telegram-бот/token и проект `-p polylive`. Укажите согласованные
`POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` и `DATABASE_URL` с хостом
`db`. Compose создаёт свою сеть и том. Общая БД paper/live запрещена проверкой
владельца. Credentials PostgreSQL применяются при первой инициализации тома.
Не используйте `down -v` для обновления — это удаление базы.

```bash
docker compose -p polylive up -d db
docker compose -p polylive build bot
docker compose -p polylive run --rm --no-deps bot python -m app.live_check
```

## Исполнение и границы проверки

Баланс читается из CLOB в pUSD, позиции — из Data API для funder. V2 SDK получает
параметры комиссии и уменьшает BUY внутри бюджета через `user_usdc_balance`
(старое название поля самого SDK). Fill может быть меньше бюджета из-за
округления, цены и ликвидности.

FAK исполняет доступную часть, отменяет остаток; FOK требует полного исполнения.
Ответ о matching и окончательный on-chain settlement — разные этапы. Неизвестное
исполнение остаётся для сверки. Лимитная цена не заменяет фактическую цену сделки.

Auto-redeem зависит от кошелька и настроек Polymarket. Бот не выполняет
wrap/approve/redeem при диагностике. Реальный кошелёк пользователя из среды
разработки не проверялся.
