#!/usr/bin/env python3
"""Стартовая точка: простой запрос к Ollama API с аутентификацией по токену.

Переменные окружения:
  OLLAMA_BASE_URL       базовый URL Ollama (по умолчанию http://ollama.int.alarislabs.com:4001)
  OLLAMA_AUTH_TOKEN     токен аутентификации (отправляется как Authorization: Bearer) — ОБЯЗАТЕЛЬНО
  OLLAMA_MODEL          имя модели (по умолчанию glm-5.2:cloud)
"""

import json
import os
import sys
import urllib.request

base_url = os.environ.get("OLLAMA_BASE_URL", "http://ollama.int.alarislabs.com:4001").rstrip("/")

token = os.environ.get("OLLAMA_AUTH_TOKEN")
if not token:
    sys.exit("Ошибка: OLLAMA_AUTH_TOKEN обязательно должен быть задан.")

model = os.environ.get("OLLAMA_MODEL", "glm-5.2:cloud")

payload = {
    "model": model,
    "messages": [{"role": "user", "content": "Сравни Python и Go"}],
    "stream": False,
}

request = urllib.request.Request(
    f"{base_url}/api/chat",
    data=json.dumps(payload).encode("utf-8"),
    headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    },
)

with urllib.request.urlopen(request) as response:
    result = json.loads(response.read().decode("utf-8"))

print(result["message"]["content"])