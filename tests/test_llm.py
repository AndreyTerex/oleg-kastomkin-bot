from llm import Provider, order_providers


def provider(name, model):
    return Provider(name=name, url="u", api_key="k", model=model)


def test_order_providers_follows_llm_order_and_keeps_model_order():
    chain = [
        provider("tokenharbor", "deepseek"),
        provider("mistral", "mistral-large-latest"),
        provider("groq", "gpt-oss"),
        provider("mistral", "mistral-medium-latest"),
        provider("other", "x"),
    ]
    ordered = order_providers(chain, ["mistral", "groq", "tokenharbor"])
    assert [p.model for p in ordered] == [
        "mistral-large-latest", "mistral-medium-latest", "gpt-oss", "deepseek", "x",
    ]


def test_default_chain_starts_with_mistral_large():
    import config

    assert config.LLM_ORDER[0] == "mistral"
    assert config.MISTRAL_MODELS[0] == "mistral-large-latest"
