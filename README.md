# ListenBrainz Scraper

Incremental listen history scraper, playcount aggregator, and YouTube Music playcount matcher replacing `lastfm/` ahead of Last.fm's December 1, 2026 API policy change.

---

## Structure

```text
listenbrainz/
├── listenbrainz_scrape_history.py                       # Incremental API scraper & playcount aggregator
├── listenbrainz_scrape_history_test.py                  # Unit test suite (CI-ready)
├── match_ytmusic_tracks_with_listenbrainz_playcounts.py # YTMusic playcount matcher & incremental feedback sync
├── match_ytmusic_tracks_with_listenbrainz_playcounts_test.py # Unit tests for matcher & incremental feedback
├── auth.py                                              # Local ListenBrainz credentials (gitignored)
├── requirements.txt                                     # Python dependencies
├── README.md                                            # Module documentation
└── old/                                                 # Local migration archive & one-off scripts (gitignored)
```

---

## Scripts

### `listenbrainz_scrape_history.py`

Drop-in replacement for `lastfm/lastfm_scrape_history.py`. Polls incremental listens via `min_ts` (`count=1000`) in ~4.5 seconds/week, computes case-folded per-track playcounts and fuzzy album playcounts, and atomically updates downstream TSV contracts in `../music-sources-unified/tsvs/`.

```bash
# Drop-in unified mode (default — writes ../music-sources-unified/tsvs/lastfm_*.tsv)
python listenbrainz_scrape_history.py

# Parallel validation mode (writes listenbrainz/tsvs/listenbrainz_*.tsv + concordance report)
python listenbrainz_scrape_history.py --parallel
```

### `match_ytmusic_tracks_with_listenbrainz_playcounts.py`

Drop-in replacement for `lastfm/match_ytmusic_tracks_with_lastfm_playcounts.py`. Matches YouTube Music tracks against `lastfm_playcounts.tsv` using an O(1) persistent ID cache plus vectorized RapidFuzz batch matching (90-day run interval, override with `--force`), plus a lightweight incremental YTMusic likes/dislikes feedback sync.

```bash
python match_ytmusic_tracks_with_listenbrainz_playcounts.py
python match_ytmusic_tracks_with_listenbrainz_playcounts.py --force
```

---

## Authentication, Setup

Create `auth.py` (gitignored) with your username and 36-character UUID User Token from `https://listenbrainz.org/settings/`:

```python
LISTENBRAINZ_USER_NAME = 'your-username'
LISTENBRAINZ_TOKEN = 'your-36-char-listenbrainz-user-token'
```

Install dependencies and run tests:

```bash
pip install -r requirements.txt
python -m unittest discover -p "*_test.py"
```

