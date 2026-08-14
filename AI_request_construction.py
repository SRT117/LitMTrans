"""OneAPI request-body constructions shared by chat and translation.

Only protocol shapes live here. Authentication remains the user's configured API key;
no desktop identity, OAuth token, attestation, or provider-specific secret is imitated.
"""

from __future__ import annotations

import base64
import re
import threading
from contextlib import contextmanager
from urllib.parse import unquote_to_bytes


REQUEST_BODY_MODE_CODEX = "codex"
REQUEST_BODY_MODE_CLAUDE = "claude"
REQUEST_BODY_MODE_STANDARD = "standard"

# 按给定的 DeepSeek 官方并发能力，本程序只开放其中一部分，
# 将同一进程内的 DeepSeek 文本推理请求统一硬限制为 100。
DEEPSEEK_REQUEST_CONCURRENCY_LIMIT = 100
_DEEPSEEK_REQUEST_SEMAPHORE = threading.BoundedSemaphore(
    DEEPSEEK_REQUEST_CONCURRENCY_LIMIT
)


def is_deepseek_request(provider_id: str) -> bool:
    """仅限制用户明确选择的 DeepSeek 直连服务，不误伤 OneAPI 等代理服务。"""
    return str(provider_id or "").strip().lower() == "deepseek"


def acquire_provider_request_slot(provider_id: str, should_stop=None):
    """Acquire a provider slot while allowing document chat to be cancelled."""
    if not is_deepseek_request(provider_id):
        return None

    while not _DEEPSEEK_REQUEST_SEMAPHORE.acquire(timeout=0.2):
        if should_stop and should_stop():
            raise InterruptedError("等待 DeepSeek 请求槽时已取消。")

    return _DEEPSEEK_REQUEST_SEMAPHORE


def release_provider_request_slot(slot) -> None:
    """释放由 acquire_provider_request_slot 取得的请求槽。"""
    if slot is not None:
        slot.release()


@contextmanager
def provider_request_slot(provider_id: str):
    """在普通响应或流式响应的完整生命周期内占用请求槽。"""
    slot = acquire_provider_request_slot(provider_id)
    try:
        yield
    finally:
        release_provider_request_slot(slot)


def is_openai_compatible_provider(provider_id: str) -> bool:
    return str(provider_id or "").strip().lower() in {"oneapi", "openai_compatible"}


def normalize_request_body_mode(provider_id: str, mode: str) -> str:
    """OpenAI-compatible gateways use the Chat Completions protocol.

    Older settings may still contain ``claude``.  Keep accepting that value so
    existing settings files load, but deliberately migrate it to the single
    supported construction instead of sending a different wire protocol.
    """
    if not is_openai_compatible_provider(provider_id):
        return REQUEST_BODY_MODE_STANDARD
    return REQUEST_BODY_MODE_CODEX


def uses_codex_construction(provider_id: str, mode: str) -> bool:
    return normalize_request_body_mode(provider_id, mode) == REQUEST_BODY_MODE_CODEX


def uses_claude_construction(provider_id: str, mode: str) -> bool:
    return normalize_request_body_mode(provider_id, mode) == REQUEST_BODY_MODE_CLAUDE


def request_url_for_construction(base_url: str, provider_id: str, mode: str) -> str:
    """Return the sole supported OpenAI-compatible endpoint."""
    root = str(base_url or "").rstrip("/")
    for suffix in ("/chat/completions", "/messages", "/responses"):
        if root.endswith(suffix):
            root = root[: -len(suffix)].rstrip("/")
            break
    return root + "/chat/completions"


def claude_headers(api_key: str, stream: bool) -> dict[str, str]:
    """Headers required by Anthropic Messages compatible gateways."""
    return {
        "x-api-key": str(api_key or ""),
        "anthropic-version": "2023-06-01",
        # NewAPI / OneAPI implementations commonly gate prompt cache support with
        # this compatibility flag; native endpoints harmlessly ignore it when no
        # longer required.
        "anthropic-beta": "prompt-caching-2024-07-31",
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "text/event-stream" if stream else "application/json",
        "User-Agent": "LitMTrans/1.0",
    }


