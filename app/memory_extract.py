"""Long-term memory extraction (phase 2, iteration 02).

Responsibilities kept narrow on purpose:
* build the extraction prompt for a recent conversation window;
* call the OpenAI-compatible Chat Completions API and ask for JSON;
* validate every returned candidate with Pydantic and drop bad items;
* filter obvious secrets and duplicate contents.

This module never writes to the database and never logs conversation
contents or API keys. The caller (app.main) is responsible for storage
and for making sure a failure here never breaks the chat endpoint.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Sequence
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError, field_validator

from app.model_config import load_model_config

logger = logging.getLogger("personal_ai_os.memory")

MEMORY_TYPES = {"fact", "preference", "goal", "experience", "pattern", "other"}
SOURCE_TYPES = {"conversation", "manual", "import"}
MAX_CANDIDATES = 10
MAX_CONTENT = 2000
MAX_REASON = 500
MAX_WINDOW_MESSAGES = 12
MAX_MESSAGE_CHARS = 1500

# Content-level guard rails. The prompt also asks the model not to extract
# secrets, but model output is untrusted so we filter defensively.
_SECRET_PATTERNS = [
    re.compile(r"(?i)(?:password|passwd|pwd|密码|口令)\s*[:：=]\s*\S+"),
    re.compile(r"(?i)(?:api[_-]?key|secret|access[_-]?token|auth[_-]?token|token|令牌|密钥)\s*[:：=]\s*[\"']?\S{6,}"),
    # hyphenated key-shaped tokens: api-key-sk-123, openai_key-abcdef1234.
    # The ":"-based pattern above misses these embedded forms.
    re.compile(r"(?i)\b\w*key\w*[-_][a-z0-9][a-z0-9_-]{3,}\b"),
    re.compile(r"(?i)\bsk-[a-z0-9][a-z0-9-]{12,}"),
    re.compile(r"(?i)\bbearer\s+[a-z0-9._\-]{8,}"),
    re.compile(r"\b[a-f0-9]{40,}\b", re.IGNORECASE),
]

EXTRACTION_SYSTEM_PROMPT = (
    "你是严格的个人长期记忆提取器。只从用户明确说出的内容中提取值得长期保存的信息："
    "稳定偏好、长期目标、持续进行的项目状态、反复出现的行为规律、重要经历。"
    "不要提取：临时问题、一次性安排、无意义闲聊、模型自己的推测或建议、无法从原文证实的内容。"
    "绝不提取密码、口令、API Key、密钥、令牌、Cookie 等任何认证凭据。"
    "只输出一个 JSON 对象，不要输出任何解释或 Markdown，格式为："
    '{"candidates":[{"content":"简洁的第三人称事实陈述","memory_type":"fact|preference|goal|experience|pattern|other",'
    '"source_type":"conversation","source_id":null,"confidence":0.0,"reason":"为什么值得长期保存"}]}。'
    "confidence 为 0 到 1 之间的数字；reason 用一句中文说明依据。"
    "如果没有值得长期保存的信息，返回 {\"candidates\": []}。"
)


class CandidateItem(BaseModel):
    content: str = Field(min_length=1, max_length=MAX_CONTENT)
    memory_type: str
    source_type: str = "conversation"
    source_id: int | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(min_length=1, max_length=MAX_REASON)

    @field_validator("content", "reason")
    @classmethod
    def _strip_nonempty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("字段不能为空")
        return value

    @field_validator("memory_type")
    @classmethod
    def _validate_memory_type(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in MEMORY_TYPES:
            raise ValueError(f"非法 memory_type: {value}")
        return value

    @field_validator("source_type")
    @classmethod
    def _validate_source_type(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in SOURCE_TYPES:
            raise ValueError(f"非法 source_type: {value}")
        return value


def _looks_like_secret(content: str) -> bool:
    return any(pattern.search(content) for pattern in _SECRET_PATTERNS)


def _extract_json_object(raw_text: str) -> dict[str, Any] | None:
    text = raw_text.strip()
    # Tolerate models wrapping JSON in Markdown code fences.
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None


def parse_extraction_json(raw_text: str, conversation_id: int | None) -> list[dict[str, Any]]:
    """Validate raw model text; return normalized candidate dicts.

    Invalid items are dropped individually while valid items survive.
    `source_type` / `source_id` are always set from the server-side
    conversation context, never trusted from the model.
    """
    payload = _extract_json_object(raw_text)
    if payload is None:
        return []
    raw_items = payload.get("candidates")
    if not isinstance(raw_items, list):
        return []

    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            continue
        try:
            item = CandidateItem.model_validate(raw_item)
        except (ValidationError, TypeError):
            continue
        content = item.content
        if _looks_like_secret(content):
            logger.info("memory candidate dropped: content matched secret pattern")
            continue
        key = content.casefold()
        if key in seen:
            continue
        seen.add(key)
        candidates.append(
            {
                "content": content,
                "memory_type": item.memory_type,
                "source_type": "conversation",
                "source_id": conversation_id,
                "confidence": item.confidence,
                "reason": item.reason,
            }
        )
    return candidates[:MAX_CANDIDATES]


def build_user_prompt(messages: Sequence[dict[str, str]]) -> str:
    window = list(messages)[-MAX_WINDOW_MESSAGES:]
    lines = []
    for msg in window:
        role = "用户" if msg.get("role") == "user" else "助手"
        content = str(msg.get("content", ""))[:MAX_MESSAGE_CHARS]
        lines.append(f"{role}：{content}")
    return (
        "下面是一段对话的最近内容，请提取值得长期保存的个人记忆候选，"
        "并只按要求输出 JSON。\n\n" + "\n".join(lines)
    )


async def extract_candidates_from_messages(
    messages: Sequence[dict[str, str]], conversation_id: int | None
) -> list[dict[str, Any]]:
    """Call the cloud model and return validated candidates.

    Returns [] in local-demo mode (no API key) or when the model returns
    no usable JSON. Transport-level failures propagate to the caller so it
    can log them; callers must treat this as a non-critical side path.
    """
    config = load_model_config()
    if not config.enabled:
        return []
    if not any(m.get("role") == "user" and str(m.get("content", "")).strip() for m in messages):
        return []

    base_url = config.base_url
    model = config.model
    request_messages = [
        {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(messages)},
    ]

    last_exc: Exception | None = None
    async with httpx.AsyncClient(timeout=45) as client:
        # Extraction is best-effort: only two short retries so chat stays responsive.
        for attempt in range(2):
            try:
                response = await client.post(
                    f"{base_url}/chat/completions",
                    headers={**config.authorization_header, "Content-Type": "application/json"},
                    json={
                        "model": model,
                        "messages": request_messages,
                        "temperature": 0.0,
                        "response_format": {"type": "json_object"},
                    },
                )
                response.raise_for_status()
                data = response.json()
                raw_text = data["choices"][0]["message"]["content"]
                return parse_extraction_json(raw_text, conversation_id)
            except Exception as exc:
                last_exc = exc
                if attempt == 0:
                    await asyncio.sleep(2)
    assert last_exc is not None
    raise last_exc
