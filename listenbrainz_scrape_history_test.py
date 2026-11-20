"""Unit tests for ListenBrainz scraper and YTMusic playcount matcher.

Self-contained so tests pass both inside `music-library` and standalone in CI.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock
from unittest.mock import patch
import zipfile

import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
  sys.path.insert(0, SCRIPT_DIR)

# isort: split
import listenbrainz_scrape_history as lb
import match_ytmusic_tracks_with_listenbrainz_playcounts as lb_match


def _mock_response(
    payload: dict,
    ok: bool = True,
    status_code: int = 200,
    headers: dict | None = None,
) -> MagicMock:
  """Constructs a mock `requests.Response`."""
  resp = MagicMock(ok=ok, status_code=status_code)
  resp.json.return_value = payload
  resp.headers = headers or {
      'X-RateLimit-Remaining': '25',
      'X-RateLimit-Reset-In': '10',
  }
  resp.text = json.dumps(payload)
  return resp


def _make_lb_listen(
    ts: int,
    artist: str = 'Pink Floyd',
    album: str = 'The Dark Side of the Moon',
    title: str = 'Brain Damage',
) -> dict:
  """Creates a synthetic ListenBrainz listen record."""
  tm: dict = {'artist_name': artist, 'track_name': title}
  if album:
    tm['release_name'] = album
  return {'listened_at': ts, 'track_metadata': tm}


class TestListenBrainzScrapeHistory(unittest.TestCase):
  """Tests zip loading, API pagination, backfill merge, and TSV contracts."""

  def test_load_listens_from_zip_streams_jsonl_and_ignores_undated(
      self,
  ) -> None:
    """Verifies JSONL records parse accurately and undated bug rows drop."""
    with tempfile.TemporaryDirectory() as tmpdir:
      zip_path = os.path.join(tmpdir, 'listenbrainz_test.zip')
      rec1 = _make_lb_listen(1700000000, 'Boards of Canada', 'Geogaddi', '1969')
      rec2 = _make_lb_listen(1700000300, 'Aphex Twin', '', 'Windowlicker')
      rec_undated = _make_lb_listen(42, 'Pink Floyd', 'Meddle', 'Echoes')
      with zipfile.ZipFile(zip_path, 'w') as zf:
        zf.writestr('user.json', '{"name": "totoro_jake"}')
        zf.writestr(
            'listens/2023/11.jsonl',
            '\n'.join(json.dumps(r) for r in (rec1, rec2, rec_undated)) + '\n',
        )

      df = lb.load_listens_from_zip(zip_path)
      self.assertEqual(len(df), 2)
      self.assertEqual(df.iloc[0]['artist'], 'Boards of Canada')
      self.assertEqual(df.iloc[0]['album'], 'Geogaddi')
      self.assertEqual(df.iloc[1]['album'], '')
      self.assertEqual(int(df.iloc[1]['timestamps']), 1700000300)

  @patch('listenbrainz_scrape_history.time.sleep')
  @patch('listenbrainz_scrape_history.requests.Session')
  def test_incremental_pagination_and_429_retry(
      self, mock_session_cls: MagicMock, _mock_sleep: MagicMock
  ) -> None:
    """Verifies min_ts advances forward across pages and retries HTTP 429."""
    session = MagicMock()
    mock_session_cls.return_value.__enter__.return_value = session

    b1 = [
        _make_lb_listen(1700000200, title='Track 2'),
        _make_lb_listen(1700000100, title='Track 1'),
    ]
    b2 = [_make_lb_listen(1700000300, title='Track 3')]

    calls = []

    def side_effect(url: str, params: dict, timeout: int = 25) -> MagicMock:
      calls.append(dict(params))
      if len(calls) == 1:
        return _mock_response(
            {},
            ok=False,
            status_code=429,
            headers={'X-RateLimit-Reset-In': '1'},
        )
      if len(calls) == 2:
        return _mock_response({'payload': {'listens': b1}})
      return _mock_response({'payload': {'listens': b2}})

    session.get.side_effect = side_effect
    df = lb.fetch_listens_incremental('totoro_jake', min_ts=1700000000, count=2)
    self.assertEqual(len(df), 3)
    self.assertEqual(calls[0]['min_ts'], 1700000000)
    self.assertEqual(calls[2]['min_ts'], 1700000199)

  def test_merge_backfill_is_idempotent_and_drops_undated(self) -> None:
    """Verifies backfilled timestamps are not duplicated once present in LB."""
    with tempfile.TemporaryDirectory() as tmpdir:
      gap_path = os.path.join(tmpdir, 'gaps.tsv')
      pd.DataFrame([
          {
              'artist': 'The Beatles',
              'album': 'Revolver',
              'title': 'Taxman',
              'timestamps': 1690000000,
          },
          {
              'artist': 'The Beatles',
              'album': 'Revolver',
              'title': 'Eleanor Rigby',
              'timestamps': 1690000200,
          },
          {
              'artist': 'Legacy Bug',
              'album': 'Undated',
              'title': 'Ignore Me',
              'timestamps': 15,
          },
      ]).to_csv(gap_path, sep='\t', index=False)

      base_df = pd.DataFrame([{
          'artist': 'The Beatles',
          'album': 'revolver',
          'title': 'Taxman',
          'timestamps': 1690000000,
      }])

      merged = lb.merge_missing_dated_backfill(
          base_df, missing_dated_tsv=gap_path
      )
      self.assertEqual(len(merged), 2)
      self.assertEqual(int(merged['datetime'].isna().sum()), 0)

  def test_case_folding_and_downstream_tsv_contracts(self) -> None:
    """Verifies written TSVs satisfy downstream unnamed `index_col=0` schema."""
    with tempfile.TemporaryDirectory() as tmpdir:
      scrobbles_tsv = os.path.join(tmpdir, 'scrobbles.tsv')
      playcounts_tsv = os.path.join(tmpdir, 'playcounts.tsv')
      albums_tsv = os.path.join(tmpdir, 'top_albums.tsv')

      raw_df = lb.finalize_scrobbles_df(
          pd.DataFrame([
              {
                  'artist': 'Jimi Hendrix',
                  'album': 'Axis: Bold as Love',
                  'title': 'Little Wing',
                  'timestamps': 1700000000,
              },
              {
                  'artist': 'Jimi Hendrix',
                  'album': 'Axis: Bold as Love',
                  'title': 'Little Wing',
                  'timestamps': 1700000300,
              },
              {
                  'artist': 'Jimi Hendrix',
                  'album': 'Axis: Bold As Love',
                  'title': 'Little Wing',
                  'timestamps': 1700000600,
              },
          ])
      )

      playcounts_df, top_albums_df = lb.write_all_outputs(
          raw_df,
          scrobbles_tsv=scrobbles_tsv,
          playcounts_tsv=playcounts_tsv,
          top_albums_tsv=albums_tsv,
      )
      self.assertEqual(len(playcounts_df), 1)
      self.assertEqual(int(playcounts_df.iloc[0]['playcount']), 3)
      self.assertEqual(playcounts_df.iloc[0]['album'], 'Axis: Bold as Love')

      ingested = lb._ingest_playcounts(playcounts_tsv)
      self.assertIn('fuzzy_id', ingested.columns)
      self.assertEqual(int(ingested['playcount'].sum()), 3)
      self.assertEqual(int(top_albums_df.iloc[0]['total_playcount']), 3)

      big_df = lb.finalize_scrobbles_df(
          pd.DataFrame([
              {
                  'artist': 'A',
                  'album': 'B',
                  'title': f'T{i}',
                  'timestamps': 1700000000 + i,
              }
              for i in range(200)
          ])
      )
      lb._atomic_write_tsv(big_df, scrobbles_tsv, index=False, force=True)
      with self.assertRaisesRegex(RuntimeError, 'Shrink guard triggered'):
        lb._atomic_write_tsv(raw_df, scrobbles_tsv, index=False, force=False)


class TestPlaycountMatcher(unittest.TestCase):
  """Tests O(1) cache lookup and batch fallback in playcount matcher."""

  def test_match_ytmusic_uses_cache_and_batch_fallback(self) -> None:
    """Verifies cached videoIds hit O(1) lookup and uncached hit batch pool."""
    fake_uni = MagicMock()
    fake_uni.query_fuzz_pool_batch.return_value = [('boards geogaddi 1969', 95)]
    yt_tracks = pd.DataFrame(
        {'fuzzy_id': ['aphex windowlicker', 'boards geogaddi 1969']},
        index=['vid_cached', 'vid_uncached'],
    )
    playcount_map = {'aphex windowlicker': 12, 'boards geogaddi 1969': 30}
    existing_map = {'vid_cached': 'aphex windowlicker'}

    with patch.object(lb_match, 'uni', fake_uni):
      matched, unmatched, updated = lb_match.match_ytmusic_with_playcounts(
          yt_tracks, playcount_map, existing_id_map=existing_map
      )

    self.assertEqual(matched, {'vid_cached': 12, 'vid_uncached': 30})
    self.assertEqual(unmatched, {})
    self.assertEqual(updated['vid_uncached'], 'boards geogaddi 1969')
    fake_uni.query_fuzz_pool_batch.assert_called_once()


if __name__ == '__main__':
  unittest.main()
