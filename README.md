# NFT Gift Hunter

Telegram-бот и юзербот: ищет новичков с NFT-подарками и шлёт карточки в админку.

## Локально

Скопируй `.env.example` → `.env`, заполни токен и `API_ID` / `API_HASH` / `PHONE`.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python run.py
```

Потом открой бота в Telegram и нажми **/start**.

## Bothost

Главный файл: `main.py`. Включи **свой Dockerfile**.

Переменные окружения:

- `BOT_TOKEN`
- `API_ID` / `TELEGRAM_API_ID`
- `API_HASH` / `TELEGRAM_API_HASH`
- `PHONE`
- `ADMIN_IDS`
- `SESSION_STRING` (локально: `python gen_session.py`)

`PROXY_*` на хостинге не ставь.
