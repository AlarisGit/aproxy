# Активная защита данных в aproxy

Статус документа: design foundation для следующей реализации.

Цель документа: сохранить накопленные выводы после первой попытки
реализации content filter и задать проектную основу для активной защиты от
утечки секретов, паролей и персональных данных через `aproxy`.

Этот документ описывает:

- какую проблему должна решать защита;
- какие выводы уже получены из первой реализации;
- какие свойства нужны следующей архитектуре;
- как должны работать детектирование, policy rules, masking, warnings и
  blocking;
- какой минимальный следующий вариант стоит реализовать;
- какой план разработки, тестирования и эксплуатации нужен дальше.

## 1. Короткое решение

Следующую реализацию не следует строить как набор regex-проверок внутри одного
HTTP handler.

Рекомендуемая архитектура:

1. Рассматривать защиту как egress control перед передачей данных из прокси в
   model upstream.
2. Разделить pipeline на:
   - нормализацию и разбор request shape;
   - извлечение всех model-visible текстовых сегментов;
   - детекторы и классификаторы;
   - policy engine;
   - применение решения `allow`, `audit`, `warn`, `mask`, `block`;
   - privacy-safe audit, metrics и observability.
3. Не разрешать model-capable маршруты, которые обходят этот pipeline.
4. Сделать политику гибкой:
   - high-confidence секреты можно блокировать или маскировать;
   - неоднозначные совпадения можно только логировать как warning;
   - PII можно обрабатывать по destination policy, типу данных и confidence.
5. Явно различать:
   - локальную модель;
   - cloud-backed модель;
   - неизвестный destination.
6. Сделать обязательным schema coverage: нельзя утверждать, что поле защищено,
   если оно не было извлечено и проверено.

## 2. Термины

`Активная защита` - механизм, который видит данные до отправки в upstream и
может изменить результат обработки запроса.

`Audit-only` - механизм фиксирует факт возможного нарушения, но не меняет body
и не блокирует запрос.

`Warning` - событие о возможной передаче защищенных данных. Оно может быть:

- внутренним warning в audit/log/metrics;
- сигналом клиенту в совместимом ответе, если протокол это позволяет;
- административным событием для последующего анализа.

`Masking` - замена найденного чувствительного фрагмента до отправки в upstream.

`Blocking` - отказ прокси отправлять запрос в upstream.

`Protected data` - секреты, учетные данные, токены, ключи, персональные данные
и иные данные, которые policy считает ограниченными для model upstream.

`Model-visible data` - данные запроса, которые upstream может включить в
контекст модели, tool payload или системный prompt.

`Detector` - компонент, который ищет кандидатов на чувствительные данные.

`Policy` - правила принятия решения по набору детекций с учетом destination,
маршрута, пользователя, категории, confidence и исключений.

## 3. Контекст aproxy

`aproxy` стоит между клиентом и Ollama:

```text
Claude Code or API client -> aproxy -> Ollama -> local or cloud-backed model
```

Сейчас прокси выполняет:

- аутентификацию API keys;
- проксирование Anthropic-compatible запросов;
- audit и metrics;
- передачу части запросов в Ollama;
- catch-all proxying для неявно описанных путей.

Для защиты данных важна не только аутентификация клиента. Даже
аутентифицированный пользователь может:

- случайно передать приватный ключ, токен или пароль в prompt;
- передать tool output с секретами;
- передать персональные данные, не предназначенные для облачной модели;
- использовать route или request shape, которые не покрыты фильтром.

Поэтому контроль должен стоять на boundary:

```text
authenticated request data -> proxy policy -> upstream egress
```

## 4. Цели защиты

### 4.1. Основные цели

Система должна:

1. Проверять все поддерживаемые model-visible данные перед egress в upstream.
2. Обнаруживать высоковероятные секреты:
   - API tokens;
   - private keys;
   - credentials в URL и structured fields;
   - authorization material;
   - другие configured secret signatures.
3. Обнаруживать определенные классы PII:
   - email;
   - телефоны;
   - документы и идентификаторы, если policy их задает;
   - платежные номера с дополнительной валидацией;
   - custom organization-specific patterns.
4. Уметь по policy:
   - пропустить;
   - зафиксировать audit event;
   - сформировать warning;
   - замаскировать;
   - заблокировать.
5. Не писать сами найденные секреты в логи, audit events и метрики.
6. Давать операторам наблюдаемость:
   - что режим защиты включен;
   - какие policy packs загружены;
   - какие категории срабатывают;
   - сколько запросов разрешено, замаскировано и заблокировано.
7. Быть тестируемой на request shapes, rules и policy decisions.

### 4.2. Не-цели первой зрелой реализации

Не стоит сразу обещать:

