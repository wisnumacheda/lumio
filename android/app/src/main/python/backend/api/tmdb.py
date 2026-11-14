"""Katalog Movie & Series dari TMDB API v3 (pola modul AniList).

Kontrak (payload v44, jalur anime):
- SELALU include_adult=false + filter keyword blacklist yang sama seperti
  jalur anime (Mode Aman). Kategori 18+ tidak pernah ditampilkan.
- Kunci API dibaca berurutan: env TMDB_API_KEY -> $LUMIO_DATA_DIR/tmdb_key.txt
  -> backend/tmdb_key.txt (pola feedback_token.txt). Tanpa kunci, fungsi
  melempar NeedsKeyError dan endpoint menjawab error jujur "needs_key".
- Nilai kunci TIDAK PERNAH di-log atau dikembalikan; hanya status terpasang
  (configured/source/length) yang boleh keluar.
- Bahasa metadata: en-US (sinopsis Inggris, sama seperti jalur anime dari
  HiAnime; subtitle Indonesia tetap dari engine subtitle Lumio).
- Modul ini sinkron (dijalankan di executor dari main.py, seperti hianime).
"""
import os
import time

import httpx

TMDB_BASE = "https://api.themoviedb.org/3"
IMG_BASE = "https://image.tmdb.org/t/p"
UA = "Lumio/2.29.1 (TMDB catalog)"

# Keyword blacklist yang sama dengan jalur anime (Mode Aman + blacklist hentai).
_ADULT_TAGS = ("ecchi", "erotica", "hentai", "magical-sex-shift")

# Rate-limit TMDB (HTTP 429) — cooldown in-memory, pola AniList.
_tmdb_rate_limit_until = 0.0

_tmdb_client = None


def _get_client():
    global _tmdb_client
    if _tmdb_client is None:
        _tmdb_client = httpx.Client(
            timeout=httpx.Timeout(15.0, connect=6.0),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            headers={"User-Agent": UA, "Accept": "application/json"},
        )
    return _tmdb_client


def close_tmdb_client():
    global _tmdb_client
    if _tmdb_client is not None:
        try:
            _tmdb_client.close()
        except Exception:
            pass
        _tmdb_client = None


class NeedsKeyError(Exception):
    """Kunci API TMDB belum dipasang — endpoint harus menjawab needs_key."""


