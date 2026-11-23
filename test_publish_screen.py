import copy
import json
import tempfile
import unittest
from pathlib import Path
from publish_screen import publish, projections, superseded, FILES

class PublishTest(unittest.TestCase):
    def setUp(self):
        self.data = dict(ok=True, price_date='2026-11-06', generated_at='2026-11-06T17:00:00+05:30',
                         built_on='2026-11-06', breadth={'as_of': '2026-11-06'}, coverage={'statements_pct': 90},
                         rows=[{'sym': f'S{i}', 'price': 100, 'swot': {'s': ['one','two','three','four']}} for i in range(450)])
    def test_projections_share_source(self):
        p = projections(self.data)
        self.assertEqual(set(p), set(FILES))
        self.assertEqual(p['screen.json']['price_date'], '2026-11-06')
        self.assertEqual(p['swot.json']['swot']['S0']['s'], ['one','two','three'])
        self.assertNotIn('swot', p['screen.json']['rows'][0])
        self.assertEqual(len(p['screen-lite.json']['rows']),450)
    def test_replay_and_older_build_do_not_replace(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(publish(self.data,d))
            before={f: (Path(d)/f).read_bytes() for f in FILES}
            self.assertFalse(publish(self.data,d))
            older=copy.deepcopy(self.data); older['price_date']='2026-11-05'; older['breadth']['as_of']='2026-11-05'
            with self.assertRaises(ValueError): publish(older,d)
            self.assertEqual(before,{f: (Path(d)/f).read_bytes() for f in FILES})
    def test_newspaper_fallback_of_the_same_build_never_replaces_the_clean_file(self):
        # 23 Nov 2026: the newspaper served last week's cache as a fallback and
        # wrote it over the published screen; same data, is_fallback=true, so
        # Rulebook's feed gate refused it until the next publish.
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(publish(self.data, d))
            fb = dict(self.data, is_fallback=True)
            self.assertTrue(superseded(fb, d), 'same build and same price date: keep the published file')
            newer = dict(self.data, generated_at='2026-11-07T17:00:00+05:30')
            self.assertFalse(superseded(newer, d), 'a genuinely newer build must still be written')
            later_day = dict(self.data, price_date='2026-11-09')
            self.assertFalse(superseded(later_day, d))
            older_day = dict(self.data, price_date='2026-11-05')
            self.assertTrue(superseded(older_day, d), 'prices never move backwards')
            self.assertFalse(superseded(fb, d + '/missing'), 'nothing published yet: write it')
            (Path(d) / 'screen.json').write_text('{not json')
            self.assertFalse(superseded(fb, d), 'an unreadable file is replaced, not trusted')

    def test_invalid_payload_never_changes_files(self):
        for key,value in [('rows',[]),('coverage',{'statements_pct':60}),('breadth',{'as_of':'2026-11-05'})]:
            with tempfile.TemporaryDirectory() as d:
                publish(self.data,d); before=(Path(d)/'screen.json').read_bytes()
                bad=copy.deepcopy(self.data);bad[key]=value
                with self.assertRaises(ValueError):publish(bad,d)
                self.assertEqual(before,(Path(d)/'screen.json').read_bytes())

if __name__ == '__main__':
    unittest.main()
