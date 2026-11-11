"""Sunny the Junipyro: Discord creature collecting and trading bot."""
from __future__ import annotations

import asyncio
import heapq
import json
import logging
import os
import random
import re
import sqlite3
import time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env')
TOKEN = os.getenv('DISCORD_TOKEN', '')
DB_PATH = os.getenv('DATABASE_PATH', str(ROOT / 'sunny.db'))
TEST_GUILD_ID = os.getenv('TEST_GUILD_ID', '').strip()
CREATURES = list(dict.fromkeys(line.strip() for line in (ROOT / 'creatures.txt').read_text(encoding='utf-8').splitlines() if line.strip()))
TRAIT_DATA = json.loads((ROOT / 'traits.json').read_text(encoding='utf-8'))
NORMAL_TRAITS = TRAIT_DATA['normal']
NOSPAWN_TRAITS = TRAIT_DATA['nospawn']
ALL_TRAITS = NORMAL_TRAITS + NOSPAWN_TRAITS
CREATURE_LOOKUP = {s.casefold(): s for s in CREATURES}
TRAIT_LOOKUP = {s.casefold(): s for s in ALL_TRAITS}
ITEMS = {
    'Rare Candy': {'gold': 500, 'shards': 2},
    'Pokeball': {'gold': 100, 'shards': 1},
    'Great Ball': {'gold': 300, 'shards': 2},
    'Ultra Ball': {'gold': 600, 'shards': 4},
    'Dice': {'gold': 1000, 'shards': 1},
    'Trait Brush': {'gold': 1500, 'shards': 5},
    'Species Egg': {'gold': 2000, 'shards': 20},
    'Trait Egg': {'gold': 1000, 'shards': 5},
    'Rare Egg': {'gold': 10000, 'shards': 100},
    'Trait Eraser': {'gold': 2000, 'shards': 15},
    'DNA Splicer': {'gold': 1000, 'shards': 9},
}
# Specific brushes are inventory items awarded only by /release.
SPECIFIC_BRUSHES = {f'{trait} Trait Brush': trait for trait in ALL_TRAITS}
RARE_BRUSH_TRAITS = set(NOSPAWN_TRAITS)
ITEMS.update({name: {'gold': None, 'shards': None} for name in SPECIFIC_BRUSHES})
ITEM_LOOKUP = {s.casefold(): s for s in ITEMS}
BALL_RATES = {'Pokeball': 0.40, 'Great Ball': 0.60, 'Ultra Ball': 0.80}
ART_TYPES = {'Headshot': 200, 'Halfbody': 500, 'Fullbody': 1000}
ART_FINISH = {'Sketch': 0, 'Lined': 100, 'Loose colour': 150, 'Full colour': 200, 'Coloured lines / Lineless': 250}
ART_SHADING = {'Unshaded': 0, 'Minimal shading': 200, 'Rendered': 500}
ART_ANIMATED = {'FBF': 2000, 'Tweened': 500}
logging.basicConfig(level=logging.INFO)
log = logging.getLogger('sunny')

intents = discord.Intents.default()
intents.members = True  # Enables cleanup when members leave; enable this in Developer Portal too.
bot = commands.Bot(command_prefix='!', intents=intents, help_command=None)
db: sqlite3.Connection | None = None
mutex = asyncio.Lock()
spawns: dict[int, dict] = {}
spawn_locks: dict[int, asyncio.Lock] = {}


def conn() -> sqlite3.Connection:
    assert db is not None
    return db


def initialize() -> None:
    global db
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript('''
    CREATE TABLE IF NOT EXISTS config(guild INTEGER PRIMARY KEY, channel INTEGER);
    CREATE TABLE IF NOT EXISTS users(
      guild INTEGER NOT NULL, user INTEGER NOT NULL,
      gold INTEGER NOT NULL DEFAULT 0 CHECK(gold>=0),
      shards INTEGER NOT NULL DEFAULT 0 CHECK(shards>=0),
      active_id INTEGER, exp INTEGER NOT NULL DEFAULT 0 CHECK(exp>=0),
      PRIMARY KEY(guild,user)
    );
    CREATE TABLE IF NOT EXISTS items(
      guild INTEGER NOT NULL, user INTEGER NOT NULL, name TEXT NOT NULL,
      qty INTEGER NOT NULL CHECK(qty>=0), PRIMARY KEY(guild,user,name)
    );
    CREATE TABLE IF NOT EXISTS creatures(
      id INTEGER PRIMARY KEY, guild INTEGER NOT NULL, owner INTEGER NOT NULL,
      species TEXT NOT NULL, traits TEXT NOT NULL, level INTEGER NOT NULL CHECK(level>=1),
      exp INTEGER NOT NULL DEFAULT 0 CHECK(exp>=0)
    );
    CREATE INDEX IF NOT EXISTS creature_owner ON creatures(guild,owner);
    CREATE TABLE IF NOT EXISTS free_ids(id INTEGER PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS id_counter(key INTEGER PRIMARY KEY CHECK(key=1), next_id INTEGER NOT NULL);
    INSERT OR IGNORE INTO id_counter(key,next_id) VALUES(1,1);
    CREATE TABLE IF NOT EXISTS image_cache(species TEXT PRIMARY KEY, image TEXT, wiki TEXT, checked INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS designs(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      guild INTEGER NOT NULL, creature_id INTEGER NOT NULL,
      submitter INTEGER NOT NULL, artist INTEGER, submission_url TEXT NOT NULL,
      attachment_url TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','approved','denied')),
      reviewer INTEGER, created_at INTEGER NOT NULL, reviewed_at INTEGER
    );
    CREATE INDEX IF NOT EXISTS designs_creature_lookup ON designs(guild,creature_id,status,id);
    CREATE TABLE IF NOT EXISTS art_submissions(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      guild INTEGER NOT NULL, submitter INTEGER NOT NULL, artist INTEGER,
      type1 TEXT NOT NULL, type2 TEXT, type3 TEXT,
      finish TEXT NOT NULL, shading TEXT NOT NULL, animated TEXT,
      total_gc INTEGER NOT NULL CHECK(total_gc>=0),
      submission_url TEXT NOT NULL, attachment_url TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','approved','denied')),
      reviewer INTEGER, created_at INTEGER NOT NULL, reviewed_at INTEGER
    );
    ''')
    cols={r['name'] for r in db.execute('PRAGMA table_info(creatures)').fetchall()}
    if 'exp' not in cols:
        db.execute('ALTER TABLE creatures ADD COLUMN exp INTEGER NOT NULL DEFAULT 0')
    if 'nickname' not in cols:
        db.execute('ALTER TABLE creatures ADD COLUMN nickname TEXT')
    design_cols={r['name'] for r in db.execute('PRAGMA table_info(designs)').fetchall()}
    if 'artist' not in design_cols:
        db.execute('ALTER TABLE designs ADD COLUMN artist INTEGER')


def userrow(guild: int, user: int) -> sqlite3.Row:
    conn().execute('INSERT OR IGNORE INTO users(guild,user) VALUES(?,?)', (guild, user))
    return conn().execute('SELECT * FROM users WHERE guild=? AND user=?', (guild,user)).fetchone()


def item_count(guild: int, user: int, name: str) -> int:
    x = conn().execute('SELECT qty FROM items WHERE guild=? AND user=? AND name=?', (guild,user,name)).fetchone()
    return x['qty'] if x else 0


def change_item(guild: int, user: int, name: str, delta: int) -> None:
    """Adjust an item balance without violating SQLite's nonnegative CHECK.

    Negative quantities must be applied using UPDATE, rather than inserting
    a negative quantity into an UPSERT (SQLite checks the inserted row first).
    """
    if delta == 0:
        return
    if delta > 0:
        conn().execute('INSERT INTO items(guild,user,name,qty) VALUES(?,?,?,?) '
                       'ON CONFLICT(guild,user,name) DO UPDATE SET qty=qty+excluded.qty',
                       (guild,user,name,delta))
        return
    result = conn().execute(
        'UPDATE items SET qty=qty+? WHERE guild=? AND user=? AND name=? AND qty>=?',
        (delta,guild,user,name,-delta))
    if result.rowcount != 1:
        raise ValueError('Not enough ' + name + '.')
    conn().execute('DELETE FROM items WHERE guild=? AND user=? AND name=? AND qty=0',
                   (guild,user,name))