- идеальное определение любого пароля в произвольном естественном тексте;
- полноценный enterprise DLP для файлов, изображений и архивов;
- надежную классификацию любой PII без false positives и false negatives;
- защиту от данных, которые отправляются в upstream в неразобранном или
  неподдерживаемом формате;
- автоматическое соблюдение всех правовых режимов без отдельно заданной
  организации policy.

Защита должна честно различать:

- covered fields;
- unsupported fields;
- unknown route/body formats;
- best-effort detectors;
- high-confidence detectors.

## 5. Что уже известно из первой попытки

Первая попытка - это текущие изменения в `proxy.py` и `rules.json`, которые
будут отменены как неполноценные.

### 5.1. Что было реализовано

В первой попытке были добавлены:

- `rules.json` с regex patterns и категориями `secrets`, `pii`, `attack`,
  `custom`;
- загрузка правил при старте;
- извлечение части текста из `messages[].content`;
- поиск совпадений по regex перед `POST /v1/messages`;
- blocking response для найденных совпадений;
- metric по блокировкам;
- CLI-команды управления rules.

Default rules покрывали, среди прочего:

- AWS access key id;
- GitHub token;
- Google API key;
- PEM private key marker;
- JWT-like token;
- Slack token;
- Basic auth-like value;
- query parameters `api_key`, `token`, `secret`, `password`;
- `sk-...` token-like strings;
- несколько PII signatures;
- SQL-like attack pattern.

### 5.2. Сильные стороны первой попытки

Первая попытка показала полезные направления:

1. Проверка должна выполняться до отправки body в upstream.
2. Rule configuration полезнее, чем hardcoded один regex.
3. Audit должен сохранять rule metadata, а не исходное matched значение.
4. Нужны метрики блокировок и режимов принятого решения.
5. Отдельный rules file упрощает настройку custom patterns.
6. Даже простой scanner полезен как guardrail для явных provider-specific
   секретов.

### 5.3. Слабые стороны первой попытки

Первая попытка не годится как security boundary по следующим причинам.

#### 5.3.1. Защита встроена в один handler

Фильтр вызывался только в explicit handler `POST /v1/messages`.

Проблема:

- у прокси есть catch-all route;
- upstream может иметь иные model-capable routes;
- route without scan становится прямым policy bypass.

Вывод:

- защита должна жить в общей egress-границе;
- model-capable routes должны быть allowlisted и classified;
- catch-all нельзя оставлять с неявным обходом.

#### 5.3.2. Проверялась узкая проекция JSON, а отправлялся исходный body

Scanner смотрел только в часть `messages[].content`, но upstream получал весь
request body.

Проблема:

- top-level `system` и другие model-visible поля могут не попасть в extraction;
- новые content blocks клиента могут появиться позже;
- tool payload может иметь nested strings, которые scanner не посетил;
- защита создает ложное ощущение полного coverage.

Вывод:

- нужен schema adapter для каждого поддерживаемого request shape;
- adapter должен возвращать список scan segments с JSON path и offsets;
- неподдерживаемые model-visible shapes должны быть явно classified.

#### 5.3.3. Было только одно действие: block

Blocking полезен для некоторых high-confidence secret matches, но плохо
подходит для всех типов данных.

Проблемы:

- email или private IP могут быть рабочей частью legitimate prompt;
- false positive на платежный номер может ломать workflow;
- для rollout сначала часто нужен audit-only режим;
- для некоторых секретов masking лучше, чем hard block.

Вывод:

- detector не должен сам решать action;
- action выбирает policy engine;
- поддержка `audit`, `warn`, `mask`, `block` нужна в архитектуре с начала.

#### 5.3.4. Regex rules не равны классификации

Regex хорошо ловит часть явных signatures, но:

- generic password в тексте не имеет стабильного signature;
- некоторые токены требуют provider-specific validation;
- credit-card-like pattern без checksum дает много noise;
- entropy-only detection имеет много false positives;
- PII требует контекста и policy scope.

Вывод:

- regex остается одним detector type;
- нужны validators, context checks, confidence и исключения;
- policy должна учитывать confidence.

#### 5.3.5. Fail-open behavior не был формализован

Если rules file отсутствовал, scanning отключался без обязательного failure.

Вывод:

- режим должен быть явным;
- `disabled` допустим только по явной конфигурации;
- `enforce` без валидной policy должен либо fail startup, либо mark service
  unhealthy по выбранному operational contract.

#### 5.3.6. Логирование error text остается отдельным риском

Даже если request body не логируется, raw upstream errors и exception strings
могут содержать чувствительные данные.

Вывод:

- active protection нельзя проектировать отдельно от privacy-safe audit/logging;
- error persistence нужно редактировать или минимизировать.

### 5.4. Главный урок

