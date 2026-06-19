# aproxy — Authenticated API Proxy for Ollama

Reverse proxy между AI-агентами и Ollama. Добавляет аутентификацию по токенам,
аудит запросов и метрики использования, сохраняя Anthropic Messages API
совместимость для Claude Code и allowlisted native Ollama API для других агентов.
Исключение: `GET /api/tags` доступен без обязательной аутентификации, чтобы
native Ollama клиенты могли загрузить список моделей для model picker.

```
Claude Code      → :4001 (aproxy, auth + audit) → :11434 (Ollama /v1/messages)
Cline / Ollama   → :4001 (aproxy, auth + audit) → :11434 (Ollama /api/chat)
```

Ollama нативно поддерживает `/v1/messages` — прокси не транслирует протокол,
а только проверяет токен, логирует запрос и перенаправляет дальше. Для native
Ollama API aproxy проксирует только явно разрешённые маршруты, необходимые для
работы агентов с уже подключёнными моделями.

---

## Содержание

- [Архитектура](#архитектура)
- [Файловая структура](#файловая-структура)
- [Руководство администратора](#руководство-администратора)
  - [Требования](#требования)
  - [Установка](#установка)
  - [Конфигурация сервера](#конфигурация-сервера)
  - [Управление токенами](#управление-токенами)
  - [Запуск и управление сервисом](#запуск-и-управление-сервисом)
  - [Тестирование](#тестирование)
  - [Логи и аудит](#логи-и-аудит)
  - [Ротация логов](#ротация-логов)
  - [Диагностика](#диагностика)
  - [Безопасность](#безопасность)
- [Руководство пользователя](#руководство-пользователя)
  - [Установка Claude Code CLI](#установка-claude-code-cli)
  - [Настройка окружения клиента](#настройка-окружения-клиента)
  - [Настройка Cline и других native Ollama клиентов](#настройка-cline-и-других-native-ollama-клиентов)
  - [Запуск](#запуск)
  - [Проверка работоспособности](#проверка-работоспособности)
  - [Типовые проблемы](#типовые-проблемы)
- [Changelog](#changelog)
- [To-Do](#to-do)

---

## Архитектура

```
                 ┌──────────────────────────────┐
                 │         Host / VM             │
                 │                               │
                 │  Claude Code / Cline          │
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
                 │  │  /api/chat       │           │
                 │  │  /api/generate   │           │
                 │  └─────────────────┘           │
                 └──────────────────────────────┘
```

Прокси перехватывает запросы от агентов, проверяет токен по `keys.json`,
аудирует запрос и перенаправляет его в Ollama с внутренним токеном `Bearer ollama`.

Поддерживаемые эндпоинты:

Anthropic-compatible:
- `POST /v1/messages` — основной (Messages API)
- `POST /v1/messages/count_tokens` — локальная совместимая оценка числа input tokens
- `GET /v1/models` — список моделей
- `GET /v1/organizations` — заглушка (пустой список)
- `GET /v1/organizations/{id}/users` — заглушка
- `POST /v1/messages/batches` — заглушка (404)

Native Ollama allowlist:
- `POST /api/generate` — генерация, streaming NDJSON поддерживается
- `POST /api/chat` — чат, streaming NDJSON поддерживается
- `POST /api/embed` — embeddings
- `GET /api/tags` — список моделей, public metadata exception для model picker
- `GET /api/ps` — загруженные/активные модели
- `GET /api/version` — версия Ollama
- `POST /api/show` — метаданные модели

Остальное:
- `GET /health` — проверка состояния
- `GET /metrics` — метрики Prometheus
- admin/model-management routes (`/api/create`, `/api/copy`, `/api/pull`,
  `/api/push`, `/api/delete`) — заблокированы политикой aproxy
- неизвестные маршруты — не проксируются, возвращается 404

## Файловая структура

```
/home/sergey/Projects/aproxy/     # Проект
├── proxy.py                       # Основной код прокси (v1.6)
├── aproxy.json                    # Серверный конфиг (секрет!)
├── keys.json                      # Токены аутентификации (секрет!)
├── models.json                    # Маппинг Anthropic model IDs (секрет!)
├── .gitignore                     # Исключения git (keys.json, aproxy.json, models.json)
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

3. Создать конфигурационные файлы:

```bash
# aproxy.json — серверный конфиг (секрет — не коммитировать)
cat > aproxy.json << 'EOF'
{
  "ollama_base_url": "http://127.0.0.1:11434",
  "port": 4001,
  "keys_file": "/home/sergey/Projects/aproxy/keys.json",
  "models_file": "/home/sergey/Projects/aproxy/models.json",
  "audit_log": "/var/log/aproxy/audit.jsonl",
  "proxy_log": "/var/log/aproxy/proxy.log",
  "max_body_size": 52428800,
  "key_reload_interval": 1.0,
  "public_tags_log_suppress_seconds": 600.0
}
EOF

# models.json — маппинг Anthropic model IDs на Ollama (секрет — не коммитировать)
cat > models.json << 'EOF'
{
  "default": "kimi-k2.7-code:cloud",
  "anthropic_mapping": {
    "opus": "kimi-k2.7-code:cloud",
    "sonnet": "kimi-k2.5:cloud",
    "haiku": "devstral-small-2:24b-cloud"
  }
}
EOF

# keys.json — первый пользователь (секрет — не коммитировать)
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

### Конфигурация сервера

Все серверные настройки живут в одном JSON-файле — `aproxy.json` (путь можно
переопределить переменной окружения `APROXY_CONFIG`). Пользователи остаются в
`keys.json`, маппинг моделей — в `models.json`.

| Поле | По умолчанию | Описание | Требует restart |
|---|---|---|---|
| `ollama_base_url` | `http://127.0.0.1:11434` | Адрес Ollama | да |
| `port` | `4001` | Порт, на котором слушает aproxy | да |
| `keys_file` | `/home/sergey/Projects/aproxy/keys.json` | Путь к файлу токенов | да |
| `models_file` | `/home/sergey/Projects/aproxy/models.json` | Путь к маппингу моделей | нет — hot-reload |
| `audit_log` | `/var/log/aproxy/audit.jsonl` | Путь к аудит-логу (пустое значение отключает аудит) | да |
| `proxy_log` | `/var/log/aproxy/proxy.log` | Путь к файлу лога приложения (дополнительно к journald) | да |
| `max_body_size` | `52428800` (50 MiB) | Максимальный размер тела запроса в байтах. Запросы без `Content-Length` или превышающие лимит отклоняются с HTTP 413 | да |
| `key_reload_interval` | `1.0` | Интервал в секундах между проверками изменений `keys.json` и `models.json` | нет — применяется при следующей проверке |
| `public_tags_log_suppress_seconds` | `600.0` | Интервал подавления повторных audit/proxy-log записей `GET /api/tags` на один IP или пользователя. `0` отключает suppression | да |

После изменения `aproxy.json` — перезапустить сервис:
```bash
systemctl --user restart aproxy
```

Изменения `keys.json` и `models.json` подхватываются автоматически; перезапуск
не требуется.

### Автоматическая трансляция Anthropic model IDs

Claude Code по умолчанию запрашивает свои "родные" модели (`claude-opus-*`,
`claude-sonnet-*`, `claude-haiku-*`). Если их не переопределить, Ollama
возвращает ошибку `model not found`, потому что в Ollama эти имена отсутствуют.

aproxy может автоматически подменять Anthropic model IDs на Ollama-модели. Конфиг
живёт в `models.json` (путь задаётся полем `models_file` в `aproxy.json`) и
перезагружается без перезапуска сервиса.

Пример `models.json`:

```json
{
  "default": "kimi-k2.7-code:cloud",
  "anthropic_mapping": {
    "opus": "kimi-k2.7-code:cloud",
    "sonnet": "kimi-k2.5:cloud",
    "haiku": "devstral-small-2:24b-cloud"
  }
}
```

Правила:

- Если клиент прислал имя, начинающееся с `claude-opus-` / `claude-sonnet-` /
  `claude-haiku-`, aproxy сначала пытается заменить его на модель из
  `anthropic_mapping` для соответствующего tier.
- Если указанная модель отсутствует в Ollama, используется `default`.
- Если и `default` отсутствует в Ollama, берётся первая доступная модель из
  `/api/tags` с предупреждением в логе.
- Native Ollama-имена (например, `--model kimi-k2.7-code:cloud` или модели,
  переопределённые клиентским `ANTHROPIC_DEFAULT_*_MODEL`) не трогаются.

Это позволяет администратору централизованно управлять тем, какие локальные
модели используются для каждого tier Anthropic, не заставляя каждого
разработчика вручную настраивать `ANTHROPIC_DEFAULT_OPUS_MODEL` и т.д.

### Тестирование

В проекте два уровня тестов.

**Unit-тесты** (не требуют запущенных сервисов):
```bash
.venv/bin/python3 -m pytest tests/ --ignore=tests/test_integration.py
```

Покрывают:
- аутентификацию (`/v1/models`, `/metrics`, 401 без токена);
- проксирование `POST /v1/messages` в обычном и streaming-режиме;
- middleware `max_body_size`;
- hot-reload `keys.json` и `models.json`;
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
после выполнения; production-токены не затрагиваются. Порт и адрес aproxy берутся
из `aproxy.json` (поле `port`), переопределяемого через `APROXY_CONFIG`.

### Конфигурация сервера

| Файл | Назначение | Hot-reload |
|---|---|---|
| `aproxy.json` | порт, адрес Ollama, пути к логам, ключи и моделям, лимиты | нет (restart) |
| `keys.json` | токены аутентификации | да |
| `models.json` | маппинг Anthropic model IDs | да |

Переменная окружения `APROXY_CONFIG` переопределяет путь к `aproxy.json`.

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
- Перезапуск требуется только после изменений `aproxy.json`; изменения `keys.json` и `models.json` подхватываются автоматически (см. `key_reload_interval`).

### Запуск и управление сервисом

```bash
# Запуск
systemctl --user start aproxy

# Остановка
systemctl --user stop aproxy

# Перезапуск (обязательно после изменений в aproxy.json)
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

**Аудит-лог** `/var/log/aproxy/audit.jsonl` — JSONL, одна запись на
аутентифицированный запрос к прокси:

```bash
# Просмотр
tail -f /var/log/aproxy/audit.jsonl

# Пример записи
{"ts":"2026-05-19T08:53:53.995847+00:00","key":"sergey","method":"GET","path":"/v1/models"}
{"ts":"2026-05-19T08:54:12.123456+00:00","key":"sergey","method":"POST","path":"/v1/messages","model":"deepseek-v4-pro:cloud","status":200,"tokens":{"input_tokens":150,"output_tokens":320}}
{"ts":"2026-05-19T08:55:01.456789+00:00","key":"sergey","method":"POST","path":"/api/chat","model":"llama3.1:8b","api_family":"ollama","route_class":"model_egress","status":200,"tokens":{"input_tokens":64,"output_tokens":18}}
```

Поля:
- `ts` — ISO 8601, UTC
- `key` — имя пользователя из `keys.json`; длинные значения сокращаются до первых 8 символов + `...`
- `method` / `path` — HTTP метод и путь
- `model` — фактическая upstream-модель после server-side mapping (если есть)
- `api_family` — `anthropic`/отсутствует для Anthropic-compatible routes, `ollama` для native Ollama routes
- `route_class` — классификация native Ollama route (`model_egress`, `metadata`, `public_metadata`, `admin_blocked`, `unsupported`)
- `status` — HTTP статус ответа upstream или итоговый статус streaming-запроса (если есть)
- `tokens` — использование токенов из upstream `usage` (если доступно)
- `error` — текст ошибки (если есть)

Для `/v1/messages` audit-запись создаётся после ответа upstream. В non-streaming
режиме токены берутся из JSON-поля `usage`. В streaming-режиме `aproxy` объединяет
usage из SSE-событий `message_start.message.usage` и `message_delta.usage`, сохраняя
последние cumulative-значения. Если upstream не прислал usage или клиент оборвал
стрим до финального usage-события, `tokens` в audit-записи не будет либо он будет
неполным. Неуспешная аутентификация не попадает в audit-log, но учитывается в
Prometheus request metrics как `user="anonymous"`.

`POST /v1/messages/count_tokens` требует аутентификацию, но не отправляет prompt
content в upstream Ollama. aproxy возвращает локальную консервативную оценку
`{"input_tokens": N}` для совместимости с Claude/Anthropic clients. Эта оценка не
добавляется в `tokens` audit-записи и не увеличивает token usage метрики, потому
что это не фактический model usage.

Для native Ollama routes audit-запись создаётся после ответа upstream либо после
завершения streaming NDJSON. Токены нормализуются из Ollama usage-полей:
`prompt_eval_count` → `input_tokens`, `eval_count` → `output_tokens`. Для
streaming `/api/generate` и `/api/chat` usage берётся из финального NDJSON chunk
с `done: true`. Если клиент оборвал поток до финального chunk, токены могут быть
неполными или отсутствовать.

`GET /api/tags` является public metadata exception: без токена или с невалидным
токеном запрос проксируется в Ollama и аудируется по IP клиента, например
`key="192.168.1.42"`, с `route_class="public_metadata"`. Если валидный токен всё
же передан, audit привязывает запрос к соответствующему пользователю. Чтобы
Cline model picker не засорял логи повторными polling-запросами, повторные
audit/proxy-log записи `GET /api/tags` от одного IP или пользователя подавляются
на `public_tags_log_suppress_seconds`.

Ротируется logrotate (30 дней).

### Метрики Prometheus

Эндпоинт `GET /metrics` отдаёт стандартный Prometheus text exposition format:

```http
Content-Type: text/plain; version=1.0.0; charset=utf-8
```

Требует аутентификации. Единственный публичный эндпоинт — `/health`.
В ответе есть стандартные runtime-метрики Python `prometheus_client` и прикладные
метрики `aproxy_*`.

**Основные прикладные метрики:**

| Метрика | Тип | Лейблы | Описание |
|---|---|---|---|
| `aproxy_requests_total` | counter | user, method, path, status_code | Суммарное количество запросов |
| `aproxy_request_duration_seconds` | histogram | user, method, path | Латентность запросов (buckets: 0.1s — 600s) |
| `aproxy_tokens_input_total` | counter | user, model | Входные токены (input_tokens) |
| `aproxy_tokens_output_total` | counter | user, model | Выходные токены (output_tokens) |
| `aproxy_active_connections` | gauge | — | Текущее количество активных соединений |

`aproxy_requests_total` и `aproxy_request_duration_seconds` считаются middleware
для API-запросов к прокси. `/health` и `/metrics` не включаются, чтобы health-check
и scrape Prometheus не искажали аналитику использования. Oversized/unbounded body,
отклонённый до входа в endpoint, также не считается proxied request. Для streaming
запросов latency и request counter фиксируются после завершения чтения SSE или
NDJSON-стрима, а `status_code` берётся из итогового состояния стрима, не из
внешнего HTTP 200 `StreamingResponse`.

Native Ollama streaming определяется по allowlisted route и параметру `stream`,
а не только по `Content-Type`: Ollama Cloud может возвращать NDJSON body с
заголовком `application/json`.

Path label имеет bounded cardinality. Известные маршруты логируются явно
(`/v1/messages`, `/v1/messages/count_tokens`, `/api/chat`, `/api/generate`,
`/api/embed`, `/api/tags`, `/api/show`, `/api/ps`, `/api/version`),
заблокированные admin routes попадают в `/api/admin`, прочие native Ollama routes
— в `/api/other`.

Пример строк Prometheus:
```text
# HELP aproxy_requests_total Total proxied requests
# TYPE aproxy_requests_total counter
aproxy_requests_total{method="POST",path="/v1/messages",status_code="200",user="sergey"} 42.0
# HELP aproxy_tokens_input_total Total input tokens proxied
# TYPE aproxy_tokens_input_total counter
aproxy_tokens_input_total{model="deepseek-v4-pro:cloud",user="sergey"} 12345.0
```

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

# Native model list для Cline model picker: public metadata exception
curl -s http://127.0.0.1:4001/api/tags | python3 -m json.tool
# {"models":[...]}

# Protected native Ollama API без токена (должно вернуть 401 без упоминания Anthropic)
curl -s http://127.0.0.1:4001/api/version | python3 -m json.tool
# {"error":"Authentication required for native Ollama API..."}

# Protected native Ollama API с неверным токеном
curl -s -H "Authorization: Bearer wrong-token" http://127.0.0.1:4001/api/version | python3 -m json.tool
# {"error":"Invalid aproxy token for native Ollama API..."}

# Проверить аутентификацию — с правильным токеном
curl -s -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:4001/v1/models | python3 -m json.tool

# Проверить через x-api-key заголовок
curl -s -H "x-api-key: $TOKEN" \
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

Скрипт загружает клиентский `.env` (обычно для `ANTHROPIC_AUTH_TOKEN` и
других переменных Claude Code), не печатая секреты, и проверяет полный путь
`Claude Code → aproxy → Ollama`:

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
Claude Code сам выбирает модельные tier-ы из переменных окружения:

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

1. **`keys.json`, `aproxy.json` и `models.json` содержат секреты.** Они исключены из git через `.gitignore`. Права:
   ```bash
   chmod 600 keys.json aproxy.json models.json
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

### Настройка окружения клиента

Claude Code общается с aproxy через стандартные переменные Anthropic. Никаких
дополнительных `ANTHROPIC_PROXY_*` переменных не требуется.

#### 1. Получить у администратора

- URL aproxy (например, `http://192.168.2.150:4001`)
- Персональный токен аутентификации (`sk-...`)

#### 2. Настроить shell-функцию

Добавьте в `~/.bashrc` или `~/.zshrc`:

```bash
# Claude Code через локальный прокси
claudelocal() {
  env -u HTTP_PROXY -u HTTPS_PROXY -u SOCKS_PROXY -u ALL_PROXY \
    ANTHROPIC_BASE_URL="http://192.168.2.150:4001" \
    ANTHROPIC_AUTH_TOKEN="sk-..." \
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
  ANTHROPIC_BASE_URL="http://192.168.2.150:4001" \
  ANTHROPIC_AUTH_TOKEN="sk-..." \
  ANTHROPIC_API_KEY="" \
  CLAUDE_CODE_ATTRIBUTION_HEADER=0 \
  claude
```

**Переменные окружения Claude Code:**

| Переменная | Значение | Зачем |
|---|---|---|
| `ANTHROPIC_BASE_URL` | `http://<host>:<port>` | Направляет запросы Claude Code на aproxy вместо Anthropic API |
| `ANTHROPIC_AUTH_TOKEN` | ваш токен | Отправляется как `Authorization: Bearer <token>` для аутентификации |
| `ANTHROPIC_API_KEY` | `""` (пусто) | Предотвращает попытки подключиться к настоящему Anthropic API |
| `CLAUDE_CODE_ATTRIBUTION_HEADER` | `0` | Убирает лишний заголовок |
| `env -u HTTP_PROXY ...` | — | Убирает прокси-переменные, которые мешают локальному подключению |

### Настройка Cline и других native Ollama клиентов

Для клиентов, которые умеют работать с Ollama напрямую, укажите URL aproxy как
Ollama base URL:

```text
http://192.168.2.150:4001
```

В поле API key/token укажите персональный токен `sk-...`. Если клиент умеет
отправлять headers, токен должен прийти как `Authorization: Bearer <token>` или
`x-api-key: <token>`. aproxy заменит этот токен на внутренний
`Authorization: Bearer ollama` при обращении к upstream Ollama.

Если native Ollama клиент не умеет задавать API key/header, используйте HTTP
Basic credentials в base URL:

```text
http://<token>@192.168.2.150:4001
http://aproxy:<token>@192.168.2.150:4001
```

aproxy принимает оба варианта: токен в username или токен в password.

Cline загружает список моделей через `GET /api/tags` без передачи API key. Этот
маршрут намеренно разрешён как public metadata exception, поэтому dropdown
моделей работает без отдельной настройки. Модельные запросы (`/api/chat`,
`/api/generate`, `/api/embed`) всё равно требуют персональный токен. Повторные
записи `/api/tags` от одного IP подавляются в audit/proxy-log на интервал
`public_tags_log_suppress_seconds`.

Поддерживаются только рабочие маршруты:

```text
POST /api/chat
POST /api/generate
POST /api/embed
GET  /api/tags
GET  /api/ps
GET  /api/version
POST /api/show
```

Из этих маршрутов только `GET /api/tags` доступен без обязательной
аутентификации. Остальные metadata и model routes требуют токен.

Управление моделями через native API намеренно отключено: `/api/create`,
`/api/copy`, `/api/pull`, `/api/push`, `/api/delete` возвращают 403 и не доходят
до Ollama.

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

Если настроена shell-функция `claudelocal` (см. выше), достаточно:

```bash
claudelocal
# или с указанием модели
claudelocal --model deepseek-v4-pro:cloud
```

**One-shot режим:**
```bash
claudelocal -p "Кратко перечисли файлы в текущей директории" --bare --dangerously-skip-permissions
```

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
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://192.168.2.150:4001/v1/models" | python3 -m json.tool

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
→ Проверьте значение `ANTHROPIC_AUTH_TOKEN`. Убедитесь, что токен добавлен администратором в `keys.json` и сервис перезапущен.

**Ошибка аутентификации Cline / native Ollama API (401):**
```text
Invalid aproxy token for native Ollama API
```
→ Проверьте API key в настройках Ollama provider или токен в base URL
(`http://<token>@host:4001`, `http://aproxy:<token>@host:4001`). Для native
Ollama routes сообщение об ошибке намеренно не упоминает `ANTHROPIC_AUTH_TOKEN`.

**Ошибка подключения:**
```
Connection refused
```
→ Проверьте, что сервис `aproxy` запущен (`systemctl --user status aproxy`).
→ Проверьте, что `ANTHROPIC_BASE_URL` указывает на правильный адрес.

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
