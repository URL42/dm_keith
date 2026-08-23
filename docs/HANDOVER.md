# Handover — 23 August 2026

State of the DM Keith revamp at the end of the first working session. Read
`CLAUDE.md` first for architecture and the rules worth not relearning.

## Where things stand

All work is on branch **`revamp`**, pushed to `origin`. `main` still holds the
original pre-revamp bot, untouched, so rolling back is `git checkout main`.

13 commits, ~11,600 lines changed, **246 tests**, ruff and mypy clean. Deployed and
played on the server; the last few sessions have been real play, not smoke tests.

Nothing is merged to `main` yet — that's a decision for the user, not a leftover.

## What the revamp replaced

The original bot made a synchronous OpenAI Responses API call from an async handler,
sent the model **no conversation history at all**, and keyed the character sheet on
the *chat* rather than the player — so a group shared one character, one inventory
and one XP pool. Game state and narration were entirely disconnected: the model
could neither read nor write HP, items or stats.

## What exists now

**Play.** `/newgame` picks a genre (or type any genre and it's generated),
`/join` builds a character through inline keyboards, `/begin` opens the story, then
plain messages are turns. `@mention` another player and Keith stays out of it.

**Dice belong to the player.** When something you attempt could fail, Keith calls
`request_roll`, stops mid-scene, and a 🎲 button appears. Tapping it shows
`d20 [14] +2 DEX = 16 vs DC 12`, awards XP, and starts a second turn where he
narrates the consequence. He still rolls damage and monsters himself.

**Progression is the engine's job.** Checks pay XP scaled to DC, half on a failure.
Mechanical achievements (first check, natural 20, natural 1, levelling, level five,
first time downed) fire from the engine; the 35-entry registry's narrative ones are
Keith's to award.

**Characters outlive campaigns.** `/export` sends a Markdown sheet with a JSON block;
uploading it into any campaign carries level, XP, gear and achievements across.
Abilities need no conversion between genres — they're stored under canonical keys and
only *displayed* under a genre's names. Edit the file and a checksum notices, letting
it import anyway but awarding one of six deliberately unkind achievements.

**Campaigns become books.** `/chronicle` writes chapters incrementally and sends the
whole book as `.md`. `/endgame` asks first, then optionally rewrites the book
end-to-end knowing how it finished. Runs on `DMK_SUMMARY_MODEL`.

**Ten tools**: `get_party`, `roll_dice`, `request_roll`, `update_hp`, `grant_xp`,
`add_item`, `remove_item`, `record_event`, `list_achievements`, `award_achievement`.

**Ten tables**: campaigns, characters, items, messages, story_events, dice_rolls,
pending_rolls, achievement_grants, chronicle_chapters, bot_assets.

## What's left

**M3 — rolling summarisation.** The only item from the original plan never built,
and the most consequential gap. `campaigns.summary` and `summary_through_id` exist
and are read by `build_turn_context`, but **nothing ever writes them**. Memory past
the 30-message transcript window depends entirely on Keith having called
`record_event`. He does that reliably in practice, but anything he didn't record is
gone. `split_into_spans` in `src/game/chronicle.py` is most of the machinery this
needs.

**M4 — group play.** The schema has supported it since M1 (`UNIQUE (campaign_id,
user_id)`) and the handlers are written for it, but **it has never been tested with
two real accounts**. Party-aware narration, simultaneous `/join`, and the
@mention out-of-character rule are all unexercised in the wild. Note the bot needs
privacy mode **off** in @BotFather or Telegram won't deliver ordinary group messages.

**M5 — sound cues.** Six mp3s sit in `assets/sounds/`. Tools already raise cues
(`reward`, `new_quest`, `new_achievement`) and `send_cues` in `src/bot/context.py`
logs them and does nothing else. `bot_assets` exists to cache Telegram file_ids.

## Known issues and untested things

- **The prose has never been judged.** Everything since M2.6 — the persona rewrite,
  the chronicler's voice — shipped on my judgement of what reads well. No chapter of
  a chronicle has been read by a human yet. This is the single most valuable thing
  to do next, and it's the one area with no mechanical fallback.
- **Turn length was over budget** (330–1,236 words against a 200-word instruction).
  Rewritten prompt plus a 900-token `max_tokens` backstop is in, but unverified in
  play. If turns still run long, cut `MAX_REPLY_TOKENS` in `src/game/session.py`.
- **`/endgame`'s final pass costs real money** — roughly two model calls per chapter.
  It warns, and asks before running.
- **Achievements front-load**: first check, first crit and first fumble all land in
  the opening minutes. May feel like a slot machine; dropping the `first_check`
  trigger is the fix if so.
- **Docker builds only verified on the server**, never locally (no daemon on the
  dev Mac).
- One benign `PTBUserWarning` about `per_message=False` in the creation
  conversation. Investigated; the only consequence is a stale button showing a
  spinner.

## Deployment

```bash
git pull && docker compose up --build -d && docker compose logs -f
```

`.env` needs `TELEGRAM_BOT_TOKEN`, `DMK_MODEL`, `DMK_SUMMARY_MODEL`, the matching
provider key(s), and **`DMK_DB_DIR`** (the host directory holding the database —
mounted at `/data`; do *not* set `DMK_DB_PATH` under Docker). `DMK_UID`/`DMK_GID`
must match whoever owns that directory, since the container runs non-root.

The current server database is `/mnt/sata/dmk/db/dmk.sqlite3`. The pre-revamp
`main.sqlite3` sits beside it, untouched and unreadable by this version — opening it
is refused deliberately.

Startup logs three lines and then goes quiet; that's normal. `docker compose logs`
shows every tool call, per-turn latency, token counts and cache hits.

## Suggested next step

Play a session and run `/chronicle`. Send back the chapter and the logs
(`docker compose logs --no-log-prefix`). Tuning the prompt without an example in
front of you is guesswork, and the logs make tool usage, cost and pacing visible in
a way the chat alone doesn't.