Следующая попытка должна начинаться не с нового списка regex, а с определения:

1. какие egress routes поддерживаются;
2. какие поля на этих routes model-visible;
3. какой pipeline обязан обработать каждый такой egress;
4. что делать при неизвестном route или unknown field shape.

## 6. Threat model для защиты данных

### 6.1. Активы

Защита бережет:

- credentials, tokens, passwords, API keys;
- private keys, signing keys, authorization headers;
- персональные данные, определенные policy;
- internal hostnames, URLs and identifiers, если policy это требует;
- audit/log files от превращения в новый storage чувствительных данных.

### 6.2. Actors

Нужно учитывать:

- обычного аутентифицированного пользователя, который ошибся;
- аутентифицированного пользователя, который пытается обойти policy;
- оператора, который конфигурирует policy и destination classes;
- upstream, который может вернуть error text с echoed fragments;
- future client version, который пришлет новый body shape.

### 6.3. Boundaries

Критичные boundaries:

1. Request client -> aproxy.
2. aproxy policy gate -> Ollama upstream.
3. Ollama -> cloud-backed model, если модель не локальная.
4. aproxy -> audit/log/metrics persistence.

### 6.4. Destination sensitivity

Policy должна различать destination class.

Минимальные классы:

- `local_model` - данные остаются в локальном model runtime по утвержденной
  конфигурации;
- `cloud_model` - данные уйдут через Ollama в удаленный сервис;
- `unknown_model` - класс модели не определен надежно;
- `metadata_only` - route не передает model-visible content.

Если классификация destination неизвестна, default для enforce-mode должен быть
консервативным и явно выбранным policy:

- treat as cloud;
- block unknown;
- allow unknown only in observe mode.

## 7. Требования к следующей реализации

### 7.1. Functional requirements

Следующая реализация должна:

1. Иметь единый protection pipeline для egress.
2. Давать route classification.
3. Поддерживать structured extraction из request JSON.
4. Поддерживать string segments и nested string leaves.
5. Возвращать scan findings с:
   - category;
   - rule id;
   - detector id;
   - confidence;
   - severity;
   - field path;
   - range inside field;
   - safe fingerprint metadata where needed.
6. Давать policy decision для request:
   - allow;
   - audit;
   - warn;
   - mask;
   - block.
7. Уметь маскировать только matched fragments или целые fields по rule.
8. Уметь сообщить оператору о coverage gap.
9. Иметь dry-run/observe mode.
10. Иметь regression fixtures для supported protocols.

### 7.2. Security requirements

Следующая реализация должна:

- не логировать raw matched values;
- не хранить полный request body в audit;
- не отдавать клиенту sensitive fragments в block message;
- не позволять неподдерживаемым model-capable routes обходить egress gate;
- не выполнять unsafe regex на неограниченных размерах без resource policy;
- не считать policy active при silent load failure;
- быть устойчивой к malformed JSON и unsupported body shapes;
- не ломать JSON сериализацию при masking.

### 7.3. Operational requirements

Нужны:

- startup log с mode, policy version и loaded detector count;
- health/readiness visibility по состоянию policy;
- metrics по action/category/destination без cardinality explosion;
- policy file validation before start/reload;
- controlled rollout:
  - off;
  - observe;
  - enforce with mask/block.

## 8. Рекомендуемая архитектура

### 8.1. Основной pipeline

Рекомендуемый pipeline:

```text
HTTP route
  -> authenticate
  -> classify route and destination
  -> parse request body
  -> extract model-visible segments
  -> run detectors
  -> aggregate findings
  -> evaluate policy
  -> transform body if mask is selected
  -> emit privacy-safe audit and metrics
  -> forward transformed body or block
```

Свойство pipeline:

- любой model-capable forward должен проходить через него;
- metadata routes могут иметь explicit bypass class `metadata_only`;
- unknown forward should not silently skip protection.

### 8.2. Route gateway

Нужно отделить:

- explicit supported Anthropic routes;
- explicit supported Ollama routes, если они нужны;
- metadata routes;
- unsupported routes.

Рекомендуемый baseline:

```text
POST /v1/messages      -> protected_model_egress
POST /api/generate     -> protected_model_egress
POST /api/chat         -> protected_model_egress
POST /api/embed        -> protected_model_egress
GET  /v1/models        -> metadata_only
GET  /api/tags         -> public_metadata
GET  /api/ps           -> metadata_only
GET  /api/version      -> metadata_only
POST /api/show         -> metadata_only
POST /api/create       -> denied_admin
POST /api/copy         -> denied_admin
POST /api/pull         -> denied_admin
POST /api/push         -> denied_admin
DELETE /api/delete     -> denied_admin
GET  /metrics          -> no_upstream_content
GET  /health           -> health
ANY  /{path}           -> denied by default or classified allowlist
```

