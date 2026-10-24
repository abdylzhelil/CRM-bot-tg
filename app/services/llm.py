"""Российские ИИ-сервисы для CRM-бота: GigaChat (Сбер), YandexGPT / AI Studio, своя модель (Ollama).

Те же ключи и строки .env, что у бота модерации (AI_PROVIDER, GIGACHAT_*, YANDEX_*, LOCAL_AI*), — достаточно
скопировать их в .env CRM. Запросы — короткие тексты без имён, ников и ID людей. Ответ — строка; любая ошибка
(сеть, ключ, лимит) → LlmError, вызывающий работает по правилам, как без ИИ.
Код вызовов повторяет bot/core/ai.py бота модерации (сам бот модерации не меняем).
"""
import asyncio
import json
import logging
import re
import time
import uuid

from app import config

log = logging.getLogger(__name__)

PROVIDERS = ("gigachat", "yandex", "local", "anthropic")
NAMES = {"gigachat": "GigaChat (Сбер)", "yandex": "YandexGPT", "local": "своя модель на сервере", "anthropic": "Claude"}
TIMEOUT = 20
LOCAL_TIMEOUT = 60  # своя модель на процессоре думает дольше
GIGACHAT_OAUTH = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
GIGACHAT_API = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
YANDEX_API = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
YANDEX_OPENAI = "https://llm.api.cloud.yandex.net/v1/chat/completions"

_token: tuple[float, str] | None = None  # GigaChat: (до когда, токен доступа)
_lock = asyncio.Lock()


class LlmError(Exception):
    pass


def has_key(provider: str) -> bool:
    if provider == "gigachat":
        return bool(config.GIGACHAT_AUTH_KEY)
    if provider == "yandex":
        return bool(config.YANDEX_API_KEY and config.YANDEX_FOLDER_ID)
    if provider == "local":
        return config.LOCAL_AI
    if provider == "anthropic":
        return bool(config.ANTHROPIC_API_KEY)
    return False


def available() -> list[str]:
    return [p for p in PROVIDERS if has_key(p)]


def pick(choice: str = "") -> str:
    """Какой ИИ работает: выбор в ⚙️ (ai_provider), иначе AI_PROVIDER из .env, иначе первый с ключом. "" — нет ни одного."""
    for p in (choice, config.AI_PROVIDER):
        if p and p != "auto" and has_key(p):
            return p
    return next(iter(available()), "")


def _strip_thinking(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL).strip()


def extract_json(raw: str) -> dict | None:
    """Первый JSON-объект в ответе (модели любят обрамлять его текстом или ```)."""
    start, end = (raw or "").find("{"), (raw or "").rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start:end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