def change_money(guild: int, user: int, column: str, delta: int) -> None:
    if column not in ('gold','shards'):
        raise ValueError('Invalid currency.')
    r = userrow(guild,user)
    if r[column]+delta < 0:
        raise ValueError('Not enough currency.')
    conn().execute(f'UPDATE users SET {column}={column}+? WHERE guild=? AND user=?',(delta,guild,user))


def allocate_id() -> int:
    free = conn().execute('SELECT id FROM free_ids ORDER BY id LIMIT 1').fetchone()
    if free:
        conn().execute('DELETE FROM free_ids WHERE id=?',(free['id'],))
        return free['id']
    nxt = conn().execute('SELECT next_id FROM id_counter WHERE key=1').fetchone()['next_id']
    conn().execute('UPDATE id_counter SET next_id=? WHERE key=1',(nxt+1,))
    return nxt


def create_creature(guild: int, user: int, name: str, traits: list[str], level: int) -> int:
    userrow(guild,user)
    cid = allocate_id()
    conn().execute('INSERT INTO creatures(id,guild,owner,species,traits,level) VALUES(?,?,?,?,?,?)',
                   (cid,guild,user,name,json.dumps(traits),level))
    return cid


def delete_creature(cid: int) -> None:
    c = conn().execute('SELECT * FROM creatures WHERE id=?',(cid,)).fetchone()
    if c is None:
        raise ValueError('Creature ID does not exist.')
    conn().execute('UPDATE users SET active_id=NULL,exp=0 WHERE guild=? AND user=? AND active_id=?',
                   (c['guild'],c['owner'],cid))
    conn().execute('DELETE FROM designs WHERE guild=? AND creature_id=?',(c['guild'],cid))
    conn().execute('DELETE FROM creatures WHERE id=?',(cid,))
    conn().execute('INSERT INTO free_ids(id) VALUES(?)',(cid,))


def own_creature(guild: int, user: int, cid: int) -> sqlite3.Row:
    c = conn().execute('SELECT * FROM creatures WHERE id=? AND guild=? AND owner=?',(cid,guild,user)).fetchone()
    if not c:
        raise ValueError('That creature ID is not in your inventory.')
    return c


def transfer_creature(cid: int, guild: int, sender: int, receiver: int) -> None:
    own_creature(guild,sender,cid)
    userrow(guild,receiver)
    conn().execute('UPDATE users SET active_id=NULL,exp=0 WHERE guild=? AND user=? AND active_id=?',
                   (guild,sender,cid))
    conn().execute('UPDATE creatures SET owner=? WHERE id=?',(receiver,cid))


def exp_needed(level: int) -> int:
    # EXP storage is SQLite's signed 64-bit integer. At very high levels,
    # 10 * 1.2 ** (level-1) is already beyond achievable saved EXP.
    if level >= 250:
        return 2**63
    # ceil(10 * 1.2**(level-1)), calculated without floating-point error.
    return (10 * pow(6,level-1) + pow(5,level-1)-1) // pow(5,level-1)


def give_catch_exp(guild: int, user: int, exp: int) -> tuple[int,int] | None:
    r = userrow(guild,user)
    cid = r['active_id']
    if cid is None:
        return None
    c = conn().execute('SELECT * FROM creatures WHERE id=? AND guild=? AND owner=?',(cid,guild,user)).fetchone()
    if c is None:
        conn().execute('UPDATE users SET active_id=NULL,exp=0 WHERE guild=? AND user=?',(guild,user))
        return None
    level = c['level']
    pool = c['exp']+exp
    raised = 0
    while pool >= exp_needed(level):
        pool -= exp_needed(level)
        level += 1
        raised += 1
    conn().execute('UPDATE creatures SET level=?,exp=? WHERE id=?',(level,pool,cid))
    return cid,raised


def canonical_creature(s: str) -> str:
    c = CREATURE_LOOKUP.get(s.strip().casefold())
    if c is None:
        raise ValueError('Creature is not in creatures.txt.')
    return c


def parse_traits(s: str, allow_nospawn: bool=True) -> list[str]:
    vals = [x.strip() for x in re.split(r'[,;\n]+',s) if x.strip()]
    if not vals:
        raise ValueError('Specify at least one trait.')
    result = []
    for val in vals:
        t = TRAIT_LOOKUP.get(val.casefold())
        if not t:
            raise ValueError(f'Unknown trait: {val}')
        if not allow_nospawn and t in NOSPAWN_TRAITS:
            raise ValueError(f'{t} is [NOSPAWN].')
        if t in result:
            raise ValueError('A creature cannot have the same trait twice.')
        result.append(t)
    return result


def roll_traits() -> list[str]:
    # 1 to 3 distinct spawnable traits. Basic is allowed as given in the list.
    return random.sample(NORMAL_TRAITS, random.randint(1,3))


def roll_die_trait(existing: list[str]) -> str | None:
    pool = [t for t in NORMAL_TRAITS if t not in existing] + [t for t in NOSPAWN_TRAITS if t not in existing]
    if not pool:
        return None
    # '80% rarer' interpreted as weight 0.2 for each NOSPAWN trait.
    return random.choices(pool, weights=[0.2 if t in NOSPAWN_TRAITS else 1.0 for t in pool],k=1)[0]


def species_list(raw: str) -> list[str]:
    """Existing rows contain a plain species; hybrids use a JSON list of two."""
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list) and parsed and all(isinstance(x, str) for x in parsed):
            return parsed
    except (ValueError, TypeError):
        pass
    return [raw]


def species_display(raw: str) -> str:
    return ' / '.join(species_list(raw))


def add_hybrid_species(raw: str, selected: str | None = None) -> str:
    species = species_list(raw)
    if len(species) >= 2:
        raise ValueError('This creature already has two species. Remove one using a DNA Splicer first.')
    if selected is None:
        choices = [name for name in CREATURES if name != species[0]]
        if not choices:
            raise ValueError('No second species available.')
        second = random.choice(choices)
    else:
        second = canonical_creature(selected)
        if second == species[0]:
            raise ValueError('The hybrid species must differ from the existing species.')
    return json.dumps([species[0], second], ensure_ascii=False)


def add_trait_to_creature(cid: int, creature: sqlite3.Row, trait: str, hybrid_species: str | None = None) -> str | None:
    traits = json.loads(creature['traits'])
    if trait in traits:
        raise ValueError('That creature already has this trait.')
    new_species = creature['species']
    if trait == 'Hybrid':
        new_species = add_hybrid_species(new_species, hybrid_species)
    elif hybrid_species is not None:
        raise ValueError('A hybrid species can only be supplied when applying Hybrid.')
    traits.append(trait)
    conn().execute('UPDATE creatures SET traits=?,species=? WHERE id=?',
                   (json.dumps(traits),new_species,cid))
    return species_list(new_species)[1] if trait == 'Hybrid' else None


def roll_release_item() -> str | None:
    """50% item chance; Rare Egg occurs in 1% of all releases."""
    if random.random() >= 0.5:
        return None
    if random.random() < 0.02:
        return 'Rare Egg'
    # Two regular eggs share the pool with the trait brushes. Epic is
    # exactly 1% of brush rewards; remaining rare brushes have low weight.
    if random.random() < 0.08:
        return random.choice(['Species Egg', 'Trait Egg'])
    if random.random() < 0.01:
        return 'Epic Trait Brush'
    brush_pool = [name for name,trait in SPECIFIC_BRUSHES.items() if trait != 'Epic']
    brush_weights = [0.25 if SPECIFIC_BRUSHES[name] in RARE_BRUSH_TRAITS else 4.0 for name in brush_pool]
    return random.choices(brush_pool, weights=brush_weights, k=1)[0]


def safe_discord_text(value: str) -> str:
    # Prevent mentions and markdown injection inside creature labels.
    return discord.utils.escape_mentions(discord.utils.escape_markdown(value))


