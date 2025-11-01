# Dungeon Master Keith (DMK)

Dungeon Master Keith is a sarcastic omniscient narrator persona built with the OpenAI Agents SDK. Every major reply starts with an achievement block, followed by theatrical narration or comedic analysis. The runtime supports multiple modes (narrator, achievements-only, explain, story) and exposes a Telegram bot bridge for live play sessions.

## Quickstart

1. **Clone & install**
   ```bash
   uv sync
   ```
   The project targets Python 3.11+ and manages dependencies via [`uv`](https://github.com/astral-sh/uv). If you prefer `pip`, use `pip install -r requirements.txt` after running `uv pip compile` (optional).

2. **Configure environment**
   ```bash
   cp .env.example .env
   ```
   Fill in:
   - `OPENAI_API_KEY`
   - `TELEGRAM_BOT_TOKEN`
   - Optional tweaks (`DMK_PROFANITY_LEVEL`, `DMK_TANGENTS_LEVEL`, etc.)

3. **Initialize the database**
   ```bash
   uv run python -m src.engine.storage.setup_db
   ```
   (The helper will create the SQLite file defined by `DMK_DB_PATH` and apply migrations.)

4. **Run the Telegram bot (long polling)**
   ```bash
   uv run python -m src.bots.telegram_bot
   ```
   Start a conversation with your bot, use `/mode narrator` or `/mode explain`, and DMK will greet you with achievements on every major response.

5. **Create your character for story mode**
   ```bash
   /character new
   /character name Rin Starweaver
   /character race Elf
   /character class Wizard
   /character finalize
   ```
   Once finalized, switch to story mode with `/mode story`, use `/story` to recap the current scene, `/choose <id>` to pick options, and `/roll <expression>` for manual dice checks.

## Project Layout

- `src/engine/` — achievement runtime, mode router, storage helpers.
- `src/bots/` — Telegram polling bot that bridges updates to the Agents SDK.
- `src/config/toggles.py` — environment-driven feature flags and paths.
- `prompts/system/` — system prompts fed into the Agents SDK.
- `characters/` — machine-readable persona definitions for orchestration.
- `docs/` — persona contract and database schema reference.
- `assets/profile/dmk.png` — production avatar for DMK.
- `tests/` — pytest suite mirroring source layout.

## Modes & Behaviors

- **Narrator**: cinematic narration after every user message. Default mode.
- **Achievements**: short, achievement-first responses for quick banter.
- **Explain**: artifact/file analysis with comedic tangents (uploads or `/mode explain`).
- **Story**: fully interactive dungeon crawl that tracks character sheets, XP, and dice checks. DMK presents numbered choices, calls out stored rolls, and remembers your progress.

### Story & Dice Commands

- `/character` — manage your character sheet (Keith auto-rolls stats; you set name/race/class/backstory).
- `/restart` — re-roll stats and restart the campaign from the top.
- `/profile` — quick readout of the current character sheet.
- `/inventory` — view or adjust your gear (`/inventory add torch 2`).
- `/story` — recap the active scene and available choices.
- `/choose <id>` — pick a story option (equivalent to replying with the text).
- `/roll <expression>` — roll dice with advantage/disadvantage or ability modifiers (e.g. `/roll 1d20adv+3`, `/roll str`). Stored rolls are consumed automatically on the next matching check.
- `/history [n]` — list recent dice rolls (defaults to 5).

All major responses start with an achievement block (`Title`, `Description`, `Reward`, `Rarity`) followed by 1–2 paragraphs of text. The runtime enforces cooldowns and dedupes via SQLite so users earn each achievement deliberately.

## Development Commands

| Command | Description |
| --- | --- |
| `make setup` | Install dependencies, configure pre-commit hooks, copy `.env.example`. |
| `make lint` | Run `ruff`, `black`, and `mypy`. |
| `make test` | Execute pytest with coverage (`pytest --cov=src`). |
| `uv run python tools/validate_story.py` | Validate that story JSON files reference valid scenes. |
| `make dev` | Launches local TMUX session with runtime + bot watchers (customize as needed). |

Ensure `make lint` and `make test` pass before opening a PR.

## Testing

The suite focuses on:
- Achievement registry schema and JSON integrity.
- Deduping/cooldown logic for `award_achievement`.
- Toggle parsing and validation.
- Prompt conformance (achievement block + tone).

Run everything with:
```bash
uv run pytest
```

## Deployment Notes

- Long polling is used for Telegram during early development. When deploying behind Cloudflare or another edge, swap in webhook mode (see bot module for TODO hook).
- SQLite lives at `DMK_DB_PATH` with WAL mode enabled. Future migrations will target a managed database service; see `docs/DB_SCHEMA.sql` for schema details.
- Store persona-wide decisions (rating, profanity, tangents) via the toggles module for consistent behavior across surfaces.
