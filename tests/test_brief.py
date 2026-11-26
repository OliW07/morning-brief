import datetime as dt
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, Mock
from zoneinfo import ZoneInfo

from rich.console import Console
from brief import display, morning, oura, storage


class TemporaryState(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = [patch.object(m, 'STATE', self.root) for m in (storage, oura, morning, display)]
        self.patches += [patch.object(m, 'CONFIG', self.root/'config') for m in (storage, oura)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()


class MorningTests(TemporaryState):
    def test_local_3am_boundary_and_dst(self):
        tz = ZoneInfo('Pacific/Auckland')
        for date in ('2026-09-27', '2026-04-05', '2026-11-11'):
            day = dt.date.fromisoformat(date)
            before = dt.datetime.combine(day, dt.time(2, 59), tz)
            after = dt.datetime.combine(day, dt.time(3, 0), tz)
            self.assertEqual(storage.morning_day(before), (day-dt.timedelta(days=1)).isoformat())
            self.assertEqual(storage.morning_day(after), date)
            self.assertFalse(morning.eligible(before))
            self.assertTrue(morning.eligible(after))

    def test_interrupted_attempt_never_launches_again_same_day(self):
        first, claimed = storage.claim_day('2026-11-11')
        self.assertTrue(claimed)
        self.assertTrue(first['sync_attempted'])
        again, claimed = storage.claim_day('2026-11-11')
        self.assertFalse(claimed)
        self.assertFalse(again['shown'])
        morning.acknowledge('2026-11-11')
        self.assertFalse(morning.eligible(dt.datetime(2026, 11, 11, 18)))
        _, claimed = storage.claim_day('2026-11-12')
        self.assertTrue(claimed)

    def test_concurrent_trigger_is_excluded(self):
        with storage.locked('run.lock'):
            with self.assertRaises(BlockingIOError):
                with storage.locked('run.lock', blocking=False):
                    self.fail('Concurrent process acquired the run lock')

    def test_timeout_releases_owned_emulator(self):
        emulator = Mock()
        clock = [0.0]
        def sleep(seconds):
            clock[0] += seconds
        with patch.object(morning, 'Emulator', return_value=emulator), \
             patch.object(oura, 'connected', return_value=True), \
             patch.object(oura, 'fetch', return_value={}), \
             patch.object(morning, 'config', return_value={'sync_timeout': 60, 'poll_interval': 15}), \
             patch.object(morning.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(morning.time, 'sleep', side_effect=sleep):
            result = morning.sync('2026-11-11')
        self.assertIn('timed out', result)
        emulator.close.assert_called_once()
        self.assertEqual(clock[0], 60)

    def test_membership_denial_still_releases_emulator(self):
        emulator = Mock()
        with patch.object(morning, 'Emulator', return_value=emulator), \
             patch.object(oura, 'connected', return_value=True), \
             patch.object(oura, 'fetch', side_effect=oura.OuraError('Oura denied access: membership')):
            result = morning.sync('2026-11-11')
        self.assertIn('not verified', result)
        emulator.close.assert_called_once()


class DataTests(TemporaryState):
    def test_yesterday_is_not_todays_sync_and_nap_does_not_replace_main_sleep(self):
        data = display.sample_data(dt.date(2026, 11, 10))
        self.assertFalse(oura.today_ready(data, '2026-11-11'))
        data['sleep'].append({'day': '2026-11-10', 'type': 'late_nap', 'total_sleep_duration': 1200})
        self.assertEqual(oura.main_sleep(data['sleep'], '2026-11-10')['type'], 'long_sleep')
        output = Console(width=112, record=True)
        with patch.object(oura, 'connected', return_value=True):
            output.print(display.dashboard(data, now=dt.datetime(2026, 11, 11, 8)))
        text = output.export_text()
        self.assertIn('Last available · 2026-11-10', text)
        self.assertIn('Waiting for today', text)

    def test_missing_values_are_not_zero(self):
        self.assertEqual(display.numeric(None), '—')
        self.assertEqual(display.duration(None), '—')
        self.assertEqual(display.numeric(0), '0')

    def test_successful_paginated_fetch_uses_correct_headers_and_saves(self):
        responses = [Mock(status_code=200, json=Mock(return_value={'data': [{'day':'2026-11-11','score':88}], 'next_token':'page2'})),
                     Mock(status_code=200, json=Mock(return_value={'data': [], 'next_token':None}))]
        responses += [Mock(status_code=200, json=Mock(return_value={'data': [], 'next_token':None})) for _ in range(3)]
        session = Mock(headers={})
        session.get.side_effect = responses
        with patch.object(oura, 'access_token', return_value='test-only'), \
             patch.object(oura.requests, 'Session') as factory:
            factory.return_value.__enter__.return_value = session
            data = oura.fetch(dt.date(2026,11,11))
        self.assertEqual(session.headers['User-Agent'], oura.UA)
        self.assertEqual(session.headers['Authorization'], 'Bearer test-only')
        self.assertEqual(len(data['daily_sleep']), 1)
        self.assertEqual(session.get.call_count, 5)
        self.assertTrue((self.root/'oura-cache.json').exists())
        self.assertEqual((self.root/'oura-cache.json').stat().st_mode & 0o777, 0o600)

    def test_403_preserves_previous_cache(self):
        storage.save(self.root/'oura-cache.json', {'old':True})
        session = Mock(headers={})
        session.get.return_value = Mock(status_code=403)
        with patch.object(oura, 'access_token', return_value='test-only'), \
             patch.object(oura.requests, 'Session') as factory:
            factory.return_value.__enter__.return_value = session
            with self.assertRaisesRegex(oura.OuraError, 'membership'):
                oura.fetch()
        self.assertEqual(storage.load(self.root/'oura-cache.json'), {'old':True})

    def test_rate_limit_prevents_more_network_requests(self):
        session = Mock(headers={})
        session.get.return_value = Mock(status_code=429, headers={'Retry-After':'120'})
        with patch.object(oura, 'access_token', return_value='test-only'), \
             patch.object(oura.requests, 'Session') as factory:
            factory.return_value.__enter__.return_value = session
            for _ in range(2):
                with self.assertRaisesRegex(oura.OuraError, 'rate limit'):
                    oura.fetch()
        self.assertEqual(session.get.call_count, 1)

    def test_refresh_token_rotation_is_serialized(self):
        storage.save(self.root/'config/oura-client.json', {'client_id':'fake', 'client_secret':'fake'})
        storage.save(self.root/'oura-token.json', {'access_token':'old', 'refresh_token':'old-refresh', 'expires_at':0})
        results = []
        def refresh(*args, **kwargs):
            time.sleep(0.05)
            return {'access_token':'new', 'refresh_token':'new-refresh', 'expires_at':time.time()+3600}
        with patch.object(oura, 'OAuth2Session') as factory:
            factory.return_value.refresh_token.side_effect = refresh
            threads = [threading.Thread(target=lambda: results.append(oura.access_token())) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(factory.return_value.refresh_token.call_count, 1)
        self.assertEqual(results, ['new','new'])
        self.assertEqual(storage.load(self.root/'oura-token.json')['refresh_token'], 'new-refresh')


if __name__ == '__main__':
    unittest.main()