Если catch-all остается, ему нужна policy:

```text
if route is public metadata:
    forward metadata without mandatory authentication
elif route is known metadata:
    forward metadata
elif route is known model egress:
    protection pipeline
else:
    reject or require explicit config
```

Нельзя держать модель:

```text
explicit route scans, catch-all forwards unchecked
```

### 8.3. Schema adapters

Schema adapter отвечает за supported request format.

Для Anthropic-compatible messages adapter должен как минимум учитывать:

- top-level `system`;
- `messages[*].content` when it is a string;
- supported content blocks that carry text;
- supported tool input payloads that become model-visible;
- supported tool result payloads;
- nested string values where the protocol intentionally carries structured
  prompt/tool context.

Adapter должен возвращать segments:

```json
{
  "segment_id": "seg-7",
  "json_path": "$.messages[2].content[0].text",
  "kind": "message_text",
  "text": "string visible to the model",
  "mutability": "substring_replace"
}
```

Для structured tool payload:

```json
{
  "segment_id": "seg-18",
  "json_path": "$.messages[3].content[1].input.password",
  "kind": "tool_input_string",
  "text": "value",
  "mutability": "field_replace"
}
```

Adapter обязан явно сообщать:

- `coverage=complete` for supported shape;
- `coverage=partial` for known ignored non-model-visible parts;
- `coverage=unsupported` for unhandled model-visible format.

Policy должна уметь реагировать на `coverage=unsupported`.

### 8.4. Detector layer

Detector layer не принимает итоговое решение.

Он только выдает findings.

Рекомендуемые detector types:

1. `regex_signature`
   - provider-specific tokens;
   - PEM markers;
   - URL credential patterns;
   - organization-specific patterns.
2. `regex_plus_validator`
   - payment numbers with checksum;
   - document format with normalization and validity checks where possible.
3. `contextual_keyword_detector`
   - pairs like `password=`, `secret:`, `authorization:`, `private_key`;
   - JSON key context for structured tool payloads.
4. `entropy_candidate_detector`
   - optional;
   - must be constrained by length, charset and context;
   - should default to low or medium confidence unless combined with context.
5. `allowlist/suppression detector`
   - test fixtures;
   - known fake token prefixes;
   - approved placeholders.

Each finding should include confidence, for example:

- `high` - provider signature or validated private key marker;
- `medium` - structured field name plus plausible secret value;
- `low` - weak entropy or ambiguous PII-like pattern.

### 8.5. Policy engine

Policy engine consumes findings and context.

Inputs:

- findings;
- route class;
- destination class;
- user or user group if policy needs it;
- request mode;
- coverage status;
- size/resource status;
- exceptions and allowlists.

Output:

```json
{
  "request_action": "mask",
  "finding_actions": [
    {
      "finding_id": "f-1",
      "action": "block"
    },
    {
      "finding_id": "f-2",
      "action": "audit"
    }
  ],
  "reason_codes": [
    "cloud_destination",
    "high_confidence_secret"
  ]
}
```

Нужно определить aggregation rule. Простой baseline:

```text
block > mask > warn > audit > allow
```

Но aggregation не должна терять детализацию per finding для audit.

### 8.6. Actions

#### 8.6.1. `allow`

Запрос отправляется без special audit event, кроме обычного request audit.

#### 8.6.2. `audit`

Запрос отправляется, но формируется privacy-safe событие:

- category;
- rule id;
- confidence;
- destination class;
- route class;
- field kind/path class;
- action.

Не писать:

- raw matched text;
- full body;
- full JSON path with user data in indexes or dynamic keys if that leaks data.

#### 8.6.3. `warn`

`warn` - это `audit` плюс отдельный warning signal.

Варианты warning signal:

- structured warning in audit event;
- counter/metric;
- optional response header only when protocol and clients tolerate it;
- optional admin notification hook later.

Для streaming requests warning нельзя проектировать так, чтобы он требовал
переписывать уже начатый upstream stream. Решение принимается до upstream
request.

#### 8.6.4. `mask`

`mask` изменяет body до upstream.

Рекомендуемый replacement:

```text
[REDACTED:secret:github-token]
```

или более короткий:

```text
[REDACTED_SECRET]
```

Policy должна задавать:

- mask matched substring;
- mask whole field;
- preserve prefix/suffix for format-specific use only if it is safe;
- whether client receives a warning that content was masked.

Masking must:

- mutate parsed structured body;
- preserve valid JSON;
- keep audit without raw value;
- avoid secondary logging of transformed original and raw input together.

#### 8.6.5. `block`

`block` не отправляет request upstream.

Block response должен содержать:

- stable error type;
- safe human message;
- reason category and maybe rule descriptions;
- no raw sensitive fragments.

