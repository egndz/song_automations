'use strict';

/**
 * Seller Radio player.
 *
 * Reads a crate export (window.CRATE or GET /api/crate), filters it entirely on the
 * client, and plays the surviving releases through the YouTube IFrame API. Dead,
 * region locked and embed disabled videos are blacklisted in localStorage and skipped
 * automatically so the radio never goes silent.
 */

const GRADE_ORDER = ['P', 'F', 'G', 'G+', 'VG', 'VG+', 'NM', 'M'];
const GRADE_RANK = GRADE_ORDER.reduce(function (acc, g, i) {
  acc[g] = i;
  return acc;
}, {});

const LS_FILTERS = 'sellerRadio.filters.v1';
const LS_DEAD = 'sellerRadio.deadVideos.v1';
const LS_UI = 'sellerRadio.ui.v1';

const MAX_ROWS = 400;
const BLANK_IMG =
  'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7';
const FATAL_ERROR_CODES = [2, 5, 100, 101, 150];
const ADVANCE_DELAY_MS = 180;
const TICK_MS = 500;
const PLAYER_WATCHDOG_MS = 9000;

const MISSING_IDS = [];

/**
 * Look up an element by id and record the id when the markup does not provide it.
 *
 * @param {string} id Element id to resolve.
 * @returns {HTMLElement|null} The element, or null when absent.
 */
function el(id) {
  const node = document.getElementById(id);
  if (!node && MISSING_IDS.indexOf(id) === -1) {
    MISSING_IDS.push(id);
  }
  return node;
}

const dom = {
  serverBadge: el('server-badge'),
  status: el('status'),
  nowThumb: el('now-thumb'),
  nowArtist: el('now-artist'),
  nowTitle: el('now-title'),
  nowLabel: el('now-label'),
  nowMeta: el('now-meta'),
  nowLink: el('now-link'),
  wantBtn: el('want-btn'),
  videoWrap: el('video-wrap'),
  playerMount: el('player'),
  elapsed: el('elapsed'),
  seek: el('seek'),
  duration: el('duration'),
  prev: el('prev'),
  playPause: el('play-pause'),
  next: el('next'),
  shuffle: el('shuffle'),
  toggleVideo: el('toggle-video'),
  queuePos: el('queue-pos'),
  queueLen: el('queue-len'),
  search: el('f-search'),
  filterCount: el('filter-count'),
  genre: el('f-genre'),
  style: el('f-style'),
  country: el('f-country'),
  label: el('f-label'),
  format: el('f-format'),
  yearFrom: el('f-year-from'),
  yearTo: el('f-year-to'),
  exGenre: el('f-exclude-genre'),
  exStyle: el('f-exclude-style'),
  exCountry: el('f-exclude-country'),
  exLabel: el('f-exclude-label'),
  exFormat: el('f-exclude-format'),
  seller: el('f-seller'),
  priceMin: el('f-price-min'),
  priceMax: el('f-price-max'),
  mediaGrade: el('f-media-grade'),
  sleeveGrade: el('f-sleeve-grade'),
  shipsFrom: el('f-ships-from'),
  source: el('f-source'),
  wantRatio: el('f-want-ratio'),
  ratingMin: el('f-rating-min'),
  haveMin: el('f-have-min'),
  excludeOwned: el('f-exclude-owned'),
  wantlistOnly: el('f-wantlist-only'),
  applyBtn: el('apply-filters'),
  resetBtn: el('reset-filters'),
  tracklist: el('tracklist')
};

/**
 * Warn loudly when the markup and the script disagree about an element id.
 *
 * A silently missing control is indistinguishable from a filter that does nothing, so the
 * mismatch is surfaced instead of being swallowed.
 *
 * @returns {void}
 */
function reportMissingIds() {
  if (!MISSING_IDS.length) {
    return;
  }
  if (window.console && typeof window.console.warn === 'function') {
    window.console.warn('Seller Radio: index.html is missing these ids: ' + MISSING_IDS.join(', '));
  }
}

const IS_FILE = location.protocol === 'file:';
const CAN_TALK_TO_SERVER = location.protocol === 'http:' || location.protocol === 'https:';

const state = {
  all: [],
  meta: null,
  queue: [],
  order: [],
  pos: -1,
  current: null,
  currentVideoIndex: 0,
  currentVideoId: null,
  loadToken: 0,
  failStreak: 0,
  stopped: false,
  shuffle: false,
  seed: 1,
  videoVisible: false,
  playerReady: false,
  pendingPlay: null,
  seeking: false,
  hasServer: false,
  dead: new Set()
};

let player = null;
let tickTimer = null;

/**
 * Read and parse a JSON value from localStorage.
 *
 * @param {string} key Storage key.
 * @param {*} fallback Value returned when the key is absent or unreadable.
 * @returns {*} The parsed value or the fallback.
 */
function readStore(key, fallback) {
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) {
      return fallback;
    }
    const parsed = JSON.parse(raw);
    return parsed === null || parsed === undefined ? fallback : parsed;
  } catch (err) {
    return fallback;
  }
}

/**
 * Serialise a value into localStorage, ignoring quota and privacy mode failures.
 *
 * @param {string} key Storage key.
 * @param {*} value JSON serialisable value.
 * @returns {void}
 */
function writeStore(key, value) {
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
  } catch (err) {
    return;
  }
}

/**
 * Write a short message into the header status region.
 *
 * @param {string} text Message to display.
 * @returns {void}
 */
function setStatus(text) {
  if (dom.status) {
    dom.status.textContent = text;
  }
}

/**
 * Update the server badge to reflect whether the local API is reachable.
 *
 * @param {boolean} online True when the local server answered.
 * @returns {void}
 */
function setServerBadge(online) {
  if (!dom.serverBadge) {
    return;
  }
  dom.serverBadge.textContent = online ? 'server' : 'offline';
  dom.serverBadge.classList.toggle('badge-on', online);
  dom.serverBadge.classList.toggle('badge-off', !online);
}

/**
 * Build a deterministic pseudo random generator.
 *
 * @param {number} seed Non zero integer seed.
 * @returns {function(): number} Generator returning floats in the range zero to one.
 */
