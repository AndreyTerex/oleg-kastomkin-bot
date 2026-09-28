from llm import Provider, order_providers


def provider(name, model):
    return Provider(name=name, url="u", api_key="k", model=model)


def test_order_providers_follows_llm_order_and_keeps_model_order():
    chain = [
        provider("tokenharbor", "deepseek"),
        provider("groq", "gpt-oss-120b"),
        provider("openrouter", "nemotron"),
        provider("groq", "qwen"),
        provider("other", "x"),
    ]
    ordered = order_providers(chain, ["groq", "tokenharbor", "openrouter"])
    assert [p.model for p in ordered] == ["gpt-oss-120b", "qwen", "deepseek", "nemotron", "x"]


def test_default_chain_starts_with_gemini_then_groq():
    import config

    assert config.LLM_ORDER[:2] == ["gemini", "groq"]
    assert config.GEMINI_MODELS[0].startswith("gemini-") and "flash" in config.GEMINI_MODELS[0]
    assert config.GROQ_MODELS[0] == "openai/gpt-oss-120b"
    assert not hasattr(config, "MISTRAL_API_KEY")


def test_region_block_detection():
    from llm import is_region_block

    google = "[{'error': {'code': 400, 'message': 'User location is not supported for the API use.', 'status': 'FAILED_PRECONDITION'}}]"
    assert is_region_block(400, google)
    assert not is_region_block(400, "Invalid argument")
    assert not is_region_block(429, google)


def test_rate_limit_wait_reads_google_retry_delay():
    from llm import _rate_limit_wait

    details = "[{'@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': '48s'}]"
    assert _rate_limit_wait("You exceeded your current quota " + details, {}, daily=True) == 48.0


def test_all_providers_go_through_vpn_by_default(monkeypatch):
    import config
    import llm

    assert config.LLM_PROXY_FOR == ["all"]
    monkeypatch.setattr(config, "LLM_PROXY", "http://127.0.0.1:10809")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "g")
    monkeypatch.setattr(config, "GROQ_API_KEY", "q")
    chain = llm.build_providers()
    assert {p.name for p in chain} >= {"gemini", "groq"}
    assert all(p.proxy == "http://127.0.0.1:10809" for p in chain)


def test_builtin_vpn_proxy_is_skipped_when_xray_is_not_running(monkeypatch):
    import config
    import llm

    monkeypatch.setattr(config, "VPN_PROXY_URL", "http://127.0.0.1:10809")
    item = Provider(name="groq", url="u", api_key="k", model="m", proxy="http://127.0.0.1:10809")
    assert llm.proxy_for(item) is None  # Xray не запущен — напрямую, а не в закрытый порт
    own = Provider(name="groq", url="u", api_key="k", model="m", proxy="http://user:pw@proxy.example:3128")
    assert llm.proxy_for(own) == own.proxy


class _FakeResponse:
    def __init__(self, status, payload):
        self.status, self._payload, self.headers = status, payload, {}

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    closed = False

    def __init__(self, script):
        self.script, self.calls = list(script), []

    def post(self, url, **kwargs):
        self.calls.append(kwargs["json"]["model"])
        status, payload = self.script.pop(0)
        return _FakeResponse(status, payload)


def _ok(text):
    return 200, {"choices": [{"message": {"content": text}}]}


def test_overloaded_gemini_is_asked_again_before_falling_back(monkeypatch):
    import asyncio

    import config
    import llm

    async def no_sleep(delay):
        return None

    monkeypatch.setattr(llm.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(config, "LLM_OVERLOAD_RETRIES", 3)
    client = llm.LLMClient([provider("gemini", "flash"), provider("groq", "oss")])
    busy = (503, {"error": {"message": "high demand"}})
    client._session = _FakeSession([busy, busy, _ok("ответ gemini")])
    reply = asyncio.run(client.complete("s", "u"))
    assert reply.text == "ответ gemini" and client._session.calls == ["flash", "flash", "flash"]

    client = llm.LLMClient([provider("gemini", "flash"), provider("groq", "oss")])
    client._session = _FakeSession([busy, busy, busy, busy, _ok("ответ groq")])
    reply = asyncio.run(client.complete("s", "u"))
    assert client._session.calls == ["flash"] * 4 + ["oss"] and reply.text == "ответ groq"


def test_scarce_gemini_models_are_not_retried_and_hand_over_to_the_next(monkeypatch):
    import asyncio

    import config
    import llm

    async def no_sleep(delay):
        return None

    monkeypatch.setattr(llm.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(config, "LLM_OVERLOAD_RETRIES", 3)
    busy = (503, {"error": {"message": "high demand"}})
    chain = [
        Provider(name="gemini", url="u", api_key="k", model="gemini-3.8-flash", scarce=True),
        Provider(name="gemini", url="u", api_key="k", model="gemini-3.6-flash", scarce=True),
        Provider(name="gemini", url="u", api_key="k", model="gemini-3.5-flash-lite"),
    ]
    client = llm.LLMClient(chain)
    client._session = _FakeSession([busy, busy, busy, _ok("ответ lite")])
    reply = asyncio.run(client.complete("s", "u"))
    # 20 запросов в день — не тратим их на повторы; у Lite лимит большой — его переспрашиваем.
    assert client._session.calls == ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.5-flash-lite"]
    assert reply.text == "ответ lite"


def test_economy_replies_skip_scarce_models():
    import asyncio

    import llm

    chain = [
        Provider(name="gemini", url="u", api_key="k", model="gemini-3.8-flash", scarce=True),
        Provider(name="gemini", url="u", api_key="k", model="gemini-3.5-flash-lite"),
    ]
    client = llm.LLMClient(chain)
    client._session = _FakeSession([_ok("экономно")])
    reply = asyncio.run(client.complete("s", "u", economy=True))
    assert client._session.calls == ["gemini-3.5-flash-lite"] and reply.text == "экономно"


def test_scarce_detection_and_default_gemini_chain():
    import config
    import llm

    assert llm.is_scarce_gemini("gemini-3.8-flash") and llm.is_scarce_gemini("gemini-3.5-flash")
    assert not llm.is_scarce_gemini("gemini-3.5-flash-lite")
    assert config.GEMINI_MODELS[-1] == "gemini-3.5-flash-lite" and len(config.GEMINI_MODELS) == 5


def test_gemini_daily_quota_waits_until_pacific_midnight(monkeypatch):
    import asyncio

    import llm

    monkeypatch.setattr(llm, "seconds_until_google_reset", lambda now=None: 5 * 3600)
    quota = (429, [{"error": {"code": 429, "message": "Quota exceeded for metric: "
                               "generativelanguage.googleapis.com/generate_content_free_tier_requests, "
                               "quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                    "details": [{"retryDelay": "31s"}]}}])
    chain = [
        Provider(name="gemini", url="u", api_key="k", model="gemini-3.8-flash", scarce=True),
        Provider(name="groq", url="u", api_key="k", model="oss"),
    ]
    client = llm.LLMClient(chain)
    client._session = _FakeSession([quota, _ok("groq")])
    assert asyncio.run(client.complete("s", "u")).text == "groq"
    left = client._blocked_until["gemini:gemini-3.8-flash"] - llm.time.time()
    assert 4.9 * 3600 < left <= 5 * 3600 and client._blocked_daily["gemini:gemini-3.8-flash"]


def test_seconds_until_google_reset_is_within_a_day():
    import llm

    assert 60 <= llm.seconds_until_google_reset() <= 25 * 3600
