"""Klien API MangaDex untuk kategori Manhwa Lumio.

API publik tanpa kunci (https://api.mangadex.org). Dipakai untuk:
- pencarian judul (manhwa/manhua/manga),
- daftar bab per judul (prioritas bahasa Indonesia, fallback Inggris),
- daftar halaman per bab lewat at-home server.

Batasan keamanan yang dijaga di sini (bukan di frontend):
- contentRating TIDAK PERNAH menyertakan erotica/pornographic. Mode Aman
  (default) hanya "safe"; bila Mode Aman dimatikan, "suggestive" ikut.
- rate limit MangaDex (5 req/detik tanpa auth) dihormati lewat throttle
  antar-request di modul ini.
- URL halaman berasal dari at-home server resmi MangaDex saja; frontend
  memuatnya lewat proksi gambar /api/img (validasi SSRF di main.py).
"""

import re
import threading
import time

import httpx

MD_API = "https://api.mangadex.org"
_MD_UA = "Lumio/2.27 (MangaDex reader)"

# Throttle: jeda minimum antar-request ke API MangaDex (5 req/detik).
_MD_LOCK = threading.Lock()
_MD_LAST = [0.0]


def _throttle():
    with _MD_LOCK:
        now = time.time()
        wait = 0.25 - (now - _MD_LAST[0])
        if wait > 0:
            time.sleep(wait)
        _MD_LAST[0] = time.time()


def _get(path, params=None):
    _throttle()
    try:
        # trust_env default (True): di sandbox, egress WAJIB lewat proxy
        # lingkungan; server selalu dijalankan dengan NO_PROXY yang bersih
        # ("localhost,127.0.0.1") sehingga parsing env httpx tidak tersandung
        # entri IPv6 bracket. Di HP tidak ada variabel proxy semacam itu.
        with httpx.Client(timeout=20.0, follow_redirects=True) as client:
            r = client.get(MD_API + path, params=params, headers={"User-Agent": _MD_UA})
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as e:
        raise ValueError("MangaDex HTTP %s" % e.response.status_code)
    except Exception as e:
        raise ValueError("MangaDex tidak terjangkau: %s" % e)


def _ratings(safe_mode):
    # safe_mode=1 (default) -> hanya "safe". safe_mode=0 -> tambah "suggestive".
    # erotica/pornographic tidak pernah diminta.
    return ["safe"] if safe_mode else ["safe", "suggestive"]


def _norm(s):
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _pick_title(attributes):
    """Judul tampil: Inggris dulu, lalu judul asli, lalu apa pun yang ada."""
    titles = attributes.get("title") or {}
    if titles.get("en"):
        return titles["en"]
    for v in titles.values():
        if v:
            return v
    for alt in attributes.get("altTitles") or []:
        for v in alt.values():
            if v:
                return v
    return "Tanpa judul"


def _all_titles(attributes):
    out = []
    for d in [attributes.get("title") or {}] + (attributes.get("altTitles") or []):
        for v in d.values():
            if v:
                out.append(v)
    return out


def _title_score(query, attributes):
    """Skor kecocokan judul: exact > diawali > semua token > sebagian token.

    Ini yang mencegah sekuel (mis. 'Solo Leveling: Ragnarok') mengalahkan
    judul yang benar-benar dicari ('Solo Leveling') hanya karena populer.
    """
    q = _norm(query)
    if not q:
        return 0
    q_tokens = set(q.split())
    best = 0
    for cand in _all_titles(attributes):
        c = _norm(cand)
        if not c:
            continue
        if c == q:
            score = 100
        elif c.startswith(q):
            score = 75
        elif q_tokens and q_tokens <= set(c.split()):
            score = 60
        elif q in c:
            score = 40
        else:
            overlap = len(q_tokens & set(c.split())) / max(1, len(q_tokens))
            score = round(overlap * 30)
        best = max(best, score)
    return best


def _cover_url(manga_id, relationships):
    for rel in relationships or []:
        if rel.get("type") == "cover_art":
            fn = (rel.get("attributes") or {}).get("fileName")
            if fn:
                return "https://uploads.mangadex.org/covers/%s/%s" % (manga_id, fn)
    return ""


def _map_manga(m, query=""):
    attrs = m.get("attributes") or {}
    desc_map = attrs.get("description") or {}
    return {
        "id": m.get("id"),
        "title": _pick_title(attrs),
        "cover": _cover_url(m.get("id"), m.get("relationships")),
        "status": attrs.get("status") or "",
        "year": attrs.get("year") or "",
        "rating": attrs.get("contentRating") or "",
        "followed": (attrs.get("followedCount") if isinstance(attrs.get("followedCount"), int) else 0),
        "description": desc_map.get("en") or next(iter(desc_map.values()), ""),
        "score": _title_score(query, attrs) if query else 0,
    }