def fmt_c(c: sqlite3.Row) -> str:
    # Link to the newest approved design.
    species = safe_discord_text(species_display(c['species']))
    approved = conn().execute(
        'SELECT submission_url FROM designs WHERE guild=? AND creature_id=? AND status=? ORDER BY id DESC LIMIT 1',
        (c['guild'], c['id'], 'approved')).fetchone()
    if approved:
        species = f'[{species}]({approved["submission_url"]})'
    nickname = f' ({safe_discord_text(c["nickname"])})' if c['nickname'] else ''
    return f"**#{c['id']} {species}{nickname}** · Lv. {c['level']} · {', '.join(json.loads(c['traits']))}"


def transacted(fn):
    """Use only within mutex; rollback on error."""
    conn().execute('BEGIN IMMEDIATE')
    try:
        result = fn()
        conn().execute('COMMIT')
        return result
    except BaseException:
        conn().execute('ROLLBACK')
        raise


async def feedback(interaction: discord.Interaction, message: str, ephemeral: bool=False) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(message, ephemeral=ephemeral)


def guild_only(interaction: discord.Interaction) -> tuple[int,int]:
    if not interaction.guild_id:
        raise ValueError('Use this command in a server.')
    return interaction.guild_id,interaction.user.id


def admin_only(interaction: discord.Interaction) -> None:
    if not isinstance(interaction.user,discord.Member) or not interaction.user.guild_permissions.manage_guild:
        raise ValueError('Manage Server permission is required.')


async def creature_autocomplete(interaction: discord.Interaction, current: str):
    q = current.casefold()
    names = [x for x in CREATURES if x.casefold().startswith(q)]
    if len(names)<25:
        names.extend(x for x in CREATURES if q in x.casefold() and x not in names)
    return [app_commands.Choice(name=x,value=x) for x in names[:25]]


async def trait_autocomplete(interaction: discord.Interaction, current: str):
    q = current.casefold()
    return [app_commands.Choice(name=x,value=x) for x in ALL_TRAITS if q in x.casefold()][:25]


async def item_autocomplete(interaction: discord.Interaction, current: str):
    return [app_commands.Choice(name=x,value=x) for x in ITEMS if current.casefold() in x.casefold()][:25]


def validate_submission_image(image: discord.Attachment, label: str='image') -> None:
    valid_type = image.content_type and image.content_type.lower().startswith('image/')
    valid_extension = Path(image.filename).suffix.lower() in ('.png', '.jpg', '.jpeg', '.gif', '.webp')
    if not (valid_type or valid_extension):
        raise ValueError(f'Upload a PNG, JPG, GIF, or WebP {label}.')
    if image.size > 10 * 1024 * 1024:
        raise ValueError(f'{label.capitalize()} files must be 10 MB or smaller.')


def calculate_art_total(type_names: list[str], finish: str, shading: str, animated: str | None) -> int:
    total = sum(ART_TYPES[name] for name in type_names)
    total += ART_FINISH[finish]
    total += ART_SHADING[shading]
    if animated:
        total += ART_ANIMATED[animated]
    return total


def art_breakdown(type_names: list[str], finish: str, shading: str, animated: str | None) -> str:
    bits = [f'{name} ({ART_TYPES[name]} GC)' for name in type_names]
    bits.append(f'{finish} ({ART_FINISH[finish]} GC)')
    bits.append(f'{shading} ({ART_SHADING[shading]} GC)')
    if animated:
        bits.append(f'{animated} ({ART_ANIMATED[animated]} GC)')
    return ' + '.join(bits)


class CatchSelect(discord.ui.Select):
    def __init__(self,guild_id: int,spawn_id: int,counts: dict[str,int],owner_id:int):
        self.guild_id,self.spawn_id,self.owner_id=guild_id,spawn_id,owner_id
        opts=[discord.SelectOption(label=k,description=f'{int(v*100)}% chance · owned: {counts[k]}',value=k)
              for k,v in BALL_RATES.items() if counts[k]>0]
        super().__init__(placeholder='Choose a Pokéball',min_values=1,max_values=1,options=opts)

    async def callback(self, interaction: discord.Interaction) -> None:
        # Acknowledge the dropdown immediately. Discord expires unacknowledged
        # component interactions after roughly three seconds.
        if interaction.user.id != self.owner_id:
            await feedback(interaction, 'This Pokéball menu belongs to another player. Click Catch! on the wild spawn to open your own.')
            return
        await interaction.response.defer(thinking=False)
        try:
            gid, uid = guild_only(interaction)
            lock = spawn_locks.setdefault(gid, asyncio.Lock())
            async with lock:
                async with mutex:
                    entry = spawns.get(gid)
                    if not entry or entry['id'] != self.spawn_id or entry['caught']:
                        await feedback(interaction, 'This wild creature is no longer available.')
                        return
                    ball = self.values[0]
                    if item_count(gid, uid, ball) < 1:
                        await feedback(interaction, f'You have no {ball}s left.')
                        return

                    def roll():
                        change_item(gid, uid, ball, -1)
                        success = random.random() < BALL_RATES[ball]
                        if not success:
                            return None
                        cid = create_creature(gid, uid, entry['species'], entry['traits'], 5)
                        gold = random.randint(1, 50)
                        change_money(gid, uid, 'gold', gold)
                        earned = random.randint(10, 100)
                        leveled = give_catch_exp(gid, uid, earned)
                        return cid, gold, earned, leveled

                    outcome = transacted(roll)
                    if outcome:
                        entry['caught'] = True

            if not outcome:
                # The wild spawn remains catchable. Refresh this public menu so
                # the player can try again, including with a different ball.
                async with mutex:
                    remaining = {name: item_count(gid, uid, name) for name in BALL_RATES}
                message = (f'{interaction.user.mention} threw a **{ball}**, but the '
                           f'**{entry["species"]}** escaped! One {ball} was used.')
                if any(remaining.values()):
                    message += '\nChoose another Pokéball to try again.'
                    await interaction.message.edit(content=message,
                        view=BallMenu(gid, self.spawn_id, remaining, uid))
                else:
                    message += '\nYou have no Pokéballs left. Use /shop buy to get more, then click Catch! again.'
                    await interaction.message.edit(content=message, view=None)
                return
            cid, gold, earned, leveled = outcome
            level_msg = f' Active creature gained {earned} EXP.' if leveled else ''
            if leveled and leveled[1]:
                level_msg += f' #{leveled[0]} gained {leveled[1]} level(s).'
            msg = (f'{interaction.user.mention} caught **{entry["species"]}** (#{cid}, Lv. 5)! '
                   f'Traits: {", ".join(entry["traits"])}. Earned **{gold} Gold Coins**.'
                   + level_msg)
            await interaction.message.edit(content=msg, view=None)
            if entry.get('message'):
                try:
                    await entry['message'].edit(view=None)
                except discord.HTTPException:
                    pass
        except Exception:
            log.exception('Error processing Pokéball selection in guild %s', interaction.guild_id)
            try:
                await feedback(interaction, 'The catch encountered an error. Please check the Railway logs.')
            except discord.HTTPException:
                log.exception('Could not send Pokéball error response')


class BallMenu(discord.ui.View):
    def __init__(self,guild_id: int,spawn_id: int,counts: dict[str,int],owner_id:int):
        super().__init__(timeout=180)
        self.add_item(CatchSelect(guild_id,spawn_id,counts,owner_id))


class CatchView(discord.ui.View):
    def __init__(self,guild_id: int,spawn_id: int):
        super().__init__(timeout=None)
        self.guild_id,self.spawn_id=guild_id,spawn_id

    @discord.ui.button(label='Catch!',style=discord.ButtonStyle.success,custom_id='sunny:catch')
    async def catch(self,interaction: discord.Interaction,button: discord.ui.Button):
        gid,uid=guild_only(interaction)
        if gid!=self.guild_id or not spawns.get(gid) or spawns[gid]['id']!=self.spawn_id or spawns[gid]['caught']:
            await feedback(interaction,'This spawn has expired.')
            return
        async with mutex:
            counts={k:item_count(gid,uid,k) for k in BALL_RATES}
        if not any(counts.values()):
            await feedback(interaction,'You need a ball. Purchase one with /shop buy.')
            return
        await interaction.response.send_message(f'{interaction.user.mention}, select a ball to throw:',view=BallMenu(gid,self.spawn_id,counts,uid),ephemeral=False)