function mulberry32(seed) {
  let a = seed >>> 0;
  return function () {
    a += 0x6d2b79f5;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/**
 * Format a duration in seconds as minutes and seconds.
 *
 * @param {number} seconds Duration in seconds.
 * @returns {string} Formatted clock string.
 */
function formatTime(seconds) {
  const total = Number.isFinite(seconds) && seconds > 0 ? Math.floor(seconds) : 0;
  const mins = Math.floor(total / 60);
  const secs = total % 60;
  return mins + ':' + (secs < 10 ? '0' + secs : String(secs));
}

/**
 * Format a listing price for display.
 *
 * @param {number|null} value Price amount.
 * @param {string|null} currency Currency abbreviation.
 * @returns {string} Formatted price, or an empty string when there is no price.
 */
function formatPrice(value, currency) {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return '';
  }
  return value.toFixed(2) + ' ' + (currency || '');
}

/**
 * Coerce a form field value into a finite number.
 *
 * @param {HTMLInputElement|null} node Input element to read.
 * @returns {number|null} The parsed number, or null when blank or invalid.
 */
function numOrNull(node) {
  if (!node) {
    return null;
  }
  const raw = String(node.value || '').trim();
  if (raw === '') {
    return null;
  }
  const parsed = Number(raw);
  return Number.isFinite(parsed) ? parsed : null;
}

/**
 * Collect the selected values of a multiple select.
 *
 * @param {HTMLSelectElement|null} node Select element to read.
 * @returns {string[]} Selected option values.
 */
function selectedValues(node) {
  if (!node) {
    return [];
  }
  const out = [];
  for (let i = 0; i < node.options.length; i += 1) {
    if (node.options[i].selected) {
      out.push(node.options[i].value);
    }
  }
  return out;
}

/**
 * Restore a saved selection on a multiple select.
 *
 * @param {HTMLSelectElement|null} node Select element to write.
 * @param {string[]} values Values that should be selected.
 * @returns {void}
 */
function setSelectedValues(node, values) {
  if (!node) {
    return;
  }
  const wanted = new Set(Array.isArray(values) ? values : []);
  for (let i = 0; i < node.options.length; i += 1) {
    node.options[i].selected = wanted.has(node.options[i].value);
  }
}

/**
 * Rank a Discogs media or sleeve grade.
 *
 * @param {string|null} grade Short grade code such as VG+.
 * @returns {number} Ordinal position, or minus one when the grade is unknown.
 */
function gradeRank(grade) {
  if (!grade) {
    return -1;
  }
  const rank = GRADE_RANK[grade];
  return rank === undefined ? -1 : rank;
}

/**
 * Fetch the crate payload, preferring the live server over the bundled export.
 *
 * @returns {Promise<Object|null>} The crate object, or null when nothing is available.
 */
async function loadCrate() {
  const bundled =
    window.CRATE && Array.isArray(window.CRATE.tracks) ? window.CRATE : null;
  if (!CAN_TALK_TO_SERVER) {
    return bundled;
  }
  try {
    const res = await fetch('/api/crate', { headers: { Accept: 'application/json' } });
    if (!res.ok) {
      return bundled;
    }
    const data = await res.json();
    if (data && Array.isArray(data.tracks)) {
      state.hasServer = true;
      return data;
    }
    return bundled;
  } catch (err) {
    return bundled;
  }
}

/**
 * Normalise one crate track and precompute the fields used by filtering and search.
 *
 * @param {Object} raw Track object from the crate export.
 * @returns {Object} The same object with derived helper fields attached.
 */
function prepareTrack(raw) {
  const t = raw;
  t.genres = Array.isArray(t.genres) ? t.genres : [];
  t.styles = Array.isArray(t.styles) ? t.styles : [];
  t.formats = Array.isArray(t.formats) ? t.formats : [];
  t.format_descriptions = Array.isArray(t.format_descriptions) ? t.format_descriptions : [];
  t.videos = Array.isArray(t.videos) ? t.videos : [];
  t.owned = Boolean(t.owned);
  t.wanted = Boolean(t.wanted);
  const formatSet = new Set();
  t.formats.forEach(function (v) {
    formatSet.add(v);
  });
  t.format_descriptions.forEach(function (v) {
    formatSet.add(v);
  });
  t._formats = Array.from(formatSet);
  t._search = [t.artist, t.title, t.label, t.catno]
    .filter(function (v) {
      return v;
    })
    .join(' ')
    .toLowerCase();
  return t;
}

/**
 * Count facet values across the crate for the dynamic multi selects.
 *
 * @param {Object[]} tracks Prepared tracks.
 * @returns {Object} Map of facet name to a Map of value and count.
 */
function buildFacets(tracks) {
  const facets = {
    genre: new Map(),
    style: new Map(),
    country: new Map(),
    label: new Map(),
    format: new Map(),
    seller: new Map(),
    shipsFrom: new Map()
  };

  /**
   * Increment a facet bucket.
   *
   * @param {Map} bucket Facet map being counted.
   * @param {string|null} value Value to count.
   * @returns {void}
   */
  function bump(bucket, value) {
    if (value === null || value === undefined || value === '') {
      return;
    }
    bucket.set(value, (bucket.get(value) || 0) + 1);
  }

  tracks.forEach(function (t) {
    t.genres.forEach(function (v) {
      bump(facets.genre, v);
    });
    t.styles.forEach(function (v) {
      bump(facets.style, v);
    });
    t._formats.forEach(function (v) {
      bump(facets.format, v);
    });
    bump(facets.country, t.country);
    bump(facets.label, t.label);
    bump(facets.seller, t.seller);
    bump(facets.shipsFrom, t.ships_from);
  });

  return facets;
}

/**
 * Replace the options of a select with sorted facet values and their counts.
 *
 * @param {HTMLSelectElement|null} node Select element to fill.
 * @param {Map} bucket Facet map of value to count.
 * @returns {void}
 */
function fillSelect(node, bucket) {
  if (!node) {
    return;
  }
  const entries = Array.from(bucket.entries()).sort(function (a, b) {
    return a[0].localeCompare(b[0], undefined, { sensitivity: 'base' });
  });
  const frag = document.createDocumentFragment();
  entries.forEach(function (entry) {
    const opt = document.createElement('option');
    opt.value = entry[0];
    opt.textContent = entry[0] + ' (' + entry[1] + ')';
    frag.appendChild(opt);
  });
  node.textContent = '';
  node.appendChild(frag);
}

/**
 * Populate every dynamic select from the crate contents.
 *
 * @param {Object[]} tracks Prepared tracks.
 * @returns {void}
 */
function populateFilterOptions(tracks) {
  const facets = buildFacets(tracks);
  fillSelect(dom.genre, facets.genre);
  fillSelect(dom.style, facets.style);
  fillSelect(dom.country, facets.country);
  fillSelect(dom.label, facets.label);
  fillSelect(dom.format, facets.format);
  fillSelect(dom.exGenre, facets.genre);
  fillSelect(dom.exStyle, facets.style);
  fillSelect(dom.exCountry, facets.country);
  fillSelect(dom.exLabel, facets.label);
  fillSelect(dom.exFormat, facets.format);
  fillSelect(dom.seller, facets.seller);
  fillSelect(dom.shipsFrom, facets.shipsFrom);
}

/**
 * Read the current filter state from the form controls.
 *
 * @returns {Object} Plain filter state object.
 */
function readFilters() {
  return {
    search: dom.search ? String(dom.search.value || '').trim().toLowerCase() : '',
    genre: selectedValues(dom.genre),
    style: selectedValues(dom.style),
    country: selectedValues(dom.country),
    label: selectedValues(dom.label),
    format: selectedValues(dom.format),
    exGenre: selectedValues(dom.exGenre),
    exStyle: selectedValues(dom.exStyle),
    exCountry: selectedValues(dom.exCountry),
    exLabel: selectedValues(dom.exLabel),
    exFormat: selectedValues(dom.exFormat),
    yearFrom: numOrNull(dom.yearFrom),
    yearTo: numOrNull(dom.yearTo),
    seller: selectedValues(dom.seller),
    priceMin: numOrNull(dom.priceMin),
    priceMax: numOrNull(dom.priceMax),
    mediaGrade: dom.mediaGrade ? dom.mediaGrade.value : '',
    sleeveGrade: dom.sleeveGrade ? dom.sleeveGrade.value : '',
    shipsFrom: selectedValues(dom.shipsFrom),
    source: dom.source ? dom.source.value : '',
    wantRatio: numOrNull(dom.wantRatio),
    ratingMin: numOrNull(dom.ratingMin),
    haveMin: numOrNull(dom.haveMin),
    excludeOwned: dom.excludeOwned ? dom.excludeOwned.checked : false,
    wantlistOnly: dom.wantlistOnly ? dom.wantlistOnly.checked : false
  };
}

/**
 * Push a saved filter state back onto the form controls.
 *
 * @param {Object} f Filter state object.
 * @returns {void}
 */
function writeFilters(f) {
  if (!f) {
    return;
  }
  if (dom.search) {
    dom.search.value = f.search || '';
  }
  setSelectedValues(dom.genre, f.genre);
  setSelectedValues(dom.style, f.style);
  setSelectedValues(dom.country, f.country);
  setSelectedValues(dom.label, f.label);
  setSelectedValues(dom.format, f.format);
  setSelectedValues(dom.exGenre, f.exGenre);
  setSelectedValues(dom.exStyle, f.exStyle);
  setSelectedValues(dom.exCountry, f.exCountry);
  setSelectedValues(dom.exLabel, f.exLabel);
  setSelectedValues(dom.exFormat, f.exFormat);
  if (dom.yearFrom) {
    dom.yearFrom.value = f.yearFrom === null || f.yearFrom === undefined ? '' : f.yearFrom;
  }
  if (dom.yearTo) {
    dom.yearTo.value = f.yearTo === null || f.yearTo === undefined ? '' : f.yearTo;
  }
  setSelectedValues(dom.seller, f.seller);
  if (dom.priceMin) {
    dom.priceMin.value = f.priceMin === null || f.priceMin === undefined ? '' : f.priceMin;
  }
  if (dom.priceMax) {
    dom.priceMax.value = f.priceMax === null || f.priceMax === undefined ? '' : f.priceMax;
  }
  if (dom.mediaGrade) {
    dom.mediaGrade.value = f.mediaGrade || '';
  }
  if (dom.sleeveGrade) {
    dom.sleeveGrade.value = f.sleeveGrade || '';
  }
  setSelectedValues(dom.shipsFrom, f.shipsFrom);
  if (dom.source) {
    dom.source.value = f.source || '';
  }
  if (dom.wantRatio) {
    dom.wantRatio.value = f.wantRatio === null || f.wantRatio === undefined ? '' : f.wantRatio;
  }
  if (dom.ratingMin) {
    dom.ratingMin.value = f.ratingMin === null || f.ratingMin === undefined ? '' : f.ratingMin;
  }
  if (dom.haveMin) {
    dom.haveMin.value = f.haveMin === null || f.haveMin === undefined ? '' : f.haveMin;
  }
  if (dom.excludeOwned) {
    dom.excludeOwned.checked = Boolean(f.excludeOwned);
  }
  if (dom.wantlistOnly) {
    dom.wantlistOnly.checked = Boolean(f.wantlistOnly);
  }
}

/**
 * Clear every filter control back to its neutral value.
 *
 * @returns {void}
 */
function clearFilters() {
  writeFilters({
    search: '',
    genre: [],
    style: [],
    country: [],
    label: [],
    format: [],
    exGenre: [],
    exStyle: [],
    exCountry: [],
    exLabel: [],
    exFormat: [],
    yearFrom: null,
    yearTo: null,
    seller: [],
    priceMin: null,
    priceMax: null,
    mediaGrade: '',
    sleeveGrade: '',
    shipsFrom: [],
    source: '',
    wantRatio: null,
    ratingMin: null,
    haveMin: null,
    excludeOwned: false,
    wantlistOnly: false
  });
}

/**
 * Test whether any member of a list appears in a selection.
 *
 * @param {string[]} values Track values.
 * @param {string[]} selection Selected filter values.
 * @returns {boolean} True when at least one value is selected.
 */
function intersects(values, selection) {
  for (let i = 0; i < values.length; i += 1) {
    if (selection.indexOf(values[i]) !== -1) {
      return true;
    }
  }
  return false;
}

/**
 * Return the videos of a track that are not known to be dead.
 *
 * @param {Object} track Prepared track.
 * @returns {Object[]} Playable video descriptors.
 */
function livingVideos(track) {
  return track.videos.filter(function (v) {
    return v && v.id && !state.dead.has(v.id);
  });
}

/**
 * Decide whether a track survives the current filter state.
 *
 * @param {Object} t Prepared track.
 * @param {Object} f Filter state object.
 * @returns {boolean} True when the track should stay in the queue.
 */
function matches(t, f) {
  if (f.search) {
    const terms = f.search.split(/\s+/);
    for (let i = 0; i < terms.length; i += 1) {
      if (t._search.indexOf(terms[i]) === -1) {
        return false;
      }
    }
  }

  if (f.genre.length && !intersects(t.genres, f.genre)) {
    return false;
  }
  if (f.style.length && !intersects(t.styles, f.style)) {
    return false;
  }
  if (f.format.length && !intersects(t._formats, f.format)) {
    return false;
  }
  if (f.country.length && (!t.country || f.country.indexOf(t.country) === -1)) {
    return false;
  }
  if (f.label.length && (!t.label || f.label.indexOf(t.label) === -1)) {
    return false;
  }

  if (f.exGenre.length && intersects(t.genres, f.exGenre)) {
    return false;
  }
  if (f.exStyle.length && intersects(t.styles, f.exStyle)) {
    return false;
  }
  if (f.exFormat.length && intersects(t._formats, f.exFormat)) {
    return false;
  }
  if (f.exCountry.length && t.country && f.exCountry.indexOf(t.country) !== -1) {
    return false;
  }
  if (f.exLabel.length && t.label && f.exLabel.indexOf(t.label) !== -1) {
    return false;
  }

  if (f.yearFrom !== null && (!t.year || t.year < f.yearFrom)) {
    return false;
  }
  if (f.yearTo !== null && (!t.year || t.year > f.yearTo)) {
    return false;
  }

  if (f.seller.length && (!t.seller || f.seller.indexOf(t.seller) === -1)) {
    return false;
  }
  if (f.priceMin !== null && (t.price === null || t.price === undefined || t.price < f.priceMin)) {
    return false;
  }
  if (f.priceMax !== null && (t.price === null || t.price === undefined || t.price > f.priceMax)) {
    return false;
  }
  if (f.mediaGrade && gradeRank(t.condition_grade) < gradeRank(f.mediaGrade)) {
    return false;
  }
  if (f.sleeveGrade && gradeRank(t.sleeve_grade) < gradeRank(f.sleeveGrade)) {
    return false;
  }
  if (f.shipsFrom.length && (!t.ships_from || f.shipsFrom.indexOf(t.ships_from) === -1)) {
    return false;
  }
  if (f.source && t.source !== f.source) {
    return false;
  }

  if (
    f.wantRatio !== null &&
    (t.want_ratio === null || t.want_ratio === undefined || t.want_ratio < f.wantRatio)
  ) {
    return false;
  }
  if (
    f.ratingMin !== null &&
    (t.rating === null || t.rating === undefined || t.rating < f.ratingMin)
  ) {
    return false;
  }
  if (f.haveMin !== null && (t.have === null || t.have === undefined || t.have < f.haveMin)) {
    return false;
  }

  if (f.excludeOwned && t.owned) {
    return false;
  }
  if (f.wantlistOnly && !t.wanted) {
    return false;
  }

  return true;
}

/**
 * Apply the filter state to the full crate.
 *
 * @param {Object} f Filter state object.
 * @returns {Object[]} Tracks that survive filtering and still have a playable video.
 */
function filterTracks(f) {
  return state.all.filter(function (t) {
    if (livingVideos(t).length === 0) {
      return false;
    }
    return matches(t, f);
  });
}

/**
 * Build the playback order for the current queue.
 *
 * @returns {void}
 */
function buildOrder() {
  const order = state.queue.map(function (_, i) {
    return i;
  });
  if (state.shuffle) {
    const rand = mulberry32(state.seed);
    for (let i = order.length - 1; i > 0; i -= 1) {
      const j = Math.floor(rand() * (i + 1));
      const tmp = order[i];
      order[i] = order[j];
      order[j] = tmp;
    }
  }
  state.order = order;
}

/**
 * Identify a track well enough to find it again after a queue rebuild.
 *
 * @param {Object|null} track Track to key.
 * @returns {string} Stable key for the track.
 */
function trackKey(track) {
  if (!track) {
    return '';
  }
  return String(track.release_id) + ':' + String(track.listing_id === undefined ? '' : track.listing_id);
}

/**
 * Recompute the queue from the filter controls without interrupting playback.
 *
 * @param {boolean} persist True to save the filter state to localStorage.
 * @returns {void}
 */
function rebuildQueue(persist) {
  const f = readFilters();
  if (persist) {
    writeStore(LS_FILTERS, f);
  }
  state.queue = filterTracks(f);
  buildOrder();
  state.stopped = false;

  const key = trackKey(state.current);
  let found = -1;
  if (key) {
    for (let i = 0; i < state.order.length; i += 1) {
      if (trackKey(state.queue[state.order[i]]) === key) {
        found = i;
        break;
      }
    }
  }
  if (found !== -1) {
    state.pos = found;
  } else if (state.pos >= state.order.length) {
    state.pos = state.order.length - 1;
  }

  updateCount();
  renderQueueMeta();
  renderTracklist();
}

/**
 * Update the live matching track count.
 *
 * @returns {void}
 */
function updateCount() {
  if (!dom.filterCount) {
    return;
  }
  const n = state.queue.length;
  dom.filterCount.textContent = n === 1 ? '1 track' : n + ' tracks';
}

/**
 * Update the queue position readout.
 *
 * @returns {void}
 */
function renderQueueMeta() {
  if (dom.queuePos) {
    dom.queuePos.textContent = state.pos >= 0 ? String(state.pos + 1) : '0';
  }
  if (dom.queueLen) {
    dom.queueLen.textContent = String(state.order.length);
  }
}

/**
 * Render an informational row inside the tracklist.
 *
 * @param {string} text Message to show.
 * @returns {void}
 */
function renderEmptyRow(text) {
  if (!dom.tracklist) {
    return;
  }
  dom.tracklist.textContent = '';
  const li = document.createElement('li');
  li.className = 'empty';
  li.textContent = text;
  dom.tracklist.appendChild(li);
}

/**
 * Render the visible slice of the queue as a clickable list.
 *
 * @returns {void}
 */
function renderTracklist() {
  if (!dom.tracklist) {
    return;
  }
  if (!state.all.length) {
    renderEmptyRow('No crate data. Run the export command to build web/crate.js, then reload.');
    return;
  }
  if (!state.order.length) {
    renderEmptyRow('Nothing matches these filters. Loosen them or reset.');
    return;
  }

  const total = state.order.length;
  let start = 0;
  if (total > MAX_ROWS && state.pos >= 0) {
    start = Math.max(0, Math.min(state.pos - Math.floor(MAX_ROWS / 2), total - MAX_ROWS));
  }
  const end = Math.min(total, start + MAX_ROWS);

  const frag = document.createDocumentFragment();
  for (let i = start; i < end; i += 1) {
    const track = state.queue[state.order[i]];
    const li = document.createElement('li');
    li.dataset.pos = String(i);
    if (i === state.pos) {
      li.setAttribute('aria-current', 'true');
    }

    const idx = document.createElement('span');
    idx.className = 'idx';
    idx.textContent = String(i + 1);
    li.appendChild(idx);

    const art = document.createElement('img');
    art.className = 'art';
    art.loading = 'lazy';
    art.alt = '';
    art.src = track.thumb || BLANK_IMG;
    li.appendChild(art);

    const meta = document.createElement('div');
    meta.className = 'meta';
    const artist = document.createElement('div');
    artist.className = 't-artist';
    artist.textContent = track.artist || 'Unknown artist';
    const title = document.createElement('div');
    title.className = 't-title';
    title.textContent = track.title || 'Untitled';
    meta.appendChild(artist);
    meta.appendChild(title);
    li.appendChild(meta);

    const price = document.createElement('span');
    price.className = 't-price';
    price.textContent = formatPrice(track.price, track.currency);
    li.appendChild(price);

    frag.appendChild(li);
  }

  dom.tracklist.textContent = '';
  dom.tracklist.appendChild(frag);

  if (end < total || start > 0) {
    const note = document.createElement('li');
    note.className = 'empty';
    note.textContent =
      'showing ' + (start + 1) + ' to ' + end + ' of ' + total + ', refine filters to narrow';
    dom.tracklist.appendChild(note);
  }
}

/**
 * Highlight the currently playing row, re rendering when it is outside the window.
 *
 * @returns {void}
 */
function highlightCurrentRow() {
  if (!dom.tracklist) {
    return;
  }
  const rows = dom.tracklist.querySelectorAll('li[data-pos]');
  let seen = false;
  rows.forEach(function (row) {
    const isCurrent = Number(row.dataset.pos) === state.pos;
    if (isCurrent) {
      seen = true;
      row.setAttribute('aria-current', 'true');
    } else {
      row.removeAttribute('aria-current');
    }
  });
  if (!seen && state.pos >= 0) {
    renderTracklist();
  }
}

/**
 * Render the now playing card for a track.
 *
 * @param {Object|null} track Track to display, or null to show the idle state.
 * @returns {void}
 */
function renderNowPlaying(track) {
  if (!track) {
    if (dom.nowArtist) {
      dom.nowArtist.textContent = 'No artist';
    }
    if (dom.nowTitle) {
      dom.nowTitle.textContent = 'Nothing queued';
    }
    if (dom.nowLabel) {
      dom.nowLabel.textContent = 'No label';
    }
    if (dom.nowMeta) {
      dom.nowMeta.textContent = 'Load a crate to start digging';
    }
    if (dom.nowThumb) {
      dom.nowThumb.src = BLANK_IMG;
      dom.nowThumb.alt = '';
    }
    if (dom.nowLink) {
      dom.nowLink.href = 'https://www.discogs.com';
      dom.nowLink.textContent = 'Open on Discogs';
    }
    renderWantButton(null);
    return;
  }

  if (dom.nowThumb) {
    dom.nowThumb.src = track.thumb || BLANK_IMG;
    dom.nowThumb.alt = track.thumb ? (track.artist || '') + ' ' + (track.title || '') : '';
  }
  if (dom.nowArtist) {
    dom.nowArtist.textContent = track.artist || 'Unknown artist';
  }
  if (dom.nowTitle) {
    dom.nowTitle.textContent = track.title || 'Untitled';
  }
  if (dom.nowLabel) {
    const labelBits = [];
    if (track.label) {
      labelBits.push(track.label);
    }
    if (track.catno) {
      labelBits.push(track.catno);
    }
    dom.nowLabel.textContent = labelBits.length ? labelBits.join(' / ') : 'No label';
  }

  if (dom.nowMeta) {
    const bits = [];
    if (track.year) {
      bits.push(String(track.year));
    }
    if (track.country) {
      bits.push(track.country);
    }
    const isSellerItem = Boolean(track.seller);
    if (isSellerItem) {
      const price = formatPrice(track.price, track.currency);
      if (price) {
        bits.push(price);
      }
      if (track.condition) {
        bits.push(
          'media ' + track.condition + (track.sleeve ? ', sleeve ' + track.sleeve : '')
        );
      }
      if (track.ships_from) {
        bits.push('ships from ' + track.ships_from);
      }
      bits.push(track.seller);
    }
    if (track.want_ratio !== null && track.want_ratio !== undefined) {
      bits.push('want ratio ' + track.want_ratio.toFixed(2));
    }
    if (track.want !== null && track.want !== undefined && track.have !== null && track.have !== undefined) {
      bits.push(track.want + ' want / ' + track.have + ' have');
    }
    if (track.rating !== null && track.rating !== undefined) {
      bits.push('rated ' + track.rating.toFixed(1));
    }
    dom.nowMeta.textContent = bits.length ? bits.join(' · ') : 'No release metadata';
  }

  if (dom.nowLink) {
    const useListing = Boolean(track.seller && track.listing_uri);
    dom.nowLink.href =
      (useListing ? track.listing_uri : track.discogs_uri) || 'https://www.discogs.com';
    dom.nowLink.textContent = useListing ? 'Open listing' : 'Open on Discogs';
  }

  renderWantButton(track);
}

/**
 * Sync the wantlist button with the current track and the server availability.
 *
 * @param {Object|null} track Track being displayed.
 * @returns {void}
 */
function renderWantButton(track) {
  const btn = dom.wantBtn;
  if (!btn) {
    return;
  }
  if (!track) {
    btn.disabled = true;
    btn.dataset.state = 'none';
    btn.textContent = 'Add to wantlist';
    return;
  }
  if (IS_FILE || !state.hasServer) {
    btn.disabled = true;
    btn.title = IS_FILE
      ? 'Wantlist editing needs the local server. Start it and open the page over http.'
      : 'The local server is not reachable, so the wantlist cannot be edited.';
  } else {
    btn.disabled = false;
    btn.title = 'Toggle this release in your Discogs wantlist';
  }
  btn.dataset.state = track.wanted ? 'wanted' : 'none';
  btn.textContent = track.wanted ? 'In wantlist' : 'Add to wantlist';
}

/**
 * Toggle the current release in the Discogs wantlist through the local server.
 *
 * @returns {Promise<void>} Resolves once the request settles.
 */
async function toggleWantlist() {
  const track = state.current;
  if (!track || IS_FILE || !state.hasServer) {
    return;
  }
  const target = !track.wanted;
  const releaseId = track.release_id;

  state.all.forEach(function (t) {
    if (t.release_id === releaseId) {
      t.wanted = target;
    }
  });
  renderWantButton(track);
  setStatus(target ? 'adding to wantlist' : 'removing from wantlist');

  try {
    const res = await fetch('/api/wantlist/' + encodeURIComponent(String(releaseId)), {
      method: target ? 'POST' : 'DELETE',
      headers: { Accept: 'application/json' }
    });
    if (!res.ok) {
      throw new Error('status ' + res.status);
    }
    setStatus(target ? 'added to wantlist' : 'removed from wantlist');
  } catch (err) {
    state.all.forEach(function (t) {
      if (t.release_id === releaseId) {
        t.wanted = !target;
      }
    });
    renderWantButton(track);
    setStatus('wantlist update failed');
  }
}

/**
 * Mark a video id as unplayable and persist the blacklist.
 *
 * @param {string|null} videoId YouTube video id.
 * @returns {void}
 */
function blacklist(videoId) {
  if (!videoId || state.dead.has(videoId)) {
    return;
  }
  state.dead.add(videoId);
  writeStore(LS_DEAD, Array.from(state.dead));
}

/**
 * Load a queue position into the player.
 *
 * @param {number} pos Index into the playback order.
 * @param {number} videoIndex Index into the playable videos of that track.
 * @returns {void}
 */
function playAt(pos, videoIndex) {
  if (!state.order.length) {
    setStatus('nothing to play');
    return;
  }
  const total = state.order.length;
  let cursor = ((pos % total) + total) % total;
  let track = null;
  let videos = [];
  for (let step = 0; step < total; step += 1) {
    const candidate = state.queue[state.order[cursor]];
    if (candidate) {
      const living = livingVideos(candidate);
      if (living.length) {
        track = candidate;
        videos = living;
        break;
      }
    }
    state.failStreak += 1;
    cursor = (cursor + 1) % total;
  }
  if (!track) {
    stopDead();
    return;
  }

  const vi = Math.min(Math.max(videoIndex, 0), videos.length - 1);
  state.pos = cursor;
  state.current = track;
  state.currentVideoIndex = vi;
  state.currentVideoId = videos[vi].id;
  state.loadToken += 1;

  renderNowPlaying(track);
  renderQueueMeta();
  highlightCurrentRow();
  setStatus('loading ' + (track.artist || '') + ' ' + (track.title || ''));

  if (!state.playerReady || !player) {
    state.pendingPlay = { pos: cursor, videoIndex: vi };
    return;
  }
  try {
    player.loadVideoById(state.currentVideoId);
  } catch (err) {
    handlePlaybackFailure();
  }
}

/**
 * Stop playback after a full pass with nothing playable.
 *
 * @returns {void}
 */
function stopDead() {
  state.stopped = true;
  state.failStreak = 0;
  setStatus('every video in this queue is unavailable, change the filters or re enrich');
  if (dom.playPause) {
    dom.playPause.textContent = 'Play';
  }
  if (player) {
    try {
      player.stopVideo();
    } catch (err) {
      return;
    }
  }
}

/**
 * React to an unplayable video by trying the next source and then the next track.
 *
 * @returns {void}
 */
function handlePlaybackFailure() {
  const track = state.current;
  blacklist(state.currentVideoId);
  if (!track) {
    return;
  }
  const remaining = livingVideos(track);
  if (remaining.length) {
    setStatus('video unavailable, trying another take');
    window.setTimeout(function () {
      playAt(state.pos, 0);
    }, ADVANCE_DELAY_MS);
    return;
  }
  state.failStreak += 1;
  if (state.failStreak >= Math.max(state.order.length, 1)) {
    stopDead();
    return;
  }
  setStatus('video unavailable, skipping');
  window.setTimeout(function () {
    playAt(state.pos + 1, 0);
  }, ADVANCE_DELAY_MS);
}

/**
 * Advance to the next queue position.
 *
 * @returns {void}
 */
function nextTrack() {
  if (!state.order.length) {
    return;
  }
  state.stopped = false;
  playAt(state.pos + 1, 0);
}

/**
 * Step back to the previous queue position.
 *
 * @returns {void}
 */
function prevTrack() {
  if (!state.order.length) {
    return;
  }
  state.stopped = false;
  playAt(state.pos - 1, 0);
}

/**
 * Toggle between playing and paused, starting the queue when idle.
 *
 * @returns {void}
 */
function togglePlay() {
  if (!state.order.length) {
    setStatus('nothing to play');
    return;
  }
  if (!state.current || state.stopped) {
    state.stopped = false;
    state.failStreak = 0;
    playAt(state.pos >= 0 ? state.pos : 0, 0);
    return;
  }
  if (!player || !state.playerReady) {
    return;
  }
  const playing = player.getPlayerState() === window.YT.PlayerState.PLAYING;
  if (playing) {
    player.pauseVideo();
  } else {
    player.playVideo();
  }
}

/**
 * Refresh the seek bar and clock readouts.
 *
 * @returns {void}
 */
function tick() {
  if (!player || !state.playerReady || state.seeking) {
    return;
  }
  let current = 0;
  let total = 0;
  try {
    current = player.getCurrentTime() || 0;
    total = player.getDuration() || 0;
  } catch (err) {
    return;
  }
  if (dom.elapsed) {
    dom.elapsed.textContent = formatTime(current);
  }
  if (dom.duration) {
    dom.duration.textContent = formatTime(total);
  }
  if (dom.seek) {
    dom.seek.value = total > 0 ? String(Math.round((current / total) * 1000)) : '0';
  }
}

/**
 * Create the YouTube player once the IFrame API is available.
 *
 * @returns {void}
 */
function createPlayer() {
  if (player || !dom.playerMount || !window.YT || !window.YT.Player) {
    return;
  }
  const vars = {
    playsinline: 1,
    rel: 0,
    modestbranding: 1,
    iv_load_policy: 3
  };
  if (!IS_FILE) {
    vars.origin = location.origin;
  }
  player = new window.YT.Player('player', {
    width: '100%',
    height: '220',
    playerVars: vars,
    events: {
      onReady: onPlayerReady,
      onStateChange: onPlayerStateChange,
      onError: onPlayerError
    }
  });
}

/**
 * Handle the player becoming usable.
 *
 * @returns {void}
 */
function onPlayerReady() {
  state.playerReady = true;
  if (tickTimer === null) {
    tickTimer = window.setInterval(tick, TICK_MS);
  }
  if (state.pendingPlay) {
    const pending = state.pendingPlay;
    state.pendingPlay = null;
    playAt(pending.pos, pending.videoIndex);
  } else {
    setStatus(state.order.length ? 'ready, press play' : 'no playable tracks');
  }
}

/**
 * Handle player state transitions, advancing the queue when a video ends.
 *
 * @param {Object} event YouTube state change event.
 * @returns {void}
 */
function onPlayerStateChange(event) {
  const S = window.YT.PlayerState;
  if (event.data === S.PLAYING) {
    state.failStreak = 0;
    if (dom.playPause) {
      dom.playPause.textContent = 'Pause';
    }
    const track = state.current;
    setStatus(track ? 'playing ' + (track.artist || '') : 'playing');
  } else if (event.data === S.PAUSED) {
    if (dom.playPause) {
      dom.playPause.textContent = 'Play';
    }
    setStatus('paused');
  } else if (event.data === S.ENDED) {
    if (dom.playPause) {
      dom.playPause.textContent = 'Play';
    }
    nextTrack();
  } else if (event.data === S.BUFFERING) {
    setStatus('buffering');
  }
}

/**
 * Handle a playback error by blacklisting the video and moving on.
 *
 * @param {Object} event YouTube error event carrying a numeric code.
 * @returns {void}
 */
function onPlayerError(event) {
  const code = event && event.data;
  if (FATAL_ERROR_CODES.indexOf(code) === -1) {
    return;
  }
  handlePlaybackFailure();
}

window.onYouTubeIframeAPIReady = function () {
  createPlayer();
};

/**
 * Toggle the shuffle mode and rebuild the playback order.
 *
 * @returns {void}
 */
function toggleShuffle() {
  state.shuffle = !state.shuffle;
  if (state.shuffle) {
    state.seed = (Date.now() % 2147483647) || 1;
  }
  if (dom.shuffle) {
    dom.shuffle.setAttribute('aria-pressed', state.shuffle ? 'true' : 'false');
  }
  persistUi();
  buildOrder();

  const key = trackKey(state.current);
  state.pos = -1;
  for (let i = 0; i < state.order.length; i += 1) {
    if (trackKey(state.queue[state.order[i]]) === key) {
      state.pos = i;
      break;
    }
  }
  renderQueueMeta();
  renderTracklist();
  setStatus(state.shuffle ? 'shuffle on' : 'shuffle off');
}

/**
 * Show or tuck the video iframe.
 *
 * The wrapper is moved off screen rather than hidden with display none, because a YouTube
 * player mounted inside a display none subtree is unreliable and can refuse to start.
 *
 * @param {boolean} visible True to reveal the iframe.
 * @returns {void}
 */
function setVideoVisible(visible) {
  state.videoVisible = visible;
  if (dom.videoWrap) {
    dom.videoWrap.classList.toggle('video-tucked', !visible);
  }
  if (dom.toggleVideo) {
    dom.toggleVideo.setAttribute('aria-pressed', visible ? 'true' : 'false');
  }
  persistUi();
}

/**
 * Persist the small pieces of interface state.
 *
 * @returns {void}
 */
function persistUi() {
  writeStore(LS_UI, {
    shuffle: state.shuffle,
    seed: state.seed,
    videoVisible: state.videoVisible
  });
}

/**
 * Build a debounced wrapper around a function.
 *
 * @param {Function} fn Function to debounce.
 * @param {number} wait Delay in milliseconds.
 * @returns {Function} Debounced function.
 */
function debounce(fn, wait) {
  let timer = null;
  return function () {
    if (timer !== null) {
      window.clearTimeout(timer);
    }
    timer = window.setTimeout(function () {
      timer = null;
      fn();
    }, wait);
  };
}

const debouncedRebuild = debounce(function () {
  rebuildQueue(true);
}, 220);

const INTERACTIVE_TAGS = [
  'input',
  'select',
  'textarea',
  'option',
  'button',
  'summary',
  'a',
  'details',
  'label'
];

/**
 * Decide whether a keyboard event originated inside a control that owns the key itself.
 *
 * Text fields must keep the space bar, and buttons, links and disclosure summaries must keep
 * their native space and arrow behaviour, otherwise the shortcut steals the activation.
 *
 * @param {EventTarget|null} target Event target.
 * @returns {boolean} True when keyboard shortcuts must be ignored.
 */
function isTypingTarget(target) {
  if (!target || !target.tagName) {
    return false;
  }
  const tag = target.tagName.toLowerCase();
  if (INTERACTIVE_TAGS.indexOf(tag) !== -1) {
    return true;
  }
  if (typeof target.getAttribute === 'function') {
    const role = target.getAttribute('role');
    if (role === 'button' || role === 'link' || role === 'textbox') {
      return true;
    }
  }
  return Boolean(target.isContentEditable);
}

/**
 * Wire every control, shortcut and delegated handler.
 *
 * @returns {void}
 */
function bindEvents() {
  if (dom.playPause) {
    dom.playPause.addEventListener('click', togglePlay);
  }
  if (dom.next) {
    dom.next.addEventListener('click', nextTrack);
  }
  if (dom.prev) {
    dom.prev.addEventListener('click', prevTrack);
  }
  if (dom.shuffle) {
    dom.shuffle.addEventListener('click', toggleShuffle);
  }
  if (dom.toggleVideo) {
    dom.toggleVideo.addEventListener('click', function () {
      setVideoVisible(!state.videoVisible);
    });
  }
  if (dom.wantBtn) {
    dom.wantBtn.addEventListener('click', toggleWantlist);
  }

  if (dom.tracklist) {
    dom.tracklist.addEventListener('click', function (event) {
      const row = event.target.closest('li[data-pos]');
      if (!row) {
        return;
      }
      state.stopped = false;
      state.failStreak = 0;
      playAt(Number(row.dataset.pos), 0);
    });
  }

  if (dom.seek) {
    dom.seek.addEventListener('input', function () {
      state.seeking = true;
    });
    dom.seek.addEventListener('change', function () {
      state.seeking = false;
      if (!player || !state.playerReady) {
        return;
      }
      try {
        const total = player.getDuration() || 0;
        if (total > 0) {
          player.seekTo((Number(dom.seek.value) / 1000) * total, true);
        }
      } catch (err) {
        return;
      }
    });
  }

  const controls = [
    dom.search,
    dom.genre,
    dom.style,
    dom.country,
    dom.label,
    dom.format,
    dom.exGenre,
    dom.exStyle,
    dom.exCountry,
    dom.exLabel,
    dom.exFormat,
    dom.yearFrom,
    dom.yearTo,
    dom.seller,
    dom.priceMin,
    dom.priceMax,
    dom.mediaGrade,
    dom.sleeveGrade,
    dom.shipsFrom,
    dom.source,
    dom.wantRatio,
    dom.ratingMin,
    dom.haveMin,
    dom.excludeOwned,
    dom.wantlistOnly
  ];
  controls.forEach(function (node) {
    if (!node) {
      return;
    }
    node.addEventListener('change', debouncedRebuild);
    if (node.tagName.toLowerCase() === 'input') {
      node.addEventListener('input', debouncedRebuild);
    }
  });

  if (dom.applyBtn) {
    dom.applyBtn.addEventListener('click', function () {
      rebuildQueue(true);
      setStatus(state.queue.length + ' tracks in the queue');
    });
  }
  if (dom.resetBtn) {
    dom.resetBtn.addEventListener('click', function () {
      clearFilters();
      rebuildQueue(true);
      setStatus('filters reset');
    });
  }

  document.addEventListener('keydown', function (event) {
    if (event.metaKey || event.ctrlKey || event.altKey) {
      return;
    }
    if (isTypingTarget(event.target)) {
      return;
    }
    if (event.key === ' ' || event.code === 'Space') {
      event.preventDefault();
      togglePlay();
    } else if (event.key === 'ArrowLeft') {
      event.preventDefault();
      prevTrack();
    } else if (event.key === 'ArrowRight') {
      event.preventDefault();
      nextTrack();
    }
  });
}

/**
 * Load data, restore saved interface state, and prepare the first queue.
 *
 * @returns {Promise<void>} Resolves when the page is ready to play.
 */
async function init() {
  const ui = readStore(LS_UI, {});
  state.shuffle = Boolean(ui.shuffle);
  state.seed = Number.isFinite(ui.seed) && ui.seed ? ui.seed : 1;
  state.videoVisible = Boolean(ui.videoVisible);
  state.dead = new Set(readStore(LS_DEAD, []));

  if (dom.shuffle) {
    dom.shuffle.setAttribute('aria-pressed', state.shuffle ? 'true' : 'false');
  }
  setVideoVisible(state.videoVisible);
  bindEvents();
  reportMissingIds();

  setStatus('loading crate');
  const crate = await loadCrate();
  setServerBadge(state.hasServer);

  if (!crate || !Array.isArray(crate.tracks) || !crate.tracks.length) {
    state.all = [];
    renderEmptyRow(
      'No crate data. Run the export command to build web/crate.js, then reload this page.'
    );
    renderNowPlaying(null);
    updateCount();
    renderQueueMeta();
    setStatus(
      window.CRATE_LOAD_ERROR ? 'crate.js is missing, run the export' : 'no crate data found'
    );
    return;
  }

  state.meta = crate;
  state.all = crate.tracks.map(prepareTrack);

  populateFilterOptions(state.all);
  writeFilters(readStore(LS_FILTERS, null));
  rebuildQueue(false);
  renderNowPlaying(null);

  const stats = crate.stats || {};
  const releases = stats.releases === undefined ? state.all.length : stats.releases;
  setStatus(state.queue.length + ' tracks ready from ' + releases + ' releases');

  createPlayer();
  startPlayerWatchdog();
}

/**
 * Report a YouTube IFrame API that never arrives instead of waiting forever.
 *
 * Without this the transport silently does nothing when the API script is blocked, which
 * looks identical to an empty crate.
 *
 * @returns {void}
 */
function startPlayerWatchdog() {
  window.setTimeout(function () {
    if (state.playerReady) {
      return;
    }
    setStatus(
      'the YouTube player did not load, check the network or an extension blocking youtube.com'
    );
  }, PLAYER_WATCHDOG_MS);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
