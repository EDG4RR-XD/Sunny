import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import bot as b

class BotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        b.DB_PATH = ':memory:'
        b.initialize()
        self.i = SimpleNamespace(guild_id=1,user=SimpleNamespace(id=2,mention='<@2>'))
        self.messages = patch.object(b,'feedback',new_callable=AsyncMock)
        self.feedback = self.messages.start()
    def tearDown(self):
        self.messages.stop()
        b.db.close()
    def test_complete_metadata(self):
        self.assertEqual(len(b.METADATA),1056)
        for name in b.CREATURES:
            self.assertTrue(b.METADATA[name.casefold()]['types'])
            self.assertTrue(b.METADATA[name.casefold()]['series'])
        for series in ('coromon','beastieball'):
            self.assertIn('Flowish',b.egg_pool('Series Egg',series))
        for series in ('tanorio','cassette'):
            self.assertIn('Dandylion',b.egg_pool('Series Egg',series))
    async def test_buy_both_eggs_both_currencies(self):
        for item in ('Series Egg','Type Egg'):
            for col,price in [('gold',500),('shards',5)]:
                b.change_money(1,2,col,price)
                before=b.item_count(1,2,item)
                await b.shop_buy.callback(self.i,item,b.app_commands.Choice(name=col,value=col))
                self.assertEqual(b.userrow(1,2)[col],0)
                self.assertEqual(b.item_count(1,2,item),before+1)
                with self.assertRaises(ValueError):
                    await b.shop_buy.callback(self.i,item,b.app_commands.Choice(name=col,value=col))
                self.assertEqual(b.item_count(1,2,item),before+1)
    async def test_hatch_every_category(self):
        for item,key,choices in [('Series Egg','series',b.SERIES_NAMES),('Type Egg','type',b.TYPE_NAMES)]:
            for selection in choices:
                for count in range(1,5):
                    b.change_item(1,2,item,1)
                    with patch.object(b.random,'randint',return_value=count):
                        await b.item_use.callback(self.i,item,**{key:selection.upper()})
                    c=b.conn().execute('SELECT * FROM creatures ORDER BY id DESC LIMIT 1').fetchone()
                    self.assertIn(c['species'],b.egg_pool(item,selection))
                    traits=json.loads(c['traits'])
                    self.assertEqual(len(set(traits)),count)
                    self.assertTrue(set(traits)<=set(b.NORMAL_TRAITS))
                    self.assertEqual(c['level'],1)
                    self.assertEqual(b.item_count(1,2,item),0)
    async def test_invalid_egg_does_not_consume(self):
        b.change_item(1,2,'Type Egg',1)
        for selection in (None,'unknown'):
            with self.assertRaises(ValueError):
                await b.item_use.callback(self.i,'Type Egg',type=selection)
            self.assertEqual(b.item_count(1,2,'Type Egg'),1)
        with self.assertRaises(ValueError):
            await b.item_use.callback(self.i,'Series Egg',series='pokemon')
        self.assertEqual(b.conn().execute('SELECT count(*) FROM creatures').fetchone()[0],0)
    async def test_hatch_rollback(self):
        b.change_item(1,2,'Type Egg',1)
        with patch.object(b,'create_creature',side_effect=RuntimeError('test')):
            with self.assertRaises(RuntimeError):
                await b.item_use.callback(self.i,'Type Egg',type='fire')
        self.assertEqual(b.item_count(1,2,'Type Egg'),1)
    async def test_nickname_and_release(self):
        cid=b.create_creature(1,2,'Junipyro',['Basic'],1)
        b.conn().execute('UPDATE creatures SET nickname=? WHERE id=?',('Sunny',cid))
        c=b.own_creature(1,2,cid)
        self.assertEqual(b.creature_label(c),'Sunny')
        self.assertIn('**Sunny**',b.fmt_c(c))
        self.assertIn('Command ID:',b.fmt_c(c))
        await b.active.callback(self.i,cid)
        self.assertIn('Sunny is now active',self.feedback.call_args.args[1])
        await b.release.callback(self.i,cid)
        self.assertIn('Released **Sunny**',self.feedback.call_args.args[1])
        self.assertIsNone(b.conn().execute('SELECT * FROM creatures WHERE id=?',(cid,)).fetchone())
    async def test_spawn_metadata(self):
        channel=SimpleNamespace(send=AsyncMock())
        with patch.object(b.bot,'get_channel',return_value=channel),patch.object(b.random,'choice',return_value='Flowish'):
            await b.post_spawn(101,202)
        embed=channel.send.call_args.kwargs['embed']
        value=embed.fields[0].value
        self.assertIn('coromon',value)
        self.assertIn('beastieball',value)
        self.assertIn('**Type:**',value)
    async def test_old_eggs_and_custom_species(self):
        b.change_item(1,2,'Species Egg',1)
        await b.item_use.callback(self.i,'Species Egg',species='Bulbasaur')
        c=b.conn().execute('SELECT * FROM creatures').fetchone()
        self.assertEqual(json.loads(c['traits']),['Basic'])
        self.assertEqual(b.creature_label(c),'Bulbasaur')
        self.assertIn('Unknown',b.metadata_display('CustomEvolution'))
    def test_commands_serialize(self):
        for command in b.bot.tree.get_commands():
            command.to_dict(b.bot.tree)
        params={p.name:p for p in b.item_use.parameters}
        self.assertTrue(params['series'].autocomplete)
        self.assertTrue(params['type'].autocomplete)
        self.assertIn('Series Egg',[c.value for c in params['item'].choices])

if __name__=='__main__':
    unittest.main()
