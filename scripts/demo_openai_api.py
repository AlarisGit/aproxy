#!/usr/bin/env python3
"""Стартовая точка: простой запрос к OpenAI-compatible API через aproxy.

Переменные окружения:
  OPENAI_BASE_URL       базовый URL aproxy (по умолчанию http://ollama.int.alarislabs.com:4001)
  OPENAI_API_KEY        aproxy token (отправляется как Authorization: Bearer) — ОБЯЗАТЕЛЬНО
  OPENAI_MODEL          имя модели Ollama (по умолчанию glm-5.2:cloud)
"""

import json
import os
import sys
import urllib.request

base_url = os.environ.get("OPENAI_BASE_URL", "http://ollama.int.alarislabs.com:4001").rstrip("/")

token = os.environ.get("OPENAI_API_KEY")
if not token:
    sys.exit("Ошибка: OPENAI_API_KEY обязательно должен быть задан.")

model = os.environ.get("OPENAI_MODEL", "glm-5.2:cloud")

payload = {
    "model": model,
    "messages": [{"role": "user", "content": "Сравни Python и Go"}],
    "stream": False,
}

request = urllib.request.Request(
    f"{base_url}/v1/chat/completions",
    data=json.dumps(payload).encode("utf-8"),
    headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    },
)

with urllib.request.urlopen(request) as response:
    result = json.loads(response.read().decode("utf-8"))

print(result["choices"][0]["message"]["content"])
