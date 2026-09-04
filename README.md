# PolyCopy

Однопользовательский Telegram-бот для paper-копитрейдинга Polymarket.

## Что есть сейчас

- live Data API для обнаружения исполненных сделок лидеров;
- live CLOB WebSocket с REST fallback для симуляции исполнения;
- FAK market execution с глубиной книги и partial fills;
- актуальный fee schedule рынка из Gamma API с консервативным fallback;
- контроль slippage относительно цены сделки лидера;
- проверка минимального размера CLOB-ордера;
- paper-позиции, баланс, ордера и история;
- пропорциональные продажи относительно позиции лидера;
- stop-loss, take-profit и trailing-stop;
- аварийные `/pause` и `/resume`;
- PostgreSQL и восстановление состояния после перезапуска.

Реальные ордера, приватные ключи, CLOB-аутентификация и кошельки намеренно не подключены.

## Запуск на VPS

```bash
git clone <репозиторий> polycopy
cd polycopy
cp .env.example .env
nano .env
docker compose up -d --build
docker compose logs -f bot
```

В `.env` укажи токен Telegram-бота и свой numeric Telegram ID. Секреты не отправляй в чат.

## Команды

`/start`, `/status`, `/settings`, `/leaders`, `/addleader 0x...`, `/removeleader 0x...`, `/portfolio`, `/orders`, `/risk TOKEN sl=0.2 tp=0.25 trail=0.1`, `/setsize 5`, `/setmax 10`, `/setslippage 5`, `/pause`, `/resume`.

Первый лидер из `.env.example` добавляется автоматически, но его адрес можно изменить в `.env`.