Пример safe message:

```text
Request blocked by data protection policy: high-confidence secret detected in
model-visible input.
```

### 8.7. Sample policy model

Формат policy можно реализовать JSON или YAML. Важно не выбрать удобство
ручного редактирования ценой слабой валидации. Для первого варианта JSON
достаточен, если schema validation обязательна.

Пример концептуальной policy:

```json
{
  "version": 1,
  "mode": "observe",
  "unknown_destination_action": "warn",
  "unsupported_model_shape_action": "block",
  "routes": {
    "anthropic_messages": {
      "class": "protected_model_egress"
    },
    "ollama_chat": {
      "class": "protected_model_egress"
    }
  },
  "rules": [
    {
      "id": "private-key",
      "category": "secret",
      "detector": "regex_signature",
      "pattern": "-----BEGIN .*PRIVATE KEY-----",
      "confidence": "high",
      "actions": {
        "local_model": "mask",
        "cloud_model": "block",
        "unknown_model": "block"
      }
    },
    {
      "id": "email",
      "category": "pii",
      "detector": "regex_signature",
      "confidence": "medium",
      "actions": {
        "local_model": "audit",
        "cloud_model": "warn",
        "unknown_model": "warn"
      }
    },
    {
      "id": "password-field",
      "category": "credential",
      "detector": "structured_key_context",
      "keys": ["password", "passwd", "secret", "access_token"],
      "confidence": "high",
      "actions": {
        "local_model": "mask",
        "cloud_model": "block",
        "unknown_model": "block"
      }
    }
  ]
}
```

Пример исключения:

```json
{
  "id": "allow-test-fixtures",
  "match": {
    "rule_id": "github-token",
    "value_prefix": "ghp_example_"
  },
  "action": "suppress",
  "reason": "documented test fixture prefix"
}
```

Исключения должны быть:

- узкими;
- reviewable;
- auditable;
- versioned with policy.

### 8.8. Default action matrix

Начальная recommended matrix:

| Finding | Local model | Cloud model | Unknown model |
| --- | --- | --- | --- |
| Private key, high-confidence token | mask or block | block | block |
| Structured password/token field | mask | block | block |
| URL credential parameter | mask | block | block |
| Validated high-risk PII | warn or mask | warn or block by org policy | warn or block |
| Email/phone ambiguous PII | audit or warn | warn | warn |
| Low-confidence entropy candidate | audit | audit or warn | warn |
| Unsupported model-visible shape | warn in observe | block in enforce | block in enforce |

Эта matrix - стартовая рекомендация, а не универсальная compliance policy.

## 9. Masking details

Masking надо проектировать отдельно от detection.

### 9.1. Требования к masking

Masking должно:

- быть deterministic для одного policy decision;
- не ломать encoding;
- не ломать request JSON;
- работать по field path и offset внутри строки;
- не удалять случайно neighboring text;
- не скрывать coverage gaps.

### 9.2. Overlapping matches

Если findings overlap:

1. Нормализовать ranges.
2. Приоритетнее оставить finding с более высокой severity/confidence.
3. Merge overlapping ranges before replacement.
4. Audit должен помнить все categories/rules, но body replacement должен быть
   один на merged range.

### 9.3. Whole-field masking

Whole-field masking предпочтительнее для structured secrets:

```json
{
  "password": "[REDACTED_SECRET]"
}
```

Substring masking предпочтительнее в свободном prompt:

```text
Use token [REDACTED_SECRET] for this failing request.
```

## 10. Warning and audit design

### 10.1. Event schema

Рекомендуемый event:

```json
{
  "event": "data_protection_decision",
  "ts": "2026-05-21T10:00:00Z",
  "request_id": "req-...",
  "user": "sergey",
  "route_class": "anthropic_messages",
  "destination_class": "cloud_model",
  "mode": "observe",
  "request_action": "warn",
  "finding_count": 2,
  "findings": [
    {
      "rule_id": "github-token",
      "category": "secret",
      "confidence": "high",
      "field_kind": "message_text",
      "path_class": "messages.content.text",
      "action": "warn"
    }
  ]
}
```

### 10.2. Privacy rules for events

Не писать в event:

- raw value;
- full prompt;
- surrounding prompt context;
- unredacted tool payload;
- authorization header;
- API key;
- full matched URL if it can contain credentials.

Можно писать при необходимости:

- safe rule id;
- stable category;
- action;
- route/destination class;
- match length bucket;
- salted or keyed fingerprint only if there is a concrete deduplication need.

Если fingerprint нужен, предпочтительнее keyed HMAC with separately protected
key, not raw hash of a low-entropy value.

### 10.3. Existing logs

Нужно отдельно пересмотреть:

