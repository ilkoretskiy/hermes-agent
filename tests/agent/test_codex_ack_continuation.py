from types import SimpleNamespace

from agent.agent_runtime_helpers import looks_like_codex_intermediate_ack
from agent.conversation_loop import _CODEX_ACK_CONTINUATION_NUDGE


def test_codex_intermediate_ack_detector_skips_chat_session_with_quoted_examples():
    """A tutor turn that *quotes* opener lines ("I'll throw…") must not trigger the ack
    continuation: the quoted "I'll" + the word "check" + a stray "/" in the user prompt
    used to inject a fake "[System: Continue now…]" user turn (2026-05-24 repro)."""
    agent = SimpleNamespace(_strip_think_blocks=lambda content: content)
    user_message = (
        "You are my conversation tutor. Talk like a sharp friend over coffee, "
        "not a grader. Pick a topic and we'll riff. Things I/we want to avoid: "
        "moralizing, padding, hedging."
    )
    assistant_content = (
        "Morning. Quick voice check before we start.\n\n"
        "Same opening line three ways:\n\n"
        "**(a) Cheeky owl**\n"
        "“Alright, little talons out: here’s a take-bait topic — bite it or dodge it.”\n\n"
        "**(b) Sharp friend**\n"
        "“Morning. I’ll throw you something concrete, you give me your gut, and we’ll sharpen it.”\n\n"
        "**(c) Light gamified**\n"
        "“Morning. One small opinion rep: I’ll serve the topic, you take a swing, we level it up.”\n\n"
        "Pick **a, b, or c**."
    )
    assert looks_like_codex_intermediate_ack(agent,
        user_message=user_message,
        assistant_content=assistant_content,
        messages=[{"role": "user", "content": user_message}],
    ) is False


def test_codex_intermediate_ack_detector_word_boundaries():
    """"report" is not "repo" and "profiles" is not "files"."""
    agent = SimpleNamespace(_strip_think_blocks=lambda content: content)
    user_message = "send me the weekly report about the team profiles"
    assert looks_like_codex_intermediate_ack(agent,
        user_message=user_message,
        assistant_content="Sure, I'll check the numbers first.",
        messages=[{"role": "user", "content": user_message}],
    ) is False


def test_codex_intermediate_ack_detector_still_fires_on_real_workspace_ack():
    """The detector must still catch the case it exists for: "I'll inspect ~/foo" with no tool call."""
    agent = SimpleNamespace(_strip_think_blocks=lambda content: content)
    user_message = "look into ~/openclaw-studio and tell me how it works"
    assistant_content = (
        "Absolutely — I can do that. I'll inspect ~/openclaw-studio and "
        "report back with a walkthrough."
    )
    assert looks_like_codex_intermediate_ack(agent,
        user_message=user_message,
        assistant_content=assistant_content,
        messages=[{"role": "user", "content": user_message}],
    ) is True


def test_codex_ack_continuation_nudge_is_ephemeral_scaffolding():
    """The nudge pair must never reach the durable transcript as a user-authored turn."""
    from agent.conversation_compression import _is_real_user_message
    from agent.session_persistence import _is_ephemeral_scaffolding

    nudge = {"role": "user", "content": _CODEX_ACK_CONTINUATION_NUDGE, "_codex_ack_continuation_nudge": True}
    interim = {"role": "assistant", "content": "I'll inspect ~/foo.", "_codex_ack_continuation_nudge": True}
    assert _is_ephemeral_scaffolding(nudge)
    assert _is_ephemeral_scaffolding(interim)
    assert not _is_real_user_message(nudge)