async def _gigachat_token(http) -> str:
    global _token
    async with _lock:
        if _token and _token[0] > time.monotonic():
            return _token[1]
        headers = {"Authorization": f"Basic {config.GIGACHAT_AUTH_KEY}", "RqUID": str(uuid.uuid4()),
                   "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
        async with http.post(GIGACHAT_OAUTH, data={"scope": config.GIGACHAT_SCOPE}, headers=headers) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200 or "access_token" not in data:
                raise LlmError(f"gigachat вход {resp.status}")
        _token = (time.monotonic() + 25 * 60, data["access_token"])  # токен живёт 30 минут
        return _token[1]


async def _gigachat(system: str, text: str, max_tokens: int) -> str:
    import aiohttp

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as http:
        token = await _gigachat_token(http)
        body = {"model": config.GIGACHAT_MODEL, "temperature": 0.01, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": text}]}
        async with http.post(GIGACHAT_API, json=body,
                             headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                raise LlmError(f"gigachat {resp.status}")
            return data["choices"][0]["message"]["content"]


def _yandex_uri() -> str:
    model = config.YANDEX_MODEL.strip()
    return model if model.startswith("gpt://") else f"gpt://{config.YANDEX_FOLDER_ID}/{model}"


async def _yandex(system: str, text: str, max_tokens: int) -> str:
    import aiohttp

    native = config.YANDEX_MODEL.strip().removeprefix(f"gpt://{config.YANDEX_FOLDER_ID}/").startswith("yandexgpt")
    headers = {"Authorization": f"Api-Key {config.YANDEX_API_KEY}", "x-folder-id": config.YANDEX_FOLDER_ID}
    if native:
        url, body = YANDEX_API, {
            "modelUri": _yandex_uri(),
            "completionOptions": {"stream": False, "temperature": 0, "maxTokens": str(max_tokens)},
            "messages": [{"role": "system", "text": system}, {"role": "user", "text": text}]}
    else:
        url, body = YANDEX_OPENAI, {
            "model": _yandex_uri(), "temperature": 0, "max_tokens": max_tokens, "reasoning_effort": "none",
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": text}]}
        headers["OpenAI-Project"] = config.YANDEX_FOLDER_ID
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as http:
        async with http.post(url, json=body, headers=headers) as resp:
            data, status = await resp.json(content_type=None), resp.status
        if status == 400 and not native:  # модель без режима рассуждения
            body.pop("reasoning_effort", None)
            async with http.post(url, json=body, headers=headers) as resp:
                data, status = await resp.json(content_type=None), resp.status
        if status == 401 and not native:  # OpenAI-совместимый вход иногда ждёт «Bearer»
            headers["Authorization"] = f"Bearer {config.YANDEX_API_KEY}"
            async with http.post(url, json=body, headers=headers) as resp:
                data, status = await resp.json(content_type=None), resp.status
    if status != 200:
        raise LlmError(f"yandex {status}")
    if native:
        return data["result"]["alternatives"][0]["message"]["text"]
    return data["choices"][0]["message"]["content"]


async def _local(system: str, text: str, max_tokens: int, json_mode: bool) -> str:
    import aiohttp

    body = {"model": config.LOCAL_AI_MODEL, "stream": False, "options": {"temperature": 0, "num_predict": max_tokens},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": text}]}
    if json_mode:
        body["format"] = "json"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=LOCAL_TIMEOUT)) as http:
        async with http.post(config.LOCAL_AI_URL.rstrip("/") + "/api/chat", json=body) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                raise LlmError(f"ollama {resp.status}")
            return data["message"]["content"]


async def complete(provider: str, system: str, text: str, max_tokens: int = 400, json_mode: bool = False) -> str:
    """Ответ модели (gigachat / yandex / local). LlmError — не вышло."""
    try:
        if provider == "gigachat":
            raw = await asyncio.wait_for(_gigachat(system, text, max_tokens), TIMEOUT + 5)
        elif provider == "yandex":
            raw = await asyncio.wait_for(_yandex(system, text, max_tokens), TIMEOUT + 5)
        elif provider == "local":
            raw = await asyncio.wait_for(_local(system, text, max_tokens, json_mode), LOCAL_TIMEOUT + 5)
        else:
            raise LlmError(f"неизвестный ИИ {provider}")
    except LlmError:
        raise
    except Exception as e:  # сеть, сертификат, формат ответа
        raise LlmError(f"{provider}: {type(e).__name__}") from None
    return _strip_thinking(raw)


def schema_hint(schema: dict) -> str:
    """Описание JSON-ответа словами для моделей без строгой схемы."""
    props = schema.get("properties", {})
    parts = []
    for name, spec in props.items():
        kind = spec.get("type")
        if "enum" in spec:
            kind = " или ".join(f'"{v}"' for v in spec["enum"])
        elif kind == "array":
            kind = "список " + ("строк" if spec.get("items", {}).get("type") == "string" else "чисел")
        elif kind == "boolean":
            kind = "true или false"
        elif kind == "integer":
            kind = "целое число"
        parts.append(f'"{name}": {kind}')
    return "Ответь ТОЛЬКО JSON без пояснений: {" + ", ".join(parts) + "}"


def fits(data: dict | None, schema: dict) -> dict | None:
    """Ответ похож на нужный: все поля есть и примерно нужного типа; лишнее отбрасываем."""
    if not isinstance(data, dict):
        return None
    out = {}
    for name, spec in schema.get("properties", {}).items():
        if name not in data:
            return None
        value, kind = data[name], spec.get("type")
        if "enum" in spec and value not in spec["enum"]:
            return None
        if kind == "boolean" and not isinstance(value, bool):
            if str(value).lower() in ("true", "false"):
                value = str(value).lower() == "true"
            else:
                return None
        if kind == "integer":
            try:
                value = int(value)
            except (TypeError, ValueError):
                return None
        if kind == "array" and not isinstance(value, list):
            return None
        if kind == "string" and not isinstance(value, str):
            value = str(value)
        out[name] = value
    return out