def _backend_dir():
    """Folder backend/ (di samping main.py) — modul ini tinggal di
    backend/api/, jadi naik satu tingkat (pola feedback_token.txt)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def tmdb_key_info():
    """Status kunci TANPA nilainya: {configured, source, length}."""
    env_key = (os.environ.get("TMDB_API_KEY") or "").strip()
    if env_key:
        return {"configured": True, "source": "env", "length": len(env_key)}
    cands = []
    dd = os.environ.get("LUMIO_DATA_DIR", "")
    if dd:
        cands.append(("folder data aplikasi", os.path.join(dd, "tmdb_key.txt")))
    try:
        cands.append(("sebelah main.py", os.path.join(_backend_dir(), "tmdb_key.txt")))
    except Exception:
        pass
    for src, p in cands:
        try:
            with open(p, "r", encoding="utf-8") as f:
                t = f.read().strip()
            if t:
                return {"configured": True, "source": src, "length": len(t)}
        except Exception:
            continue
    return {"configured": False, "source": "", "length": 0}


def _tmdb_key():
    """Nilai kunci (internal saja). Melempar NeedsKeyError bila belum ada."""
    env_key = (os.environ.get("TMDB_API_KEY") or "").strip()
    if env_key:
        return env_key
    cands = []
    dd = os.environ.get("LUMIO_DATA_DIR", "")
    if dd:
        cands.append(os.path.join(dd, "tmdb_key.txt"))
    try:
        cands.append(os.path.join(_backend_dir(), "tmdb_key.txt"))
    except Exception:
        pass
    for p in cands:
        try:
            with open(p, "r", encoding="utf-8") as f:
                t = f.read().strip()
            if t:
                return t
        except Exception:
            continue
    raise NeedsKeyError("needs_key")


def tmdb_key_path():
    """Lokasi penyimpanan kunci: folder data aplikasi bila ada, else di
    samping main.py (pola feedback_token.txt)."""
    dd = os.environ.get("LUMIO_DATA_DIR", "")
    if dd:
        return os.path.join(dd, "tmdb_key.txt"), "folder data aplikasi"
    return os.path.join(_backend_dir(), "tmdb_key.txt"), "sebelah main.py"


def save_tmdb_key(key):
    """Simpan kunci ke file (atau hapus bila kunci kosong). Mengembalikan
    info status tanpa nilai kunci. Nilai kunci tidak pernah di-log."""
    path, src = tmdb_key_path()
    key = (key or "").strip()
    if not key:
        try:
            os.remove(path)
        except Exception:
            pass
        return {"configured": False, "source": ""}
    if len(key) > 128 or any(ch.isspace() for ch in key):
        raise ValueError("Format kunci tidak valid")
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(key)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass
    return {"configured": True, "source": src, "length": len(key)}


def _is_rate_limited():
    return time.time() < _tmdb_rate_limit_until


def _get(path, params=None):
    """Satu panggilan GET TMDB. Melempar NeedsKeyError / RuntimeError jujur."""
    global _tmdb_rate_limit_until
    if _is_rate_limited():
        raise RuntimeError("TMDB membatasi laju (429) — coba lagi sebentar lagi")
    key = _tmdb_key()
    q = dict(params or {})
    q["api_key"] = key
    q["language"] = "en-US"
    q["include_adult"] = "false"
    try:
        resp = _get_client().get(TMDB_BASE + path, params=q)
    except httpx.TimeoutException:
        raise RuntimeError("TMDB timeout — koneksi lambat, coba lagi")
    except Exception as e:
        raise RuntimeError("TMDB tidak terjangkau: %s" % str(e)[:100])
    if resp.status_code == 401:
        raise RuntimeError("Kunci API TMDB ditolak (401) — periksa kunci di Pengaturan")
    if resp.status_code == 429:
        retry = resp.headers.get("Retry-After")
        try:
            wait = max(5.0, min(float(retry), 120.0)) if retry else 30.0
        except Exception:
            wait = 30.0
        _tmdb_rate_limit_until = time.time() + wait
        raise RuntimeError("TMDB membatasi laju (429) — coba lagi sebentar lagi")
    if resp.status_code == 404:
        raise RuntimeError("Judul tidak ditemukan di TMDB (404)")
    if resp.status_code != 200:
        msg = ""
        try:
            msg = (resp.json() or {}).get("status_message") or ""
        except Exception:
            pass
        raise RuntimeError("TMDB HTTP %d%s" % (resp.status_code, (": " + msg[:100]) if msg else ""))
    try:
        return resp.json()
    except Exception:
        raise RuntimeError("TMDB mengembalikan respons yang tidak bisa dibaca")


def _img(path, size="w500"):
    if not path:
        return ""
    return IMG_BASE + "/" + size + path


def _is_blocked(title, overview):
    """Filter blacklist: flag adult TMDB + keyword dewasa di judul/overview."""
    text = ((title or "") + " " + (overview or "")).lower()
    return any(tag in text for tag in _ADULT_TAGS)


# --- Peta genre (id -> nama), di-cache di memori per proses ---

_GENRE_MAPS = {}


def _genre_map(media_type):
    mt = "tv" if media_type == "tv" else "movie"
    if mt in _GENRE_MAPS:
        return _GENRE_MAPS[mt]
    try:
        data = _get("/genre/%s/list" % mt)
        m = {g.get("id"): g.get("name") for g in (data or {}).get("genres") or [] if g.get("id")}
    except Exception:
        m = {}
    _GENRE_MAPS[mt] = m
    return m


def _fmt_duration(minutes):
    try:
        m = int(minutes or 0)
    except Exception:
        m = 0
    if m <= 0:
        return ""
    if m < 60:
        return "%dm" % m
    return "%dj %dm" % (m // 60, m % 60)


def _normalize_item(raw, media_type, genre_map, safe_mode=1):
    """Normalisasi satu hasil TMDB ke bentuk kartu anime (id/title/poster/
    synopsis/type/year/score/duration/genres). None bila dibuang filter."""
    if not isinstance(raw, dict):
        return None
    if raw.get("adult"):
        return None
    mt = "tv" if media_type == "tv" else "movie"
    title = (raw.get("title") or raw.get("name") or "").strip()
    overview = (raw.get("overview") or "").strip()
    if safe_mode == 1 and _is_blocked(title, overview):
        return None
    tid = raw.get("id")
    if not tid:
        return None
    date = (raw.get("release_date") or raw.get("first_air_date") or "")
    year = ""
    try:
        year = int(str(date)[:4]) if date else ""
    except Exception:
        year = ""
    try:
        score = round(float(raw.get("vote_average") or 0), 1)
    except Exception:
        score = 0
    gids = raw.get("genre_ids") or []
    genres = [genre_map.get(g) for g in gids if genre_map.get(g)]
    return {
        "id": "tmdb:%s:%s" % (mt, tid),
        "tmdb_id": tid,
        "media_type": mt,
        "title": title,
        "poster": _img(raw.get("poster_path"), "w500"),
        "backdrop": _img(raw.get("backdrop_path"), "w780"),
        "synopsis": overview,
        "type": "Movie" if mt == "movie" else "TV",
        "year": year,
        "score": score,
        "duration": "",
        "badge": "",
        "eps_counter": "",
        "genres": genres,
        "origin_lang": raw.get("original_language") or "",
    }


_SORT_MAP = {
    "movie": {"populer": "popularity.desc", "terbaru": "primary_release_date.desc", "az": "popularity.desc"},
    "tv": {"populer": "popularity.desc", "terbaru": "first_air_date.desc", "az": "popularity.desc"},
}


def discover(media_type, page=1, sort="", genre="", lang="", safe_mode=1):
    """Katalog discover TMDB — cermin /api/browse jalur anime."""
    mt = "tv" if media_type == "tv" else "movie"
    sort_key = (sort or "").strip().lower()
    sort_by = _SORT_MAP[mt].get(sort_key, _SORT_MAP[mt]["populer"])
    params = {"page": max(1, int(page or 1)), "sort_by": sort_by}
    if genre:
        try:
            params["with_genres"] = str(int(genre))
        except Exception:
            pass
    if lang:
        params["with_original_language"] = str(lang).strip().lower()[:5]
    data = _get("/discover/%s" % mt, params)
    gmap = _genre_map(mt)
    items = []
    for raw in (data or {}).get("results") or []:
        it = _normalize_item(raw, mt, gmap, safe_mode)
        if it:
            items.append(it)
    return {
        "items": items,
        "page": int((data or {}).get("page") or page or 1),
        "total_pages": int((data or {}).get("total_pages") or 1),
        "has_next": bool(int((data or {}).get("page") or 1) < int((data or {}).get("total_pages") or 1)),
    }


def search_multi(query, page=1, safe_mode=1):
    """Pencarian multi TMDB (film + series; person dibuang)."""
    q = (query or "").strip()
    if not q:
        return {"results": [], "page": 1, "total_pages": 1, "has_next": False}
    data = _get("/search/multi", {"query": q, "page": max(1, int(page or 1))})
    gmap_m = _genre_map("movie")
    gmap_t = _genre_map("tv")
    results = []
    for raw in (data or {}).get("results") or []:
        mt = raw.get("media_type")
        if mt not in ("movie", "tv"):
            continue
        it = _normalize_item(raw, mt, gmap_t if mt == "tv" else gmap_m, safe_mode)
        if it:
            results.append(it)
    return {
        "results": results,
        "page": int((data or {}).get("page") or page or 1),
        "total_pages": int((data or {}).get("total_pages") or 1),
        "has_next": bool(int((data or {}).get("page") or 1) < int((data or {}).get("total_pages") or 1)),
    }


_STATUS_MAP = {
    "Ended": "Tamat",
    "Returning Series": "Masih Tayang",
    "Canceled": "Batal",
    "In Production": "Produksi",
    "Planned": "Segera",
    "Pilot": "Pilot",
    "Released": "Rilis",
    "Post Production": "Pasca Produksi",
}


def _detail_common(raw, mt):
    title = (raw.get("title") or raw.get("name") or "").strip()
    overview = (raw.get("overview") or "").strip()
    date = (raw.get("release_date") or raw.get("first_air_date") or "")
    year = ""
    try:
        year = int(str(date)[:4]) if date else ""
    except Exception:
        year = ""
    try:
        score = round(float(raw.get("vote_average") or 0), 1)
    except Exception:
        score = 0
    status_raw = raw.get("status") or ""
    genres = [{"id": g.get("id"), "name": g.get("name")}
              for g in (raw.get("genres") or []) if g.get("id") and g.get("name")]
    studios = []
    for c in (raw.get("production_companies") or []):
        if c.get("name"):
            studios.append(c["name"])
    for n in (raw.get("networks") or []):
        if n.get("name") and n["name"] not in studios:
            studios.append(n["name"])
    return {
        "id": "tmdb:%s:%s" % (mt, raw.get("id")),
        "tmdb_id": raw.get("id"),
        "media_type": mt,
        "title": title,
        "synopsis": overview,
        "poster": _img(raw.get("poster_path"), "w500"),
        "backdrop": _img(raw.get("backdrop_path"), "w780"),
        "score": score,
        "status": _STATUS_MAP.get(status_raw, status_raw),
        "status_raw": status_raw,
        "year": year,
        "studio": studios[0] if studios else "",
        "source": "TMDB",
        "genres": genres,
        "tagline": (raw.get("tagline") or "").strip(),
        "origin_lang": raw.get("original_language") or "",
    }


def movie_detail(tmdb_id, safe_mode=1):
    raw = _get("/movie/%s" % int(tmdb_id))
    if raw.get("adult"):
        raise RuntimeError("Konten dewasa diblokir")
    title = (raw.get("title") or "").strip()
    if safe_mode == 1 and _is_blocked(title, raw.get("overview") or ""):
        raise RuntimeError("Konten dewasa diblokir (Mode Aman)")
    d = _detail_common(raw, "movie")
    d["duration"] = _fmt_duration(raw.get("runtime"))
    d["runtime"] = raw.get("runtime") or 0
    return d


def tv_detail(tmdb_id, safe_mode=1):
    raw = _get("/tv/%s" % int(tmdb_id))
    if raw.get("adult"):
        raise RuntimeError("Konten dewasa diblokir")
    title = (raw.get("name") or "").strip()
    if safe_mode == 1 and _is_blocked(title, raw.get("overview") or ""):
        raise RuntimeError("Konten dewasa diblokir (Mode Aman)")
    d = _detail_common(raw, "tv")
    runtimes = raw.get("episode_run_time") or []
    d["duration"] = _fmt_duration(runtimes[0]) if runtimes else ""
    d["number_of_seasons"] = raw.get("number_of_seasons") or 0
    d["number_of_episodes"] = raw.get("number_of_episodes") or 0
    seasons = []
    for s in (raw.get("seasons") or []):
        sn = s.get("season_number")
        if sn is None:
            continue
        seasons.append({
            "season_number": sn,
            "name": s.get("name") or ("Season %s" % sn),
            "episode_count": s.get("episode_count") or 0,
            "air_date": s.get("air_date") or "",
            "poster": _img(s.get("poster_path"), "w500"),
            "overview": (s.get("overview") or "").strip(),
        })
    seasons.sort(key=lambda s: s["season_number"])
    d["seasons"] = seasons
    return d


def season_episodes(tmdb_id, season):
    """Daftar episode satu season — cermin /api/anime/{slug}/episodes."""
    data = _get("/tv/%s/season/%s" % (int(tmdb_id), int(season)))
    eps = []
    for e in (data or {}).get("episodes") or []:
        num = e.get("episode_number")
        if num is None:
            continue
        eps.append({
            "ep": int(num),
            "ep_id": str(int(num)),
            "name": (e.get("name") or "").strip(),
            "overview": (e.get("overview") or "").strip(),
            "air_date": e.get("air_date") or "",
            "runtime": e.get("runtime") or 0,
            "still": _img(e.get("still_path"), "w300"),
        })
    eps.sort(key=lambda e: e["ep"])
    return {
        "season": int(season),
        "season_name": (data or {}).get("name") or ("Season %s" % season),
        "episodes": eps,
        "total": len(eps),
    }


def watch_providers(media_type, tmdb_id):
    """Provider resmi per region ID — tombol "Tonton di" (pola MangaDex
    berlisensi: tautan resmi, paywall tidak pernah dilewati). URL tiap
    tombol = halaman tonton TMDB untuk judul itu (otoritatif, tidak pernah
    404); nama + logo provider berasal dari data TMDB region ID."""
    mt = "tv" if media_type == "tv" else "movie"
    data = _get("/%s/%s/watch/providers" % (mt, int(tmdb_id)))
    results = (data or {}).get("results") or {}
    region = results.get("ID") or {}
    link = region.get("link") or ""
    providers = []
    seen = set()
    for bucket in ("flatrate", "ads", "free"):
        for p in region.get(bucket) or []:
            pid = p.get("provider_id")
            if not pid or pid in seen:
                continue
            seen.add(pid)
            providers.append({
                "provider_id": pid,
                "name": p.get("provider_name") or "",
                "logo": _img(p.get("logo_path"), "w185"),
                "url": link,
                "bucket": bucket,
            })
    providers.sort(key=lambda p: ({"flatrate": 0, "ads": 1, "free": 2}.get(p["bucket"], 3), p["name"]))
    return {"providers": providers, "link": link, "region": "ID"}
