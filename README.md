# 💸 Finance Tracker Bot

Telegram-бот для трекінгу витрат з Notion як базою даних.

## Як запустити на Railway

1. Завантаж цю папку на GitHub (новий репозиторій)
2. Зайди на railway.app → New Project → Deploy from GitHub
3. У Settings → Variables додай:
   - BOT_TOKEN — токен від @BotFather
   - NOTION_TOKEN — secret_... від Notion integrations
   - DATABASE_ID — ID твоєї Notion таблиці
   - ALLOWED_USER — твій Telegram username (без @)
4. Deploy → бот запрацює!

## Структура Notion бази

Створи базу з такими полями:
- Назва (Title)
- Сума (Number)
- Категорія (Select)
- Дата (Date)
- Нотатка (Text)

## Використання

- Надішли суму: `500` або `500 кава`
- Обери категорію з кнопок
- Переглядай статистику через кнопки меню
