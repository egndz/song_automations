# System Improvement Roadmap

Trackable checklist for song_automations improvements.

---

## Phase 1: Reliability Foundation (CRITICAL)

- [ ] Add `tenacity` dependency to pyproject.toml
- [ ] Create retry decorator with exponential backoff (3 attempts, 2-30s wait)
- [ ] Add explicit timeouts to httpx calls: `httpx.Timeout(30.0, connect=10.0)`
- [ ] Replace `except Exception: pass` with specific error handling in spotify.py:142
- [ ] Add rate limit detection (429 status) with `Retry-After` header handling

**Files:** `soundcloud.py`, `spotify.py`, `pyproject.toml`

---

## Phase 2: SoundCloud Connection Pooling (CRITICAL)

- [ ] Create persistent `httpx.Client` in `__init__` with connection limits
- [ ] Add `close()` method and context manager protocol (`__enter__`/`__exit__`)
- [ ] Replace all 8 `with httpx.Client()` calls with `self.http_client`
- [ ] Implement token refresh using stored `refresh_token`

**File:** `soundcloud.py`

---

## Phase 3: Spotify API Optimization (HIGH)

- [ ] Batch artist lookups: collect unique IDs, use `self._client.artists(ids)`
- [ ] Add playlist cache with 5-minute TTL
- [ ] Add proper exception handling for artist lookup failures

**File:** `spotify.py`

---

## Phase 4: Database Optimization (MEDIUM)

- [ ] Add composite index: `matched_tracks(discogs_release_id, discogs_track_position, destination)`
- [ ] Add index on `folder_releases(discogs_folder_id)`
- [ ] Convert `update_folder_releases()` to use `executemany()`
- [ ] Add optional cache TTL parameter to `get_cached_match()` (default: 30 days)

**File:** `tracker.py`

---

## Phase 5: Concurrent Release Processing (MEDIUM)

- [ ] Add `ThreadPoolExecutor` for processing releases (max_workers=4)
- [ ] Add early exit in `_find_track_match()` when confidence >= 0.95
- [ ] Add configurable concurrency setting in `config.py`

**File:** `engine.py`

---

## Phase 6: Matching Algorithm Tuning (MEDIUM)

- [ ] Rebalance weights: artist=0.45, title=0.35, verified=0.10, popularity=0.10
- [ ] Make weights configurable via Settings
- [ ] Add version-string matching bonus (+10% if remix info matches)

**Files:** `fuzzy.py`, `config.py`

---

## Phase 7: Code Quality Cleanup (LOW)

- [ ] Extract `_create_sync_command()` factory to eliminate CLI duplication
- [ ] Remove or implement unused `medium_confidence` setting
- [ ] Remove or implement unused `log_level` setting

**Files:** `cli.py`, `config.py`

---

## Phase 8: Structured Logging (LOW)

- [ ] Create `src/song_automations/logging.py`
- [ ] Add Python `logging` module with file handler
- [ ] Log API calls, errors, match decisions
- [ ] Use `log_level` from Settings

---

## Phase 9: SoundCloud Matching Fix (CRITICAL)

- [ ] Investigate SoundCloud search API response format
- [ ] Verify track parsing extracts correct fields (title vs name)
- [ ] Compare scoring for same track on Spotify vs SoundCloud
- [ ] Check if search query formatting differs for SoundCloud
- [ ] Fix any discrepancies found

**Files:** `soundcloud.py`, `engine.py`, `fuzzy.py`

---

## Priority Order

| Phase | Priority | Effort | Impact |
|-------|----------|--------|--------|
| 9. SoundCloud Fix | CRITICAL | Medium | High |
| 1. Reliability | CRITICAL | Medium | High |
| 2. SoundCloud Pooling | CRITICAL | Low | High |
| 3. Spotify Optimization | HIGH | Medium | High |
| 4. Database Optimization | MEDIUM | Low | Medium |
| 5. Concurrent Processing | MEDIUM | High | High |
| 6. Matching Tuning | MEDIUM | Medium | Medium |
| 7. Code Quality | LOW | Low | Low |
| 8. Logging | LOW | Low | Medium |

---

## Verification Commands

```bash
# Run tests
pytest tests/ -v

# Integration test
song-automations sync all --dry-run

# Performance benchmark
time song-automations sync spotify
time song-automations sync soundcloud
```