async def post_spawn(guild_id: int,channel_id: int) -> None:
    channel=bot.get_channel(channel_id)
    if channel is None:
        try:
            channel=await bot.fetch_channel(channel_id)
        except (discord.NotFound,discord.Forbidden,discord.HTTPException):
            return
    if not hasattr(channel,'send'):
        return
    old=spawns.pop(guild_id,None)
    if old and old.get('message'):
        if not old.get('caught'):
            try:
                await old['message'].delete()
            except discord.HTTPException:
                pass
        else:
            try:
                await old['message'].edit(view=None)
            except discord.HTTPException:
                pass
    species=random.choice(CREATURES)
    traits=roll_traits()
    embed=discord.Embed(title=f'Wild {species} appeared!',description='**Traits:** '+', '.join(traits)+'\n**Catch level:** 5\nUse **Catch!** to choose a ball.',color=discord.Color.orange())
    token=random.getrandbits(63)
    message=await channel.send(embed=embed,view=CatchView(guild_id,token))
    spawns[guild_id]={'id':token,'species':species,'traits':traits,'caught':False,'message':message}


@tasks.loop(minutes=5)
async def spawn_loop():
    rows=conn().execute('SELECT * FROM config WHERE channel IS NOT NULL').fetchall()
    for config in rows:
        try:
            await post_spawn(config['guild'],config['channel'])
        except (discord.HTTPException,ValueError,sqlite3.Error):
            log.exception('Unable to spawn in guild %s',config['guild'])


@spawn_loop.before_loop
async def before_spawn():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    log.info('Logged in as %s',bot.user)


@bot.event
async def on_member_remove(member:discord.Member):
    async with mutex:
        def clear():
            rows=conn().execute('SELECT id FROM creatures WHERE guild=? AND owner=?',(member.guild.id,member.id)).fetchall()
            for row in rows:
                delete_creature(row['id'])
            conn().execute('DELETE FROM items WHERE guild=? AND user=?',(member.guild.id,member.id))
            conn().execute('DELETE FROM users WHERE guild=? AND user=?',(member.guild.id,member.id))
        transacted(clear)


@bot.tree.error
async def command_error(interaction:discord.Interaction,error:app_commands.AppCommandError):
    if isinstance(error,app_commands.CommandInvokeError):
        root=error.original
    else:
        root=error
    spawn_admin = interaction.command is not None and interaction.command.name in ('cspawn', 'spawnnow')
    if isinstance(root,ValueError):
        await feedback(interaction,str(root),ephemeral=spawn_admin)
    else:
        log.exception('Slash command failed',exc_info=error)
        await feedback(interaction,'An error occurred. Please contact a server admin.',ephemeral=spawn_admin)


@bot.tree.command(name='spawnchannel',description='Admin: set the channel for five-minute wild creature spawns.')
@app_commands.default_permissions(manage_guild=True)
async def spawnchannel(interaction:discord.Interaction,channel:discord.TextChannel):
    admin_only(interaction)
    gid,_=guild_only(interaction)
    async with mutex:
        conn().execute('INSERT INTO config(guild,channel) VALUES(?,?) ON CONFLICT(guild) DO UPDATE SET channel=excluded.channel',(gid,channel.id))
    await feedback(interaction,f'Wild creatures will spawn in {channel.mention} every 5 minutes.')


@bot.tree.command(name='spawnnow',description='Admin: immediately spawn one wild creature in the configured channel.')
@app_commands.default_permissions(manage_guild=True)
async def spawnnow(interaction:discord.Interaction):
    admin_only(interaction)
    gid,_=guild_only(interaction)
    row=conn().execute('SELECT channel FROM config WHERE guild=?',(gid,)).fetchone()
    if not row:
        raise ValueError('Set /spawnchannel first.')
    await interaction.response.defer(ephemeral=True,thinking=True)
    await post_spawn(gid,row['channel'])
    await feedback(interaction,'Wild creature spawned.',ephemeral=True)


@bot.tree.command(name='cspawn',description='Admin: grant a creature and any comma-separated traits.')
@app_commands.default_permissions(manage_guild=True)
@app_commands.autocomplete(creature=creature_autocomplete)
async def cspawn(interaction:discord.Interaction,creature:str,user:discord.Member,traits:str,level:app_commands.Range[int,1,1000000]=1):
    admin_only(interaction)
    gid,_=guild_only(interaction)
    creature=canonical_creature(creature)
    trait_list=parse_traits(traits)
    async with mutex:
        cid=transacted(lambda:create_creature(gid,user.id,creature,trait_list,level))
    await feedback(interaction,f'Added **{creature}** #{cid} at Lv. {level} to {user.mention}. Traits: {", ".join(trait_list)}.',ephemeral=True)


@bot.tree.command(name='clevel',description='Admin: add levels to a creature without replacing its level.')
@app_commands.default_permissions(manage_guild=True)
async def clevel(interaction:discord.Interaction,creature_id:int,levels_to_add:app_commands.Range[int,1,1000000]):
    admin_only(interaction)
    gid,_=guild_only(interaction)
    async with mutex:
        c=conn().execute('SELECT * FROM creatures WHERE id=? AND guild=?',(creature_id,gid)).fetchone()
        if not c:
            raise ValueError('Creature ID not found in this server.')
        conn().execute('UPDATE creatures SET level=level+? WHERE id=?',(levels_to_add,creature_id))
    await feedback(interaction,f'#{creature_id} gained {levels_to_add} levels. Now Lv. {c["level"]+levels_to_add}.',False)


@bot.tree.command(name='cdelete',description='Admin: permanently remove a creature, freeing its ID.')
@app_commands.default_permissions(manage_guild=True)
async def cdelete(interaction:discord.Interaction,creature_id:int):
    admin_only(interaction)
    gid,_=guild_only(interaction)
    async with mutex:
        c=conn().execute('SELECT * FROM creatures WHERE id=? AND guild=?',(creature_id,gid)).fetchone()
        if not c:
            raise ValueError('Creature ID not found in this server.')
        transacted(lambda:delete_creature(creature_id))
    await feedback(interaction,f'Deleted #{creature_id}. Its ID can now be reused.',False)


@bot.tree.command(name='active',description='Choose your active creature to receive EXP from successful catches.')
async def active(interaction:discord.Interaction,id:int):
    gid,uid=guild_only(interaction)
    async with mutex:
        c=own_creature(gid,uid,id)
        userrow(gid,uid)
        conn().execute('UPDATE users SET active_id=? WHERE guild=? AND user=?',(id,gid,uid))
    await feedback(interaction,f'#{id} {species_display(c["species"])} is now active. EXP: {c["exp"]}/{exp_needed(c["level"])}.')


@bot.tree.command(name='candy',description='Use Rare Candies to raise your creature level.')
async def candy(interaction:discord.Interaction,id:int,quantity:app_commands.Range[int,1,100000]=1):
    gid,uid=guild_only(interaction)
    async with mutex:
        def use():
            c=own_creature(gid,uid,id)
            change_item(gid,uid,'Rare Candy',-quantity)
            conn().execute('UPDATE creatures SET level=level+? WHERE id=?',(quantity,id))
            return c['level']+quantity
        result=transacted(use)
    await feedback(interaction,f'#{id} used {quantity} Rare Candy(s). Now Lv. {result}.')


@bot.tree.command(name='dice',description='Use one Dice to add a randomly selected new trait.')
async def dice(interaction:discord.Interaction,id:int):
    gid,uid=guild_only(interaction)
    async with mutex:
        def use():
            c=own_creature(gid,uid,id)
            traits=json.loads(c['traits'])
            rolled=roll_die_trait(traits)
            if not rolled:
                raise ValueError('This creature already has every available trait.')
            change_item(gid,uid,'Dice',-1)
            second=add_trait_to_creature(id,c,rolled)
            return rolled,second
        result=transacted(use)
    await feedback(interaction,f'#{id} gained **{result[0]}**!' + (f' Hybrid species: **{result[1]}**.' if result[1] else ''))


