# Twitch Unlink

Консольная утилита для **просмотра и отвязки подключений** аккаунта Twitch:

- **OAuth-приложения** («Other Connections» / сторонние компании)
- **социальные аккаунты** (Steam, Amazon, Riot и др.)
- **организации разработчика** (выход из организации)

Вход выполняется **только по OAuth-токену** (логин и пароль не используются). Поддерживается строка вида `login:password:token` — из неё берётся только последняя часть.

## Установка

```bash
cd twitch_unlink
pip install -r requirements.txt
```

## Использование

Интерактивное меню:

```bash
export TWITCH_TOKEN='ваш_токен'
python -m twitch_unlink
```

Только список приложений:

```bash
python -m twitch_unlink apps --list --token 'ваш_токен'
```

Отвязать приложение по имени:

```bash
python -m twitch_unlink apps --name "Streamlabs" --yes --token 'ваш_токен'
```

Файл с учётными данными (первая строка `login:password:token`):

```bash
python -m twitch_unlink apps --list --token-file accounts.txt
```

Команды: `apps`, `social`, `orgs`, `interactive`.

## Веб‑программа (интерфейс + ротация прокси)

Графический интерфейс в стиле Twitch: списки подключений, отвязка в один клик, фильтр и ручной ввод ID.

**Ротация прокси:** при указании нескольких прокси трафик к Twitch переключается на следующий прокси **каждые 60 секунд** (интервал можно изменить на экране входа, 10–600 с).

```bash
cd twitch_unlink
pip install -r requirements.txt
python3 -m twitch_unlink web
```

Откройте в браузере: **http://127.0.0.1:8765**

Форматы прокси в текстовом поле (по одному на строку):

- `http://user:pass@host:port`
- `host:port:user:pass`
- `socks5://host:port`

## Откуда взять токен

Токен — секрет, как пароль. Обычно его получают через OAuth вашего приложения на [dev.twitch.tv](https://dev.twitch.tv) или из сессии браузера (на свой риск). Утилита использует неофициальный GraphQL Twitch, как веб-сайт; для OAuth-приложений применяется мутация `deleteOauthApp`.

## Безопасность

Не публикуйте токен и не коммитьте файлы с токенами в git.