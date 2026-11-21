"""Unit tests for sync_listenbrainz_playlists_to_ytmusic.py."""

from __future__ import annotations

import os
import shutil
import tempfile
from typing import Any, Dict
import unittest
from unittest.mock import MagicMock
from unittest.mock import patch

import pandas as pd
import sync_listenbrainz_playlists_to_ytmusic as lb_sync


def _make_createdfor_payload() -> Dict[str, Any]:
  """Builds a representative ListenBrainz `/playlists/createdfor` response."""

  def _item(mbid: str, patch: str, date: str, title: str) -> Dict[str, Any]:
    return {
        'playlist': {
            'identifier': f'https://listenbrainz.org/playlist/{mbid}',
            'title': title,
            'date': date,
            'extension': {
                lb_sync.JSPF_PLAYLIST_EXT: {
                    'additional_metadata': {
                        'algorithm_metadata': {'source_patch': patch}
                    }
                }
            },
        }
    }

  return {
      'playlist_count': 7,
      'playlists': [
          _item('mbid-exp-old', 'weekly-exploration', '2026-08-24', 'Old Exp'),
          _item('mbid-exp-new', 'weekly-exploration', '2026-09-14', 'New Exp'),
          _item('mbid-jam-new', 'weekly-jams', '2026-09-14', 'New Jams'),
          _item(
              'mbid-disc-2025',
              'top-discoveries-of-2025',
              '2026-02-22',
              'Top Discoveries 2025',
          ),
          _item(
              'mbid-disc-2024',
              'top-discoveries-of-2024',
              '2026-02-22',
              'Top Discoveries 2024',
          ),
          _item(
              'mbid-miss-2024',
              'top-missed-recordings-of-2024',
              '2026-02-21',
              'Top Missed 2024',
          ),
          _item(
              'mbid-miss-2025',
              'top-missed-recordings-of-2025',
              '2026-02-22',
              'Top Missed 2025',
          ),
      ],
  }