trait_group=app_commands.Group(name='trait',description='Creature trait commands')
@trait_group.command(name='add',description='Apply a trait using its specific brush or a generic Trait Brush.')
@app_commands.autocomplete(trait=trait_autocomplete,hybrid_species=creature_autocomplete)
async def trait_add(interaction:discord.Interaction,id:int,trait:str,hybrid_species:str|None=None):
    gid,uid=guild_only(interaction)
    wanted=parse_traits(trait)
    if len(wanted)!=1:
        raise ValueError('Apply one trait per brush.')
    trait_name=wanted[0]
    if hybrid_species is not None and trait_name != 'Hybrid':
        raise ValueError('hybrid_species is only valid for Hybrid.')
    async with mutex:
        def use():
            c=own_creature(gid,uid,id)
            named=f'{trait_name} Trait Brush'
            if item_count(gid,uid,named):
                used=named
            elif (trait_name in NORMAL_TRAITS or trait_name == 'Hybrid') and item_count(gid,uid,'Trait Brush'):
                used='Trait Brush'
            else:
                raise ValueError(f'You need a {named}' + (' or generic Trait Brush.' if trait_name in NORMAL_TRAITS or trait_name == 'Hybrid' else '.'))
            if hybrid_species is not None and used != named:
                raise ValueError('Choosing the second species requires a specific Hybrid Trait Brush. Generic brushes roll randomly.')
            # Validate all species/traits before consuming the brush.
            second=add_trait_to_creature(id,c,trait_name,hybrid_species if used == named else None)
            change_item(gid,uid,used,-1)
            return used,second
        used,second=transacted(use)
    await feedback(interaction,f'Used **{used}**. Added **{trait_name}** to creature #{id}.' + (f' Second species: **{second}**.' if second else ''))

bot.tree.add_command(trait_group)


@bot.tree.command(name='creature',description='View an owned creature and its current EXP progress.')
async def creature_info(interaction:discord.Interaction,id:int):
    gid,uid=guild_only(interaction)
    async with mutex:
        c=own_creature(gid,uid,id)
        current=userrow(gid,uid)['active_id']
    await feedback(interaction,fmt_c(c)+f'\nEXP: {c["exp"]:,}/{exp_needed(c["level"]):,}' + (' · **ACTIVE**' if id==current else ''))


@bot.tree.command(name='nickname',description='Give one of your creatures a nickname, or clear it.')
async def nickname(interaction:discord.Interaction,id:int,name:app_commands.Range[str,1,40]|None=None):
    gid,uid=guild_only(interaction)
    if name is not None:
        name=name.strip()
        if not name or len(name)>40 or any(ord(ch)<32 or ord(ch)==127 for ch in name):
            raise ValueError('Nicknames must contain 1–40 printable characters.')
    async with mutex:
        own_creature(gid,uid,id)
        conn().execute('UPDATE creatures SET nickname=? WHERE id=?',(name,id))
    if name:
        await feedback(interaction,f'Creature #{id} is now nicknamed **{safe_discord_text(name)}**.')
    else:
        await feedback(interaction,f'Nickname cleared for creature #{id}.')


design_group=app_commands.Group(name='design',description='Submit creature artwork and review submissions')

