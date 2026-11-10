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
    # Genre untuk filter klien: nama Inggris tag grup genre+theme.
    # Tag konten/format + tag dewasa-dekat tidak ikut (bukan data filter).
    genres = []
    for t in attrs.get("tags") or []:
        ta = (t.get("attributes") or {})
        if ta.get("group") not in ("genre", "theme"):
            continue
        nm = ((ta.get("name") or {}).get("en") or "").strip()
        if nm and nm not in _MD_GENRE_EXCLUDE:
            genres.append(nm)
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
        "alt_titles": _all_titles(attrs),
        "genres": genres,
    }


# Peta nama genre Inggris -> UUID tag MangaDex, diverifikasi LIVE dari
# GET https://api.mangadex.org/manga/tag pada 10 Nov 2026. Daftar ini adalah
# irisan genre teratas Komiku (pustaka, by jumlah judul) dengan tag MangaDex
# (grup genre+theme); genre dewasa-dekat (Ecchi/Harem/Mature) sengaja tidak
# ada di sini. Nama tak dikenal diabaikan (fail-open, pencarian tetap jalan).
_MD_TAG_IDS = {
    "Action": "391b0423-d847-456f-aff0-8b0cfc03066b",
    "Adventure": "87cc87cd-a395-47af-b27a-93258283bbc6",
    "Comedy": "4d32cc48-9f00-4cca-9b5a-a839f0764984",
    "Romance": "423e2eae-a7a2-4a8b-ac03-a8351462d71d",
    "Drama": "b9af3a63-f058-46de-a9a0-e0c13906197a",
    "Fantasy": "cdc58593-87dd-415e-bbc0-2ec27bf404cc",
    "Slice of Life": "e5301a23-ebd9-49dd-a0cb-2add944c7fe9",
    "Supernatural": "eabc5b4c-6aff-42f3-b657-3e90cbd00b75",
    "Martial Arts": "799c202e-7daa-44eb-9cf7-8a3c0441531e",
    "Isekai": "ace04997-f6bd-436e-b261-779182193d3d",
    "Mystery": "ee968100-4191-4968-93d3-f82d72be7e46",
    "Historical": "33771934-028e-4cb3-8744-691e866a923e",
    "Magic": "a1f53773-c69a-4ce5-8cab-fffcd90b1565",
    "Sci-Fi": "256c8bd9-4904-4360-bf4f-508a76d67183",
    "School Life": "caaa44eb-cd40-4177-b930-79d3ef2afe87",
    "Psychological": "3b60b75c-a2d7-4860-ab56-05f391bb889c",
    "Horror": "cdad7e68-1419-41dd-bdce-27753074a640",
    "Reincarnation": "0bc90acb-ccc1-44ca-a34a-b9f3a73259d0",
    "Tragedy": "f8f62932-27da-4fe4-8ee1-6779a8c5edba",
    "Demons": "39730448-9a5f-48a2-85b0-a70db87b1233",
}
_MD_GENRE_EXCLUDE = {"Harem", "Reverse Harem", "Incest", "Loli", "Shota"}
_MD_STATUS_OK = {"ongoing", "completed", "hiatus", "cancelled"}


def search_manga(query, limit=24, offset=0, safe_mode=1, genre=None, status=None):
    """Cari judul. Hasil diurutkan: skor judul dulu, lalu jumlah pengikut.

    Filter server-side (bila diberi): genre = nama genre Inggris dari
    _MD_TAG_IDS (dipetakan ke UUID tag MangaDex, includedTags AND);
    status = salah satu dari _MD_STATUS_OK. Nilai tak dikenal diabaikan
    (fail-open) supaya pencarian tidak pernah rusak karena filter.
    """
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
    tag_id = _MD_TAG_IDS.get((genre or "").strip())
    if tag_id:
        params.append(("includedTags[]", tag_id))
        params.append(("includedTagsMode", "AND"))
    st = (status or "").strip().lower()
    if st in _MD_STATUS_OK:
        params.append(("status[]", st))
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
        "alt_titles": _all_titles(attrs),
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
