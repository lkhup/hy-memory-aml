"""Compatibility fix for Hy-Memory 2.0.3's shared LLM HTTP client.

Upstream closes a cached AsyncOpenAI client on *every* request exception.
Concurrent requests share that client: one 502/429 can disconnect its peers.
Keep the pool alive until provider shutdown; httpx handles failed connections
individually. Extraction, reconciliation, prompts and outer retries stay intact.
"""

from __future__ import annotations

import asyncio
import logging
import time
from importlib.metadata import version
from typing import Any

logger = logging.getLogger(__name__)


async def shared_pool_chat(
    self: Any,
    messages: list[dict[str, Any]],
    *,
    max_tokens: int,
    temperature: float | None = None,
    stop: list[str] | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any = None,
) -> Any:
    from hy_memory.agent.llm_provider import LLMResponse, _serialize_tool_calls
    from hy_memory.core.workload import llm_slot
    from openai import AsyncOpenAI

    api_key, base_url = self._resolve_endpoint()
    loop = asyncio.get_running_loop()
    base_api_key = (api_key or "").split("?", 1)[0]
    client_key = (base_url or "", base_api_key, self._llm_config.timeout)
    clients = self._clients.setdefault(loop, {})
    client = clients.get(client_key)
    if client is None:
        options = {
            "api_key": base_api_key,
            "base_url": base_url,
            "timeout": self._llm_config.timeout,
            "max_retries": 0,
        }
        transport_factory = getattr(self.config, "http_transport_factory", None)
        if callable(transport_factory):
            import httpx

            options["http_client"] = httpx.AsyncClient(transport=transport_factory())
        client = AsyncOpenAI(**options)
        clients[client_key] = client

    if not any(m.get("role") == "system" for m in messages):
        messages = [{"role": "system", "content": ""}] + list(messages)
    kwargs: dict[str, Any] = {
        "model": self._llm_config.model,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    resolved_temperature = self._resolve_temperature(temperature)
    if resolved_temperature is not None:
        kwargs["temperature"] = resolved_temperature
    if stop:
        kwargs["stop"] = stop
    if tools:
        kwargs["tools"] = tools
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
    headers = dict(self._llm_config.extra_headers or {})
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if headers:
        kwargs["extra_headers"] = headers
    if self._llm_config.extra_body:
        kwargs["extra_body"] = self._llm_config.extra_body

    started = time.perf_counter()
    try:
        async with llm_slot():
            response = await client.chat.completions.create(**kwargs)
    except Exception as exc:
        # Do not close/pop the shared client here, including for transport
        # failures. Peers may be actively reading responses from its pool.
        logger.warning(
            "llm_attempt outcome=error type=%s status=%s seconds=%.3f",
            type(exc).__name__,
            getattr(exc, "status_code", None),
            time.perf_counter() - started,
        )
        raise

    msg, usage, finish, count = self._extract_response(response)
    reasoning_content, reasoning_tokens = self._extract_reasoning(msg, usage)
    logger.info(
        "llm_attempt outcome=ok seconds=%.3f prompt_tokens=%d completion_tokens=%d",
        time.perf_counter() - started,
        usage.prompt_tokens if usage else 0,
        usage.completion_tokens if usage else 0,
    )
    return LLMResponse(
        content=msg.content or "",
        tokens_used=usage.total_tokens if usage else 0,
        prompt_tokens=usage.prompt_tokens if usage else 0,
        completion_tokens=usage.completion_tokens if usage else 0,
        model=getattr(response, "model", None) or self._llm_config.model,
        finish_reason=finish,
        response_id=getattr(response, "id", "") or "",
        choices_count=count,
        reasoning_content=reasoning_content,
        reasoning_tokens=reasoning_tokens,
        tool_calls=_serialize_tool_calls(getattr(msg, "tool_calls", None)),
    )


def install_shared_pool_fix() -> None:
    from hy_memory.agent.llm_provider import LLMProvider

    if version("hy-memory") != "2.0.3":
        raise RuntimeError("Review the LLM pool compatibility fix before upgrading hy-memory")
    LLMProvider._chat_completions = shared_pool_chat
    logger.info("Hy-Memory 2.0.3 shared LLM pool fix enabled")
