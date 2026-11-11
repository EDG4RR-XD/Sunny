# Sunny the Junipyro

A Python 3.11+ Discord creature-collection bot using discord.py and SQLite.

## Getting started

1. Create a Discord application at https://discord.com/developers/applications and name it **Sunny the Junipyro**. On **Bot**, create/reset a bot token. Keep the token private.
2. Turn on the **Server Members Intent** under the bot's privileged gateway intents. This lets the bot clean up inventories when users leave a server.
3. Invite the bot using the **bot** and **applications.commands** OAuth2 scopes. Give it **View Channels**, **Send Messages**, **Embed Links**, **Read Message History**, and **Manage Messages** so it can remove expired wild spawns. You can restrict the Manage Messages permission to the spawn channel.
4. Install Python 3.11 or newer. In this folder run `pip install -r requirements.txt`.
5. Copy `.env.example` to `.env`; add `DISCORD_TOKEN=...`. Never commit `.env` to GitHub.
6. Run `python bot.py`. Use `/spawnchannel #your-channel` in your Discord server. The bot spawns every five minutes. `/spawnnow` immediately posts a test spawn.
7. Global slash commands can take time to appear. Set `TEST_GUILD_ID` to your development server ID to sync commands to that server immediately; remove it for global use.

## Railway setup

Upload the folder to a private GitHub repository, create a Railway service from the repository, and use the start command `python bot.py`. Add the `DISCORD_TOKEN` environment variable under Railway Variables. To retain inventories across deploys, mount a Railway volume at `/data` and set `DATABASE_PATH=/data/sunny.db`. The database is local to the service; don't run multiple bot replicas against it. Set `TEST_GUILD_ID` optionally. Check deployment logs for startup errors.

## Commands

| Command | Purpose |
| --- | --- |
| `/spawnchannel channel` | Admin: choose recurring spawn channel |
| `/spawnnow` | Admin: post a wild spawn immediately |
| `/cspawn creature user traits [level]` | Admin: grant a listed creature with any number of comma-separated traits; level defaults to 1 |
| `/clevel creature_id levels_to_add` | Admin: add levels |
| `/cdelete creature_id` | Admin: delete a creature and recycle its unique ID |
| `/giveitem user item quantity` | Admin: grant items |
| `/takeitem user item quantity` | Admin: remove items |
| `/givecurrency user currency amount` | Admin: grant Gold Coins or Diamond Shards |
| `/takecurrency user currency amount` | Admin: remove currency |
| `/inventory creatures [user] [page]` | Browse creature inventory (10/page) |
| `/inventory items [user]` | Browse items |
| `/creature id` | View the creature's traits, level and EXP (owners) |
| `/balance [user]` | Check currencies |
| `/active id` | Select a creature to receive EXP from successful catches |
| `/candy id [quantity]` | Consume Rare Candies to gain levels |
| `/trait add id trait` | Consume Trait Brush; [NOSPAWN] traits are blocked |
| `/dice id` | Consume Dice; roll a random new trait, including rarer [NOSPAWN] traits |
| `/shop view` | Prices |
| `/shop buy item currency [quantity]` | Purchase with Gold Coins or Diamond Shards |
| `/trade user [offer/request options]` | Trade a creature, one item type, Gold Coins, and/or Diamond Shards in either direction; other user must accept within five minutes |
| `/help_sunny` | Help |

### Catch mechanics

A wild creature is selected uniformly from `creatures.txt` every five minutes per configured server. The previous **unclaimed** message is removed when a replacement is posted. A button opens a private dropdown containing only the three ball types in that user's inventory. Throwing a ball consumes exactly one ball, whether the catch works or fails. Pokéball = 40%, Great Ball = 60%, Ultra Ball = 80%. Failed catches leave the wild creature available. The first successful catch wins, and the catch message is closed.

Newly caught creatures have **level 5**, **1–3 random non-[NOSPAWN] traits**, and a unique sequential ID. Each successful catch earns **1–50 Gold Coins**, plus **10–100 EXP for the user's active creature** (if any). The starting EXP requirement is 10 at level 1, rising 20% per level; leftover EXP carries over. EXP persists on each creature when switching which is active. Rare Candies add exactly one level each. Currency and items are tracked per server.

IDs start at 1 and are global across servers sharing the database. Deletions and a guild member's departure return IDs to the pool, which always reuses the **lowest available** ID first. Trades transfer creatures intact without changing their IDs. The bot deliberately never generates images.

### Shop prices

| Item | Gold Coins | Diamond Shards |
| --- | ---: | ---: |
| Rare Candy | 500 | 2 |
| Pokeball | 100 | 1 |
| Great Ball | 300 | 2 |
| Ultra Ball | 600 | 4 |
| Dice | 1,000 | 1 |
| Trait Brush | 1,500 | 5 |

`traits.json` has two groups. Admins can apply either group in `/cspawn`, with multiple traits separated by commas. Wild spawns roll only regular traits. Dice can roll a [NOSPAWN] trait with **0.2× the selection weight** of each regular trait; this implements “80% rarer” as 80% lower per-trait weight. Dice never add a duplicate trait. A brush can apply only a regular trait.

## Wild creature spawn display

Wild spawns display the creature name, traits, catch level, and Catch button as a text-only embed. No image or image lookup failure message is displayed or requested.

## Editable catalogs

- `creatures.txt`: one creature per line, with the user-provided game lists combined and duplicate names removed. Case-insensitive creature matching; autocomplete suggests matches.
- `traits.json`: `normal` and `nospawn` trait names.

To add a species or trait, edit the corresponding file and restart the bot. To customize the name/avatar in Discord, use the Discord Developer Portal.

## Implementation notes

The shop, catch, and trade use SQLite transactions with an async mutex to prevent duplicate claims and partial balance changes. Pending trades are kept in bot memory and expire after five minutes. If the bot restarts, any outstanding wild spawn button/trade request expires and the next scheduled spawn replaces the wild message. For a high-traffic production installation, consider durable spawn/trade state. Discord doesn't let bot developers grant permissions automatically, and a successful startup requires the correct Discord token, gateway intent, and channel permissions.

## Creature designs and nicknames

- `/design submit id:<creature ID> image:<attached image>`: submit artwork for a creature you own. Sunny posts the image publicly and assigns a unique design ID.
- `/design approve design_id:<design ID>`: staff with **Manage Server** permission approve a pending design. The creature species name in `/inventory creatures` and `/creature` becomes a clickable link to the approved submission message.
- `/design deny design_id:<design ID> reason:<optional reason>`: staff deny a pending submission.
- `/nickname id:<creature ID> name:<nickname>`: nickname one of your creatures (up to 40 characters). Leave `name` empty to clear it.

Discord requires `/design` to be a slash-command group so its submit, approve, and deny actions use `/design submit`, `/design approve`, and `/design deny` instead of `/design <id>`.

Design IDs are unique and increment independently of creature IDs. A creature may have multiple submissions; its newest approved design is linked. Approved designs and nicknames stay with the creature on trade. When a creature is deleted, its submissions are removed from the database so reused creature IDs do not inherit an old design. Old Discord submission messages are not deleted. All design and nickname responses are public. Store your SQLite database at `/data/sunny.db` on the Railway volume so approval records and nicknames persist across deployments. Existing databases are automatically migrated on startup.
