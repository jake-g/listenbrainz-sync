"""ListenBrainz history scraper, archive seeder, and playcount aggregator.

Zero-flag drop-in replacement for `lastfm/lastfm_scrape_history.py`:
- `python listenbrainz_scrape_history.py` updates the canonical unified TSVs in
  `../music-sources-unified/tsvs/lastfm_{scrobbles,playcounts,top_albums}.tsv`.
- `python listenbrainz_scrape_history.py --parallel` writes side-by-side
  validation outputs to `listenbrainz/tsvs/` without modifying unified TSVs.
"""

from __future__ import annotations

import argparse
from collections import Counter
import glob
import json
import logging
import os
import re
import sys
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd
import requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OLD_ARCHIVE_DIR = os.path.join(SCRIPT_DIR, 'old')
if SCRIPT_DIR not in sys.path:
  sys.path.insert(0, SCRIPT_DIR)

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

try:
  import auth as lb_auth  # type: ignore
except ImportError:
  lb_auth = None  # type: ignore

logger = logging.getLogger(__name__)

API_BASE_URL = 'https://api.listenbrainz.org/1'
MAX_ITEMS_PER_GET = 1000
DEFAULT_OVERLAP_DAYS = 7
SCROBBLE_SEP = '////'
SHRINK_GUARD_RATIO = 0.99
MIN_VALID_UTS = int(getattr(uni, 'LASTFM_MIN_VALID_UTS', 1_000_000_000))

DEFAULT_USER_NAME = os.environ.get(
    'LISTENBRAINZ_USER_NAME',
    getattr(lb_auth, 'LISTENBRAINZ_USER_NAME', 'totoro_jake'),
)
DEFAULT_TOKEN = os.environ.get(
    'LISTENBRAINZ_TOKEN',
    getattr(lb_auth, 'LISTENBRAINZ_TOKEN', ''),
)

# Canonical unified paths consumed by music-sources-unified, redacted, ytmusic
SCROBBLES_TSV = os.path.join(OUTPUT_DIR, 'tsvs', 'lastfm_scrobbles.tsv')
PLAYCOUNTS_TSV = os.path.join(OUTPUT_DIR, 'tsvs', 'lastfm_playcounts.tsv')
LASTFM_TOP_ALBUMS = os.path.join(OUTPUT_DIR, 'tsvs', 'lastfm_top_albums.tsv')

# Parallel validation output paths (`--parallel` mode)
PARALLEL_TSV_DIR = os.path.join(SCRIPT_DIR, 'tsvs')
LB_SCROBBLES_TSV = os.path.join(PARALLEL_TSV_DIR, 'listenbrainz_scrobbles.tsv')
LB_PLAYCOUNTS_TSV = os.path.join(
    PARALLEL_TSV_DIR, 'listenbrainz_playcounts.tsv'
)
LB_TOP_ALBUMS_TSV = os.path.join(
    PARALLEL_TSV_DIR, 'listenbrainz_top_albums.tsv'
)


def _parse_timestamps(uts: pd.Series) -> pd.Series:
  """Parses Unix timestamps into UTC `datetime64[ns]` (`<1e9` -> `NaT`)."""
  if uni is not None:
    return uni.parse_scrobble_timestamps(uts)
  numeric = pd.to_numeric(uts, errors='coerce')
  valid = numeric.where(numeric >= MIN_VALID_UTS)
  return pd.to_datetime(valid, unit='s', errors='coerce')


def _ingest_playcounts(playcounts_tsv: str) -> pd.DataFrame:
  """Loads `playcounts_tsv` (`index_col=0`) and attaches fuzzy match IDs."""
  if uni is not None:
    return uni.ingest_lastfm_playcounts(playcounts_tsv)
  df = pd.read_csv(
      playcounts_tsv,
      sep='\t',
      index_col=0,
      dtype={'artist': str, 'album': str, 'title': str},
      keep_default_na=False,
  )
  def norm(s): return re.sub(r'\s+', ' ', str(s).lower().strip())
  df['fuzzy_id'] = [
      norm(f'{a} {b} {t}')
      for a, b, t in zip(df['artist'], df['album'], df['title'])
  ]
  df['fuzzy_album_id'] = [
      f'{norm(a)} -- {norm(b)}' for a, b in zip(df['artist'], df['album'])
  ]
  return df


