"""Telegram choices. Stored model IDs stay separate from their labels."""

MODELS = {
    "antigravity": (
        ("Gemini 4 Neon", "gemini-4-neon", ("low", "medium", "high", "xhigh")),
        ("Gemini 4 Neon Orchestrator", "gemini-pro-orchestrator-neon", ("",)),
        ("Gemini 4 Carbon", "gemini-4-carbon", ("low", "high")),
        ("Gemini 4 Hydrogen", "gemini-4-hydrogen", ("low", "medium", "high", "xhigh")),
    ),
    "claude": (
        ("Claude Opus 5.5", "claude-opus-5-5", ("",)),
        ("Claude Sonnet 5.5", "claude-sonnet-5-5", ("",)),
    ),
    "codex": (
        ("GPT-6.2 Sol", "gpt-6.2-sol", ("low", "medium", "high", "xhigh")),
    ),
}


def resolve_choice(runtime_id, family, effort):
    _, model, efforts = MODELS[runtime_id][family]
    if effort not in efforts:
        raise ValueError("Unknown model effort")
    if runtime_id == "antigravity" and effort:
        return model + "-" + effort, None
    return model, effort or None
