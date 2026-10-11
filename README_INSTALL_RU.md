# Кнопка «Когда выходить?» — установка на Google Cloud

Эти изменения рассчитаны на репозиторий `alikpublicenemy/weather-telegram-bot` и Cloud Run Job `telegram-weather-daily`.

## Что делает код

- Добавляет под ежедневное сообщение Telegram кнопку `🚇 Когда выходить?`.
- После нажатия webhook-сервис запрашивает у официального VAG PULS API отправления Herrnhütte и детали конкретной поездки U2 в сторону Röthenbach.
- Подбирает последнюю подходящую поездку, которая позволяет прибыть на Rathenauplatz и дойти до школы к 08:00.
- Учитывает 3 минуты пешком до метро, 2 минуты запаса и 5 минут пешком до школы. Время поездки использует из данных выбранного рейса VAG.

## Файлы

- `send_weather_once.py` — добавляет inline-кнопку к утренней сводке.
- `vag_commute.py` — запросы к VAG PULS и расчёт.
- `commute_service.py` — Telegram webhook на Flask.
- `Dockerfile` и `requirements.txt` — включают webhook-код и его зависимости.

## Почему нужен отдельный сервис

Cloud Run Job завершает работу после отправки утреннего сообщения и не может слушать нажатия. Поэтому нужно создать отдельный Cloud Run **Service**, который постоянно принимает Telegram webhook. Код подготовлен, но само облачное развёртывание ещё требует шагов ниже.

## Развёртывание webhook-сервиса

1. После слияния PR дождись успешной сборки Cloud Build. Используемый образ:
   `europe-west3-docker.pkg.dev/telegram-weather-bot-511313/telegram-bot/weather-telegram-bot:latest`
2. Создай Secret Manager secret `telegram-webhook-secret` с длинным случайным значением. Не используй Telegram bot token как webhook secret.
3. Дай сервисному аккаунту `telegram-bot-runtime@telegram-weather-bot-511313.iam.gserviceaccount.com` роль Secret Manager Secret Accessor только для секретов `telegram-bot-token` и `telegram-webhook-secret`.
4. Создай Cloud Run Service `telegram-commute-webhook` в `europe-west3` с тем же образом. Команда контейнера: `gunicorn`. Аргументы: `--bind 0.0.0.0:8080 commute_service:app`.
5. Настрой переменные и секреты сервиса:
   - `BOT_TOKEN` — Secret Manager `telegram-bot-token`, version `1`.
   - `TELEGRAM_USER_ID` — тот же числовой Telegram ID, что в Cloud Run Job.
   - `WEBHOOK_SECRET` — Secret Manager `telegram-webhook-secret`, version `1`.
   - `TIMEZONE=Europe/Berlin`.
6. Разреши неаутентифицированные HTTP-вызовы к Cloud Run Service, чтобы Telegram мог вызвать webhook. Сам запрос проверяется по заголовку `X-Telegram-Bot-Api-Secret-Token`.
7. После создания сервиса скопируй его URL. Выполни следующий блок в Cloud Shell, подставив URL сервиса. Секреты не публикуй и не отправляй в чат:

```bash
PROJECT_ID="telegram-weather-bot-511313"
SERVICE_URL="ВСТАВЬ_URL_СЕРВИСА_БЕЗ_ЗАКЛЮЧИТЕЛЬНОГО_СЛЭША"
BOT_TOKEN="$(gcloud secrets versions access 1 --secret=telegram-bot-token --project="$PROJECT_ID")"
WEBHOOK_SECRET="$(gcloud secrets versions access 1 --secret=telegram-webhook-secret --project="$PROJECT_ID")"
curl --fail-with-body -X POST "https://api.telegram.org/bot${BOT_TOKEN}/setWebhook" \
  --data-urlencode "url=${SERVICE_URL}/telegram/webhook" \
  --data-urlencode "secret_token=${WEBHOOK_SECRET}" \
  --data-urlencode 'allowed_updates=["callback_query"]'
unset BOT_TOKEN WEBHOOK_SECRET
```

## Проверка

- Открой URL сервиса + `/healthz`; должен вернуться `{"status":"ok"}`.
- Запусти Cloud Run Job вручную после успешной сборки. Под ежедневным сообщением должна появиться кнопка.
- Нажми кнопку и проверь, что бот пришлёт расчёт.
- Если VAG API не вернёт данные, бот покажет сообщение об ошибке и ссылку на VAGstart, а не будет придумывать время.

## Безопасность

Не добавляй токены и пароли в репозиторий. Webhook проверяет секретный заголовок Telegram и разрешает расчёт только заданному `TELEGRAM_USER_ID`.
