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