- upstream error body persistence;
- exception string persistence;
- audit `error` fields;
- log retention;
- file permissions.

Активная защита не должна создавать новый канал утечки через observability.

## 11. Правила и детекторы

### 11.1. Recommended rule categories

Категории первого зрелого варианта:

- `secret.api_token`;
- `secret.private_key`;
- `secret.authorization`;
- `credential.password_like`;
- `credential.url_parameter`;
- `pii.email`;
- `pii.phone`;
- `pii.document_id`;
- `pii.payment_card`;
- `pii.custom`;
- `internal.custom`.

`attack` patterns вроде SQL keywords не являются ядром защиты от утечки
секретов и PII. Их нужно держать отдельно, если прокси вообще должен выполнять
content safety or prompt attack policy. Иначе они смешивают разные цели и
создают false positives.

### 11.2. Rule quality

Rule definition должна иметь:

- rationale;
- sample positives;
- sample negatives;
- default confidence;
- preferred action by destination;
- validator if needed;
- limit on body size or scanning cost if detector expensive.

### 11.3. Password-like data

Generic password detection - самый опасный источник ложных обещаний.

Recommended baseline:

1. High confidence:
   - structured key names like `password`, `passwd`, `client_secret`,
     `refresh_token`, `access_token`;
   - URL credentials;
   - authentication header material;
   - config snippets with clear key/value credential context.
2. Medium/low confidence:
   - natural-language text that says "my password is ...";
   - entropy without context.

Policy для low-confidence password-like text должна по умолчанию начинаться с
`audit` or `warn`, а не blind block.

## 12. Resource limits and failure modes

Защита сама не должна стать DoS surface.

Нужно определить:

- max body size для protected parsing;
- max extracted text bytes;
- max number of segments;
- max matches per request;
- detector time budget;
- regex safety review;
- behavior on oversize input.

Рекомендуемый behavior:

| Condition | Observe mode | Enforce mode |
| --- | --- | --- |
| Policy load error | startup fail or unhealthy | startup fail |
| Malformed protected JSON | reject request | reject request |
| Unsupported protected schema | warning and metric | block or reject |
| Scan budget exceeded | warning and metric | policy-defined block or reject |
| Detector internal error | warning and metric | fail according to mandatory policy |

Fail-open допустим только как явно выбранный policy behavior, а не как
неявное следствие `except Exception`.

## 13. Recommended module boundaries

Репозиторий сейчас маленький, но следующую реализацию лучше не встраивать
целиком в `proxy.py`.

Возможная структура:

```text
aproxy/
  proxy.py or app.py
  protection/
    model.py
    config.py
    routes.py
    extractors.py
    detectors.py
    validators.py
    policy.py
    transform.py
    audit.py
tests/
  protection/
    fixtures/
```

Если переход к package structure пока нежелателен, минимальный компромисс:

```text
proxy.py
protection.py
protection_policy.json
tests/test_protection.py
```

Даже в минимальном варианте не смешивать в одной функции:

- JSON traversal;
- regex execution;
- policy aggregation;
- body mutation;
- HTTP response construction.

## 14. Data model sketch

### 14.1. Segment

```python
@dataclass
class Segment:
    id: str
    path: tuple[str | int, ...]
    kind: str
    text: str
    mutability: Literal["substring_replace", "field_replace", "read_only"]
```

### 14.2. Finding

```python
@dataclass
class Finding:
    id: str
    rule_id: str
    detector_id: str
    category: str
    confidence: Literal["low", "medium", "high"]
    segment_id: str
    start: int
    end: int
    evidence_kind: str
```

### 14.3. Decision

```python
@dataclass
class Decision:
    request_action: Literal["allow", "audit", "warn", "mask", "block"]
    finding_actions: list[FindingAction]
    reason_codes: list[str]
    transformed_body: bytes | None
```

Data model нужен, чтобы:

- не передавать raw body между loosely defined helper functions;
- иметь stable tests;
- сохранять distinction между detector and policy.

## 15. Recommended first next implementation

Следующий рабочий инкремент должен быть меньше enterprise DLP, но уже
архитектурно правильным.

### 15.1. Scope первого следующего инкремента

Поддержать:

- only explicit `POST /v1/messages` protected egress;
- route allowlist that prevents unchecked model egress through catch-all;
- `system` and supported `messages` text fields;
- structured tool string traversal for known shapes;
- detector layer:
  - provider token signatures;
  - PEM key markers;
  - URL credential patterns;
  - structured password/token field names;
  - selected PII patterns with validators where practical;
- policy actions:
  - `audit`;
  - `warn`;
  - `mask`;
  - `block`;
- default runtime mode `observe` for rollout unless operator explicitly sets
  enforce policy.

### 15.2. Recommended default behavior in that increment

