# Twitch Drops — отвязка игр

Утилита для **отвязки игровых аккаунтов Twitch Drops** (как Standoff 2 на странице **Инвентарь Drops** → «управлять подключениями»).  
Не трогает прочие OAuth-приложения, соцсети и организации — **только игры с Drops**.

Вход: **OAuth-токен** (`login:password:token` — используется только токен).

## Установка

```bash
cd twitch_unlink
pip install -r requirements.txt
```

## Веб-интерфейс (рекомендуется)

```bash
python3 -m twitch_unlink web
```

Откройте **http://127.0.0.1:8765**

- список привязанных игр из кампаний Drops и инвентаря;
- отвязка по кнопке или вручную по `client_id`;
- **ротация прокси** каждые 60 с (интервал настраивается).

## Консоль

```bash
export TWITCH_TOKEN='…'
python3 -m twitch_unlink drops --list
python3 -m twitch_unlink drops --name "Standoff" -y
python3 -m twitch_unlink drops --id '<client_id игры>' -y
```

## Как это работает

Привязка игры для Drops — это OAuth-подключение **Client ID издателя игры**.  
Отвязка выполняется через GQL `deleteOauthApp` с этим `client_id` (как «Disconnect» в настройках Twitch).

Список строится из:

- кампаний в инвентаре (`dropCampaignsInProgress`), где `isAccountConnected`;
- остальных `dropCampaigns` с активной привязкой;
- при необходимости — `clientAuthorizations`, если `client_id` совпадает с игрой из Drops.

Токен храните в секрете. Используется неофициальный GQL Twitch.

## Установщик (Windows / Linux)

Готовые сборки появляются в **GitHub Actions** после push в ветку с программой:

1. Откройте **https://github.com/lolkiper/fefwfwwfe/actions/workflows/build-installer.yml**
2. Выберите последний успешный запуск → **Artifacts**
3. **TwitchDropsUnlink-Windows** — `TwitchDropsUnlink-Setup-1.2.0.exe` (установщик) и portable-папка
4. **TwitchDropsUnlink-Linux** — `TwitchDropsUnlink-linux-x86_64.tar.gz`

**Windows:** установите `.exe`, запустите ярлык — откроется браузер на `http://127.0.0.1:8765`.

**Сборка у себя (Windows):**

```powershell
cd twitch_unlink
.\scripts\build_windows.ps1
```

**Сборка у себя (Linux):**

```bash
cd twitch_unlink && bash scripts/build_linux.sh
```