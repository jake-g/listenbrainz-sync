"""Matches YouTube Music tracks against aggregated ListenBrainz playcounts.

Drop-in replacement for `lastfm/match_ytmusic_tracks_with_lastfm_playcounts.py`,
preserving exact TSV schemas, file paths, and 90-day run-interval gating.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import Any, Dict, Iterable, Optional, Tuple

import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.abspath(
    os.path.join(SCRIPT_DIR, '..', 'music-sources-unified')
)
if os.path.isdir(OUTPUT_DIR) and OUTPUT_DIR not in sys.path:
  sys.path.append(OUTPUT_DIR)

# isort: split
try:
  import unify_lib as uni  # type: ignore  # noqa: E402
except ImportError:
  uni = None  # type: ignore

logger = logging.getLogger(__name__)

RUN_INTERVAL_DAYS = 90
JOB_NAME = 'ytmusic_lastfm_match'
DATE = time.strftime('%m-%d-%Y')

PLAYCOUNTS_TSV = os.path.join(OUTPUT_DIR, 'tsvs', 'lastfm_playcounts.tsv')
YT_TRACK_DB_LATEST = os.path.join(
    OUTPUT_DIR, '..', 'ytmusic', 'playlists', '_tracks_db.tsv'
)
YT_TRACK_DB_OUT = os.path.join(OUTPUT_DIR, 'tsvs', 'ytmusic_all_database.tsv')
YT_MATCH_ID_MAP = os.path.join(
    OUTPUT_DIR, '..', 'ytmusic', 'playlists', '_ytmusic_lastfm_match_id_map.tsv'
)
YT_PLAYCOUNT_OUT = os.path.join(
    OUTPUT_DIR, '..', 'ytmusic', 'playlists', '_ytmusic_lastfm_playcount.tsv'
)


def match_ytmusic_with_playcounts(
    yt_tracks: pd.DataFrame,
    playcount_map: Dict[str, Any],
    existing_id_map: Optional[Dict[str, str]] = None,
    score_thresh: int = 60,
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, str]]:
  """Matches YT tracks against playcounts via O(1) cache + batch RapidFuzz.

  Args:
    yt_tracks: DataFrame of YouTube Music tracks indexed by `videoId` with a
      `fuzzy_id` column.
    playcount_map: Mapping of scrobble `fuzzy_id` -> integer `playcount`.
    existing_id_map: Optional pre-existing `videoId` -> `fuzzy_id` cache.
    score_thresh: Minimum fuzzy match score threshold (default 60).

  Returns:
    Tuple of `(matched_playcounts, unmatched_tracks, updated_id_map)`.
  """
  if uni is None:
    raise RuntimeError(
        'unify_lib is required for batch fuzzy matching; verify '
        f'{OUTPUT_DIR} exists.'
    )

  matched_playcounts: Dict[str, Any] = {}
  unmatched_tracks: Dict[str, Any] = {}
  updated_id_map: Dict[str, str] = (
      dict(existing_id_map) if existing_id_map else {}
  )
  pool = list(playcount_map.keys())

  unmatched_vids = []
  unmatched_queries = []

  for vid, fuzzy_id in zip(yt_tracks.index, yt_tracks['fuzzy_id']):
    cached_key = updated_id_map.get(vid)
    if cached_key is not None and cached_key in playcount_map:
      matched_playcounts[vid] = playcount_map[cached_key]
    else:
      unmatched_vids.append(vid)
      unmatched_queries.append(str(fuzzy_id) if pd.notna(fuzzy_id) else '')

  if unmatched_queries:
    logger.info(
        'Matching %s uncached YT tracks against %s scrobble tracks...',
        f'{len(unmatched_queries):,}',
        f'{len(pool):,}',
    )
    batch_results = uni.query_fuzz_pool_batch(
        unmatched_queries, pool, score_thresh=score_thresh
    )
    for vid, (match, _) in zip(unmatched_vids, batch_results):
      if match is not None and match in playcount_map:
        matched_playcounts[vid] = playcount_map[match]
        updated_id_map[vid] = match
      else:
        unmatched_tracks[vid] = None

  return matched_playcounts, unmatched_tracks, updated_id_map


def save_tsvs(
    yt_playcounts: Dict[str, Any],
    yt_unmatched: Dict[str, Any],
    yt_id_map: Dict[str, str],
    yt_tracks: pd.DataFrame,
    date_tag: str = DATE,
) -> None:
  """Writes playcount match maps, dated audit logs, and updated YTMusic DB."""
  logs_dir = os.path.join(OUTPUT_DIR, 'logs')
  os.makedirs(logs_dir, exist_ok=True)

  no_match_f = os.path.join(logs_dir, f'ytmusic_no_lastfm_match_{date_tag}.tsv')
  playcounts_log_f = os.path.join(
      logs_dir, f'ytmusic_lastfm_playcounts_{date_tag}.tsv'
  )
  id_map_log_f = os.path.join(
      logs_dir, f'ytmusic_lastfm_match_id_map_{date_tag}.tsv'
  )

  pd.DataFrame.from_dict(yt_unmatched, orient='index').to_csv(
      no_match_f, header=True, sep='\t', index=True, encoding='utf-8'
  )

  playcounts_df = pd.DataFrame.from_dict(yt_playcounts, orient='index')
  playcounts_df.columns = ['lastfm_playcount']
  playcounts_df.to_csv(
      playcounts_log_f, header=True, sep='\t', index=True, encoding='utf-8'
  )
  playcounts_df.to_csv(
      YT_PLAYCOUNT_OUT, header=True, sep='\t', index=True, encoding='utf-8'
  )

  id_map_df = pd.DataFrame.from_dict(yt_id_map, orient='index')
  id_map_df.columns = ['lastfm_fuzzy_id']
  id_map_df.to_csv(
      id_map_log_f, header=True, sep='\t', index=True, encoding='utf-8'
  )
  id_map_df.to_csv(
      YT_MATCH_ID_MAP, header=True, sep='\t', index=True, encoding='utf-8'
  )

  track_db = pd.concat([yt_tracks, playcounts_df], axis=1)
  track_db = track_db.sort_values(['artist', 'album'])
  if len(track_db) != len(yt_tracks):
    raise ValueError(
        f'Track DB length mismatch: {len(track_db)} != {len(yt_tracks)}'
    )
  if track_db['lastfm_playcount'].sum() <= 0:
    raise ValueError('Total matched playcount must be > 0')
  track_db.to_csv(YT_TRACK_DB_OUT, sep='\t', header=True)
  if uni is not None:
    uni.touch_last_run(JOB_NAME)


def _normalize_feedback_key(artist: Any, title: Any) -> Tuple[str, str]:
  """Normalizes (artist, title) for feedback matching."""
  import re

  a = re.sub(r'\s+', ' ', str(artist or '').strip().lower())
  t = re.sub(r'\s+', ' ', str(title or '').strip().lower())
  return a, t


def _load_ytmusic_desired_feedback(
    liked_path: str, not_liked_path: str
) -> Dict[Tuple[str, str], Tuple[int, str, str]]:
  """Loads desired ListenBrainz scores (1 / -1) from YTMusic TSVs."""
  desired: Dict[Tuple[str, str], Tuple[int, str, str]] = {}
  if os.path.isfile(not_liked_path):
    df = pd.read_csv(not_liked_path, sep='\t', low_memory=False)
    for row in df.itertuples(index=False):
      artist = str(getattr(row, 'artist', '') or '')
      title = str(getattr(row, 'title', '') or '')
      key = _normalize_feedback_key(artist, title)
      if key[0] and key[1]:
        desired[key] = (-1, artist, title)
  if os.path.isfile(liked_path):
    df = pd.read_csv(liked_path, sep='\t', low_memory=False)
    for row in df.itertuples(index=False):
      status = str(getattr(row, 'likeStatus', 'LIKE')).strip().upper()
      if status and status != 'LIKE':
        continue
      artist = str(getattr(row, 'artist', '') or '')
      title = str(getattr(row, 'title', '') or '')
      key = _normalize_feedback_key(artist, title)
      if key[0] and key[1]:
        desired[key] = (1, artist, title)
  return desired


def _sync_incremental_feedback_if_configured(
    max_lookups: int = 100,
) -> int:
  """Syncs newly added YTMusic likes & dislikes to ListenBrainz.

  Uses a local state cache (`listenbrainz/tsvs/_feedback_synced_cache.tsv`) so
  weekly runs only query/submit newly liked or disliked YTMusic tracks.

  Args:
    max_lookups: Maximum uncached metadata/lookup API calls per run.

  Returns:
    Number of newly submitted feedback items.
  """
  if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)
  try:
    import auth as lb_auth  # type: ignore
    import requests
  except ImportError as exc:
    logger.info('Skipping incremental feedback sync: %s', exc)
    return 0

  token = os.environ.get('LISTENBRAINZ_TOKEN') or getattr(
      lb_auth, 'LISTENBRAINZ_TOKEN', ''
  )
  if not token:
    logger.info('ListenBrainz token missing; skipping feedback sync.')
    return 0

  ytm_dir = os.path.join(OUTPUT_DIR, '..', 'ytmusic', 'playlists')
  liked_tsv = os.path.join(ytm_dir, '_liked_tracks.tsv')
  not_liked_tsv = os.path.join(ytm_dir, '_not_liked_tracks.tsv')
  desired = _load_ytmusic_desired_feedback(liked_tsv, not_liked_tsv)
  if not desired:
    return 0

  tsvs_dir = os.path.join(SCRIPT_DIR, 'tsvs')
  cache_path = os.path.join(tsvs_dir, '_feedback_synced_cache.tsv')
  mbid_cache_path = os.path.join(tsvs_dir, '_mbid_lookup_cache.tsv')

  synced_scores: Dict[Tuple[str, str], int] = {}
  if os.path.isfile(cache_path):
    cache_df = pd.read_csv(cache_path, sep='\t', keep_default_na=False)
    for row in cache_df.itertuples(index=False):
      key = (
          str(getattr(row, 'norm_artist', '')),
          str(getattr(row, 'norm_title', '')),
      )
      if key[0] and key[1]:
        try:
          synced_scores[key] = int(getattr(row, 'score', 0))
        except (TypeError, ValueError):
          continue

  pending = [
      (key, score, artist, title)
      for key, (score, artist, title) in desired.items()
      if synced_scores.get(key) != score
      and not (synced_scores.get(key) == 1 and score == -1)
  ]
  if not pending:
    logger.info('Incremental YTMusic feedback sync: 0 new changes.')
    return 0

  mbid_cache: Dict[Tuple[str, str], str] = {}
  if os.path.isfile(mbid_cache_path):
    mbid_df = pd.read_csv(mbid_cache_path, sep='\t', keep_default_na=False)
    for row in mbid_df.itertuples(index=False):
      k = (
          str(getattr(row, 'norm_artist', '')),
          str(getattr(row, 'norm_title', '')),
      )
      if k[0] and k[1]:
        mbid_cache[k] = str(getattr(row, 'recording_mbid', ''))

  session = requests.Session()
  headers = {'Authorization': f'Token {token}'}
  submitted = 0
  lookups_done = 0

  for key, score, artist, title in pending:
    mbid = mbid_cache.get(key) or ''
    if not mbid and key not in mbid_cache:
      if lookups_done >= max_lookups:
        continue
      resp = session.get(
          'https://api.listenbrainz.org/1/metadata/lookup/',
          headers=headers,
          params={'artist_name': artist, 'recording_name': title},
          timeout=30,
      )
      if resp.status_code == 200:
        mbid = str(resp.json().get('recording_mbid') or '')
      mbid_cache[key] = mbid
      lookups_done += 1
    if mbid:
      post_resp = session.post(
          'https://api.listenbrainz.org/1/feedback/recording-feedback',
          headers=headers,
          json={'recording_mbid': mbid, 'score': score},
          timeout=30,
      )
      post_resp.raise_for_status()
      submitted += 1
    synced_scores[key] = score

  os.makedirs(tsvs_dir, exist_ok=True)
  if lookups_done > 0:
    mbid_rows = [
        {'norm_artist': k[0], 'norm_title': k[1], 'recording_mbid': v}
        for k, v in sorted(mbid_cache.items())
    ]
    tmp_mbid = f'{mbid_cache_path}.tmp'
    pd.DataFrame(
        mbid_rows, columns=['norm_artist', 'norm_title', 'recording_mbid']
    ).to_csv(tmp_mbid, sep='\t', index=False)
    os.replace(tmp_mbid, mbid_cache_path)

  rows = [
      {'norm_artist': k[0], 'norm_title': k[1], 'score': s}
      for k, s in sorted(synced_scores.items())
  ]
  tmp_cache = f'{cache_path}.tmp'
  pd.DataFrame(rows, columns=['norm_artist', 'norm_title', 'score']).to_csv(
      tmp_cache, sep='\t', index=False
  )
  os.replace(tmp_cache, cache_path)
  logger.info(
      'Incremental YTMusic feedback sync: submitted %d items (%d lookups).',
      submitted,
      lookups_done,
  )
  return submitted


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
  """Parses CLI arguments."""
  parser = argparse.ArgumentParser(
      description='Match YTMusic tracks against ListenBrainz playcounts.'
  )
  parser.add_argument(
      '--force',
      '-f',
      action='store_true',
      help='Force matching run, bypassing 90-day interval threshold.',
  )
  parser.add_argument(
      '--skip-feedback-sync',
      action='store_true',
      help='Skip incremental ListenBrainz Hearts/Hates sync.',
  )
  return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Optional[Iterable[str]] = None) -> int:
  """CLI entrypoint."""
  logging.basicConfig(
      level=logging.INFO,
      format='%(asctime)s [%(levelname)s] %(message)s',
      datefmt='%Y-%m-%d %H:%M:%S',
  )
  if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)

  if uni is None:
    raise RuntimeError(f'unify_lib not found at {OUTPUT_DIR}')

  args = parse_args(argv)
  if not args.skip_feedback_sync:
    _sync_incremental_feedback_if_configured()

  if not uni.check_should_run(
      JOB_NAME,
      RUN_INTERVAL_DAYS,
      force=args.force,
      fallback_path=YT_PLAYCOUNT_OUT,
  ):
    return 0

  t0 = time.time()
  existing_id_map: Dict[str, str] = {}
  if os.path.exists(YT_MATCH_ID_MAP):
    existing_id_map = pd.read_csv(
        YT_MATCH_ID_MAP, sep='\t', index_col=0
    ).to_dict()['lastfm_fuzzy_id']
    logger.info('Loaded %d cached YT <-> playcount mappings',
                len(existing_id_map))

  playcounts_df = uni.ingest_lastfm_playcounts(PLAYCOUNTS_TSV)
  playcount_map = (
      playcounts_df.drop_duplicates(subset=['fuzzy_id'], keep='first')
      .set_index('fuzzy_id')['playcount']
      .to_dict()
  )
  yt_tracks = uni.ingest_ytmusic_db_assets(
      YT_TRACK_DB_LATEST, save_tsv=True, ytm_db=YT_TRACK_DB_OUT
  )
  logger.info('Loaded databases in %.1fs', time.time() - t0)

  matched, unmatched, updated_map = match_ytmusic_with_playcounts(
      yt_tracks,
      playcount_map,
      existing_id_map=existing_id_map,
      score_thresh=60,
  )
  save_tsvs(matched, unmatched, updated_map, yt_tracks)

  logger.info(
      'Matched %s / %s YTMusic tracks (%.1f%%) in %.1f minutes',
      f'{len(matched):,}',
      f'{len(yt_tracks):,}',
      100.0 * len(matched) / max(len(yt_tracks), 1),
      (time.time() - t0) / 60.0,
  )
  return 0


if __name__ == '__main__':
  sys.exit(main())
