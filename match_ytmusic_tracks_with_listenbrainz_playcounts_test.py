"""Unit tests for match_ytmusic_tracks_with_listenbrainz_playcounts.py."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import match_ytmusic_tracks_with_listenbrainz_playcounts as matcher  # noqa: E402


class MatchYtmusicPlaycountsTest(unittest.TestCase):
  """Tests playcount matching, CLI args, and incremental YTMusic feedback."""

  def test_parse_args_flags(self) -> None:
    args = matcher.parse_args(['--force', '--skip-feedback-sync'])
    self.assertTrue(args.force)
    self.assertTrue(args.skip_feedback_sync)

  def test_match_ytmusic_with_playcounts_uses_cache_and_batch(self) -> None:
    yt_tracks = pd.DataFrame(
        {'fuzzy_id': ['artist a - track 1', 'artist b - track 2']},
        index=['vid1', 'vid2'],
    )
    playcount_map = {'artist a - track 1': 12, 'artist b - track 2': 5}
    existing_id_map = {'vid1': 'artist a - track 1'}

    fake_uni = mock.MagicMock()
    fake_uni.query_fuzz_pool_batch.return_value = [
        ('artist b - track 2', 95.0)
    ]

    with mock.patch.object(matcher, 'uni', fake_uni):
      matched, unmatched, updated = matcher.match_ytmusic_with_playcounts(
          yt_tracks, playcount_map, existing_id_map=existing_id_map
      )

    self.assertEqual(matched, {'vid1': 12, 'vid2': 5})
    self.assertEqual(unmatched, {})
    self.assertEqual(updated['vid2'], 'artist b - track 2')
    fake_uni.query_fuzz_pool_batch.assert_called_once()

  def test_incremental_ytmusic_feedback_sync_submits_only_new(self) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
      fake_session = mock.MagicMock()
      lookup_resp = mock.MagicMock(status_code=200)
      lookup_resp.json.side_effect = [
          {'recording_mbid': 'mbid-1'},
          {'recording_mbid': 'mbid-2'},
      ]
      fake_session.get.return_value = lookup_resp
      post_resp = mock.MagicMock(status_code=200)
      fake_session.post.return_value = post_resp

      desired = {
          ('boards of canada', 'roygbiv'): (1, 'Boards of Canada', 'Roygbiv'),
          ('bad artist', 'bad song'): (-1, 'Bad Artist', 'Bad Song'),
      }

      with (
          mock.patch.object(matcher, 'SCRIPT_DIR', tmpdir),
          mock.patch.dict(os.environ, {'LISTENBRAINZ_TOKEN': 'tok'}),
          mock.patch.object(
              matcher, '_load_ytmusic_desired_feedback', return_value=desired
          ),
          mock.patch('requests.Session', return_value=fake_session),
      ):
        count1 = matcher._sync_incremental_feedback_if_configured()
        count2 = matcher._sync_incremental_feedback_if_configured()

      self.assertEqual(count1, 2)
      self.assertEqual(count2, 0)
      self.assertEqual(fake_session.post.call_count, 2)


if __name__ == '__main__':
  unittest.main()
