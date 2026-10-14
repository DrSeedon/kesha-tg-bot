"""What the CLI does with a prompt under --replay-user-messages (V-43 spike).

`ClaudeSession` sends each prompt as one user frame carrying its own uuid; the
CLI echoes that frame back as a `UserMessage` with the same uuid when it takes
the prompt into a turn. Fake clients call `take_prompt` from `query()` so a
turn they script afterwards is attributed to the prompt, like in production.
"""
from claude_agent_sdk import UserMessage


async def prompt_frames(prompt) -> list[dict]:
    if isinstance(prompt, str):
        return [{"message": {"content": prompt}, "uuid": None}]
    return [frame async for frame in prompt]


def text_of(frames: list[dict]) -> str:
    return "\n".join(str(frame["message"]["content"]) for frame in frames)


def echo(frames: list[dict]) -> list[UserMessage]:
    return [
        UserMessage(content=frame["message"]["content"], uuid=frame["uuid"])
        for frame in frames
        if frame.get("uuid")
    ]
