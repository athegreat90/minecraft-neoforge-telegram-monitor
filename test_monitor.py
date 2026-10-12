import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('monitor', Path(__file__).with_name('monitor.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class MonitorTests(unittest.TestCase):
    def test_neoforge_metadata_major_and_betas(self):
        fixture = '''<metadata><versioning><versions>
        <version>21.1.1</version><version>26.1.2.70-beta</version>
        <version>26.2.0.57</version><version>27.1.0.1</version>
        </versions></versioning></metadata>'''
        with patch.object(m, 'fetch_text', return_value=fixture):
            with patch.dict(os.environ, {'NEOFORGE_BETAS': 'false'}):
                self.assertEqual([v['id'] for v in m.get_neoforge_versions()],
                                 ['26.2.0.57', '27.1.0.1'])
            with patch.dict(os.environ, {'NEOFORGE_BETAS': 'true'}):
                self.assertEqual([v['id'] for v in m.get_neoforge_versions()],
                                 ['26.1.2.70-beta', '26.2.0.57', '27.1.0.1'])

    def test_seed_prevents_old_notifications_and_new_version_posts_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(m, 'DB_PATH', Path(tmp, 'releases.sqlite3')):
                with m.connect_db() as db:
                    sent = []
                    def sender(token, channel, body):
                        sent.append(body)
                    old = [{'id': '26.2.0.57'}]
                    new = old + [{'id': '26.2.0.58'}]
                    with patch.object(m, 'send_telegram', side_effect=sender):
                        def notify(entries):
                            m.process_source(db, source='neoforge', channel='@example', entries=entries,
                                             formatter=lambda v: v['id'], token='test')
                        notify(old)
                        self.assertEqual(sent, [])
                        notify(new)
                        notify(new)
                        self.assertEqual(sent, ['26.2.0.58'])

    def test_minecraft_article_extracts_highlights(self):
        page = ('<html><h1>Minecraft Java Edition 26.3</h1>'
                '<h2>New Features</h2><ul><li>Added poplar trees</li>'
                '<li>Added abandoned camps</li></ul></html>')
        with patch.object(m, 'fetch_text', return_value=page):
            notes, url = m.minecraft_release_notes('26.3')
        self.assertEqual(notes, ['Added poplar trees', 'Added abandoned camps'])
        self.assertEqual(url, 'https://www.minecraft.net/en-us/article/minecraft-java-edition-26-3')

    def test_released_timestamp_is_converted_to_madrid(self):
        self.assertIn('CEST', m.date_string('2026-09-15T10:00:00+00:00'))


if __name__ == '__main__':
    unittest.main()
