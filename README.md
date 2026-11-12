# Sunny the Junipyro: Storage Edition

A Discord creature storage and trading bot using discord.py and SQLite. There are no wild spawns, catch buttons, Pokéballs, or eggs. Staff add creatures directly to inventories using `/cadd`.

## Setup

Install dependencies with `pip install -r requirements.txt`. Configure `DISCORD_TOKEN` in `.env` or environment variables. Optionally set `TEST_GUILD_ID` for immediate guild-level slash command synchronization. Run `python bot.py`.

For Railway, use `python bot.py` as the start command. To preserve creature inventories across deployments, mount `/data` and set `DATABASE_PATH=/data/sunny.db`. **Back up the existing database before deploying.** This version retains existing creatures, user inventories, designs, levels, nicknames, trades, items, and currency. Existing egg and ball entries in old databases remain stored as historical inventory items but can no longer be bought, used, or given through item command autocomplete. No automated conversion deletes them.

## Storage and management

- `/cadd creature user traits [level]`: staff add a listed species to a user's inventory, with comma-separated traits and a default level of 1. This replaces `/cspawn`; it does not create wild spawns.
- `/inventory creatures`, `/inventory items`, `/creature`, `/balance`: browse stored creatures, items, levels, traits, nicknames, type/series metadata, and currencies.
- `/nickname`, `/active`: name a creature or mark it active. There is no catch-based EXP gain.
- `/evolve id species`: staff change any species, including ones absent from `creatures.txt`, if capitalized.
- `/clevel`, `/cdelete`, `/giveitem`, `/takeitem`, `/givecurrency`, `/takecurrency`: staff management commands.
- `/trade`: exchange creatures, items, and currencies.
- `/design submit`, `/design approve`, `/design deny`, `/art submit`, `/art approve`, `/art deny`: preserved art and design workflows. Approved art still grants currency and levels.

## Shop and trait tools

| Item | Gold Coins | Diamond Shards |
| --- | ---: | ---: |
| Rare Candy | 500 | 2 |
| Dice | 1,000 | 1 |
| Trait Brush | 1,500 | 5 |
| Trait Eraser | 2,000 | 15 |
| DNA Splicer | 1,000 | 9 |

Use `/shop view` and `/shop buy` to buy items, `/candy` to level up, `/dice` to roll a random new trait, and `/trait add` to apply a generic or specific Trait Brush. Specific Trait Brushes exist for every trait in `traits.json`; they may be traded, given by staff, or obtained from `/release`, but are not purchasable. Rare `[NOSPAWN]` traits still have reduced Dice and brush-drop weights; their label refers to the existing trait catalog rather than a spawning system.

Use `/item use` for **Trait Eraser** and **DNA Splicer** only. No egg uses remain.

## Release rewards

`/release id` deletes an owned creature, makes its ID reusable, and awards **50–100 Gold Coins**. Every release has a **50% chance to drop one specific Trait Brush**. Epic brushes remain exceptionally rare, and other rare brushes remain less likely than regular brushes. Eggs have been removed entirely from the drop pool.

## Notes

Species data remains in `creatures.txt` and `creature_metadata.json`; traits remain in `traits.json`. A restart re-syncs slash commands. If Discord still shows removed commands immediately after deployment, allow time for global command propagation or configure `TEST_GUILD_ID` for fast guild syncing. Persist the SQLite database across deployments to retain existing collections.
