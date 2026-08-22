# Dungeon Master Keith (DMK)

![DM Keith](assets/profile/dmk.png)

A sarcastic AI dungeon master that runs persistent, multiplayer campaigns over Telegram.

Keith picks a genre with you, helps everyone roll up a character, then narrates the
campaign — rolling dice, handing out loot, tracking HP and XP, and remembering what
happened forty turns ago. The model doing the narrating is a config line: Claude,
OpenAI, DeepSeek, or a local model on your own hardware.

> **Status: mid-revamp.** Milestone 1 (storage, config, packaging, bot skeleton) is
> done. The DM agent and campaign commands land in milestone 2 — see
> [Roadmap](#roadmap).

## Quickstart

```bash
uv sync --extra dev
cp .env.example .env      # fill in TELEGRAM_BOT_TOKEN and one provider key
make run
```

Then message your bot on Telegram: `/start`.

## Configuration

Everything is environment variables (see [.env.example](.env.example)):

| Variable | Purpose |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Required. From [@BotFather](https://t.me/BotFather). |
| `DMK_MODEL` | The DM's model, as `provider:model`. Default `anthropic:claude-opus-5`. |
| `DMK_SUMMARY_MODEL` | Cheap model used only to compress old transcript into the campaign summary. |
| `ANTHROPIC_API_KEY` etc. | Credentials for whichever provider you picked. Read by pydantic-ai directly. |
| `DMK_DB_PATH` | SQLite file. Default `./local/dmk.sqlite3`. |
| `DMK_LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, … |

### Switching models

Change one line and restart:

```bash
DMK_MODEL=anthropic:claude-opus-5     # Claude
DMK_MODEL=openai:gpt-5                # OpenAI
DMK_MODEL=deepseek:deepseek-chat      # DeepSeek
DMK_MODEL=ollama:qwen3:14b            # local, needs OLLAMA_BASE_URL
```

Campaign history lives in our own database rather than in provider-specific message
objects, so a campaign started on Claude continues on a local model without losing
anything.

## Development

```bash
make check    # ruff + mypy + pytest
make test
make fmt
```

Layout:

```
src/
  main.py       entrypoint (python -m src.main)
  config.py     environment settings
  storage/      schema.sql + async repository (all SQL lives here)
  game/         dice, character maths, genres, memory assembly
  llm/          model factory + the DM agent and its tools
  bot/          Telegram handlers
  achievements/ registry + award rules
```

Design notes worth knowing:

- **Async all the way down.** aiosqlite and pydantic-ai are both async, so one
  player's turn never blocks another chat. Ruff's `ASYNC` rules guard against
  regressions here.
- **One character per player per campaign** (`UNIQUE (campaign_id, user_id)`), which
  is what makes group play work.
- **All SQL is in `storage/repo.py`.** Everything else talks to typed dataclasses.

## Deployment

```bash
docker compose up --build -d
```

The `dmk_data` named volume holds the campaign database — keep it and your stories
survive image rebuilds.

## Roadmap

| Milestone | Contents | Status |
| --- | --- | --- |
| M1 | Storage, config, logging, packaging, bot skeleton | ✅ done |
| M2 | DM agent + tools, `/newgame`, `/join`, solo play | next |
| M3 | Rolling memory, level-ups, items, all genres | |
| M4 | Multiplayer group play | |
| M5 | Achievements, sound cues, provider smoke tests, deploy | |