@design_group.command(name='submit',description='Submit a design for one of your creatures.')
async def design_submit(interaction:discord.Interaction,id:int,image:discord.Attachment,artist:discord.Member|None=None):
    gid,uid=guild_only(interaction)
    if not interaction.channel or not hasattr(interaction.channel, 'send'):
        raise ValueError('Submit designs in a server text channel.')
    validate_submission_image(image, 'design image')
    if artist is not None and artist.bot:
        raise ValueError('Artist must be a human server member.')
    async with mutex:
        c=own_creature(gid,uid,id)
        species=species_display(c['species'])
    reward_user = artist or interaction.user
    await interaction.response.defer(thinking=True)
    header = (
        f'**Creature design submission** · #{id} {safe_discord_text(species)}\n'
        f'Submitted by {interaction.user.mention}\n'
        + (f'Artist: {artist.mention}\n' if artist else '')
        + 'Pending staff review'
    )
    posted=await interaction.channel.send(
        header,
        file=await image.to_file(),
        allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
    try:
        async with mutex:
            def save():
                own_creature(gid,uid,id)
                cur=conn().execute('INSERT INTO designs(guild,creature_id,submitter,artist,submission_url,attachment_url,status,created_at) VALUES(?,?,?,?,?,?,?,?)',
                    (gid,id,uid,reward_user.id if artist else None,posted.jump_url,image.url,'pending',int(time.time())))
                change_money(gid,reward_user.id,'gold',500)
                change_money(gid,reward_user.id,'shards',1)
                return cur.lastrowid
            did=transacted(save)
    except Exception:
        await posted.delete()
        raise
    public = (
        f'**Design #{did}** · Creature #{id} {safe_discord_text(species)}\n'
        f'Submitted by {interaction.user.mention}\n'
        + (f'Artist: {artist.mention}\n' if artist else '')
        + f'**PENDING** staff review\n'
        + f'Reward granted to {reward_user.mention}: **500 Gold Coins** and **1 Diamond Shard**'
    )
    await posted.edit(content=public, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
    await interaction.followup.send(
        f'Design **#{did}** submitted for creature **#{id}**. Reward granted to {reward_user.mention}: **500 Gold Coins** and **1 Diamond Shard**. Review: {posted.jump_url}',
        allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))


@design_group.command(name='approve',description='Staff: approve a pending creature design.')
@app_commands.default_permissions(manage_guild=True)
async def design_approve(interaction:discord.Interaction,design_id:int):
    gid,_=guild_only(interaction)
    admin_only(interaction)
    async with mutex:
        def approve():
            d=conn().execute('SELECT * FROM designs WHERE id=? AND guild=?',(design_id,gid)).fetchone()
            if d is None:
                raise ValueError('Design ID does not exist in this server.')
            if d['status']!='pending':
                raise ValueError(f'That design is already {d["status"]}.')
            c=conn().execute('SELECT id FROM creatures WHERE guild=? AND id=?',(gid,d['creature_id'])).fetchone()
            if c is None:
                raise ValueError('The associated creature no longer exists.')
            conn().execute('UPDATE designs SET status=?,reviewer=?,reviewed_at=? WHERE id=?',
                ('approved',interaction.user.id,int(time.time()),design_id))
            return d
        d=transacted(approve)
    await feedback(interaction,f'Approved design **#{design_id}** for creature **#{d["creature_id"]}**. Its inventory name now links to the [approved design]({d["submission_url"]}).')


@design_group.command(name='deny',description='Staff: deny a pending creature design.')
@app_commands.default_permissions(manage_guild=True)
async def design_deny(interaction:discord.Interaction,design_id:int,reason:app_commands.Range[str,1,250]|None=None):
    gid,_=guild_only(interaction)
    admin_only(interaction)
    async with mutex:
        def deny():
            d=conn().execute('SELECT * FROM designs WHERE id=? AND guild=?',(design_id,gid)).fetchone()
            if d is None:
                raise ValueError('Design ID does not exist in this server.')
            if d['status']!='pending':
                raise ValueError(f'That design is already {d["status"]}.')
            conn().execute('UPDATE designs SET status=?,reviewer=?,reviewed_at=? WHERE id=?',
                ('denied',interaction.user.id,int(time.time()),design_id))
            return d
        d=transacted(deny)
    message=f'Denied design **#{design_id}** for creature **#{d["creature_id"]}**.'
    if reason:
        message+=f' Reason: {safe_discord_text(reason)}'
    await feedback(interaction,message)


bot.tree.add_command(design_group)


art_group=app_commands.Group(name='art',description='Submit art for staff approval and payout')

@art_group.command(name='submit',description='Submit art for approval and a currency payout.')
@app_commands.choices(
    type=[app_commands.Choice(name='Headshot',value='Headshot'),app_commands.Choice(name='Halfbody',value='Halfbody'),app_commands.Choice(name='Fullbody',value='Fullbody')],
    type2=[app_commands.Choice(name='Headshot',value='Headshot'),app_commands.Choice(name='Halfbody',value='Halfbody'),app_commands.Choice(name='Fullbody',value='Fullbody')],
    type3=[app_commands.Choice(name='Headshot',value='Headshot'),app_commands.Choice(name='Halfbody',value='Halfbody'),app_commands.Choice(name='Fullbody',value='Fullbody')],
    finish=[app_commands.Choice(name='sketch',value='Sketch'),app_commands.Choice(name='lined',value='Lined'),app_commands.Choice(name='loose colour',value='Loose colour'),app_commands.Choice(name='full colour',value='Full colour'),app_commands.Choice(name='coloured lines / lineless',value='Coloured lines / Lineless')],
    shading=[app_commands.Choice(name='unshaded',value='Unshaded'),app_commands.Choice(name='minimal shading',value='Minimal shading'),app_commands.Choice(name='rendered',value='Rendered')],
    animated=[app_commands.Choice(name='fbf',value='FBF'),app_commands.Choice(name='tweened',value='Tweened')]
)
async def art_submit(interaction:discord.Interaction,
                     image:discord.Attachment,
                     type:app_commands.Choice[str],
                     finish:app_commands.Choice[str],
                     shading:app_commands.Choice[str],
                     type2:app_commands.Choice[str]|None=None,
                     type3:app_commands.Choice[str]|None=None,
                     animated:app_commands.Choice[str]|None=None):
    gid,uid=guild_only(interaction)
    if not interaction.channel or not hasattr(interaction.channel, 'send'):
        raise ValueError('Submit art in a server text channel.')
    validate_submission_image(image, 'art image')
    types=[type.value]
    if type2: types.append(type2.value)
    if type3: types.append(type3.value)
    if len(set(types)) != len(types):
        raise ValueError('TYPE, TYPE2, and TYPE3 must all be different.')
    total=calculate_art_total(types, finish.value, shading.value, animated.value if animated else None)
    breakdown=art_breakdown(types, finish.value, shading.value, animated.value if animated else None)
    await interaction.response.defer(thinking=True)
    posted=await interaction.channel.send(
        f'**Art submission**\n'
        f'Submitted by {interaction.user.mention}\n'
        f'Breakdown: {breakdown}\n'
        f'Total on approval: **{total:,} Gold Coins** and **{total//100:,} Diamond Shards**\n'
        f'**PENDING** staff review',
        file=await image.to_file(),
        allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
    try:
        async with mutex:
            def save():
                cur=conn().execute('INSERT INTO art_submissions(guild,submitter,artist,type1,type2,type3,finish,shading,animated,total_gc,submission_url,attachment_url,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (gid,uid,None,types[0],types[1] if len(types)>1 else None,types[2] if len(types)>2 else None,finish.value,shading.value,animated.value if animated else None,total,posted.jump_url,image.url,'pending',int(time.time())))
                return cur.lastrowid
            aid=transacted(save)
    except Exception:
        await posted.delete()
        raise
    await posted.edit(content=(
        f'**Art #{aid}**\n'
        f'Submitted by {interaction.user.mention}\n'
        f'Breakdown: {breakdown}\n'
        f'Total on approval: **{total:,} Gold Coins** and **{total//100:,} Diamond Shards**\n'
        f'**PENDING** staff review'),
        allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
    await interaction.followup.send(
        f'Art **#{aid}** submitted. Total on approval: **{total:,} Gold Coins** and **{total//100:,} Diamond Shards**. Review: {posted.jump_url}')


@art_group.command(name='approve',description='Staff: approve a pending art submission and pay it out.')
@app_commands.default_permissions(manage_guild=True)
async def art_approve(interaction:discord.Interaction,art_id:int):
    gid,_=guild_only(interaction)
    admin_only(interaction)
    async with mutex:
        def approve():
            a=conn().execute('SELECT * FROM art_submissions WHERE id=? AND guild=?',(art_id,gid)).fetchone()
            if a is None:
                raise ValueError('Art ID does not exist in this server.')
            if a['status']!='pending':
                raise ValueError(f'That art submission is already {a["status"]}.')
            payout_gold=a['total_gc']
            payout_shards=payout_gold // 100
            change_money(gid,a['submitter'],'gold',payout_gold)
            if payout_shards:
                change_money(gid,a['submitter'],'shards',payout_shards)
            conn().execute('UPDATE art_submissions SET status=?,reviewer=?,reviewed_at=? WHERE id=?',
                ('approved',interaction.user.id,int(time.time()),art_id))
            return a, payout_gold, payout_shards
        a,payout_gold,payout_shards=transacted(approve)
    await feedback(interaction,
        f'Approved art **#{art_id}**. Paid <@{a["submitter"]}> **{payout_gold:,} Gold Coins** and **{payout_shards:,} Diamond Shards**.')


@art_group.command(name='deny',description='Staff: deny a pending art submission.')
@app_commands.default_permissions(manage_guild=True)
async def art_deny(interaction:discord.Interaction,art_id:int,reason:app_commands.Range[str,1,250]|None=None):
    gid,_=guild_only(interaction)
    admin_only(interaction)
    async with mutex:
        def deny():
            a=conn().execute('SELECT * FROM art_submissions WHERE id=? AND guild=?',(art_id,gid)).fetchone()
            if a is None:
                raise ValueError('Art ID does not exist in this server.')
            if a['status']!='pending':
                raise ValueError(f'That art submission is already {a["status"]}.')
            conn().execute('UPDATE art_submissions SET status=?,reviewer=?,reviewed_at=? WHERE id=?',
                ('denied',interaction.user.id,int(time.time()),art_id))
            return a
        a=transacted(deny)
    message=f'Denied art **#{art_id}**.'
    if reason:
        message += f' Reason: {safe_discord_text(reason)}'
    await feedback(interaction,message)

bot.tree.add_command(art_group)


@bot.tree.command(name='balance',description='View Gold Coins and Diamond Shards.')
async def balance(interaction:discord.Interaction,user:discord.Member|None=None):
    gid,uid=guild_only(interaction)
    owner=user or interaction.user
    async with mutex:
        r=userrow(gid,owner.id)
    await feedback(interaction,f'**{owner.display_name}** · {r["gold"]:,} Gold Coins · {r["shards"]:,} Diamond Shards',False)


inventory_group=app_commands.Group(name='inventory',description='View your inventories')
@inventory_group.command(name='items',description='Show item inventory.')
async def inventory_items(interaction:discord.Interaction,user:discord.Member|None=None):
    gid,uid=guild_only(interaction)
    owner=user or interaction.user
    async with mutex:
        rows=conn().execute('SELECT name,qty FROM items WHERE guild=? AND user=? ORDER BY name',(gid,owner.id)).fetchall()
    await feedback(interaction,f'**{owner.display_name} · Items**\n'+('\n'.join(f'{r["name"]}: {r["qty"]}' for r in rows) or 'No items.'),False)

@inventory_group.command(name='creatures',description='Show a page of creature inventory, sorted by unique ID.')
async def inventory_creatures(interaction:discord.Interaction,user:discord.Member|None=None,page:app_commands.Range[int,1,100000]=1):
    gid,uid=guild_only(interaction)
    owner=user or interaction.user
    async with mutex:
        rows=conn().execute('SELECT * FROM creatures WHERE guild=? AND owner=? ORDER BY id LIMIT 10 OFFSET ?', (gid,owner.id,(page-1)*10)).fetchall()
        total=conn().execute('SELECT count(*) AS n FROM creatures WHERE guild=? AND owner=?',(gid,owner.id)).fetchone()['n']
        active_id=userrow(gid,owner.id)['active_id']
    if total and not rows:
        raise ValueError('Page is beyond the end of the inventory.')
    lines=[fmt_c(r)+(' · **ACTIVE**' if r['id']==active_id else '') for r in rows]
    await feedback(interaction,f'**{owner.display_name} · Creatures** ({total} total, page {page})\n'+('\n'.join(lines) or 'No creatures.'),False)
bot.tree.add_command(inventory_group)


shop_group=app_commands.Group(name='shop',description='Buy helpful creature items')
@shop_group.command(name='view',description='See shop prices.')
async def shop_view(interaction:discord.Interaction):
    lines=[f'**{item}**: {cost["gold"]:,} Gold Coins OR {cost["shards"]} Diamond Shards' for item,cost in ITEMS.items() if cost['gold'] is not None]
    lines.append('Specific Trait Brushes: release-only; cannot be purchased.')
    await feedback(interaction,'**Sunny’s Shop**\n'+'\n'.join(lines),False)

@shop_group.command(name='buy',description='Buy one or more items with one currency.')
@app_commands.autocomplete(item=item_autocomplete)
@app_commands.choices(currency=[app_commands.Choice(name='Gold Coins',value='gold'),app_commands.Choice(name='Diamond Shards',value='shards')])
async def shop_buy(interaction:discord.Interaction,item:str,currency:app_commands.Choice[str],quantity:app_commands.Range[int,1,100000]=1):
    gid,uid=guild_only(interaction)
    item=ITEM_LOOKUP.get(item.casefold().strip())
    if not item:
        raise ValueError('That item is not available in the shop.')
    if ITEMS[item][currency.value] is None:
        raise ValueError('Specific Trait Brushes can only be obtained from /release.')
    price=ITEMS[item][currency.value]*quantity
    async with mutex:
        def buy():
            change_money(gid,uid,currency.value,-price)
            change_item(gid,uid,item,quantity)
        transacted(buy)
    await feedback(interaction,f'Purchased {quantity}× {item} for {price:,} {"Gold Coins" if currency.value=="gold" else "Diamond Shards"}.')
bot.tree.add_command(shop_group)


@bot.tree.command(name='release',description='Release your creature, recycle its ID, and receive 50–100 GC plus a possible item.')
async def release(interaction:discord.Interaction,id:int):
    gid,uid=guild_only(interaction)
    async with mutex:
        def execute():
            c=own_creature(gid,uid,id)
            species=species_display(c['species'])
            gold=random.randint(50,100)
            item=roll_release_item()
            delete_creature(id)
            change_money(gid,uid,'gold',gold)
            if item:
                change_item(gid,uid,item,1)
            return species,gold,item
        species,gold,item=transacted(execute)
    await feedback(interaction,f'Released **#{id} {safe_discord_text(species)}**. ID #{id} is available for reuse. Earned **{gold} Gold Coins**.' + (f' Item drop: **{item}**!' if item else ' No item dropped.'))


item_group=app_commands.Group(name='item',description='Use eggs, trait erasers, and DNA splicers')
@item_group.command(name='use',description='Use an egg, Trait Eraser, or DNA Splicer.')
@app_commands.choices(item=[app_commands.Choice(name=x,value=x) for x in ('Species Egg','Trait Egg','Rare Egg','Trait Eraser','DNA Splicer')])
@app_commands.autocomplete(species=creature_autocomplete,trait=trait_autocomplete)
async def item_use(interaction:discord.Interaction,item: str,
                   id:int|None=None,species:str|None=None,trait:str|None=None,
                   remove_species:app_commands.Range[int,1,2]|None=None):
    gid,uid=guild_only(interaction)
    item_names={name.casefold():name for name in ('Species Egg','Trait Egg','Rare Egg','Trait Eraser','DNA Splicer')}
    item=item_names.get(item.strip().casefold())
    if item is None:
        raise ValueError('Choose Species Egg, Trait Egg, Rare Egg, Trait Eraser, or DNA Splicer.')
    # Slash command optional fields supply context appropriate to each item.
    if item in ('Species Egg','Trait Egg','Rare Egg'):
        if item in ('Species Egg','Rare Egg') and species is None:
            raise ValueError('Choose a species.')
        if item in ('Trait Egg','Rare Egg') and trait is None:
            raise ValueError('Choose a trait.')
        selected_species=canonical_creature(species) if species is not None and item != 'Trait Egg' else None
        selected_trait=parse_traits(trait)[0] if trait is not None and item != 'Species Egg' else None
        if selected_trait and len(parse_traits(trait)) != 1:
            raise ValueError('Choose exactly one trait.')
        async with mutex:
            def hatch():
                if item_count(gid,uid,item)<1:
                    raise ValueError('You do not have a '+item+'.')
                name=selected_species or random.choice(CREATURES)
                t=selected_trait or 'Basic'
                raw_species=add_hybrid_species(name) if t == 'Hybrid' else name
                change_item(gid,uid,item,-1)
                cid=create_creature(gid,uid,raw_species,[t],1)
                return cid,raw_species,t
            cid,raw_species,t=transacted(hatch)
        await feedback(interaction,f'Hatched **#{cid} {safe_discord_text(species_display(raw_species))}**, Lv. 1, trait: **{t}**.')
        return
    if id is None:
        raise ValueError('Supply the creature ID.')
    async with mutex:
        def modify():
            c=own_creature(gid,uid,id)
            traits=json.loads(c['traits'])
            if item == 'Trait Eraser':
                if trait is None:
                    raise ValueError('Choose the trait to erase.')
                chosen=parse_traits(trait)
                if len(chosen)!=1 or chosen[0] not in traits:
                    raise ValueError('That creature does not have that trait.')
                if chosen[0]=='Hybrid' and len(species_list(c['species']))>1:
                    raise ValueError('Remove one hybrid species with a DNA Splicer before erasing Hybrid.')
                change_item(gid,uid,item,-1)
                traits.remove(chosen[0])
                conn().execute('UPDATE creatures SET traits=? WHERE id=?',(json.dumps(traits),id))
                return 'Removed trait '+chosen[0]
            species_entries=species_list(c['species'])
            if len(species_entries)!=2:
                raise ValueError('This creature does not have two hybrid species.')
            if remove_species is None:
                raise ValueError('Set remove_species to 1 or 2 to select the species to remove.')
            change_item(gid,uid,item,-1)
            removed=species_entries.pop(remove_species-1)
            if 'Hybrid' in traits:
                traits.remove('Hybrid')
            conn().execute('UPDATE creatures SET species=?,traits=? WHERE id=?',
                           (species_entries[0],json.dumps(traits),id))
            return f'Removed hybrid species {removed}; remaining species: {species_entries[0]}'
        result=transacted(modify)
    await feedback(interaction,f'Used **{item}** on #{id}. {result}.')
bot.tree.add_command(item_group)


@bot.tree.command(name='giveitem',description='Admin: grant items to a user.')
@app_commands.default_permissions(manage_guild=True)
@app_commands.autocomplete(item=item_autocomplete)
async def giveitem(interaction:discord.Interaction,user:discord.Member,item:str,quantity:app_commands.Range[int,1,1000000]=1):
    admin_only(interaction)
    gid,_=guild_only(interaction)
    name=ITEM_LOOKUP.get(item.casefold().strip())
    if not name:
        raise ValueError('Unknown item.')
    async with mutex:
        transacted(lambda:change_item(gid,user.id,name,quantity))
    await feedback(interaction,f'Granted {quantity}× {name} to {user.mention}.',False)


@bot.tree.command(name='takeitem',description='Admin: take items from a user.')
@app_commands.default_permissions(manage_guild=True)
@app_commands.autocomplete(item=item_autocomplete)
async def takeitem(interaction:discord.Interaction,user:discord.Member,item:str,quantity:app_commands.Range[int,1,1000000]=1):
    admin_only(interaction)
    gid,_=guild_only(interaction)
    name=ITEM_LOOKUP.get(item.casefold().strip())
    if not name:
        raise ValueError('Unknown item.')
    async with mutex:
        transacted(lambda:change_item(gid,user.id,name,-quantity))
    await feedback(interaction,f'Removed {quantity}× {name} from {user.mention}.',False)


@bot.tree.command(name='givecurrency',description='Admin: grant Gold Coins or Diamond Shards.')
@app_commands.default_permissions(manage_guild=True)
@app_commands.choices(currency=[app_commands.Choice(name='Gold Coins',value='gold'),app_commands.Choice(name='Diamond Shards',value='shards')])
async def givecurrency(interaction:discord.Interaction,user:discord.Member,currency:app_commands.Choice[str],amount:app_commands.Range[int,1,1000000000]):
    admin_only(interaction)
    gid,_=guild_only(interaction)
    async with mutex:
        transacted(lambda:change_money(gid,user.id,currency.value,amount))
    await feedback(interaction,f'Granted {amount:,} {currency.name} to {user.mention}.',False)


@bot.tree.command(name='takecurrency',description='Admin: take Gold Coins or Diamond Shards.')
@app_commands.default_permissions(manage_guild=True)
@app_commands.choices(currency=[app_commands.Choice(name='Gold Coins',value='gold'),app_commands.Choice(name='Diamond Shards',value='shards')])
async def takecurrency(interaction:discord.Interaction,user:discord.Member,currency:app_commands.Choice[str],amount:app_commands.Range[int,1,1000000000]):
    admin_only(interaction)
    gid,_=guild_only(interaction)
    async with mutex:
        transacted(lambda:change_money(gid,user.id,currency.value,-amount))
    await feedback(interaction,f'Took {amount:,} {currency.name} from {user.mention}.',False)


class TradeView(discord.ui.View):
    def __init__(self,guild:int,sender:int,receiver:int,give_c:int|None,want_c:int|None,
                 give_item:str|None,want_item:str|None,give_qty:int,want_qty:int,
                 give_gold:int,want_gold:int,give_shards:int,want_shards:int):
        super().__init__(timeout=300)
        self.details=(guild,sender,receiver,give_c,want_c,give_item,want_item,give_qty,want_qty,give_gold,want_gold,give_shards,want_shards)

    @discord.ui.button(label='Accept Trade',style=discord.ButtonStyle.success)
    async def accept(self,interaction:discord.Interaction,button:discord.ui.Button):
        gid,sender,receiver,gc,wc,gi,wi,gq,wq,gg,wg,gs,ws=self.details
        if interaction.user.id != receiver or interaction.guild_id != gid:
            await feedback(interaction,'Only the receiving user can accept this trade.')
            return
        async with mutex:
            def execute():
                if gc: own_creature(gid,sender,gc)
                if wc: own_creature(gid,receiver,wc)
                # Validate all balances before any mutation; transaction protects atomicity.
                for user,item,qty in [(sender,gi,gq),(receiver,wi,wq)]:
                    if item and item_count(gid,user,item)<qty:
                        raise ValueError('An offered item is no longer available.')
                for user,col,value in [(sender,'gold',gg),(receiver,'gold',wg),(sender,'shards',gs),(receiver,'shards',ws)]:
                    if userrow(gid,user)[col]<value:
                        raise ValueError('An offered currency amount is no longer available.')
                if gc: transfer_creature(gc,gid,sender,receiver)
                if wc: transfer_creature(wc,gid,receiver,sender)
                if gi: change_item(gid,sender,gi,-gq); change_item(gid,receiver,gi,gq)
                if wi: change_item(gid,receiver,wi,-wq); change_item(gid,sender,wi,wq)
                for col,a,b in [('gold',gg,wg),('shards',gs,ws)]:
                    change_money(gid,sender,col,-a)
                    change_money(gid,receiver,col,a)
                    change_money(gid,receiver,col,-b)
                    change_money(gid,sender,col,b)
            try:
                transacted(execute)
            except ValueError as e:
                await feedback(interaction,str(e))
                return
        self.stop()
        await interaction.response.edit_message(content=f'Trade completed between <@{sender}> and <@{receiver}>.',view=None)

    @discord.ui.button(label='Decline',style=discord.ButtonStyle.secondary)
    async def decline(self,interaction:discord.Interaction,button:discord.ui.Button):
        _,sender,receiver,*_=self.details
        if interaction.user.id not in (sender,receiver):
            await feedback(interaction,'Only the trade participants can decline.')
            return
        self.stop()
        await interaction.response.edit_message(content='Trade declined.',view=None)


@bot.tree.command(name='trade',description='Offer creature, item and/or currency in exchange for another user’s goods.')
@app_commands.autocomplete(give_item=item_autocomplete,want_item=item_autocomplete)
async def trade(interaction:discord.Interaction,user:discord.Member,
                give_creature_id:int|None=None,want_creature_id:int|None=None,
                give_item:str|None=None,give_quantity:app_commands.Range[int,1,1000000]=1,
                want_item:str|None=None,want_quantity:app_commands.Range[int,1,1000000]=1,
                give_gold:app_commands.Range[int,0,1000000000]=0,want_gold:app_commands.Range[int,0,1000000000]=0,
                give_shards:app_commands.Range[int,0,1000000000]=0,want_shards:app_commands.Range[int,0,1000000000]=0):
    gid,uid=guild_only(interaction)
    if uid==user.id or user.bot:
        raise ValueError('Trade with another human server member.')
    gi=ITEM_LOOKUP.get(give_item.strip().casefold()) if give_item else None
    wi=ITEM_LOOKUP.get(want_item.strip().casefold()) if want_item else None
    if (give_item and gi is None) or (want_item and wi is None):
        raise ValueError('An item name is invalid.')
    if give_creature_id is not None and give_creature_id==want_creature_id:
        raise ValueError('Use two different creature IDs.')
    if not any([give_creature_id,want_creature_id,gi,wi,give_gold,want_gold,give_shards,want_shards]):
        raise ValueError('Offer at least one creature, item, or currency amount.')
    async with mutex:
        if give_creature_id is not None: own_creature(gid,uid,give_creature_id)
        if want_creature_id is not None: own_creature(gid,user.id,want_creature_id)
    view=TradeView(gid,uid,user.id,give_creature_id,want_creature_id,gi,wi,give_quantity,want_quantity,give_gold,want_gold,give_shards,want_shards)
    def describe(cid,item,qty,gold,shards):
        bits=[]
        if cid is not None: bits.append(f'Creature #{cid}')
        if item: bits.append(f'{qty}× {item}')
        if gold: bits.append(f'{gold:,} Gold Coins')
        if shards: bits.append(f'{shards:,} Diamond Shards')
        return ', '.join(bits) or 'Nothing'
    await interaction.response.send_message(
        f'**Trade offer** · {interaction.user.mention} ⇄ {user.mention}\n'
        f'{interaction.user.mention} offers: {describe(give_creature_id,gi,give_quantity,give_gold,give_shards)}\n'
        f'{user.mention} offers: {describe(want_creature_id,wi,want_quantity,want_gold,want_shards)}\n'
        f'{user.mention}: accept or decline within 5 minutes.',view=view)


@bot.tree.command(name='help_sunny',description='List Sunny the Junipyro commands.')
async def help_sunny(interaction:discord.Interaction):
    await feedback(interaction,
        '**Collect:** /active, /inventory creatures, /inventory items, /balance, /release, /item use, /candy, /dice, /trait add, /nickname, /design submit, /art submit\n'
        '**Shop:** /shop view, /shop buy\n'
        '**Trade:** /trade\n'
        '**Admin:** /spawnchannel, /spawnnow, /cspawn, /clevel, /cdelete, /giveitem, /takeitem, /givecurrency, /takecurrency, /design approve, /design deny, /art approve, /art deny\n'
        'Catches grant 1–50 Gold Coins and 10–100 EXP to your active creature. Wild catches enter your inventory at Lv. 5.',False)


async def main():
    if not TOKEN:
        raise SystemExit('Set DISCORD_TOKEN in .env or your host environment.')
    initialize()
    # sync once at startup, not once for every on_ready / gateway reconnect.
    async def setup_hook():
        if TEST_GUILD_ID:
            guild=discord.Object(id=int(TEST_GUILD_ID))
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
        else:
            await bot.tree.sync()
        spawn_loop.start()
    bot.setup_hook=setup_hook
    await bot.start(TOKEN)


if __name__=='__main__':
    asyncio.run(main())
