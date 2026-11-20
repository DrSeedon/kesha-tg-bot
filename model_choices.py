"""Selectable model catalogue per runtime. Edit this list to expose models."""

MODELS = {
    "antigravity": (
        ("Gemini 4 Neon", "gemini-4-neon", ("low", "medium", "high")),
        ("Gemini 4 Neon Orchestrator", "gemini-pro-orchestrator-neon", ("",)),
        ("Gemini 4 Carbon", "gemini-4-carbon", ("low", "medium", "high")),
        ("Gemini 4 Hydrogen", "gemini-4-hydrogen", ("low", "medium", "high")),
        ("Gemini 4 Argon", "gemini-4-argon", ("low", "medium", "high")),
        ("Gemini 3.8 Flash", "gemini-3.8-flash", ("low", "medium", "high")),
        ("Claude Sonnet 5.5 (3P)", "claude-sonnet-5-5", ("low", "medium", "high")),
        ("Claude Opus 5.5 (3P)", "claude-opus-5-5", ("low", "medium", "high")),
        ("GPT-OSS 120B (3P)", "gpt-oss-120b", ("medium",)),
    ),
    "claude": (
        ("Claude Sonnet 5.5", "claude-sonnet-5-5", ("",)),
        ("Claude Opus 5.5", "claude-opus-5-5", ("",)),
    ),
    "codex": (
        ("GPT-6.2 Sol", "gpt-6.2-sol", ("low", "medium", "high")),
        ("GPT-6.2 Astra", "gpt-6.2-astra", ("low", "medium", "high")),
    ),
}


def resolve_choice(runtime: str, index: int, effort: str) -> tuple[str, str | None]:
    """Return (model_id, reasoning_effort) for a valid callback; raise on mismatch."""
    _, family, efforts = MODELS[runtime][index]
    if effort not in efforts:
        raise ValueError(f"invalid effort {effort!r} for {runtime}/{family}")
    if runtime in ("claude", "antigravity"):
        return (f"{family}-{effort}" if effort else family, None)
    return (family, effort)
