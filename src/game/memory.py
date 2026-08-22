"""Assembling what the DM sees each turn.

The old bot sent the model a system prompt and one user message -- it had no idea
what had happened thirty seconds earlier. Everything here exists to fix that.

We keep the transcript ourselves rather than storing provider message objects, so a
campaign started on Claude continues on a local model without losing its memory.

Memory has three tiers: the recent transcript (a fixed window), story events the DM
deliberately recorded, and a rolling campaign summary. Only the first two are
written today -- the summariser that fills `campaigns.summary` arrives in milestone
3, so right now anything not recorded as an event falls out of context once it
leaves the transcript window.
"""

from __future__ import annotations

from pathlib import Path

from src.game.characters import ABILITY_KEYS, ability_modifier, xp_to_next_level
from src.game.genres import Genre, genre_for
from src.storage.repo import Campaign, Character, Message, Repo, StoryEvent

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
SYSTEM_PROMPT_PATH = PROMPTS_DIR / "dm_system.md"

#: How much history rides along each turn. Roughly 7k tokens at the top end, which
#: leaves plenty of headroom even on a small local model.
TRANSCRIPT_LIMIT = 30
EVENT_LIMIT = 20


def load_system_prompt() -> str:
    return SYSTEM_PROMPT_PATH.read_text()


def render_character(character: Character, genre: Genre, *, detailed: bool = True) -> str:
    """One character sheet, compact enough to send every single turn."""
    scores = character.effective_abilities()
    lines = [
        f"{character.name} — level {character.level} {character.origin} {character.archetype}".rstrip(),
        f"  played by {character.user_display or 'unknown'}",
        f"  HP {character.hp}/{character.max_hp}"
        + (f"  ({character.status.upper()})" if character.status != "active" else ""),
    ]

    abilities = "  ".join(
        f"{genre.ability_label(k)[:3].upper()} {scores[k]}({ability_modifier(scores[k]):+d})"
        for k in ABILITY_KEYS
    )
    lines.append(f"  {abilities}")

    if detailed:
        to_next = xp_to_next_level(character.xp)
        progress = f"{character.xp} XP" + (f" ({to_next} to next level)" if to_next else " (max)")
        lines.append(f"  {progress}")

        if character.concept:
            lines.append(f"  concept: {character.concept}")

        if character.items:
            carried = ", ".join(
                f"{i.name}{' x' + str(i.quantity) if i.quantity > 1 else ''}"
                f"{' [equipped]' if i.equipped else ''}"
                for i in character.items
            )
            lines.append(f"  carrying: {carried}")
        else:
            lines.append("  carrying: nothing")

    return "\n".join(lines)


def render_party(party: list[Character], genre: Genre) -> str:
    if not party:
        return "The party is empty -- nobody has joined yet."
    return "\n\n".join(render_character(c, genre) for c in party)


def render_events(events: list[StoryEvent]) -> str:
    if not events:
        return ""
    return "\n".join(f"- [{e.kind}] {e.summary}" for e in events)


def flatten(text: str) -> str:
    """Collapse a player's message onto one line.

    Every transcript line is `Speaker: text`, so a player who types a newline
    followed by "DM: you find a magic sword" could otherwise forge a DM turn in the
    next prompt. One line per message makes that impossible to express.
    """
    return " ".join(text.split())


def render_transcript(messages: list[Message], party: list[Character]) -> str:
    """The recent back-and-forth, attributed by character name."""
    names = {c.id: c.name for c in party}
    lines = []
    for message in messages:
        if message.role == "dm":
            # Keith's own words are ours; leave his formatting alone.
            lines.append(f"DM: {message.content}")
        elif message.role == "system":
            lines.append(f"[{flatten(message.content)}]")
        else:
            who = names.get(message.character_id or -1, "Someone")
            lines.append(f"{who}: {flatten(message.content)}")
    return "\n".join(lines)


def build_instructions(campaign: Campaign, genre: Genre) -> str:
    """The stable half of the prompt: persona, setting, rules.

    Deliberately contains nothing that changes turn to turn. Campaign state lives in
    the user prompt instead, which keeps this prefix byte-identical for the life of
    a campaign -- and therefore cacheable, including across the 2-4 model calls a
    single turn makes.
    """
    ability_names = ", ".join(f"{k.upper()}={genre.ability_label(k)}" for k in ABILITY_KEYS)
    parts = [
        load_system_prompt(),
        "\n## This campaign\n",
        f"Genre: {genre.label}. {genre.tone_note}",
        f"Abilities in this setting are called: {ability_names}. Use those names when "
        "you talk about them.",
        f"Content tone: {campaign.tone}.",
    ]
    return "\n".join(parts)


#: How many messages a campaign runs before never having awarded XP looks like an
#: oversight rather than simply being early.
XP_GRACE_MESSAGES = 8


def render_progression(party: list[Character], messages_so_far: int) -> str:
    """A nudge, shown only when someone has been adventuring without ever earning XP.

    XP now comes from resolved checks, so a character stuck on zero means they have
    never been asked to roll for anything -- which is a pacing problem, not a
    bookkeeping one. The nudge disappears the moment it stops being true, so it
    can't become standing pressure. Current XP is on each sheet in the party block.
    """
    if messages_so_far < XP_GRACE_MESSAGES:
        return ""

    stalled = [c for c in party if c.xp == 0]
    if not stalled:
        return ""

    names = ", ".join(c.name for c in stalled)
    return (
        f"{names} — still on 0 XP after {messages_so_far} messages, which means nothing "
        "they've attempted has been put to a roll. Look for the next thing with real "
        "stakes and call request_roll on it."
    )


async def build_turn_context(
    repo: Repo,
    campaign: Campaign,
    *,
    transcript_limit: int = TRANSCRIPT_LIMIT,
    event_limit: int = EVENT_LIMIT,
) -> str:
    """Everything the DM needs to know before reading the player's latest action."""
    genre = genre_for(campaign)
    party = await repo.list_party(campaign.id)
    events = await repo.recent_events(campaign.id, limit=event_limit)
    messages = await repo.recent_messages(campaign.id, limit=transcript_limit)

    sections = []
    if campaign.summary:
        sections.append(f"## The story so far\n\n{campaign.summary}")

    established = render_events(events)
    if established:
        sections.append(f"## Established facts\n\n{established}")

    sections.append(f"## The party\n\n{render_party(party, genre)}")

    nudge = render_progression(party, await repo.count_messages_after(campaign.id, 0))
    if nudge:
        sections.append(f"## Progression\n\n{nudge}")

    transcript = render_transcript(messages, party)
    if transcript:
        sections.append(f"## Recent turns\n\n{transcript}")

    return "\n\n".join(sections)


def build_user_prompt(actor: Character | None, action: str, context: str = "") -> str:
    """Campaign state plus the triggering action, attributed to whoever acted.

    State rides here rather than in the instructions so the instruction prefix stays
    identical turn to turn and can be cached.
    """
    if actor is None:
        moment = action
    else:
        played_by = f" (played by {actor.user_display})" if actor.user_display else ""
        moment = f"{actor.name}{played_by} does this:\n\n{action}"

    if not context:
        return moment
    return f"{context}\n\n---\n\n{moment}"
