Личный Telegram-бот с автономной ежедневной погодой (GitHub Actions)

Что изменилось
- GitHub Actions запускает отдельный скрипт ежедневно в 07:07 по времени Europe/Berlin.
- Скрипт берёт свежую погоду из Open-Meteo, отправляет одно сообщение в Telegram и завершает работу.
- Компьютер не должен быть включён.
- GitHub может иногда запускать расписание с задержкой; это не система для доставки строго в секунду.
- Этот сценарий отвечает только за ежедневную сводку. Команды /start, /weather и /help доступны, когда отдельно запущен bot.py. Для круглосуточных команд потребуется постоянно работающий хостинг.

Часть 1. Создай приватный репозиторий GitHub
1. Войди или зарегистрируйся на https://github.com
2. Нажми + (вверху справа) -> New repository.
3. Назови репозиторий weather-telegram-bot и выбери Private.
4. Не добавляй README, .gitignore или лицензию на GitHub при создании. Нажми Create repository.

Часть 2. Загрузи файлы через GitHub Desktop (сохраняет и папку .github)
1. Установи GitHub Desktop: https://desktop.github.com/ и войди в тот же аккаунт.
2. В GitHub Desktop нажми File -> Clone repository, выбери вкладку GitHub.com, выбери weather-telegram-bot и нажми Clone.
3. Открой распакованную папку weather_telegram_bot_cloud из этого архива.
4. Скопируй ВСЕ файлы и папку .github из неё в папку клонированного репозитория. Если Windows спросит, подтверди копирование. Не копируй файл .env, если он у тебя есть; он не должен попадать на GitHub.
5. Вернись в GitHub Desktop. Внизу слева в Summary напиши Initial upload, нажми Commit to main, затем Push origin.
6. На сайте GitHub обнови страницу репозитория и проверь, что видны bot.py, send_weather_once.py, requirements.txt и папка .github/workflows.

Часть 3. Сохрани секреты (самое важное)
В репозитории на GitHub открой Settings -> Secrets and variables -> Actions -> New repository secret.
Создай два секрета:
- Name: BOT_TOKEN     Value: токен от @BotFather
- Name: TELEGRAM_USER_ID   Value: твой числовой Telegram ID
Не добавляй кавычки вокруг значений. Не отправляй токен в чат и не вноси его в файлы проекта.

Часть 4. Проверь вручную
1. Открой вкладку Actions репозитория. Если GitHub попросит включить Actions, нажми Enable.
2. Слева выбери Daily weather.
3. Нажми Run workflow -> Run workflow.
4. Открой появившийся запуск и проверь, что все шаги зелёные. Сообщение должно прийти в Telegram.

Часть 5. Ежедневный запуск
После загрузки workflow на основную ветку GitHub будет запускать его каждый день в 07:07 по времени Нюрнберга (Europe/Berlin). Переходы CET/CEST обрабатываются часовым поясом автоматически.
GitHub может задерживать запланированные запуски при высокой нагрузке, поэтому доставка не гарантируется ровно в 07:07.

Локальный запуск (необязательно)
В PowerShell в папке проекта:
  py -m pip install -r requirements.txt
  py send_weather_once.py
Для локального запуска нужны переменные BOT_TOKEN и TELEGRAM_USER_ID в .env.

Защита
- Оставляй репозиторий Private.
- Никогда не загружай .env и не публикуй BOT_TOKEN.
- Если токен случайно попал в публичное место, открой @BotFather и перевыпусти его.