def _to_anthropic_image_block(url: str) -> dict | None:
    """Convert an OpenAI data URL into a Messages image source without fetching URLs."""
    if not isinstance(url, str) or not url.startswith("data:"):
        return None
    match = re.match(r"^data:([^;,]+)(;base64)?,(.*)$", url, re.DOTALL)
    if not match:
        return None
    media_type, base64_marker, encoded_data = match.groups()
    try:
        raw_data = encoded_data if base64_marker else base64.b64encode(unquote_to_bytes(encoded_data)).decode("ascii")
        # Validate to prevent malformed persisted content from causing opaque API errors.
        base64.b64decode(raw_data, validate=True)
    except Exception:
        return None
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type or "image/png",
            "data": raw_data,
        },
    }


def _content_to_claude_blocks(content) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return [{"type": "text", "text": str(content or "")}]

    blocks: list[dict] = []
    for part in content:
        if isinstance(part, str):
            blocks.append({"type": "text", "text": part})
            continue
        if not isinstance(part, dict):
            continue
        kind = str(part.get("type") or "")
        if kind in {"text", "input_text"}:
            blocks.append({"type": "text", "text": str(part.get("text") or "")})
        elif kind == "image_url":
            image_value = part.get("image_url")
            image_url = image_value.get("url") if isinstance(image_value, dict) else image_value
            image_block = _to_anthropic_image_block(str(image_url or ""))
            if image_block is None:
                # Anthropic Messages accepts base64 image sources, not an OpenAI
                # image_url. Keep a clear textual placeholder rather than silently
                # dropping an attachment.
                blocks.append({"type": "text", "text": "[此图片无法按 Claude Messages 格式传输：仅支持本地 data URL 图片。]"})
            else:
                blocks.append(image_block)
    return blocks or [{"type": "text", "text": ""}]


def _append_cache_control(blocks: list[dict]) -> None:
    """Mark the stable ending block without changing its text or message order."""
    for block in reversed(blocks):
        if block.get("type") == "text":
            block["cache_control"] = {"type": "ephemeral"}
            return


def build_claude_messages_payload(
    model: str,
    messages: list[dict],
    stream: bool,
    max_tokens: int = 8192,
    temperature: float | None = None,
) -> dict:
    """Losslessly map the app's OpenAI-style history to Anthropic Messages.

    System messages become the dedicated `system` field. Consecutive user/assistant
    items are merged as required by Messages. Cache markers are attached to the
    stable system material and to the final user content block. The latter is
    intentional: it writes the complete current prefix, so the very next turn
    can read that cache before its new user message is appended.
    """
    system_blocks: list[dict] = []
    output_messages: list[dict] = []
    for item in messages or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "user")
        blocks = _content_to_claude_blocks(item.get("content"))
        if role in {"system", "developer"}:
            system_blocks.extend(blocks)
            continue
        if role not in {"user", "assistant"}:
            role = "user"
        if output_messages and output_messages[-1]["role"] == role:
            output_messages[-1]["content"].extend(blocks)
        else:
            output_messages.append({"role": role, "content": blocks})

    if system_blocks:
        _append_cache_control(system_blocks)
    if output_messages and output_messages[-1]["role"] == "user":
        _append_cache_control(output_messages[-1]["content"])

    payload = {
        "model": str(model or ""),
        "messages": output_messages or [{"role": "user", "content": [{"type": "text", "text": ""}]}],
        "max_tokens": int(max_tokens),
        "stream": bool(stream),
    }
    if system_blocks:
        payload["system"] = system_blocks
    if temperature is not None:
        payload["temperature"] = float(temperature)
    return payload


def normalize_claude_usage(usage: dict, previous: dict | None = None) -> dict:
    """Expose Anthropic cache usage through the app's existing OpenAI-style UI."""
    result = dict(previous or {})
    if not isinstance(usage, dict):
        return result
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or result.get("completion_tokens") or 0)
    cached_tokens = int(usage.get("cache_read_input_tokens") or 0)
    cache_write_tokens = int(usage.get("cache_creation_input_tokens") or 0)
    result.update(
        {
            "prompt_tokens": input_tokens + cached_tokens + cache_write_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + cached_tokens + cache_write_tokens + output_tokens,
            "prompt_tokens_details": {
                "cached_tokens": cached_tokens,
                "cache_write_tokens": cache_write_tokens,
            },
        }
    )
    return result


def extract_claude_response_text(response: dict) -> str:
    """Extract text from a non-streaming Messages response."""
    pieces = []
    for block in (response or {}).get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            pieces.append(str(block.get("text") or ""))
    return "".join(pieces)