1. In `observe`:
   - no body mutation;
   - no blocks caused by detector matches;
   - emit privacy-safe decisions and coverage warnings.
2. In `enforce`:
   - high-confidence secret in cloud or unknown destination -> block;
   - high-confidence secret in local destination -> mask or block by policy;
   - structured credential field -> mask or block by policy;
   - medium PII -> warn or mask only if org policy says so;
   - unsupported model-visible schema -> block.

### 15.3. Why this increment

It avoids the two main failures of the first attempt:

- no unchecked alternate model route;
- no misleading claim that one `messages[].content` extraction covers a full
  request body.

It also gives operational learning before aggressive enforcement:

- false positive rate;
- common field kinds;
- which secrets users actually leak;
- which PII rules are usable in this environment.

## 16. Testing strategy

### 16.1. Unit tests

Нужны unit tests for:

- policy file validation;
- detector positive/negative examples;
- checksum validators;
- confidence assignment;
- policy aggregation precedence;
- masking range merge;
- log/audit serialization without raw match.

### 16.2. Extractor fixture tests

Для каждого supported shape нужны fixtures:

- user text message;
- assistant message if it can be proxied back in context;
- system prompt;
- content block arrays;
- tool use input;
- tool result content;
- nested structured string values;
- malformed body;
- new/unsupported block type.

Каждый fixture должен отвечать на вопрос:

```text
which exact strings are model-visible and are they extracted?
```

### 16.3. Route tests

Нужно проверить:

- explicit protected route goes through pipeline;
- metadata route does not falsely scan empty content;
- unknown model-capable route is not silently forwarded;
- catch-all cannot create unchecked egress;
- request body is transformed before upstream mock receives it.

### 16.4. Integration tests

With mock upstream:

- upstream receives raw body for `allow`;
- upstream receives masked body for `mask`;
- upstream receives nothing for `block`;
- audit event contains metadata only;
- streaming and non-streaming request paths both make decision before upstream.

### 16.5. Security regression tests from first attempt

Permanent regression tests:

1. Secret in `messages[].content` is detected.
2. Secret in `system` is detected.
3. Secret in known tool input/result string is detected.
4. Secret sent through alternate model route cannot bypass pipeline.
5. Missing policy in enforce mode does not silently disable protection.
6. Block/warn messages never contain the raw matched secret.

## 17. Observability and operations

### 17.1. Metrics

Recommended bounded metrics:

- `aproxy_data_protection_decisions_total{action,destination,route_class}`;
- `aproxy_data_protection_findings_total{category,confidence,action}`;
- `aproxy_data_protection_coverage_gaps_total{route_class,reason}`;
- `aproxy_data_protection_policy_load_failures_total`;
- `aproxy_data_protection_scan_duration_seconds`.

Avoid labels containing:

- username with high cardinality if many users exist;
- model string if it is unbounded;
- rule text;
- raw path;
- raw values.

### 17.2. Admin visibility

At startup:

```text
data protection mode=observe policy_version=1 rules=42 routes=3
```

Health/readiness should expose safe status:

```json
{
  "data_protection": {
    "mode": "observe",
    "policy_loaded": true,
    "policy_version": 1
  }
}
```

Do not expose policy internals or patterns publicly if that matters for threat
model. If `/health` remains public, expose only safe status or move detailed
status to authenticated endpoint.

### 17.3. Rollout

Recommended rollout sequence:

1. Enable route allowlist and common protection pipeline in staging.
2. Run `observe` on production traffic with privacy-safe events.
3. Measure categories, false positives and unsupported shapes.
4. Fix extraction gaps and suppressions.
5. Enable masking for selected high-confidence local use cases.
6. Enable blocking for high-confidence secrets to cloud or unknown destinations.
7. Review PII policy separately with organizational/legal requirements.

## 18. Detailed implementation plan

### Phase 0. Decide contract

Outputs:

- route inventory;
- destination classification contract;
- supported request shapes list;
- initial action matrix;
- policy file schema.

Decisions made in the native Ollama allowlist increment:

- catch-all stays only as authenticated deny-by-default;
- Ollama non-Anthropic model routes are supported only through explicit allowlist:
  `/api/generate`, `/api/chat`, `/api/embed`;
- `GET /api/tags` is a public metadata exception for native Ollama model pickers
  such as Cline; unauthenticated requests are audited by client IP, with repeated
  audit/proxy-log records suppressed by `public_tags_log_suppress_seconds`;
- native Ollama metadata routes are allowlisted:
  `/api/ps`, `/api/version`, `/api/show`;
- native Ollama admin/model-management routes are denied:
  `/api/create`, `/api/copy`, `/api/pull`, `/api/push`, `/api/delete`;

Open decisions:

