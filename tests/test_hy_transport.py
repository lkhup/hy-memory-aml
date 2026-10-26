import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.hy_transport import shared_pool_chat


@pytest.mark.parametrize("fixed", [False, True])
def test_one_failed_request_does_not_disconnect_fifteen_peers(fixed):
    from hy_memory.agent.llm_provider import LLMConfig, LLMProvider

    async def run():
        entered = 0
        all_entered = asyncio.Event()
        failure_processed = asyncio.Event()
        closed = False

        class FakeClient:
            def __init__(self, **kwargs):
                self.chat = SimpleNamespace(completions=self)

            async def create(self, **kwargs):
                nonlocal entered
                index = entered
                entered += 1
                if entered == 16:
                    all_entered.set()
                await all_entered.wait()
                if index == 0:
                    raise ConnectionError("one injected upstream failure")
                await failure_processed.wait()
                if closed:
                    raise ConnectionError("shared client closed under an active request")
                return SimpleNamespace(
                    choices=[SimpleNamespace(
                        message=SimpleNamespace(content="ok", tool_calls=None),
                        finish_reason="stop",
                    )],
                    usage=None,
                    id="test",
                    model="test",
                )

            async def close(self):
                nonlocal closed
                closed = True

        provider = object.__new__(LLMProvider)
        provider._llm_config = LLMConfig(api_key="test", base_url="https://example.test/v1")
        provider.config = SimpleNamespace(http_transport_factory=None)
        provider._clients = {}
        method = shared_pool_chat if fixed else LLMProvider._chat_completions

        async def call(index):
            try:
                return await method(provider, [{"role": "user", "content": "test"}], max_tokens=10)
            finally:
                if index == 0:
                    failure_processed.set()

        with patch("openai.AsyncOpenAI", FakeClient):
            results = await asyncio.wait_for(
                asyncio.gather(*(call(i) for i in range(16)), return_exceptions=True),
                timeout=5,
            )
        failures = sum(isinstance(item, Exception) for item in results)
        assert failures == (1 if fixed else 16)
        if fixed:
            assert not closed
        await provider.close()
        assert closed

    asyncio.run(run())
