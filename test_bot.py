"""Storage-edition regression tests. Run with python -m unittest -v test_bot."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import bot as b

class StorageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        b.DB_PATH = ':memory:'
        b.initialize()
        self.interaction = SimpleNamespace(guild_id=1, user=SimpleNamespace(id=2, mention='<@2>'))
        self.feedback_patch = patch.object(b, 'feedback', new_callable=AsyncMock)
        self.feedback = self.feedback_patch.start()

    def tearDown(self):
        self.feedback_patch.stop()
        b.db.close()

    def test_no_spawn_or_egg_commands(self):
        names = {c.name for c in b.bot.tree.get_commands()}
        self.assertFalse({'spawnchannel', 'spawnnow', 'cspawn'} & names)
        self.assertIn('cadd', names)
        self.assertFalse(any('Egg' in name or 'ball' in name.lower() for name in b.ITEMS))
        self.assertEqual({x.name for x in b.item_group.commands}, {'use'})

    def test_release_pool_is_brushes_only(self):
        with patch.object(b.random, 'random', return_value=0.2):
            for _ in range(100):
                self.assertIn(b.roll_release_item(), b.SPECIFIC_BRUSHES)
        with patch.object(b.random, 'random', return_value=0.9):
            self.assertIsNone(b.roll_release_item())

    async def test_release_awards_currency_and_brush(self):
        cid = b.create_creature(1, 2, 'Bulbasaur', ['Basic'], 1)
        with patch.object(b, 'roll_release_item', return_value='Shiny Trait Brush'), patch.object(b.random, 'randint', return_value=80):
            await b.release.callback(self.interaction, cid)
        self.assertEqual(b.userrow(1, 2)['gold'], 80)
        self.assertEqual(b.item_count(1, 2, 'Shiny Trait Brush'), 1)
        self.assertIsNone(b.conn().execute('SELECT * FROM creatures WHERE id=?', (cid,)).fetchone())

    async def test_eraser_survives(self):
        cid = b.create_creature(1, 2, 'Bulbasaur', ['Shiny'], 1)
        b.change_item(1, 2, 'Trait Eraser', 1)
        await b.item_use.callback(self.interaction, 'Trait Eraser', cid, trait='Shiny')
        self.assertEqual(b.item_count(1, 2, 'Trait Eraser'), 0)
        self.assertEqual(b.own_creature(1, 2, cid)['traits'], '[]')

if __name__ == '__main__':
    unittest.main()