class SyncListenBrainzPlaylistsTest(unittest.TestCase):
  """Tests naming, selection, 3-tier resolution, and YTMusic playlist sync."""

  def setUp(self) -> None:
    self.tmp_dir = tempfile.mkdtemp()

  def tearDown(self) -> None:
    shutil.rmtree(self.tmp_dir)

  def test_format_ytmusic_playlist_name(self) -> None:
    """Formats ListenBrainz patches with lowercase `zz lb ` prefix."""
    self.assertEqual(
        lb_sync.format_ytmusic_playlist_name('weekly-exploration'),
        'zz lb weekly exploration',
    )
    self.assertEqual(
        lb_sync.format_ytmusic_playlist_name('weekly-jams'),
        'zz lb weekly jams',
    )
    self.assertEqual(
        lb_sync.format_ytmusic_playlist_name('top-discoveries-of-2025'),
        'zz lb top discoveries 2025',
    )
    self.assertEqual(
        lb_sync.format_ytmusic_playlist_name('top-missed-recordings-all'),
        'zz lb missed tracks',
    )

  def test_extract_mbid_handles_string_and_list(self) -> None:
    """Extracts UUIDs from both string and list JSPF `identifier` fields."""
    rec_uri = 'https://musicbrainz.org/recording/a7747fb6-457f-48d7'
    self.assertEqual(
        lb_sync.extract_mbid([rec_uri], lb_sync.RECORDING_URI_PREFIX),
        'a7747fb6-457f-48d7',
    )
    self.assertEqual(
        lb_sync.extract_mbid(rec_uri, lb_sync.RECORDING_URI_PREFIX),
        'a7747fb6-457f-48d7',
    )

  def test_select_target_playlists_combines_missed_newest_to_oldest(
      self,
  ) -> None:
    """Selects weekly, all discovery years, and combined missed tracks."""
    session = MagicMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = _make_createdfor_payload()
    session.get.return_value = resp

    available = lb_sync.fetch_createdfor_playlists('demo_user', session=session)
    selected = lb_sync.select_target_playlists(available)
    by_patch = {r['source_patch']: r for r in selected}
    self.assertEqual(
        {k: v['playlist_mbid'] for k, v in by_patch.items()},
        {
            'weekly-exploration': 'mbid-exp-new',
            'weekly-jams': 'mbid-jam-new',
            'top-discoveries-of-2025': 'mbid-disc-2025',
            'top-discoveries-of-2024': 'mbid-disc-2024',
            'top-missed-recordings-all': 'mbid-miss-2025,mbid-miss-2024',
        },
    )
    self.assertEqual(
        by_patch['top-missed-recordings-all']['source_mbids'],
        ['mbid-miss-2025', 'mbid-miss-2024'],
    )

  def test_resolve_tracks_three_tiers_and_not_like_exclusion(self) -> None:
    """Resolves via cache, local DB, and live search while dropping dislikes."""
    tracks = [
        {
            'recording_mbid': 'rec-cached',
            'artist': 'Artist One',
            'title': 'Cached Song',
            'album': 'Album A',
            'duration_ms': 200000,
        },
        {
            'recording_mbid': 'rec-local',
            'artist': 'Joni Mitchell',
            'title': 'Blue (2021 Remaster)',
            'album': 'Blue',
            'duration_ms': 184000,
        },
        {
            'recording_mbid': 'rec-search',
            'artist': 'Boards of Canada',
            'title': 'Roygbiv',
            'album': 'Music Has the Right to Children',
            'duration_ms': 149000,
        },
        {
            'recording_mbid': 'rec-hated',
            'artist': 'Banned Band',
            'title': 'No Thanks',
            'album': 'Nope',
            'duration_ms': 180000,
        },
    ]
    vid_cache = {'mbid:rec-cached': 'vid_cached'}
    local_index = {
        ('joni mitchell', 'blue'): [
            {
                'video_id': 'vid_local_indiff',
                'like_status': 'INDIFFERENT',
                'in_library': False,
                'norm_album': 'hits',
                'duration_sec': 184.0,
            },
            {
                'video_id': 'vid_local_liked',
                'like_status': 'LIKE',
                'in_library': True,
                'norm_album': 'blue',
                'duration_sec': 184.0,
            },
        ]
    }
    excluded_vids = {'vid_banned'}
    excluded_songs = {('banned band', 'no thanks')}

    yt_api = MagicMock()
    yt_api.search.return_value = [
        {
            'videoId': 'vid_search_hit',
            'title': 'Roygbiv',
            'artists': [{'name': 'Boards of Canada'}],
            'album': {'name': 'Music Has the Right to Children'},
            'duration_seconds': 149,
        }
    ]

    vids, stats, dirty = lb_sync.resolve_tracks_to_video_ids(
        tracks=tracks,
        vid_cache=vid_cache,
        local_index=local_index,
        excluded_vids=excluded_vids,
        excluded_songs=excluded_songs,
        yt_api=yt_api,
    )
    self.assertEqual(vids, ['vid_cached', 'vid_local_liked', 'vid_search_hit'])
    self.assertTrue(dirty)
    self.assertEqual(stats['cache_hits'], 1)
    self.assertEqual(stats['local_db_hits'], 1)
    self.assertEqual(stats['live_search_hits'], 1)
    self.assertEqual(stats['excluded_not_like'], 1)
    self.assertEqual(vid_cache['mbid:rec-search'], 'vid_search_hit')

  def test_load_not_like_exclusions_reads_azz_not_like_tsv(self) -> None:
    """Loads exclusions from not-like ledgers and `azz not like 0.tsv`."""
    pl_dir = os.path.join(self.tmp_dir, 'playlists')
    os.makedirs(pl_dir, exist_ok=True)
    pd.DataFrame([
        {'videoId': 'v_nl_1', 'title': 'Bad Song', 'artist': 'Bad Artist'}
    ]).to_csv(
        os.path.join(pl_dir, 'azz not like 0.tsv'), sep='\t', index=False
    )
    with patch.object(lb_sync, 'uni', None):
      vids, songs = lb_sync.load_not_like_exclusions(pl_dir)
    self.assertIn('v_nl_1', vids)
    self.assertIn(('bad artist', 'bad song'), songs)

  def test_upsert_replaces_existing_rolling_playlist_in_place(self) -> None:
    """Replaces stale items in-place while preserving existing `playlistId`."""
    yt_lib = MagicMock()
    yt_lib.playlists = pd.DataFrame([
        {'title': 'zz lb weekly jams', 'playlistId': 'PL_EXISTING'}
    ])
    yt_lib.yt.get_playlist.return_value = {
        'id': 'PL_EXISTING',
        'tracks': [
            {'videoId': 'old_v1', 'setVideoId': 'set_1'},
            {'videoId': 'old_v2', 'setVideoId': 'set_2'},
        ],
    }
    yt_lib.playlist_get_info.return_value = {
        'id': 'PL_EXISTING',
        'title': 'zz lb weekly jams',
        'tracks': [{'videoId': 'new_v1'}, {'videoId': 'new_v2'}],
    }

    pl_id = lb_sync.upsert_ytmusic_playlist(
        yt_lib=yt_lib,
        pl_name='zz lb weekly jams',
        video_ids=['new_v1', 'new_v2'],
        description='Updated desc',
        existing_playlist_id='PL_EXISTING',
    )
    self.assertEqual(pl_id, 'PL_EXISTING')
    yt_lib.remove_playlist_items_chunked.assert_called_once()
    yt_lib.yt.add_playlist_items.assert_called_once_with(
        playlistId='PL_EXISTING',
        videoIds=['new_v1', 'new_v2'],
        duplicates=True,
    )
    yt_lib.yt.edit_playlist.assert_called_once_with(
        'PL_EXISTING', description='Updated desc'
    )
    yt_lib.save_playlist_tsv.assert_called_once()

  def test_sync_playlists_skips_when_state_is_unchanged(self) -> None:
    """Exits cleanly without touching YTMusic when `playlist_mbid` matches."""
    state_path = os.path.join(self.tmp_dir, 'synced.json')
    cache_path = os.path.join(self.tmp_dir, 'cache.tsv')
    lb_sync.save_sync_state(
        {'weekly-jams': {'playlist_mbid': 'mbid-jam-new'}}, state_path
    )
    session = MagicMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = _make_createdfor_payload()
    session.get.return_value = resp

    yt_lib = MagicMock()
    results = lb_sync.sync_playlists(
        username='demo_user',
        patches=['weekly-jams'],
        yt_lib=yt_lib,
        session=session,
        state_path=state_path,
        cache_path=cache_path,
    )
    self.assertEqual(results, [])
    yt_lib.playlist_from_yt_vids.assert_not_called()


if __name__ == '__main__':
  unittest.main()
