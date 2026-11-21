"""Syncs ListenBrainz recommendation playlists to YouTube Music.

Fetches generated JSPF playlists from ListenBrainz's `/playlists/createdfor`
endpoint (`weekly-exploration`, `weekly-jams`, and all annual
`top-discoveries-of-YYYY` years), resolves tracks to YouTube Music `videoId`s
via a three-tier lookup (persistent cache -> local YTMusic database -> live
YTMusic search), excludes not-liked/disliked tracks, and creates or replaces
rolling `zz lb ...` playlists on YouTube Music via `ytmusic_library.py`.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import json
import logging
import os
import re
import sys
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import pandas as pd
import requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
  sys.path.insert(0, SCRIPT_DIR)

UNIFIED_DIR = os.path.abspath(
    os.path.join(SCRIPT_DIR, '..', 'music-sources-unified')
)
YTMUSIC_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, '..', 'ytmusic'))
for _extra_dir in (UNIFIED_DIR, YTMUSIC_DIR):
  if os.path.isdir(_extra_dir) and _extra_dir not in sys.path:
    sys.path.append(_extra_dir)

try:
  import auth as lb_auth  # type: ignore
except ImportError:
  lb_auth = None  # type: ignore

try:
  import unify_lib as uni  # type: ignore
except ImportError:
  uni = None  # type: ignore

logger = logging.getLogger(__name__)

API_BASE_URL = 'https://api.listenbrainz.org/1'
JSPF_PLAYLIST_EXT = 'https://musicbrainz.org/doc/jspf#playlist'
RECORDING_URI_PREFIX = 'https://musicbrainz.org/recording/'
PLAYLIST_URI_PREFIX = 'https://listenbrainz.org/playlist/'

PLAYLIST_PREFIX = 'zz lb '
PLAYLIST_ADD_CHUNK = 50
DEFAULT_CREATEDFOR_COUNT = 100
CACHE_MISS_SENTINEL = '__MISS__'

WEEKLY_PATCHES = ('weekly-exploration', 'weekly-jams')
ANNUAL_DISCOVERIES_PREFIX = 'top-discoveries-of-'
ANNUAL_MISSED_PREFIX = 'top-missed-recordings-of-'
COMBINED_MISSED_PATCH = 'top-missed-recordings-all'

TSV_DIR = os.path.join(SCRIPT_DIR, 'tsvs')
VID_CACHE_TSV = os.path.join(TSV_DIR, '_listenbrainz_ytmusic_vid_cache.tsv')
SYNC_STATE_JSON = os.path.join(TSV_DIR, '_listenbrainz_playlists_synced.json')

YT_UNIFIED_DB_TSV = os.path.join(
    UNIFIED_DIR, 'tsvs', 'ytmusic_all_database.tsv'
)
YT_PLAYLISTS_DIR = os.path.join(YTMUSIC_DIR, 'playlists')
YT_TRACKS_DB_TSV = os.path.join(YT_PLAYLISTS_DIR, '_tracks_db.tsv')
YT_NOT_LIKED_TSV = os.path.join(YT_PLAYLISTS_DIR, '_not_liked_tracks.tsv')
YT_UNLIKED_TSV = os.path.join(YT_PLAYLISTS_DIR, '_unliked_detected.tsv')

BRACKET_NOISE_RE = re.compile(
    r'\s*[\(\[\{][^)\]\}]*\b('
    r'remaster(?:ed)?|deluxe|anniversary|mono|stereo|explicit|clean|'
    r'bonus\s+track|radio\s+edit|single\s+version|album\s+version|'
    r'feat\.?|ft\.?|featuring'
    r')\b[^)\]\}]*[\)\]\}]',
    re.IGNORECASE,
)
FEAT_TAIL_RE = re.compile(
    r'\s+(?:feat\.?|ft\.?|featuring)\s+.+$',
    re.IGNORECASE,
)
NON_ALNUM_RE = re.compile(r'[^a-z0-9]+')


def normalize_text(value: Any, strip_noise: bool = False) -> str:
  """Normalizes artist, title, or album strings to lowercase alphanumeric words.

  Args:
    value (Any): Raw text value to normalize.
    strip_noise (bool): Whether to strip remaster/featuring suffixes first.

  Returns:
    str: Normalized space-separated alphanumeric string.
  """
  text = str(value or '').strip()
  if not text:
    return ''
  if strip_noise:
    text = BRACKET_NOISE_RE.sub('', text)
    text = FEAT_TAIL_RE.sub('', text)
  return NON_ALNUM_RE.sub(' ', text.lower()).strip()


def format_ytmusic_playlist_name(source_patch: str) -> str:
  """Maps a ListenBrainz `source_patch` slug to a `zz lb ...` playlist title.

  Args:
    source_patch (str): ListenBrainz Troi patch slug (e.g., `weekly-jams`).

  Returns:
    str: Lowercase YouTube Music playlist title prefixed with `zz lb `.
  """
  slug = source_patch.strip().lower()
  if slug == 'weekly-exploration':
    return f'{PLAYLIST_PREFIX}weekly exploration'
  if slug == 'weekly-jams':
    return f'{PLAYLIST_PREFIX}weekly jams'
  if slug == COMBINED_MISSED_PATCH:
    return f'{PLAYLIST_PREFIX}missed tracks'
  if slug.startswith('top-discoveries-of-'):
    year = slug[len('top-discoveries-of-') :]
    return f'{PLAYLIST_PREFIX}top discoveries {year}'
  if slug.startswith('top-missed-recordings-of-'):
    year = slug[len('top-missed-recordings-of-') :]
    return f'{PLAYLIST_PREFIX}top missed {year}'
  cleaned = slug.replace('-', ' ').strip()
  return f'{PLAYLIST_PREFIX}{cleaned}'


def extract_mbid(uri_or_list: Any, prefix: str) -> str:
  """Extracts a trailing MusicBrainz/ListenBrainz UUID from a JSPF identifier.

  Args:
    uri_or_list (Any): String URI or list of string URIs from JSPF JSON.
    prefix (str): Expected URI prefix before the UUID.

  Returns:
    str: Extracted UUID string, or empty string when absent.
  """
  candidates: List[str] = []
  if isinstance(uri_or_list, str):
    candidates = [uri_or_list]
  elif isinstance(uri_or_list, list):
    candidates = [str(item) for item in uri_or_list if item]
  for item in candidates:
    trimmed = item.strip().rstrip('/')
    if trimmed.startswith(prefix):
      return trimmed[len(prefix) :]
    if '/' in trimmed:
      return trimmed.rsplit('/', 1)[-1]
  return ''


def _lb_headers(token: str) -> Dict[str, str]:
  """Builds HTTP headers for ListenBrainz API calls.

  Args:
    token (str): Optional ListenBrainz user token.

  Returns:
    Dict[str, str]: Request headers dictionary.
  """
  headers = {'Accept': 'application/json'}
  if token:
    headers['Authorization'] = f'Token {token}'
  return headers


def _request_lb_json(
    session: requests.Session,
    url: str,
    headers: Dict[str, str],
    params: Optional[Dict[str, Any]] = None,
    max_retries: int = 4,
) -> Dict[str, Any]:
  """Performs a GET request against ListenBrainz with rate-limit backoff.

  Args:
    session (requests.Session): Active HTTP session.
    url (str): Full ListenBrainz endpoint URL.
    headers (Dict[str, str]): Request headers.
    params (Optional[Dict[str, Any]]): Query parameters.
    max_retries (int): Maximum retry attempts on 429/5xx errors.

  Returns:
    Dict[str, Any]: Parsed JSON response payload.
  """
  for attempt in range(max_retries):
    resp = session.get(url, headers=headers, params=params, timeout=30)
    if resp.status_code == 429 or resp.status_code >= 500:
      wait_sec = float(resp.headers.get('X-RateLimit-Reset-In', 2**attempt))
      if attempt < max_retries - 1:
        time.sleep(max(1.0, wait_sec))
        continue
    resp.raise_for_status()
    return resp.json()
  raise RuntimeError(f'ListenBrainz GET failed after retries: {url}')


def fetch_createdfor_playlists(
    username: str,
    token: str = '',
    count: int = DEFAULT_CREATEDFOR_COUNT,
    session: Optional[requests.Session] = None,
) -> List[Dict[str, Any]]:
  """Fetches playlist summaries from `/1/user/{username}/playlists/createdfor`.

  Args:
    username (str): ListenBrainz username.
    token (str): Optional ListenBrainz user token.
    count (int): Maximum number of created-for playlists to retrieve.
    session (Optional[requests.Session]): Optional requests session.

  Returns:
    List[Dict[str, Any]]: Normalized playlist metadata records ordered newest
    first.
  """
  if not username:
    raise ValueError(
        'ListenBrainz username is required (set LISTENBRAINZ_USER_NAME or '
        'configure listenbrainz/auth.py).'
    )
  http = session or requests.Session()
  url = f'{API_BASE_URL}/user/{username}/playlists/createdfor'
  records: List[Dict[str, Any]] = []
  offset = 0
  page_size = max(1, min(count, 100))
  while True:
    payload = _request_lb_json(
        http,
        url,
        _lb_headers(token),
        params={'count': page_size, 'offset': offset},
    )
    items = payload.get('playlists', []) or []
    for wrapper in items:
      pl = wrapper.get('playlist') or {}
      ext = (pl.get('extension') or {}).get(JSPF_PLAYLIST_EXT) or {}
      algo = (ext.get('additional_metadata') or {}).get(
          'algorithm_metadata'
      ) or {}
      source_patch = str(algo.get('source_patch') or '').strip()
      playlist_mbid = extract_mbid(pl.get('identifier'), PLAYLIST_URI_PREFIX)
      if not playlist_mbid or not source_patch:
        continue
      records.append({
          'playlist_mbid': playlist_mbid,
          'source_patch': source_patch,
          'title': str(pl.get('title') or '').strip(),
          'date': str(pl.get('date') or '').strip(),
          'annotation': str(pl.get('annotation') or '').strip(),
      })
    total = int(payload.get('playlist_count') or 0)
    offset += len(items)
    if not items or len(items) < page_size or (total and offset >= total):
      break
  records.sort(key=lambda r: r['date'], reverse=True)
  return records


def _build_combined_missed_record(
    latest_by_patch: Dict[str, Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
  """Builds a combined missed-tracks record ordered newest year -> oldest year.

  Args:
    latest_by_patch (Dict[str, Dict[str, Any]]): Newest playlist per patch.

  Returns:
    Optional[Dict[str, Any]]: Combined record with `source_mbids` ordered
    newest-to-oldest so the oldest year lands at the bottom of the playlist.
  """
  prefix = ANNUAL_MISSED_PREFIX
  missed_years = sorted(
      (
          p[len(prefix) :]
          for p in latest_by_patch
          if p.startswith(prefix) and p[len(prefix) :].isdigit()
      ),
      reverse=True,
  )
  if not missed_years:
    return None
  recs = [latest_by_patch[f'{prefix}{yr}'] for yr in missed_years]
  return {
      'playlist_mbid': ','.join(r['playlist_mbid'] for r in recs),
      'source_mbids': [r['playlist_mbid'] for r in recs],
      'years': missed_years,
      'source_patch': COMBINED_MISSED_PATCH,
      'title': f'Missed Tracks ({missed_years[-1]}-{missed_years[0]})',
      'date': max(r.get('date', '') for r in recs),
      'annotation': '',
  }


def select_target_playlists(
    available: Sequence[Dict[str, Any]],
    patches: Optional[Sequence[str]] = None,
    year: Optional[int] = None,
) -> List[Dict[str, Any]]:
  """Selects the newest ListenBrainz playlist per requested `source_patch`.

  When neither `patches` nor `year` is specified, selects the latest
  `weekly-exploration` and `weekly-jams` playlists, all available
  `top-discoveries-of-YYYY` annual playlists, and one combined
  `top-missed-recordings-all` playlist (`zz lb missed tracks`) ordered from
  newest year at the top to oldest year at the bottom.

  Args:
    available (Sequence[Dict[str, Any]]): Playlists sorted newest first.
    patches (Optional[Sequence[str]]): Explicit `source_patch` slugs to select.
    year (Optional[int]): Specific annual Year-in-Music year to include.

  Returns:
    List[Dict[str, Any]]: Selected playlist metadata dicts (one per target).
  """
  latest_by_patch: Dict[str, Dict[str, Any]] = {}
  for rec in sorted(available, key=lambda r: r.get('date', ''), reverse=True):
    patch = rec['source_patch']
    if patch not in latest_by_patch:
      latest_by_patch[patch] = rec

  combined_missed = _build_combined_missed_record(latest_by_patch)
  if combined_missed is not None:
    latest_by_patch[COMBINED_MISSED_PATCH] = combined_missed

  wanted: List[str] = []
  if patches:
    wanted.extend(p.strip() for p in patches if p.strip())
  else:
    for wp in WEEKLY_PATCHES:
      if wp in latest_by_patch:
        wanted.append(wp)
    if year is None:
      prefix = ANNUAL_DISCOVERIES_PREFIX
      matching_years = sorted(
          (
              p[len(prefix) :]
              for p in latest_by_patch
              if p.startswith(prefix) and p[len(prefix) :].isdigit()
          ),
          reverse=True,
      )
      for yr in matching_years:
        wanted.append(f'{prefix}{yr}')
      if COMBINED_MISSED_PATCH in latest_by_patch:
        wanted.append(COMBINED_MISSED_PATCH)

  if year is not None:
    patch_name = f'{ANNUAL_DISCOVERIES_PREFIX}{year}'
    if patch_name not in wanted:
      wanted.append(patch_name)

  selected: List[Dict[str, Any]] = []
  for patch in wanted:
    if patch in latest_by_patch:
      selected.append(latest_by_patch[patch])
    else:
      logger.warning('Requested ListenBrainz patch not found: %s', patch)
  return selected


def fetch_playlist_tracks(
    playlist_mbid: str,
    token: str = '',
    session: Optional[requests.Session] = None,
) -> List[Dict[str, Any]]:
  """Fetches full JSPF track entries for a ListenBrainz playlist UUID.

  Args:
    playlist_mbid (str): ListenBrainz playlist UUID.
    token (str): Optional ListenBrainz user token.
    session (Optional[requests.Session]): Optional requests session.

  Returns:
    List[Dict[str, Any]]: Parsed track dictionaries with `recording_mbid`,
    `artist`, `title`, `album`, and `duration_ms`.
  """
  http = session or requests.Session()
  url = f'{API_BASE_URL}/playlist/{playlist_mbid}'
  payload = _request_lb_json(http, url, _lb_headers(token))
  pl = payload.get('playlist') or {}
  tracks: List[Dict[str, Any]] = []
  for item in pl.get('track', []) or []:
    artist = str(item.get('creator') or '').strip()
    title = str(item.get('title') or '').strip()
    if not artist or not title:
      continue
    dur_raw = item.get('duration')
    duration_ms = int(dur_raw) if isinstance(dur_raw, (int, float)) else 0
    tracks.append({
        'recording_mbid': extract_mbid(
            item.get('identifier'), RECORDING_URI_PREFIX
        ),
        'artist': artist,
        'title': title,
        'album': str(item.get('album') or '').strip(),
        'duration_ms': duration_ms,
    })
  return tracks


def load_sync_state(state_path: str = SYNC_STATE_JSON) -> Dict[str, Any]:
  """Loads the persisted JSON mapping of synced ListenBrainz playlists.

  Args:
    state_path (str): Path to `_listenbrainz_playlists_synced.json`.

  Returns:
    Dict[str, Any]: Mapping of `source_patch` -> last sync metadata.
  """
  if not os.path.isfile(state_path):
    return {}
  with open(state_path, encoding='utf-8') as fh:
    data = json.load(fh)
  return data if isinstance(data, dict) else {}


def save_sync_state(
    state: Dict[str, Any], state_path: str = SYNC_STATE_JSON
) -> None:
  """Atomically saves the synced ListenBrainz playlist JSON state.

  Args:
    state (Dict[str, Any]): Mapping of `source_patch` -> sync metadata.
    state_path (str): Destination JSON path.
  """
  os.makedirs(os.path.dirname(state_path), exist_ok=True)
  tmp_path = f'{state_path}.tmp'
  with open(tmp_path, 'w', encoding='utf-8') as fh:
    json.dump(state, fh, indent=2, sort_keys=True)
  os.replace(tmp_path, state_path)


def load_vid_cache(cache_path: str = VID_CACHE_TSV) -> Dict[str, str]:
  """Loads the persistent `cache_key -> video_id` lookup cache TSV.

  Args:
    cache_path (str): Path to `_listenbrainz_ytmusic_vid_cache.tsv`.

  Returns:
    Dict[str, str]: Mapping of `cache_key` -> YouTube Music `videoId`.
  """
  if not os.path.isfile(cache_path):
    return {}
  cache: Dict[str, str] = {}
  with open(cache_path, encoding='utf-8', newline='') as fh:
    reader = csv.DictReader(fh, delimiter='\t')
    for row in reader:
      key = str(row.get('cache_key') or '').strip()
      vid = str(row.get('video_id') or '').strip()
      if key and vid:
        cache[key] = vid
  return cache


def save_vid_cache(
    cache: Dict[str, str], cache_path: str = VID_CACHE_TSV
) -> None:
  """Atomically writes the `cache_key -> video_id` lookup cache TSV.

  Args:
    cache (Dict[str, str]): Mapping of `cache_key` -> `videoId`.
    cache_path (str): Destination TSV path.
  """
  os.makedirs(os.path.dirname(cache_path), exist_ok=True)
  tmp_path = f'{cache_path}.tmp'
  with open(tmp_path, 'w', encoding='utf-8', newline='') as fh:
    writer = csv.writer(fh, delimiter='\t', lineterminator='\n')
    writer.writerow(['cache_key', 'video_id'])
    for key in sorted(cache):
      writer.writerow([key, cache[key]])
  os.replace(tmp_path, cache_path)


def load_not_like_exclusions(
    playlists_dir: str = YT_PLAYLISTS_DIR,
) -> Tuple[Set[str], Set[Tuple[str, str]]]:
  """Loads excluded `videoId`s and `(norm_artist, norm_title)` song keys.

  Args:
    playlists_dir (str): Path to `ytmusic/playlists`.

  Returns:
    Tuple[Set[str], Set[Tuple[str, str]]]: Set of excluded `videoId`s and set
    of excluded normalized `(artist, clean_title)` pairs.
  """
  excluded_vids: Set[str] = set()
  excluded_songs: Set[Tuple[str, str]] = set()

  if uni is not None and os.path.isdir(UNIFIED_DIR):
    vids, songs, _ = uni.load_yt_not_like_exclusions(UNIFIED_DIR)
    excluded_vids |= set(vids)
    excluded_songs |= set(songs)

  if not os.path.isdir(playlists_dir):
    return excluded_vids, excluded_songs

  not_like_re = re.compile(r'not[\s_-]*like', re.IGNORECASE)
  tsv_files: List[str] = []
  for ledger in ('_not_liked_tracks.tsv', '_unliked_detected.tsv'):
    path = os.path.join(playlists_dir, ledger)
    if os.path.isfile(path):
      tsv_files.append(path)
  for entry in sorted(os.listdir(playlists_dir)):
    if (
        entry.endswith('.tsv')
        and not entry.startswith('_')
        and not_like_re.search(entry)
    ):
      tsv_files.append(os.path.join(playlists_dir, entry))

  for path in tsv_files:
    df = pd.read_csv(path, sep='\t', quoting=csv.QUOTE_NONE, low_memory=False)
    if 'videoId' in df.columns:
      excluded_vids |= set(df['videoId'].dropna().astype(str))
    if {'artist', 'title'}.issubset(df.columns):
      for artist, title in zip(
          df['artist'].fillna(''), df['title'].fillna('')
      ):
        na = normalize_text(artist)
        nt = normalize_text(title, strip_noise=True)
        if na and nt:
          excluded_songs.add((na, nt))
  return excluded_vids, excluded_songs


def load_local_ytmusic_index(
    db_paths: Sequence[str] = (YT_UNIFIED_DB_TSV, YT_TRACKS_DB_TSV),
    excluded_vids: Optional[Set[str]] = None,
) -> Dict[Tuple[str, str], List[Dict[str, Any]]]:
  """Indexes local YouTube Music database TSVs by `(norm_artist, norm_title)`.

  Args:
    db_paths (Sequence[str]): Candidate database TSV paths (checked in order).
    excluded_vids (Optional[Set[str]]): Disliked/not-liked `videoId`s to skip.

  Returns:
    Dict[Tuple[str, str], List[Dict[str, Any]]]: Lookup index mapping normalized
    `(artist, clean_title)` to candidate track rows.
  """
  banned = excluded_vids or set()
  chosen_path = next((p for p in db_paths if os.path.isfile(p)), '')
  if not chosen_path:
    return {}

  wanted_cols = {
      'videoId',
      'artist',
      'title',
      'album',
      'likeStatus',
      'inLibrary',
      'duration_seconds',
  }
  df = pd.read_csv(
      chosen_path,
      sep='\t',
      usecols=lambda c: c in wanted_cols,
      low_memory=False,
  )
  index: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
  for row in df.to_dict('records'):
    vid = str(row.get('videoId') or '').strip()
    like_status = str(row.get('likeStatus') or '').strip().upper()
    if not vid or vid in banned or like_status == 'DISLIKE':
      continue
    na = normalize_text(row.get('artist'))
    nt = normalize_text(row.get('title'), strip_noise=True)
    if not na or not nt:
      continue
    dur_val = pd.to_numeric(row.get('duration_seconds'), errors='coerce')
    dur_sec = float(dur_val) if pd.notna(dur_val) else 0.0
    in_lib = str(row.get('inLibrary') or '').strip().lower() == 'true'
    candidate = {
        'video_id': vid,
        'like_status': like_status,
        'in_library': in_lib,
        'norm_album': normalize_text(row.get('album'), strip_noise=True),
        'duration_sec': dur_sec,
    }
    index.setdefault((na, nt), []).append(candidate)
  return index


def _pick_best_local_candidate(
    candidates: Sequence[Dict[str, Any]],
    norm_album: str,
    duration_ms: int,
    excluded_vids: Set[str],
) -> str:
  """Chooses the highest-quality local YTMusic `videoId` candidate.

  Args:
    candidates (Sequence[Dict[str, Any]]): Candidate local DB records.
    norm_album (str): Normalized target album name.
    duration_ms (int): Target track duration in milliseconds.
    excluded_vids (Set[str]): Excluded `videoId`s.

  Returns:
    str: Best matching `videoId`, or empty string if none qualify.
  """
  target_sec = duration_ms / 1000.0 if duration_ms > 0 else 0.0
  valid = [c for c in candidates if c['video_id'] not in excluded_vids]
  if not valid:
    return ''

  def _rank_key(cand: Dict[str, Any]) -> Tuple[int, int, int, float]:
    is_liked = 1 if cand['like_status'] == 'LIKE' else 0
    in_lib = 1 if cand['in_library'] else 0
    album_match = (
        1 if norm_album and cand['norm_album'] == norm_album else 0
    )
    if target_sec > 0 and cand['duration_sec'] > 0:
      dur_penalty = -abs(cand['duration_sec'] - target_sec)
    else:
      dur_penalty = 0.0
    return (is_liked, in_lib, album_match, dur_penalty)

  best = max(valid, key=_rank_key)
  return str(best['video_id'])


def _similarity(left: str, right: str) -> float:
  """Computes normalized text similarity in `[0.0, 1.0]`.

  Args:
    left (str): First normalized string.
    right (str): Second normalized string.

  Returns:
    float: Similarity score between 0.0 and 1.0.
  """
  if not left or not right:
    return 0.0
  if left == right:
    return 1.0
  if left in right or right in left:
    shorter = min(len(left), len(right))
    longer = max(len(left), len(right))
    if shorter >= 3:
      return max(0.85, shorter / float(longer))
  return difflib.SequenceMatcher(None, left, right).ratio()


def score_ytmusic_search_result(
    result: Dict[str, Any],
    norm_artist: str,
    norm_title: str,
    norm_album: str,
    duration_ms: int,
) -> float:
  """Scores a `ytmusicapi` search result against a ListenBrainz track.

  Args:
    result (Dict[str, Any]): Item returned by `yt.search(...)`.
    norm_artist (str): Normalized target artist.
    norm_title (str): Normalized target clean title.
    norm_album (str): Normalized target album.
    duration_ms (int): Target duration in milliseconds.

  Returns:
    float: Composite match score (`< 0` when below minimum confidence).
  """
  res_title_clean = normalize_text(result.get('title'), strip_noise=True)
  title_sim = _similarity(norm_title, res_title_clean)
  if title_sim < 0.62:
    return -1.0

  artists_raw = result.get('artists') or []
  artist_names = [
      normalize_text(a.get('name'))
      for a in artists_raw
      if isinstance(a, dict) and a.get('name')
  ]
  joined_artists = ' '.join(n for n in artist_names if n)
  artist_sims = [_similarity(norm_artist, name) for name in artist_names]
  if joined_artists:
    artist_sims.append(_similarity(norm_artist, joined_artists))
  artist_sim = max(artist_sims) if artist_sims else 0.0
  if artist_sim < 0.55:
    return -1.0

  score = 0.55 * title_sim + 0.45 * artist_sim
  if score < 0.70:
    return -1.0

  album_obj = result.get('album')
  res_album = (
      normalize_text(album_obj.get('name'), strip_noise=True)
      if isinstance(album_obj, dict)
      else ''
  )
  if norm_album and res_album and norm_album == res_album:
    score += 0.06

  dur_sec = float(result.get('duration_seconds') or 0.0)
  target_sec = duration_ms / 1000.0 if duration_ms > 0 else 0.0
  if dur_sec > 0 and target_sec > 0:
    diff = abs(dur_sec - target_sec)
    if diff <= 8.0:
      score += 0.04
    elif diff > 45.0:
      score -= 0.12
  return score


def search_ytmusic_track(
    yt_api: Any,
    track: Dict[str, Any],
    excluded_vids: Set[str],
) -> str:
  """Searches YouTube Music live for a ListenBrainz JSPF track.

  Args:
    yt_api (Any): Authenticated `ytmusicapi.YTMusic` client.
    track (Dict[str, Any]): JSPF track dict (`artist`, `title`, `album`, etc.).
    excluded_vids (Set[str]): Disliked/not-liked `videoId`s to skip.

  Returns:
    str: Best matching YouTube Music `videoId`, or empty string on miss.
  """
  norm_artist = normalize_text(track['artist'])
  norm_title = normalize_text(track['title'], strip_noise=True)
  norm_album = normalize_text(track.get('album', ''), strip_noise=True)
  duration_ms = int(track.get('duration_ms') or 0)
  query = f"{track['artist']} {track['title']}"

  for search_filter in ('songs', 'videos'):
    results = yt_api.search(query, filter=search_filter, limit=5) or []
    best_vid = ''
    best_score = -1.0
    for item in results:
      vid = str(item.get('videoId') or '').strip()
      if not vid or vid in excluded_vids:
        continue
      like_status = str(item.get('likeStatus') or '').strip().upper()
      if like_status == 'DISLIKE':
        continue
      sc = score_ytmusic_search_result(
          item, norm_artist, norm_title, norm_album, duration_ms
      )
      if sc > best_score:
        best_score = sc
        best_vid = vid
    if best_vid:
      return best_vid
  return ''


def resolve_tracks_to_video_ids(
    tracks: Sequence[Dict[str, Any]],
    vid_cache: Dict[str, str],
    local_index: Dict[Tuple[str, str], List[Dict[str, Any]]],
    excluded_vids: Set[str],
    excluded_songs: Set[Tuple[str, str]],
    yt_api: Optional[Any] = None,
    retry_misses: bool = False,
) -> Tuple[List[str], Dict[str, int], bool]:
  """Resolves ListenBrainz tracks to deduplicated YouTube Music `videoId`s.

  Args:
    tracks (Sequence[Dict[str, Any]]): JSPF tracks in playlist order.
    vid_cache (Dict[str, str]): Mutable persistent `cache_key -> video_id` map.
    local_index (Dict[Tuple[str, str], List[Dict[str, Any]]]): Local YTMusic DB.
    excluded_vids (Set[str]): Excluded `videoId`s.
    excluded_songs (Set[Tuple[str, str]]): Excluded `(norm_artist, norm_title)`.
    yt_api (Optional[Any]): Live `YTMusic` client for Tier-3 search fallback.
    retry_misses (bool): Whether to re-query cached `__MISS__` entries.

  Returns:
    Tuple[List[str], Dict[str, int], bool]: Ordered unique `videoId`s,
    resolution stats dictionary, and boolean indicating whether `vid_cache`
    was modified.
  """
  resolved: List[str] = []
  seen_vids: Set[str] = set()
  cache_dirty = False
  stats = {
      'total': len(tracks),
      'excluded_not_like': 0,
      'cache_hits': 0,
      'local_db_hits': 0,
      'live_search_hits': 0,
      'misses': 0,
  }

  for track in tracks:
    norm_artist = normalize_text(track['artist'])
    norm_title = normalize_text(track['title'], strip_noise=True)
    norm_album = normalize_text(track.get('album', ''), strip_noise=True)
    if not norm_artist or not norm_title:
      stats['misses'] += 1
      continue
    if (norm_artist, norm_title) in excluded_songs:
      stats['excluded_not_like'] += 1
      continue

    mbid = track.get('recording_mbid', '')
    mbid_key = f'mbid:{mbid}' if mbid else ''
    song_key = f'song:{norm_artist}|{norm_title}'

    cached_vid = ''
    for key in (mbid_key, song_key):
      if key and key in vid_cache:
        cached_vid = vid_cache[key]
        break

    if cached_vid == CACHE_MISS_SENTINEL and not retry_misses:
      stats['misses'] += 1
      continue

    vid = ''
    if cached_vid and cached_vid != CACHE_MISS_SENTINEL:
      if cached_vid in excluded_vids:
        stats['excluded_not_like'] += 1
        continue
      vid = cached_vid
      stats['cache_hits'] += 1

    if not vid:
      candidates = local_index.get((norm_artist, norm_title), [])
      vid = _pick_best_local_candidate(
          candidates,
          norm_album,
          int(track.get('duration_ms') or 0),
          excluded_vids,
      )
      if vid:
        stats['local_db_hits'] += 1
        if mbid_key:
          vid_cache[mbid_key] = vid
        vid_cache[song_key] = vid
        cache_dirty = True

    if not vid and yt_api is not None:
      vid = search_ytmusic_track(yt_api, track, excluded_vids)
      if vid:
        stats['live_search_hits'] += 1
        if mbid_key:
          vid_cache[mbid_key] = vid
        vid_cache[song_key] = vid
        cache_dirty = True
      else:
        if mbid_key:
          vid_cache[mbid_key] = CACHE_MISS_SENTINEL
        vid_cache[song_key] = CACHE_MISS_SENTINEL
        cache_dirty = True

    if not vid:
      stats['misses'] += 1
      logger.info(
          'Unresolved ListenBrainz track: %s - %s',
          track['artist'],
          track['title'],
      )
      continue

    if vid not in seen_vids:
      seen_vids.add(vid)
      resolved.append(vid)

  return resolved, stats, cache_dirty


def upsert_ytmusic_playlist(
    yt_lib: Any,
    pl_name: str,
    video_ids: Sequence[str],
    description: str,
    existing_playlist_id: str = '',
    dry_run: bool = False,
) -> str:
  """Creates or in-place replaces a rolling playlist on YouTube Music.

  Args:
    yt_lib (Any): Initialized `YTMusicPlaylists` instance.
    pl_name (str): Target YouTube Music playlist title (`zz lb ...`).
    video_ids (Sequence[str]): Ordered deduplicated `videoId`s.
    description (str): Playlist description string.
    existing_playlist_id (str): Known `playlistId` from previous sync state.
    dry_run (bool): When True, logs planned changes without modifying YTMusic.

  Returns:
    str: Resulting YouTube Music `playlistId` (or `'DRY_RUN'` in dry-run mode).
  """
  vids = list(dict.fromkeys(v for v in video_ids if v))
  if not vids:
    raise ValueError(f'Cannot sync empty videoId list for {pl_name}')

  pl_id = ''
  known_ids = set(yt_lib.playlists['playlistId'].dropna().astype(str))
  if existing_playlist_id and existing_playlist_id in known_ids:
    pl_id = existing_playlist_id
  elif pl_name in set(yt_lib.playlists['title'].dropna().astype(str)):
    row = yt_lib.query_by_title(pl_name)
    if row is not None:
      pl_id = str(row.playlistId)

  if dry_run:
    action = 'replace' if pl_id else 'create'
    logger.info(
        '[DRY RUN] Would %s playlist "%s" (%d tracks, id=%s)',
        action,
        pl_name,
        len(vids),
        pl_id or 'NEW',
    )
    return pl_id or 'DRY_RUN'

  if not pl_id:
    pl_id = str(
        yt_lib.playlist_from_yt_vids(
            vids,
            pl_name=pl_name,
            sleep=1,
            public='PRIVATE',
            desc=description,
            dry=False,
        )
    )
  else:
    info = yt_lib.yt.get_playlist(pl_id, limit=None) or {}
    cur_tracks = info.get('tracks') or []
    cur_vids = [
        str(t.get('videoId'))
        for t in cur_tracks
        if isinstance(t, dict) and t.get('videoId')
    ]
    if cur_vids != vids:
      removable = [
          t
          for t in cur_tracks
          if isinstance(t, dict) and t.get('videoId') and t.get('setVideoId')
      ]
      if removable:
        yt_lib.remove_playlist_items_chunked(
            pl_id, removable, sleep=1, label='stale lb tracks'
        )
      for idx in range(0, len(vids), PLAYLIST_ADD_CHUNK):
        chunk = vids[idx : idx + PLAYLIST_ADD_CHUNK]
        yt_lib.yt.add_playlist_items(
            playlistId=pl_id, videoIds=chunk, duplicates=True
        )
      yt_lib._invalidate_playlist_cache(pl_id)
    yt_lib.yt.edit_playlist(pl_id, description=description)

  fresh_info = yt_lib.playlist_get_info(pl_id, use_cache=False)
  if fresh_info:
    yt_lib.save_playlist_tsv(dict(fresh_info))
  return pl_id


def sync_playlists(
    username: str,
    token: str = '',
    patches: Optional[Sequence[str]] = None,
    year: Optional[int] = None,
    force: bool = False,
    dry_run: bool = False,
    yt_lib: Optional[Any] = None,
    session: Optional[requests.Session] = None,
    state_path: str = SYNC_STATE_JSON,
    cache_path: str = VID_CACHE_TSV,
) -> List[Dict[str, Any]]:
  """End-to-end ListenBrainz recommendation playlists -> YouTube Music sync.

  Args:
    username (str): ListenBrainz username.
    token (str): Optional ListenBrainz user token.
    patches (Optional[Sequence[str]]): Explicit `source_patch` slugs to sync.
    year (Optional[int]): Optional specific Year-in-Music year to sync.
    force (bool): Re-sync even if `playlist_mbid` matches saved sync state.
    dry_run (bool): Resolve tracks without writing playlists to YTMusic.
    yt_lib (Optional[Any]): Injected `YTMusicPlaylists` instance (for tests).
    session (Optional[requests.Session]): Optional requests session.
    state_path (str): Path to sync state JSON.
    cache_path (str): Path to `videoId` lookup cache TSV.

  Returns:
    List[Dict[str, Any]]: Summary dictionaries for each synced playlist.
  """
  http = session or requests.Session()
  available = fetch_createdfor_playlists(
      username=username, token=token, session=http
  )
  selected = select_target_playlists(available, patches=patches, year=year)
  if not selected:
    logger.info('No matching ListenBrainz recommendation playlists found.')
    return []

  sync_state = load_sync_state(state_path)
  pending: List[Dict[str, Any]] = []
  for pl_meta in selected:
    patch = pl_meta['source_patch']
    prev = sync_state.get(patch) or {}
    if not force and prev.get('playlist_mbid') == pl_meta['playlist_mbid']:
      logger.info(
          'Skipping unchanged ListenBrainz playlist %s (%s)',
          format_ytmusic_playlist_name(patch),
          pl_meta['playlist_mbid'],
      )
      continue
    pending.append(pl_meta)

  if not pending:
    logger.info('All target ListenBrainz playlists are already up to date.')
    return []

  if yt_lib is None:
    from ytmusic_library import YTMusicPlaylists  # type: ignore

    try:
      yt_lib = YTMusicPlaylists()
    except RuntimeError:
      if not dry_run:
        raise
      from types import SimpleNamespace
      from ytmusicapi import YTMusic  # type: ignore

      logger.warning(
          'browser.json expired; using unauthenticated YTMusic search for '
          '--dry-run.'
      )
      yt_lib = SimpleNamespace(
          yt=YTMusic(),
          playlists=pd.DataFrame(columns=['title', 'playlistId']),
      )

  excluded_vids, excluded_songs = load_not_like_exclusions()
  local_index = load_local_ytmusic_index(excluded_vids=excluded_vids)
  vid_cache = load_vid_cache(cache_path)
  any_cache_dirty = False
  results: List[Dict[str, Any]] = []

  for pl_meta in pending:
    patch = pl_meta['source_patch']
    pl_mbid = pl_meta['playlist_mbid']
    yt_title = format_ytmusic_playlist_name(patch)
    lb_date = pl_meta['date'][:10] if pl_meta.get('date') else ''

    source_mbids = pl_meta.get('source_mbids') or [pl_mbid]
    tracks: List[Dict[str, Any]] = []
    for sub_mbid in source_mbids:
      tracks.extend(fetch_playlist_tracks(sub_mbid, token=token, session=http))

    video_ids, stats, cache_dirty = resolve_tracks_to_video_ids(
        tracks=tracks,
        vid_cache=vid_cache,
        local_index=local_index,
        excluded_vids=excluded_vids,
        excluded_songs=excluded_songs,
        yt_api=yt_lib.yt,
        retry_misses=force,
    )
    any_cache_dirty = any_cache_dirty or cache_dirty

    if not video_ids:
      logger.warning(
          'No playable YouTube Music tracks resolved for %s (%s)',
          yt_title,
          pl_mbid,
      )
      continue

    years = pl_meta.get('years') or []
    if len(source_mbids) > 1 and years:
      desc = (
          f'ListenBrainz missed tracks ({years[0]} down to {years[-1]}, '
          f'{lb_date}) | {len(video_ids)}/{len(tracks)} tracks matched'
      )
    else:
      desc = (
          f'ListenBrainz {patch} ({lb_date}) | '
          f'{len(video_ids)}/{len(tracks)} tracks matched | '
          f'https://listenbrainz.org/playlist/{pl_mbid}'
      )
    prev_yt_id = str((sync_state.get(patch) or {}).get('yt_playlist_id') or '')
    yt_pl_id = upsert_ytmusic_playlist(
        yt_lib=yt_lib,
        pl_name=yt_title,
        video_ids=video_ids,
        description=desc,
        existing_playlist_id=prev_yt_id,
        dry_run=dry_run,
    )
    logger.info(
        'Synced "%s" (%s): %d/%d tracks (cache=%d, db=%d, search=%d, '
        'excluded=%d, miss=%d) -> %s',
        yt_title,
        lb_date,
        len(video_ids),
        stats['total'],
        stats['cache_hits'],
        stats['local_db_hits'],
        stats['live_search_hits'],
        stats['excluded_not_like'],
        stats['misses'],
        yt_pl_id,
    )

    summary = {
        'source_patch': patch,
        'yt_title': yt_title,
        'yt_playlist_id': yt_pl_id,
        'playlist_mbid': pl_mbid,
        'date': lb_date,
        'track_count': len(video_ids),
        'lb_track_count': len(tracks),
        'synced_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'stats': stats,
    }
    results.append(summary)
    if not dry_run:
      sync_state[patch] = {
          'playlist_mbid': pl_mbid,
          'yt_playlist_id': yt_pl_id,
          'yt_title': yt_title,
          'date': lb_date,
          'track_count': len(video_ids),
          'synced_at': summary['synced_at'],
      }

  if any_cache_dirty:
    save_vid_cache(vid_cache, cache_path)
  if results and not dry_run:
    save_sync_state(sync_state, state_path)
  return results


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
  """Parses CLI flags for ListenBrainz -> YouTube Music playlist sync.

  Args:
    argv (Optional[Sequence[str]]): Command-line argument list.

  Returns:
    argparse.Namespace: Parsed CLI options.
  """
  parser = argparse.ArgumentParser(
      description='Sync ListenBrainz recommendation playlists to YouTube Music.'
  )
  parser.add_argument(
      '--user',
      default=os.environ.get(
          'LISTENBRAINZ_USER_NAME',
          getattr(lb_auth, 'LISTENBRAINZ_USER_NAME', ''),
      ),
      help='ListenBrainz username (defaults to LISTENBRAINZ_USER_NAME / auth).',
  )
  parser.add_argument(
      '--patches',
      default='',
      help='Comma-separated source_patch list (default: weekly + latest year).',
  )
  parser.add_argument(
      '--year',
      type=int,
      default=None,
      help='Specific Year-in-Music year to include (e.g., 2025).',
  )
  parser.add_argument(
      '--force',
      action='store_true',
      help='Re-sync playlists even when playlist_mbid is unchanged.',
  )
  parser.add_argument(
      '--dry-run',
      action='store_true',
      help='Resolve tracks without creating or modifying YTMusic playlists.',
  )
  return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
  """CLI entrypoint for syncing ListenBrainz playlists to YouTube Music.

  Args:
    argv (Optional[Sequence[str]]): Optional CLI argument vector.

  Returns:
    int: Process exit code (`0` on success).
  """
  logging.basicConfig(
      level=logging.INFO,
      format='%(asctime)s %(levelname)s %(message)s',
      datefmt='%H:%M:%S',
  )
  args = parse_args(argv)
  token = os.environ.get(
      'LISTENBRAINZ_TOKEN', getattr(lb_auth, 'LISTENBRAINZ_TOKEN', '')
  )
  patches = (
      [p.strip() for p in args.patches.split(',') if p.strip()]
      if args.patches
      else None
  )
  sync_playlists(
      username=args.user,
      token=token,
      patches=patches,
      year=args.year,
      force=args.force,
      dry_run=args.dry_run,
  )
  return 0


if __name__ == '__main__':
  sys.exit(main())
