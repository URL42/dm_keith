# DM Keith — working notes

A sarcastic AI dungeon master running persistent, multiplayer campaigns over
Telegram. Rewritten from an early-agents prototype; see `docs/HANDOVER.md` for
where the work currently stands.

**Live on a home server ("bossbitch") via docker compose.** Changes get pulled and
rebuilt there, so anything that breaks startup breaks a running game.

## Commands

```bash
make check     # ruff + mypy + pytest — run before every commit
make test
make fmt
make run       # local, needs .env
```

## Architecture

```
src/
  main.py       entrypoint; config errors exit 1 with a readable message
  config.py     env → Settings (frozen pydantic model)
  storage/      schema.sql + repo.py — ALL SQL lives here, nothing else writes it
  game/         dice, character maths, genres, memory assembly, turn loop, sheets, chronicle
  llm/          model factory, the DM agent + its tools, genre builder, chronicler
  bot/          Telegram handlers
  achievements/ registry.json + awarding rules
```

The DM is a pydantic-ai agent (`src/llm/dm_agent.py`) with ten tools. Game state
changes **only** through those tools, so what Keith narrates and what's in the
database can't drift. `src/game/session.py` runs one turn: assemble context, run the
agent, persist both sides.

Model spec is `provider:model` (`anthropic:…`, `openai:…`, `deepseek:…`,
`ollama:…`). The transcript is ours, not provider message objects, so a campaign
survives switching providers mid-story. `DMK_SUMMARY_MODEL` is a separate model used
only by the chronicler — the game can run cheap while the book gets written well.

## Rules learned the hard way

Each of these cost a real bug. Don't relearn them.

**If it must happen, the engine does it — not the model.** Two different models
(Opus and DeepSeek) each narrated whole sessions without ever calling `grant_xp`, so
nobody could level. Prompting harder failed twice. XP now comes from resolved checks
and mechanical achievements fire from the engine. Reach for a tool only when the
outcome is genuinely the model's judgement call.

**Never add a column to an existing table.** `CREATE TABLE IF NOT EXISTS` runs on
every boot, so *new tables* are free — but a new column silently isn't there on the
deployed database. Use a new table, or derive the value.

**Any multi-statement write goes through `repo.transaction()`.** sqlite leaves the
transaction open after an exception, and the connection is shared, so the next
commit anywhere would persist half-finished work. This nearly ate a whole book.

**Escape everything that reaches Telegram with `parse_mode` set.** Character names
are player-typed and item names, genre pitches and roll reasons are model-written.
An underscore in "80s_action" once killed a whole flow. Prefer HTML with
`html.escape`, and guard `edit_text` — a cosmetic edit must never fail a command.

**Nothing user-identifying in model-facing context.** A player's `@handle` was in
the prompt twice per turn and ended up inside the fiction. Characters are addressed
by character name; handles appear only in `/sheet` and `/party`.

**Provider-specific model settings must be gated on the prefix.** `anthropic_*` keys
would be rejected elsewhere; see `model_settings_for` in `src/game/session.py`.

**Player text is flattened before it re-enters a prompt** (`flatten` in
`src/game/memory.py`), so nobody can forge a `DM:` line in the transcript.

**Tools bound their inputs** and raise `ModelRetry` rather than trusting the model —
XP per award, item bonuses, dice size, DC range.

## Workflow

Per the user's global preferences: plan non-trivial work and wait for approval;
**spawn a review agent on the diff before every commit** (it has caught a real bug
every single time, including two data-loss bugs); ask before committing.

Tests use pydantic-ai's `FunctionModel` to script model responses — no API calls, no
keys needed. `tests/conftest.py` gives a throwaway database per test.
