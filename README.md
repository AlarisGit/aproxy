# aproxy — Anthropic Proxy for Ollama

Reverse proxy между Claude Code и Ollama. Добавляет аутентификацию по токенам
и аудит запросов, сохраняя полную Anthropic Messages API совместимость.

```
Claude Code → :4001 (aproxy, auth + audit) → :11434 (Ollama /v1/messages)
```

Ollama нативно поддерживает `/v1/messages` — прокси не транслирует протокол,
а только проверяет токен, логирует запрос и перенаправляет дальше.

---

## Содержание

- [Архитектура](#архитектура)
- [Файловая структура](#файловая-структура)
- [Руководство администратора](#руководство-администратора)
  - [Требования](#требования)
  - [Установка](#установка)
  - [Конфигурация](#конфигурация)
  - [Управление токенами](#управление-токенами)
  - [Запуск и управление сервисом](#запуск-и-управление-сервисом)
  - [Тестирование](#тестирование)
  - [Логи и аудит](#логи-и-аудит)
  - [Ротация логов](#ротация-логов)
  - [Диагностика](#диагностика)
  - [Безопасность](#безопасность)
- [Руководство пользователя](#руководство-пользователя)
  - [Установка Claude Code](#установка-claude-code)
  - [Настройка окружения](#настройка-окружения)
  - [Запуск](#запуск)
  - [Проверка работоспособности](#проверка-работоспособности)
  - [Доступные модели](#доступные-модели)
  - [Устранение неполадок](#устранение-неполадок)

---

## Архитектура

```
                 ┌──────────────────────────────┐
                 │         Host / VM             │
                 │                               │
                 │  Claude Code                  │
                 │       │                       │
                 │       │ HTTP (Bearer token)   │
                 │       ▼                       │
                 │  ┌─────────────────┐          │
                 │  │     aproxy       │          │
                 │  │    :4001          │          │
                 │  │                  │          │
                 │  │  • Auth (keys.json)        │
                 │  │  • Audit (audit.jsonl)      │
                 │  │  • Log (proxy.log)          │
                 │  │  • CORS headers              │
                 │  └────────┬─────────┘          │
                 │           │                     │
                 │           │ HTTP (Bearer ollama)│
                 │           ▼                     │
                 │  ┌─────────────────┐           │
                 │  │     Ollama       │           │
                 │  │    :11434         │           │
                 │  │                  │           │
                 │  │  /v1/messages    │           │
                 │  │  (Anthropic API) │           │
                 │  └─────────────────┘           │
                 └──────────────────────────────┘
```

Прокси перехватывает запросы от Claude Code, проверяет токен по `keys.json`,
аудирует запрос и перенаправляет его в Ollama с внутренним токеном `Bearer ollama`.

Поддерживаемые эндпоинты:
- `POST /v1/messages` — основной (Messages API)
- `GET /v1/models` — список моделей
- `GET /v1/organizations` — заглушка (пустой список)
- `GET /v1/organizations/{id}/users` — заглушка
- `POST /v1/messages/batches` — заглушка (404)
- `GET /health` — проверка состояния
- `GET /metrics` — метрики Prometheus
- `ANY /{path}` — catch-all прокси с аутентификацией

## Файловая структура

```
/home/sergey/Projects/aproxy/     # Проект
├── proxy.py                       # Основной код прокси (v1.6)
├── keys.json                      # Токены аутентификации (секрет!)
├── .env                           # Конфигурация окружения (секрет!)
├── .gitignore                     # Исключения git (keys.json, .env)
├── README.md                      # Эта документация
├── ACTIVE_DATA_PROTECTION.md      # Design-doc будущей DLP-системы
├── pytest.ini                     # Конфигурация pytest
├── aproxy.service                 # systemd unit (копируется в ~/.config/systemd/user/)
└── tests/                         # Тесты
    ├── conftest.py                # Фикстуры pytest
    ├── test_health_and_metadata.py
    ├── test_messages.py
    ├── test_middleware.py
    ├── test_cli.py
    └── test_integration.py        # End-to-end тесты с Claude Code

/var/log/aproxy/                   # Логи
├── audit.jsonl                    # Аудит-лог (JSONL, одна запись на запрос)
└── proxy.log                      # Лог приложения

/etc/logrotate.d/aproxy            # Конфигурация ротации логов
```

---

## Руководство администратора

### Требования

- Linux (systemd)
- Python 3.10+ с venv
- Ollama запущен и слушает на `127.0.0.1:11434`
- Доступ к моделям с суффиксом `:cloud` (если используются облачные модели)

### Установка

1. Клонировать репозиторий:

```bash
cd ~/Projects
git clone git@github.com:AlarisGit/aproxy.git
cd aproxy
```

2. Создать Python-виртуальное окружение и установить зависимости:

```bash
python3 -m venv .venv
.venv/bin/pip install fastapi uvicorn httpx prometheus-client
```

Для разработки и запуска тестов также установите:
```bash
.venv/bin/pip install pytest pytest-asyncio respx
```

3. Создать конфигурационные файлы из примеров:

```bash
# .env (секрет — не коммитировать)
cat > .env << 'EOF'
OLLAMA_BASE_URL=http://127.0.0.1:11434
ANTHROPIC_PROXY_PORT=4001
API_KEYS_FILE=/home/sergey/Projects/aproxy/keys.json
AUDIT_LOG=/var/log/aproxy/audit.jsonl
PROXY_LOG=/var/log/aproxy/proxy.log
EOF

# keys.json (секрет — не коммитировать)
.venv/bin/python3 proxy.py keys add admin
```

4. Создать директорию для логов:

```bash
sudo mkdir -p /var/log/aproxy
sudo chown sergey:sergey /var/log/aproxy
sudo chmod 750 /var/log/aproxy
```

5. Настроить ротацию логов:

```bash
sudo cp logrotate.conf /etc/logrotate.d/aproxy
```

Содержимое `logrotate.conf`:
```
/var/log/aproxy/audit.jsonl {
    daily
    rotate 30
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
    create 640 sergey sergey
}

/var/log/aproxy/proxy.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
    create 640 sergey sergey
}
```

- `audit.jsonl` — 30 дней (детализация использования)
- `proxy.log` — 14 дней (технический лог)

6. Установить systemd user unit:

```bash
mkdir -p ~/.config/systemd/user/
cp aproxy.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now aproxy
```

### Конфигурация

Файл `.env` в директории проекта:

| Переменная | По умолчанию | Описание |
|---|---|---|
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | Адрес Ollama |
| `ANTHROPIC_PROXY_PORT` | `4001` | Порт прокси |
| `API_KEYS_FILE` | `/home/sergey/Projects/aproxy/keys.json` | Путь к файлу токенов |
| `AUDIT_LOG` | `/var/log/aproxy/audit.jsonl` | Путь к аудит-логу (пустое значение отключает аудит) |
| `PROXY_LOG` | *(пусто)* | Путь к файлу лога приложения (дополнительно к journald) |
| `APROXY_MAX_BODY_SIZE` | `52428800` (50 MiB) | Максимальный размер тела запроса в байтах. Запросы без `Content-Length` или превышающие лимит отклоняются с HTTP 413 |
| `APROXY_KEY_RELOAD_INTERVAL` | `1.0` | Интервал в секундах между проверками изменений `keys.json` |

После изменения `.env` — перезапустить сервис:
```bash
systemctl --user restart aproxy
```

### Тестирование

В проекте два уровня тестов.

**Unit-тесты** (не требуют запущенных сервисов):
```bash
.venv/bin/python3 -m pytest tests/ --ignore=tests/test_integration.py
```

Покрывают:
- аутентификацию (`/v1/models`, `/metrics`, 401 без токена);
- проксирование `POST /v1/messages` в обычном и streaming-режиме;
- middleware `MAX_BODY_SIZE`;
- hot-reload `keys.json`;
- CLI управления ключами.

**Интеграционные тесты** (требуют работающих aproxy, Ollama и Claude Code CLI):
```bash
APROXY_RUN_INTEGRATION_TESTS=1 .venv/bin/python3 -m pytest tests/test_integration.py -v
```

Интеграционные тесты:
- проверяют `/health`, список моделей Ollama, аутентификацию на прокси;
- запускают настоящий `claude -p` через aproxy;
- проверяют работу инструментов Claude Code: Bash, WebFetch, WebSearch, фоновые агенты.

По умолчанию для сложных тестов выбирается наиболее способная доступная модель
(`kimi-k2.7-code:cloud` > `kimi-k2.5:cloud` > `deepseek-v4-pro:cloud` ...).
Переопределить можно через `APROXY_INTEGRATION_MODEL`:
```bash
APROXY_RUN_INTEGRATION_TESTS=1 APROXY_INTEGRATION_MODEL=kimi-k2.7-code:cloud \
  .venv/bin/python3 -m pytest tests/test_integration.py -v
```

Интеграционные тесты создают временных пользователей в `keys.json` и удаляют их
после выполнения; production-токены не затрагиваются.

### Управление токенами

Файл `keys.json` хранит хэши токенов вместо plaintext. Формат:

```json
{
  "_salt": "3b74d84067abb2a7...64_hex_chars",
  "users": {
    "sha256$e9818110c5a1fa72...64_hex_chars": "sergey",
    "sha256$781490daf155e3b4...64_hex_chars": "hermes"
  }
}
```

Хранится `SHA-256(salt + token)` — восстановить оригинальный токен из хэша невозможно.
Случайный salt генерируется при первом добавлении ключа или миграции.

**CLI-команды** (управление ключами через `proxy.py keys`):

```bash
# Добавить пользователя (токен генерируется автоматически)
.venv/bin/python3 proxy.py keys add <имя_пользователя>

# Добавить пользователя с конкретным токеном
.venv/bin/python3 proxy.py keys add <имя_пользователя> sk-мой_токен

# Мигрировать plaintext keys.json → хэшированный формат
.venv/bin/python3 proxy.py keys migrate

# Список пользователей (видны только хэши)
.venv/bin/python3 proxy.py keys list

# Удалить пользователя
.venv/bin/python3 proxy.py keys remove <имя_пользователя>
```

**Важно:** изменения `keys.json` подхватываются работающим сервисом автоматически.
Перезапуск `aproxy` после добавления/удаления токенов не требуется.

**Обратная совместимость:** если `keys.json` в старом формате (`{"sk-xxx": "user"}`), прокси
продолжит работать. Команда `keys add` автоматически мигрирует файл при первом добавлении.
Ручная миграция через `keys migrate` заменяет plaintext значения на хэши — **сохраните
резервную копию токенов перед миграцией, восстановить из хэша невозможно.**

**Правила:**
- Токен — произвольная строка. Рекомендуется префикс `sk-` и длина 32+ символов.
- Имя пользователя используется в логах и аудите.
- Нет bypass-токенов. Каждый токен должен быть явно прописан в `keys.json`.
- Перезапуск требуется только после изменений `.env`; изменения `keys.json` подхватываются автоматически (см. `APROXY_KEY_RELOAD_INTERVAL`).

### Запуск и управление сервисом

```bash
# Запуск
systemctl --user start aproxy

# Остановка
systemctl --user stop aproxy

# Перезапуск (обязательно после изменений в .env)
systemctl --user restart aproxy

# Статус
systemctl --user status aproxy

# Автозапуск при загрузке
systemctl --user enable aproxy
```

### Логи и аудит

**Журнал приложения — два источника:**

1. **systemd journal** (рекомендуется для просмотра):
```bash
# Текущий лог (live)
journalctl --user -u aproxy -f

# Последние 100 записей
journalctl --user -u aproxy -n 100

# Логи за сегодня
journalctl --user -u aproxy --since today
```

2. **Файловый лог** `/var/log/aproxy/proxy.log`:
```bash
tail -f /var/log/aproxy/proxy.log
```

Ротируется logrotate (14 дней).

**Аудит-лог** `/var/log/aproxy/audit.jsonl` — JSONL, одна запись на запрос:

```bash
# Просмотр
tail -f /var/log/aproxy/audit.jsonl

# Пример записи
{"ts":"2026-05-19T08:53:53.995847+00:00","key":"sergey","method":"GET","path":"/v1/models"}
{"ts":"2026-05-19T08:54:12.123456+00:00","key":"sk-TzM5...","method":"POST","path":"/v1/messages","model":"deepseek-v4-pro:cloud","status":200,"tokens":{"input_tokens":150,"output_tokens":320}}
```

Поля:
- `ts` — ISO 8601, UTC
- `key` — маскированный токен (первые 8 символов + `...`) или имя пользователя
- `method` / `path` — HTTP метод и путь
- `model` — запрошенная модель (если есть)
- `status` — HTTP статус ответа Ollama
- `tokens` — использование токенов (если доступно)
- `error` — текст ошибки (если есть)

Ротируется logrotate (30 дней).

### Метрики Prometheus

Эндпоинт `GET /metrics` отдаёт метрики в формате Prometheus. Требует аутентификации. Единственный публичный эндпоинт — `/health`.

**Доступные метрики:**

| Метрика | Тип | Лейблы | Описание |
|---|---|---|---|
| `aproxy_requests_total` | counter | user, method, path, status_code | Суммарное количество запросов |
| `aproxy_request_duration_seconds` | histogram | user, method, path | Латентность запросов (buckets: 0.1s — 600s) |
| `aproxy_tokens_input_total` | counter | user, model | Входные токены (input_tokens) |
| `aproxy_tokens_output_total` | counter | user, model | Выходные токены (output_tokens) |
| `aproxy_active_connections` | gauge | — | Текущее количество активных соединений |

Пример запросов:
```bash
# Аутентификация — передать токен, как для любого другого эндпоинта
TOKEN="sk-..."

# Токены по пользователям
curl -s -H "x-api-key: $TOKEN" http://127.0.0.1:4001/metrics | grep "^aproxy_tokens"

# Запросы по статусам
curl -s -H "x-api-key: $TOKEN" http://127.0.0.1:4001/metrics | grep "^aproxy_requests_total"

# Латентность (p50, p95 можно вычислить в Grafana)
curl -s -H "x-api-key: $TOKEN" http://127.0.0.1:4001/metrics | grep "^aproxy_request_duration"
```

Пример scrape-конфигурации Prometheus:
```yaml
scrape_configs:
  - job_name: aproxy
    static_configs:
      - targets: ['192.168.2.150:4001']
    metrics_path: /metrics
    authorization:
      credentials: sk-XXXX  # токен из keys.json
```

### Ротация логов

Настроена через `/etc/logrotate.d/aproxy`:

| Файл | Периодичность | Хранение | Компрессия |
|---|---|---|---|
| `audit.jsonl` | daily | 30 дней | gzip (с задержкой 1 день) |
| `proxy.log` | daily | 14 дней | gzip (с задержкой 1 день) |

Используется `copytruncate` — не требует перезапуска сервиса.

Проверка конфигурации logrotate:
```bash
# Dry-run (без реальной ротации)
sudo logrotate -d /etc/logrotate.d/aproxy

# Принудительная ротация
sudo logrotate -f /etc/logrotate.d/aproxy
```

### Диагностика

```bash
# Проверить, что прокси работает
curl http://127.0.0.1:4001/health
# Ожидаемый ответ:
# {"status":"ok","ollama":{"version":"0.30.8"},"proxy":"aproxy/1.6"}

# Проверить аутентификацию — без токена (должно вернуть 401)
curl -s http://127.0.0.1:4001/v1/models | python3 -m json.tool
# {"type":"error","error":{"type":"authentication_error","message":"Authentication required..."}}

# Проверить аутентификацию — с неверным токеном (должно вернуть 401)
curl -s -H "Authorization: Bearer wrong-token" http://127.0.0.1:4001/v1/models | python3 -m json.tool
# {"type":"error","error":{"type":"authentication_error","message":"Invalid authentication token..."}}

# Проверить аутентификацию — с правильным токеном
curl -s -H "Authorization: Bearer $ANTHROPIC_PROXY_TOKEN" \
  http://127.0.0.1:4001/v1/models | python3 -m json.tool

# Проверить через x-api-key заголовок
curl -s -H "x-api-key: $ANTHROPIC_PROXY_TOKEN" \
  http://127.0.0.1:4001/v1/models | python3 -m json.tool

# Проверить лог-файлы
tail /var/log/aproxy/proxy.log
tail /var/log/aproxy/audit.jsonl
```

### Smoke-тест Claude Code через aproxy

Для end-to-end проверки используйте скрипт:

```bash
scripts/smoke_claude_via_aproxy.sh
```

Скрипт загружает клиентский `.env`, не печатая секреты, и проверяет полный
путь `Claude Code → aproxy → Ollama`:

- `GET /health`;
- отказ `/v1/models` без токена;
- успешный `/v1/models` с токеном;
- `/metrics` с токеном;
- headless запуск `claude -p` с простой задачей через `ANTHROPIC_BASE_URL`;
- последние строки `audit.jsonl` и `proxy.log`, локально или по SSH на host из
  `ANTHROPIC_BASE_URL`.

Полезные параметры:

```bash
# Явно выбрать модель
scripts/smoke_claude_via_aproxy.sh --model deepseek-v4-pro:cloud

# Проверить другой env-файл
scripts/smoke_claude_via_aproxy.sh --env .env.prod

# Не читать удалённые логи
scripts/smoke_claude_via_aproxy.sh --no-logs

# Задать SSH host для логов, если он отличается от host в ANTHROPIC_BASE_URL
APROXY_LOG_HOST=10.200.0.58 scripts/smoke_claude_via_aproxy.sh
```

### Integration suite Claude Code

Для более реалистичной проверки используйте отдельный набор:

```bash
scripts/integration_claude_code_suite.sh
```

В отличие от smoke-теста, integration suite **не передаёт `--model`** в
`claude -p`. Это намеренно: тест эмулирует обычную работу разработчика, где
Claude Code сам выбирает модельные tier-ы из переменных `.env`:

- `ANTHROPIC_DEFAULT_OPUS_MODEL`;
- `ANTHROPIC_DEFAULT_SONNET_MODEL`;
- `ANTHROPIC_DEFAULT_HAIKU_MODEL`.

Скрипт выполняет preflight `aproxy`, затем headless-сценарии Claude Code:

- простой prompt/response;
- правка файла во временной директории;
- shell workflow во временной директории;
- проверка отсутствия SSE-регрессии `Could not parse message into JSON`;
- итоговая проверка `/metrics`;
- анализ свежих записей `audit.jsonl`, какие default model id реально
  использовал Claude Code.

Для автоматических файловых и shell-проверок скрипт запускает Claude Code с
`--permission-mode bypassPermissions`; все правки выполняются только во
временных директориях `mktemp`.

Опциональные расширения:

```bash
# WebFetch/WebSearch проверки
scripts/integration_claude_code_suite.sh --web

# Длинные shell/output сценарии
scripts/integration_claude_code_suite.sh --long

# Capability probe для agent/background поведения
scripts/integration_claude_code_suite.sh --agent

# Всё вместе
scripts/integration_claude_code_suite.sh --full

# Считать ошибкой, если в свежем audit не появились все три default model tier
scripts/integration_claude_code_suite.sh --require-all-tiers
```

Важно: отсутствие одного из tier в коротком прогоне не всегда означает дефект
`aproxy`; это может означать, что Claude Code не выбрал этот tier для данных
сценариев. Для обязательного покрытия используйте более сложные сценарии и
проверяйте свежие audit-записи.

### Безопасность

1. **`keys.json` и `.env` содержат секреты.** Они исключены из git через `.gitignore`. Права:
   ```bash
   chmod 600 keys.json .env
   ```
   Начиная с v1.4, `keys.json` хранит не сами токены, а их SHA-256 хэши с salt —
   даже при утечке файла восстановить токены невозможно.

2. **Порт 4001 не должен быть открыт в интернет.** Если используется UFW:
   ```bash
   # Запретить доступ извне
   sudo ufw deny 4001
   # Или разрешить только из локальной сети
   sudo ufw allow from 192.168.0.0/16 to any port 4001
   sudo ufw allow from 172.16.0.0/16 to any port 4001
   ```

3. **Генерировать токены достаточной длины.** Минимум 32 символа, рекомендуются случайные значения через `openssl rand`.

4. **Регулярно ротировать токены** при компрометации.

5. **Не использовать bypass-токены.** Каждый пользователь должен иметь уникальный токен в `keys.json`.

6. **Логи содержат маскированные токены.** Аудит-лог доступен только владельцу (`sergey`).

---

## Руководство пользователя

### Установка Claude Code

```bash
# Установка через npm
npm install -g @anthropic-ai/claude-code

# Проверка версии
claude --version
```

### Настройка окружения

#### 1. Определить переменные окружения

Получите у администратора:
- `ANTHROPIC_PROXY_HOST` — адрес хоста с прокси (например, `192.168.2.150`)
- `ANTHROPIC_PROXY_PORT` — порт прокси (по умолчанию `4001`)
- `ANTHROPIC_PROXY_TOKEN` — ваш персональный токен аутентификации

Добавьте в `~/.bashrc` или `~/.zshrc`:

```bash
export ANTHROPIC_PROXY_HOST="192.168.2.150"    # адрес прокси-сервера
export ANTHROPIC_PROXY_PORT="4001"               # порт прокси
export ANTHROPIC_PROXY_TOKEN="sk-..."             # ваш персональный токен
```

#### 2. Настроить shell-функцию для запуска

#### 3. Отключить проверку URL для WebFetch

По умолчанию Claude Code перед каждым WebFetch-запросом обращается к `api.anthropic.com/api/web/domain_info?domain=...` для проверки безопасности домена. В изолированной сети без доступа к Anthropic-серверам этот запрос завершается с ошибкой, и WebFetch блокируется с сообщением:

```
Unable to verify if domain X is safe to fetch. This may be due to network restrictions
or enterprise security policies blocking claude.ai.
```

Чтобы обойти это, добавьте параметр `skipWebFetchPreflight` в настройки Claude Code. Создайте или отредактируйте файл `~/.claude/settings.json`:

```json
{
  "skipWebFetchPreflight": true
}
```

Этот параметр полностью пропускает доменную верификацию — WebFetch начинает работать без обращения к внешним серверам. Для WebSearch этот параметр не нужен (поиск идёт через прокси).

Если файл `settings.json` уже содержит другие настройки (например, MCP-серверы), добавьте `skipWebFetchPreflight` в существующий объект, не перезаписывая его:

```json
{
  "mcpServers": {
    "memory": { "command": "mcp-server-memory", "args": [] }
  },
  "skipWebFetchPreflight": true
}
```

### Запуск

Добавьте в `~/.bashrc` или `~/.zshrc` (переменные уже определены в шаге 1):

```bash
# Claude Code через локальный прокси
claudelocal() {
  env -u HTTP_PROXY -u HTTPS_PROXY -u SOCKS_PROXY -u ALL_PROXY \
    ANTHROPIC_BASE_URL="http://${ANTHROPIC_PROXY_HOST}:${ANTHROPIC_PROXY_PORT}" \
    ANTHROPIC_AUTH_TOKEN="${ANTHROPIC_PROXY_TOKEN}" \
    ANTHROPIC_API_KEY="" \
    CLAUDE_CODE_ATTRIBUTION_HEADER=0 \
    claude "$@"
}
```

Перезапустите shell или выполните `source ~/.bashrc`.

Запуск:
```bash
claudelocal
# или с указанием модели
claudelocal --model deepseek-v4-pro:cloud
```

**One-shot режим (неинтерактивный):**
```bash
claudelocal -p "Кратко перечисли файлы в текущей директории" --bare --dangerously-skip-permissions
```

Флаги `--bare` и `--dangerously-skip-permissions` полезны для автоматизации и тестов:
- `--bare` отключает OAuth, обращения к keychain, attribution, LSP и фоновые prefetch;
- `--dangerously-skip-permissions` автоматически подтверждает все запросы инструментов.

**Полный вызов без функции:**

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u SOCKS_PROXY -u ALL_PROXY \
  ANTHROPIC_BASE_URL="http://${ANTHROPIC_PROXY_HOST}:${ANTHROPIC_PROXY_PORT}" \
  ANTHROPIC_AUTH_TOKEN="${ANTHROPIC_PROXY_TOKEN}" \
  ANTHROPIC_API_KEY="" \
  CLAUDE_CODE_ATTRIBUTION_HEADER=0 \
  claude
```

**Пояснение по переменным окружения:**

| Переменная | Значение | Зачем |
|---|---|---|
| `ANTHROPIC_BASE_URL` | `http://${HOST}:${PORT}` | Направляет запросы Claude Code на прокси вместо Anthropic API |
| `ANTHROPIC_AUTH_TOKEN` | ваш токен | Отправляется как `Authorization: Bearer <token>` для аутентификации |
| `ANTHROPIC_API_KEY` | `""` (пусто) | Предотвращает попытки подключиться к настоящему Anthropic API |
| `CLAUDE_CODE_ATTRIBUTION_HEADER` | `0` | Убирает лишний заголовок |
| `env -u HTTP_PROXY ...` | — | Убирает прокси-переменные, которые мешают локальному подключению |

### Проверка работоспособности

После запуска Claude Code проверьте два ключевых инструмента:

1. **WebSearch** — выполните любой поисковый запрос. Должен вернуть результаты.
2. **WebFetch** — запросите URL. Должен вернуть содержимое.
   - Без `skipWebFetchPreflight: true` в `settings.json` — ошибка "Unable to verify if domain X is safe to fetch".

Если оба инструмента работают — настройка корректна.

### Доступные модели

Модели определяются конфигурацией Ollama. Просмотр списка:

```bash
# Через прокси (с аутентификацией)
curl -s -H "Authorization: Bearer $ANTHROPIC_PROXY_TOKEN" \
  "http://${ANTHROPIC_PROXY_HOST}:${ANTHROPIC_PROXY_PORT}/v1/models" | python3 -m json.tool

# Напрямую в Ollama
ollama list
```

Модели с суффиксом `:cloud` выполняются на удалённых серверах, без суффикса — локально.

**Рекомендации по выбору модели:**

| Задача | Рекомендуемая модель | Почему |
|---|---|---|
| Сложные multi-tool задачи, код, самодиагностика | `kimi-k2.7-code:cloud` или `kimi-k2.5:cloud` | Хорошо следуют сложным инструкциям и стабильно вызывают инструменты |
| Повседневные разговорные и аналитические задачи | `deepseek-v4-pro:cloud` | Сильная рассуждающая модель |
| Быстрые простые запросы | `devstral-small-2:24b-cloud` | Лёгкая и быстрая, достаточная для коротких одношаговых задач |

Выбор модели в Claude Code:
```bash
claudelocal --model kimi-k2.7-code:cloud
```

**Примечание для thinking-моделей:**
`deepseek-v4-pro:cloud` требует `max_tokens ≥ 8192`. Если Claude Code не устанавливает
это автоматически, укажите явно или используйте `--model deepseek-v4-flash:cloud`.

### Устранение неполадок

**Ошибка аутентификации (401):**
```
authentication_error: Invalid authentication token
```
→ Проверьте значение `ANTHROPIC_PROXY_TOKEN`. Убедитесь, что токен добавлен администратором в `keys.json` и сервис перезапущен.

**Ошибка подключения:**
```
Connection refused
```
→ Проверьте, что сервис `aproxy` запущен (`systemctl --user status aproxy`).
→ Проверьте, что `ANTHROPIC_PROXY_HOST` и `ANTHROPIC_PROXY_PORT` указывают на правильный адрес.

**WebFetch не работает:**
```
Unable to verify if domain X is safe to fetch
```
→ Добавьте `"skipWebFetchPreflight": true` в `~/.claude/settings.json`.

**Переменные прокси мешают:**
```
ECONNREFUSED, timeout, network error
```
→ Убедитесь, что переменные `HTTP_PROXY`, `HTTPS_PROXY`, `SOCKS_PROXY`, `ALL_PROXY` удалены через `env -u`.
→ `NO_PROXY` с IP-адресами ненадёжен в Node.js — лучше убирать прокси-переменные полностью.

**Модель не найдена:**
```
model not found
```
→ Проверьте доступные модели через `ollama list`. Убедитесь, что имя модели указано точно (включая суффикс `:cloud`).

**Deepseek thinking-модели зависают:**
→ Thinking-модели (deepseek-v4-pro:cloud) требуют `max_tokens ≥ 8192`. Если Claude Code не устанавливает это автоматически, укажите явно или используйте `deepseek-v4-flash:cloud`.

**Claude Code возвращает "API returned an empty or malformed response (HTTP 200)":**
→ Обычно локальная модель не справилась с multi-tool streaming-запросом. Попробуйте:
  - более способную модель (`kimi-k2.7-code:cloud`, `kimi-k2.5:cloud`);
  - упростить промпт (один инструмент за раз);
  - добавить `--no-session-persistence` для one-shot задач.
