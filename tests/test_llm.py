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


def test_default_chain_starts_with_groq_gpt_oss():
    import config

    assert config.LLM_ORDER[0] == "groq"
    assert config.GROQ_MODELS[0] == "openai/gpt-oss-120b"
    assert not hasattr(config, "MISTRAL_API_KEY")