def _find_archive_file(pattern: str) -> Optional[str]:
  """Searches `SCRIPT_DIR` and `OLD_ARCHIVE_DIR` for newest matching file."""
  matches: List[str] = []
  for folder in (SCRIPT_DIR, OLD_ARCHIVE_DIR):
    if os.path.isdir(folder):
      matches.extend(glob.glob(os.path.join(folder, pattern)))
  if not matches:
    return None
  matches.sort(key=os.path.getmtime, reverse=True)
  return matches[0]


def find_default_backfill_tsv() -> Optional[str]:
  """Finds `lastfm_missing_dated_backfill.tsv` in `.` or `./old/` if present."""
  return _find_archive_file('lastfm_missing_dated_backfill.tsv')


def parse_listen_record(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
  """Extracts canonical scrobble fields from a ListenBrainz listen payload.

  Reads directly from `track_metadata` (not `mbid_mapping`, which omits
  `release_name` and substitutes canonical MusicBrainz titles).

  Args:
    record: Raw ListenBrainz JSON object from `/listens` API.

  Returns:
    Dict with `artist`, `album`, `title`, and `timestamps`, or None if invalid.
  """
  if 'listened_at' not in record:
    return None
  ts = int(record['listened_at'])
  if ts < MIN_VALID_UTS:
    return None
  tm = record.get('track_metadata') or {}
  return {
      'artist': str(tm.get('artist_name') or ''),
      'album': str(tm.get('release_name') or ''),
      'title': str(tm.get('track_name') or ''),
      'timestamps': ts,
  }


def _empty_scrobbles_df() -> pd.DataFrame:
  """Returns an empty DataFrame with canonical scrobble schema."""
  return pd.DataFrame({
      'artist': pd.Series(dtype='str'),
      'album': pd.Series(dtype='str'),
      'title': pd.Series(dtype='str'),
      'timestamps': pd.Series(dtype='int64'),
      'datetime': pd.Series(dtype='datetime64[ns]'),
  })


def _respect_rate_limit(resp: requests.Response) -> None:
  """Sleeps when ListenBrainz rate-limit headers indicate quota exhaustion."""
  remaining = resp.headers.get('X-RateLimit-Remaining')
  reset_in = resp.headers.get('X-RateLimit-Reset-In')
  if remaining is not None and reset_in is not None:
    try:
      if int(remaining) <= 1:
        sleep_sec = max(1, int(float(reset_in)) + 1)
        logger.info(
            'Rate limit low (remaining=%s); sleeping %ds',
            remaining,
            sleep_sec,
        )
        time.sleep(sleep_sec)
    except ValueError:
      pass


def _fetch_listens_page(
    session: requests.Session,
    username: str,
    count: int = MAX_ITEMS_PER_GET,
    min_ts: Optional[int] = None,
    max_ts: Optional[int] = None,
) -> List[Dict[str, Any]]:
  """Fetches a single `/1/user/{username}/listens` page with retries."""
  url = f'{API_BASE_URL}/user/{username}/listens'
  params: Dict[str, Any] = {'count': min(count, MAX_ITEMS_PER_GET)}
  if min_ts is not None:
    params['min_ts'] = int(min_ts)
  if max_ts is not None:
    params['max_ts'] = int(max_ts)

  for attempt in range(5):
    try:
      resp = session.get(url, params=params, timeout=25)
      if resp.status_code == 429:
        reset_in = int(float(resp.headers.get('X-RateLimit-Reset-In', '5')))
        wait_sec = max(1, reset_in + 1)
        logger.warning('HTTP 429 rate-limited; sleeping %ds', wait_sec)
        time.sleep(wait_sec)
        continue
      if resp.ok:
        _respect_rate_limit(resp)
        data = resp.json()
        payload = data.get('payload') if isinstance(data, dict) else None
        if isinstance(payload, dict) and isinstance(
            payload.get('listens'), list
        ):
          return payload['listens']
    except (requests.RequestException, ValueError) as exc:
      logger.warning('ListenBrainz GET attempt %d failed: %s', attempt + 1, exc)
    time.sleep(2**attempt)

  raise RuntimeError(
      f'Incomplete ListenBrainz fetch for user={username} params={params}.'
  )


def _build_session(token: str = '') -> requests.Session:
  """Creates a configured `requests.Session` with optional bearer token."""
  session = requests.Session()
  session.headers.update({'Accept': 'application/json'})
  if token:
    session.headers.update({'Authorization': f'Token {token}'})
  return session


def fetch_listens_incremental(
    username: str,
    min_ts: int,
    token: str = '',
    count: int = MAX_ITEMS_PER_GET,
) -> pd.DataFrame:
  """Fetches listens strictly newer than `min_ts` moving forward in time.

  ListenBrainz returns the oldest `count` listens with `listened_at > min_ts`,
  ordered descending within each batch (`listens[0]` is newest in batch).

  Args:
    username: ListenBrainz username.
    min_ts: Exclusive lower-bound Unix timestamp.
    token: Optional ListenBrainz user token for higher rate limits.
    count: Page size (max 1000).

  Returns:
    DataFrame of fetched listens.
  """
  rows: List[Dict[str, Any]] = []
  cursor = max(0, int(min_ts))
  page_size = min(count, MAX_ITEMS_PER_GET)

  with _build_session(token) as session:
    while True:
      batch = _fetch_listens_page(
          session, username=username, count=page_size, min_ts=cursor
      )
      if not batch:
        break
      for rec in batch:
        parsed = parse_listen_record(rec)
        if parsed is not None:
          rows.append(parsed)
      if len(batch) < page_size:
        break
      newest_in_batch = int(batch[0]['listened_at'])
      if newest_in_batch <= cursor:
        break
      cursor = (
          newest_in_batch - 1
          if newest_in_batch - 1 > cursor
          else newest_in_batch
      )

  logger.info(
      'Fetched %d incremental listens from ListenBrainz (min_ts=%d)',
      len(rows),
      min_ts,
  )
  return _empty_scrobbles_df() if not rows else pd.DataFrame(rows)


def fetch_listens_full(
    username: str,
    token: str = '',
    count: int = MAX_ITEMS_PER_GET,
    max_pages: int = 0,
) -> pd.DataFrame:
  """Fetches complete listen history backward via `max_ts` pagination.

  Args:
    username: ListenBrainz username.
    token: Optional ListenBrainz user token.
    count: Page size (max 1000).
    max_pages: Optional page cap (0 fetches all pages).

  Returns:
    DataFrame of fetched listens.
  """
  rows: List[Dict[str, Any]] = []
  cursor: Optional[int] = None
  page_size = min(count, MAX_ITEMS_PER_GET)
  pages_fetched = 0

  with _build_session(token) as session:
    while True:
      batch = _fetch_listens_page(
          session, username=username, count=page_size, max_ts=cursor
      )
      pages_fetched += 1
      if not batch:
        break
      for rec in batch:
        parsed = parse_listen_record(rec)
        if parsed is not None:
          rows.append(parsed)
      if len(batch) < page_size or (max_pages and pages_fetched >= max_pages):
        break
      oldest_in_batch = int(batch[-1]['listened_at'])
      if cursor is not None and oldest_in_batch >= cursor:
        break
      cursor = (
          oldest_in_batch + 1
          if cursor is not None and oldest_in_batch + 1 < cursor
          else oldest_in_batch
      )

  logger.info(
      'Fetched %d full-history listens across %d pages',
      len(rows),
      pages_fetched,
  )
  return _empty_scrobbles_df() if not rows else pd.DataFrame(rows)


def merge_missing_dated_backfill(
    base_df: pd.DataFrame,
    missing_dated_tsv: Optional[str] = None,
) -> pd.DataFrame:
  """Merges dated Last.fm importer gaps not yet present in `base_df`.

  Matched by timestamp multiplicity so that once the backfill script submits
  those rows to ListenBrainz, zero duplicate listens are produced.

  Args:
    base_df: Primary ListenBrainz DataFrame.
    missing_dated_tsv: Optional path to `lastfm_missing_dated_backfill.tsv`.

  Returns:
    Unified, deduplicated, chronologically sorted dated DataFrame.
  """
  resolved_tsv = missing_dated_tsv or find_default_backfill_tsv()
  parts: List[pd.DataFrame] = [
      base_df[['artist', 'album', 'title', 'timestamps']].copy()
      if not base_df.empty
      else pd.DataFrame(columns=['artist', 'album', 'title', 'timestamps'])
  ]

  if resolved_tsv and os.path.exists(resolved_tsv):
    gap_df = pd.read_csv(
        resolved_tsv,
        sep='\t',
        dtype={'artist': str, 'album': str, 'title': str},
        keep_default_na=False,
    )
    gap_df['timestamps'] = gap_df['timestamps'].astype(int)
    gap_df = gap_df[gap_df['timestamps'] >= MIN_VALID_UTS]
    existing_counts = Counter(int(ts) for ts in parts[0]['timestamps'])
    keep_indices: List[int] = []
    for idx, ts in gap_df['timestamps'].items():
      ts_int = int(ts)
      if existing_counts.get(ts_int, 0) > 0:
        existing_counts[ts_int] -= 1
      else:
        keep_indices.append(idx)
    if keep_indices:
      needed_gaps = gap_df.loc[
          keep_indices, ['artist', 'album', 'title', 'timestamps']
      ]
      logger.info(
          'Merged %d/%d dated backfill rows not yet on ListenBrainz',
          len(needed_gaps),
          len(gap_df),
      )
      parts.append(needed_gaps)

  return finalize_scrobbles_df(pd.concat(parts, ignore_index=True))


def finalize_scrobbles_df(df: pd.DataFrame) -> pd.DataFrame:
  """Normalizes, drops undated legacy rows, deduplicates, and sorts listens."""
  if df.empty:
    return _empty_scrobbles_df()
  out = df[['artist', 'album', 'title', 'timestamps']].copy()
  for col in ('artist', 'album', 'title'):
    out[col] = out[col].fillna('').astype(str)
  out['timestamps'] = (
      pd.to_numeric(out['timestamps'], errors='coerce')
      .fillna(0)
      .astype('int64')
  )
  out = out[out['timestamps'] >= MIN_VALID_UTS]
  if out.empty:
    return _empty_scrobbles_df()
  out = out.drop_duplicates(subset=['artist', 'album', 'title', 'timestamps'])
  out['datetime'] = _parse_timestamps(out['timestamps'])
  return out.sort_values(
      by=['datetime', 'timestamps'],
      ascending=[True, True],
      na_position='last',
  ).reset_index(drop=True)


def get_scrobbles(
    username: str = DEFAULT_USER_NAME,
    token: str = DEFAULT_TOKEN,
    existing_tsv: Optional[str] = None,
    overlap_days: int = DEFAULT_OVERLAP_DAYS,
    full_api: bool = False,
    include_backfill: bool = True,
    missing_dated_tsv: Optional[str] = None,
) -> pd.DataFrame:
  """Builds up-to-date scrobble history from local TSV cache + incremental API.

  Args:
    username: ListenBrainz username.
    token: Optional ListenBrainz user token.
    existing_tsv: Existing local scrobbles TSV for fast incremental updates.
    overlap_days: Lookback window in days for incremental `min_ts` polling.
    full_api: If True, re-fetch entire history from API via `max_ts`.
    include_backfill: Merge any un-ingested dated Last.fm gap rows.
    missing_dated_tsv: Optional explicit path to missing dated backfill TSV.

  Returns:
    Complete chronologically sorted scrobble DataFrame.
  """
  base_df = _empty_scrobbles_df()
  try:
    if full_api:
      base_df = fetch_listens_full(username=username, token=token)
    else:
      if existing_tsv and os.path.exists(existing_tsv):
        logger.info('Loading existing scrobbles from %s', existing_tsv)
        base_df = pd.read_csv(
            existing_tsv,
            sep='\t',
            dtype={'artist': str, 'album': str, 'title': str},
            keep_default_na=False,
        )
        base_df = finalize_scrobbles_df(base_df)
      else:
        logger.info('No local TSV found; running full API fetch')
        base_df = fetch_listens_full(username=username, token=token)

      if not base_df.empty:
        max_ts = int(base_df['timestamps'].max())
        min_ts = max(0, max_ts - int(overlap_days) * 86400)
      else:
        min_ts = 0

      inc_df = fetch_listens_incremental(
          username=username, min_ts=min_ts, token=token
      )
      if not inc_df.empty:
        base_df = finalize_scrobbles_df(
            pd.concat(
                [
                    base_df[['artist', 'album', 'title', 'timestamps']],
                    inc_df[['artist', 'album', 'title', 'timestamps']],
                ],
                ignore_index=True,
            )
        )
  except Exception as exc:
    logger.warning('ListenBrainz API fetch failed (%s)', exc)
    if not base_df.empty:
      logger.warning('Proceeding with %d cached/seeded listens', len(base_df))
    elif existing_tsv and os.path.exists(existing_tsv):
      base_df = finalize_scrobbles_df(
          pd.read_csv(existing_tsv, sep='\t', keep_default_na=False)
      )
    else:
      raise

  if include_backfill:
    return merge_missing_dated_backfill(
        base_df, missing_dated_tsv=missing_dated_tsv
    )
  return finalize_scrobbles_df(base_df)


def compute_scrobble_playcounts(
    scrobbles_df: pd.DataFrame, case_fold: bool = True
) -> pd.DataFrame:
  """Aggregates scrobble rows into per-track playcount counts.

  Args:
    scrobbles_df: DataFrame with `artist`, `album`, `title` columns.
    case_fold: Fold case-only variations (`Axis: Bold As Love` vs `as Love`)
      into the most frequent canonical spelling.

  Returns:
    DataFrame with columns `['artist', 'album', 'title', 'playcount']` sorted
    descending by `playcount` with a `0..N-1` `RangeIndex`.
  """
  if scrobbles_df.empty:
    return pd.DataFrame(columns=['artist', 'album', 'title', 'playcount'])

  exact_keys = (
      scrobbles_df['artist'].fillna('')
      + SCROBBLE_SEP
      + scrobbles_df['album'].fillna('')
      + SCROBBLE_SEP
      + scrobbles_df['title'].fillna('')
  )
  exact_counts = exact_keys.value_counts()
  entries = pd.DataFrame(
      data=exact_counts.index.str.split(SCROBBLE_SEP, n=2).tolist(),
      columns=['artist', 'album', 'title'],
  )
  entries['playcount'] = exact_counts.values

  if not case_fold:
    return entries.reset_index(drop=True)

  entries['_fold_key'] = (
      entries['artist'].str.lower()
      + SCROBBLE_SEP
      + entries['album'].str.lower()
      + SCROBBLE_SEP
      + entries['title'].str.lower()
  )
  folded = (
      entries.groupby('_fold_key', sort=False)
      .agg({
          'artist': 'first',
          'album': 'first',
          'title': 'first',
          'playcount': 'sum',
      })
      .sort_values(by='playcount', ascending=False)
      .reset_index(drop=True)
  )
  return folded


def compute_top_albums(playcounts_ingested_df: pd.DataFrame) -> pd.DataFrame:
  """Aggregates track playcounts by `fuzzy_album_id`."""
  if playcounts_ingested_df.empty:
    empty = pd.DataFrame(
        columns=['total_playcount', 'mean_playcount', 'artist', 'album']
    )
    empty.index.name = 'fuzzy_album_id'
    return empty
  grouped = playcounts_ingested_df.groupby('fuzzy_album_id').agg({
      'playcount': ['sum', 'mean'],
      'artist': 'first',
      'album': 'first',
  })
  grouped.columns = ['total_playcount', 'mean_playcount', 'artist', 'album']
  return grouped.sort_values(by='total_playcount', ascending=False)


def _atomic_write_tsv(
    df: pd.DataFrame, target_path: str, index: bool, force: bool = False
) -> None:
  """Atomically writes `df` to `target_path` with a row-count shrink guard."""
  os.makedirs(os.path.dirname(os.path.abspath(target_path)), exist_ok=True)
  if not force and os.path.exists(target_path):
    try:
      with open(target_path, encoding='utf-8') as fh:
        prev_rows = sum(1 for _ in fh) - 1
      if prev_rows > 100 and len(df) < int(prev_rows * SHRINK_GUARD_RATIO):
        raise RuntimeError(
            f'Shrink guard triggered for {target_path}: '
            f'new={len(df):,} < {SHRINK_GUARD_RATIO:.0%} of prev={prev_rows:,}.'
            ' Pass --force to override.'
        )
    except OSError:
      pass

  tmp_path = f'{target_path}.tmp'
  df.to_csv(tmp_path, sep='\t', index=index, encoding='utf-8')
  os.replace(tmp_path, target_path)


def write_all_outputs(
    scrobbles_df: pd.DataFrame,
    scrobbles_tsv: str,
    playcounts_tsv: str,
    top_albums_tsv: str,
    case_fold: bool = True,
    force: bool = False,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
  """Writes canonical scrobbles, playcounts, and top_albums TSV contracts."""
  _atomic_write_tsv(scrobbles_df, scrobbles_tsv, index=False, force=force)

  playcounts_df = compute_scrobble_playcounts(
      scrobbles_df, case_fold=case_fold
  )
  # CRITICAL: downstream `uni.ingest_lastfm_playcounts` & `calibration_engine`
  # read `playcounts_tsv` with `index_col=0` (unnamed RangeIndex).
  _atomic_write_tsv(playcounts_df, playcounts_tsv, index=True, force=force)

  ingested = _ingest_playcounts(playcounts_tsv)
  top_albums_df = compute_top_albums(ingested)
  _atomic_write_tsv(top_albums_df, top_albums_tsv, index=True, force=force)
  return playcounts_df, top_albums_df


def format_comparison_report(
    lb_scrobbles: pd.DataFrame,
    lb_playcounts: pd.DataFrame,
    lb_top_albums: pd.DataFrame,
    lfm_scrobbles_path: str = SCROBBLES_TSV,
    lfm_top_albums_path: str = LASTFM_TOP_ALBUMS,
) -> str:
  """Generates a side-by-side concordance summary against Last.fm TSVs."""
  if not os.path.exists(lfm_scrobbles_path):
    return f'Comparison skipped: {lfm_scrobbles_path} not found.'

  lfm_scrobbles = pd.read_csv(
      lfm_scrobbles_path,
      sep='\t',
      dtype={'artist': str, 'album': str, 'title': str},
      keep_default_na=False,
  )
  lfm_scrobbles['timestamps'] = lfm_scrobbles['timestamps'].astype(int)

  lb_dated = lb_scrobbles[lb_scrobbles['timestamps'] >= MIN_VALID_UTS]
  lfm_dated = lfm_scrobbles[lfm_scrobbles['timestamps'] >= MIN_VALID_UTS]
  lfm_undated = len(lfm_scrobbles) - len(lfm_dated)

  shared_cutoff = min(
      int(lb_dated['timestamps'].max()) if not lb_dated.empty else 0,
      int(lfm_dated['timestamps'].max()) if not lfm_dated.empty else 0,
  )
  recent_min = shared_cutoff - 7 * 86400
  lb_recent_7d = len(
      lb_dated[
          (lb_dated['timestamps'] > recent_min)
          & (lb_dated['timestamps'] <= shared_cutoff)
      ]
  )
  lfm_recent_7d = len(
      lfm_dated[
          (lfm_dated['timestamps'] > recent_min)
          & (lfm_dated['timestamps'] <= shared_cutoff)
      ]
  )

  lb_top20_artists = set(lb_dated['artist'].value_counts().head(20).index)
  lfm_top20_artists = set(lfm_dated['artist'].value_counts().head(20).index)
  artist_overlap = len(lb_top20_artists & lfm_top20_artists)

  album_overlap_str = 'N/A'
  if os.path.exists(lfm_top_albums_path):
    lfm_albums = pd.read_csv(lfm_top_albums_path, sep='\t', index_col=0)
    lb_top20_alb = set(lb_top_albums.head(20).index)
    lfm_top20_alb = set(lfm_albums.head(20).index)
    album_overlap_str = f'{len(lb_top20_alb & lfm_top20_alb)}/20'

  lines = [
      '=== ListenBrainz vs Last.fm Dated Concordance Summary ===',
      f'Dated Scrobbles:   LB={len(lb_dated):>9,} | '
      f'LFM_Dated={len(lfm_dated):>9,} '
      f'(delta={len(lb_dated) - len(lfm_dated):+,})',
      f'Ignored LFM Undated Bug Rows: {lfm_undated:,} (dropped cleanly)',
      f'Unique Playcount Rows: LB={len(lb_playcounts):,}',
      f'Shared 7d Window (<= {shared_cutoff}): '
      f'LB={lb_recent_7d:,} | LFM={lfm_recent_7d:,} '
      f'(delta={lb_recent_7d - lfm_recent_7d:+,})',
      f'Dated Top-20 Artist Overlap: {artist_overlap}/20 | '
      f'Top-20 Album Overlap (vs all LFM): {album_overlap_str}',
  ]
  return '\n'.join(lines)


def _log_dropin_overview(
    scrobbles_df: pd.DataFrame,
    playcounts_df: pd.DataFrame,
    top_albums_df: pd.DataFrame,
) -> None:
  """Logs the standard overview tables matching `lastfm_scrape_history.py`."""
  logger.info(
      'Total Scrobbles: %s | Artists: %s | Albums: %s | Tracks: %s',
      f'{len(scrobbles_df):,}',
      f"{scrobbles_df['artist'].nunique():,}",
      f"{scrobbles_df['album'].nunique():,}",
      f"{scrobbles_df['title'].nunique():,}",
  )
  if not scrobbles_df.empty:
    logger.info(
        'Date Range: %s to %s',
        scrobbles_df['datetime'].min(),
        scrobbles_df['datetime'].max(),
    )
    top_artists = scrobbles_df['artist'].value_counts().head(25).reset_index()
    top_artists.columns = ['artist', 'scrobbles']
    logger.info('Top 25 Artists:\n%s', top_artists.to_string(index=False))
  if not playcounts_df.empty:
    logger.info(
        'Top 25 Tracks:\n%s', playcounts_df.head(25).to_string(index=False)
    )
  if not top_albums_df.empty:
    cols = ['artist', 'album', 'total_playcount', 'mean_playcount']
    logger.info(
        'Top 25 Albums:\n%s',
        top_albums_df[cols].head(25).to_string(index=False),
    )


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
  """Parses command-line arguments."""
  parser = argparse.ArgumentParser(
      description='Scrape ListenBrainz listen history and compute playcounts.'
  )
  parser.add_argument(
      '--username',
      default=DEFAULT_USER_NAME,
      help='ListenBrainz username (default: %(default)s).',
  )
  parser.add_argument(
      '--parallel',
      action='store_true',
      help=(
          'Write side-by-side validation outputs to listenbrainz/tsvs/ '
          'without modifying music-sources-unified/tsvs/lastfm_*.tsv.'
      ),
  )
  parser.add_argument(
      '--write-unified',
      action='store_true',
      help='Explicit alias for default drop-in unified TSV output mode.',
  )
  parser.add_argument(
      '--compare',
      action='store_true',
      default=True,
      help='Print concordance summary in --parallel mode (default: True).',
  )
  parser.add_argument(
      '--no-compare',
      action='store_false',
      dest='compare',
      help='Skip concordance summary in --parallel mode.',
  )
  parser.add_argument(
      '--reseed',
      action='store_true',
      help='Ignore existing target TSV and re-fetch full history from API.',
  )
  parser.add_argument(
      '--full',
      action='store_true',
      help='Re-fetch all historical pages from live ListenBrainz API.',
  )
  parser.add_argument(
      '--overlap-days',
      type=int,
      default=DEFAULT_OVERLAP_DAYS,
      help='Incremental min_ts overlap window in days (default: %(default)s).',
  )
  parser.add_argument(
      '--no-backfill',
      action='store_true',
      help='Exclude local missing-dated Last.fm backfill TSV.',
  )
  parser.add_argument(
      '--no-case-fold',
      action='store_true',
      help='Disable case-insensitive folding in playcount aggregation.',
  )
  parser.add_argument(
      '--force',
      action='store_true',
      help='Bypass TSV shrink-guard safety check when writing outputs.',
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

  pd.set_option('display.max_columns', None)
  pd.set_option('display.width', 1000)
  pd.set_option('display.max_rows', 200)

  args = parse_args(argv)
  t0 = time.time()

  use_parallel = args.parallel and not args.write_unified
  if use_parallel:
    scrobbles_out = LB_SCROBBLES_TSV
    playcounts_out = LB_PLAYCOUNTS_TSV
    top_albums_out = LB_TOP_ALBUMS_TSV
    # Force prevents shrink guard if unified had 299,356 incl. 9,345 undated
    force_write = args.force
  else:
    scrobbles_out = SCROBBLES_TSV
    playcounts_out = PLAYCOUNTS_TSV
    top_albums_out = LASTFM_TOP_ALBUMS
    # First cutover from 299,356 (incl. 9,345 undated) -> 290,468 dated (~97.0%)
    force_write = args.force

  existing_for_load = None if args.reseed else scrobbles_out
  scrobbles_df = get_scrobbles(
      username=args.username,
      token=DEFAULT_TOKEN,
      existing_tsv=existing_for_load,
      overlap_days=args.overlap_days,
      full_api=args.full,
      include_backfill=not args.no_backfill,
  )

  playcounts_df, top_albums_df = write_all_outputs(
      scrobbles_df,
      scrobbles_tsv=scrobbles_out,
      playcounts_tsv=playcounts_out,
      top_albums_tsv=top_albums_out,
      case_fold=not args.no_case_fold,
      force=force_write,
  )

  logger.info(
      'Saved %s dated scrobbles -> %s', f'{len(scrobbles_df):,}', scrobbles_out
  )
  if use_parallel and args.compare:
    report = format_comparison_report(
        scrobbles_df, playcounts_df, top_albums_df
    )
    logger.info('\n%s', report)
  else:
    _log_dropin_overview(scrobbles_df, playcounts_df, top_albums_df)

  logger.info('Completed in %.2f seconds.', time.time() - t0)
  return 0


if __name__ == '__main__':
  sys.exit(main())