def search_manga(query, limit=24, offset=0, safe_mode=1):
    """Cari judul. Hasil diurutkan: skor judul dulu, lalu jumlah pengikut."""
    query = (query or "").strip()
    if len(query) < 2:
        raise ValueError("Kata kunci minimal 2 karakter")
    params = [
        ("title", query),
        ("limit", min(max(int(limit), 1), 50)),
        ("offset", max(int(offset), 0)),
        ("includes[]", "cover_art"),
        ("order[relevance]", "desc"),
    ]
    for r in _ratings(safe_mode):
        params.append(("contentRating[]", r))
    data = _get("/manga", params=params)
    items = [_map_manga(m, query) for m in data.get("data") or []]
    items.sort(key=lambda it: (-it["score"], -it["followed"]))
    return {"items": items, "total": data.get("total") or len(items),
            "limit": data.get("limit") or len(items), "offset": data.get("offset") or 0}


def popular_manga(limit=24, safe_mode=1):
    """Manhwa Korea terpopuler (bahasa asli Korea) untuk halaman awal kategori.

    Bukan 'semua komik': kategori ini bernama Manhwa, jadi daftar awalnya
    memang manhwa. Pencarian tetap mencakup semua asal bahasa.
    """
    params = [
        ("limit", min(max(int(limit), 1), 50)),
        ("offset", 0),
        ("includes[]", "cover_art"),
        ("order[followedCount]", "desc"),
        ("originalLanguage[]", "ko"),
    ]
    for r in _ratings(safe_mode):
        params.append(("contentRating[]", r))
    data = _get("/manga", params=params)
    items = [_map_manga(m) for m in data.get("data") or []]
    items.sort(key=lambda it: -it["followed"])
    return {"items": items, "total": data.get("total") or len(items)}


def get_manga_chapters(manga_id, safe_mode=1):
    """Daftar bab: satu entri per nomor bab (duplikat grup scanlation digabung).

    Prioritas bahasa per nomor bab: Indonesia dulu, lalu Inggris. Bab diurut
    menaik; bab tanpa nomor (oneshot) diletakkan di akhir.
    """
    manga_id = (manga_id or "").strip()
    if not manga_id:
        raise ValueError("ID manga kosong")
    meta = _get("/manga/%s" % manga_id, params=[("includes[]", "cover_art")])
    attrs = (meta.get("data") or {}).get("attributes") or {}
    params = [
        ("translatedLanguage[]", "id"),
        ("translatedLanguage[]", "en"),
        ("order[chapter]", "asc"),
        ("limit", 500),
        ("offset", 0),
    ]
    for r in _ratings(safe_mode):
        params.append(("contentRating[]", r))
    feed = _get("/manga/%s/feed" % manga_id, params=params)
    by_chapter = {}
    for ch in feed.get("data") or []:
        ca = ch.get("attributes") or {}
        num = (ca.get("chapter") or "").strip()
        key = num if num else "oneshot"
        lang = ca.get("translatedLanguage") or ""
        cand = {
            "id": ch.get("id"),
            "chapter": num,
            "title": ca.get("title") or "",
            "lang": lang,
            "volume": ca.get("volume") or "",
            # Bab resmi (Tapas/Webnovel/dll): MangaDex hanya menautkan ke
            # situs resmi, tidak meng-host gambarnya. Frontend menampilkan
            # tombol buka situs resmi, bukan memanggil /pages (yang kosong).
            "external": ca.get("externalUrl") or "",
        }
        cur = by_chapter.get(key)
        if cur is None:
            by_chapter[key] = cand
        else:
            # Preferensi bahasa: id > en > lainnya; jangan timpa id dengan en.
            rank = {"id": 0, "en": 1}.get(lang, 2)
            cur_rank = {"id": 0, "en": 1}.get(cur["lang"], 2)
            if rank < cur_rank:
                by_chapter[key] = cand

    def _sort_key(item):
        key, ch = item
        if key == "oneshot":
            return (1, 0.0)
        try:
            return (0, float(key))
        except ValueError:
            return (1, 0.0)

    chapters = [ch for _, ch in sorted(by_chapter.items(), key=_sort_key)]
    return {
        "id": manga_id,
        "title": _pick_title(attrs),
        "cover": _cover_url(manga_id, (meta.get("data") or {}).get("relationships")),
        "status": attrs.get("status") or "",
        "year": attrs.get("year") or "",
        "description": (attrs.get("description") or {}).get("en") or "",
        "chapters": chapters,
        "total": len(chapters),
    }


def get_chapter_pages(chapter_id):
    """Daftar URL halaman bab lewat at-home server resmi MangaDex."""
    chapter_id = (chapter_id or "").strip()
    if not chapter_id:
        raise ValueError("ID bab kosong")
    data = _get("/at-home/server/%s" % chapter_id)
    base = data.get("baseUrl") or ""
    ch = data.get("chapter") or {}
    h = ch.get("hash") or ""
    files = ch.get("data") or []
    if not base or not h or not files:
        # Terjadi pada bab placeholder/eksternal (mis. sebagian bab Solo
        # Leveling di MangaDex): bab terdaftar, tapi tidak ada gambar di
        # server MangaDex. Bukan kegagalan jaringan — jangan di-retry buta.
        raise ValueError("Halaman bab ini tidak tersedia di MangaDex")
    pages = ["%s/data/%s/%s" % (base, h, fn) for fn in files]
    return {"id": chapter_id, "baseUrl": base, "hash": h, "pages": pages, "total": len(pages)}