- how model names map to `local_model` or `cloud_model`;
- what to do with unknown destination and unsupported shapes.

### Phase 1. Build protection skeleton

Tasks:

1. Introduce protection model objects:
   - `Segment`;
   - `Finding`;
   - `Decision`.
2. Introduce route classification.
3. Introduce a single protected upstream forwarding function.
4. Make explicit routes call that function.
5. Make catch-all reject or classify before forward.
6. Add mock-upstream tests proving the path is centralized.

Exit criteria:

- no model-capable request reaches upstream outside a reviewed forwarding path.

### Phase 2. Implement extraction coverage

Tasks:

1. Implement Anthropic messages extractor.
2. Add fixture corpus.
3. Extract `system`.
4. Extract supported text blocks.
5. Extract known tool payload string leaves.
6. Surface `coverage_status`.
7. Add unsupported-shape tests.

Exit criteria:

- every supported model-visible string in fixtures maps to a segment.

### Phase 3. Implement detectors

Tasks:

1. Implement detector interface.
2. Add regex signature detector with validated config.
3. Add structured-key contextual detector.
4. Add selected validators.
5. Add positive/negative test corpus per rule.
6. Add resource limits.

Exit criteria:

- findings are structured and detectors do not decide HTTP outcome.

### Phase 4. Implement policy and actions

Tasks:

1. Implement policy schema validation.
2. Implement action matrix by destination/confidence/category.
3. Implement aggregation precedence.
4. Implement `observe`, `warn`, `mask`, `block`.
5. Implement mask transformer over parsed body.
6. Add safe error/warning message builder.

Exit criteria:

- the same finding can produce different actions by policy and destination.

### Phase 5. Implement audit and operations

Tasks:

1. Emit privacy-safe decision events.
2. Add bounded metrics.
3. Add startup policy status.
4. Add health/readiness policy status.
5. Remove or redact raw sensitive error persistence paths where appropriate.
6. Document runtime configuration and examples.

Exit criteria:

- operator can prove whether protection is active without reading code.

### Phase 6. Roll out policy

Tasks:

1. Run observe mode.
2. Review false positives and coverage gaps.
3. Add narrow suppressions.
4. Enable selected masking.
5. Enable selected blocks.
6. Document incident and override workflow.

Exit criteria:

- enforcement is based on measured behavior, not assumed regex quality.

## 19. Acceptance criteria

Следующая реализация считается готовой к первой эксплуатации, если:

1. Every supported model egress route is classified.
2. Every classified protected model route enters the same protection pipeline.
3. Secret in `system`, text content and supported tool string payload is
   detected in fixtures.
4. Unknown model route cannot silently bypass the pipeline.
5. Actions are policy-driven, not hardcoded in detectors.
6. `mask` changes upstream body without breaking JSON.
7. `block` prevents any upstream call.
8. `warn/audit` events do not contain raw sensitive values.
9. Enforce mode cannot start with missing or invalid policy unnoticed.
10. Docs state coverage, limitations and operator knobs.

## 20. Open questions for later

These questions should be resolved before broad enforce mode:

1. Как надежно классифицировать local versus cloud models in current Ollama
   setup?
2. Должен ли proxy поддерживать non-Anthropic Ollama model routes at all?
3. Какие PII classes реально нужны organization policy?
4. Нужны ли user/group exceptions and who approves them?
5. Нужно ли предупреждать end user в API response, или достаточно operator
   audit plus metrics?
6. Что делать с attachments, images, binary inputs and future content blocks?
7. Требуется ли response-side protection on model output, or request egress is
   the only scope?
8. Нужен ли hot reload policy, or restart is safer initially?

## 21. Decision log to avoid repeating mistakes

### Decided

- Не начинать новую попытку с hardcoded handler-local block regex.
- Не смешивать detector and policy action.
- Не считать `messages[].content` полным coverage of request body.
- Не оставлять unchecked catch-all for model egress.
- Не логировать raw matched text.
- Не обещать reliable generic password detection without context.
- Не смешивать data-loss prevention and prompt attack keyword blocking in one
  default rule pack.

### Recommended next implementation posture

- Build architecture for enforcement.
- Start runtime policy in observe mode.
- Enforce high-confidence cases only after extraction and observability are
  proven.

## 22. Summary

Правильная следующая реализация для `aproxy` - это не "лучший `rules.json`".
Это контролируемый egress subsystem:

- с route coverage;
- со schema-aware extraction;
- с detector layer;
- с policy engine;
- с flexible actions;
- с privacy-safe observability;
- с постепенным переходом от observe к enforce.

Такой фундамент позволяет позже добавлять новые secret signatures, PII
validators, organization-specific policies и более строгие destination rules
без повторения архитектурных ошибок первой попытки.
