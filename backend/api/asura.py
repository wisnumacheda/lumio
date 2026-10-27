"""Klien API Asura Scans untuk kategori Manhwa Lumio (Bahasa Inggris).

API publik tanpa kunci (https://api.asurascans.com), diverifikasi live dari
sandbox pada 27 Okt 2026:
- cari judul:   GET /api/search?q=...          -> {data: [{id, slug, title,
  alt_titles, cover, genres[], status, chapter_count, type, ...}]}
- detail judul:  GET /api/series/{slug}         -> {series: {...}, ...}
- daftar bab:    GET /api/series/{slug}/chapters (limit/offset; default semua,
  urutan terbaru dulu) -> {data: [{id, number, title, slug, page_count}]}
- halaman bab:   GET /api/series/{slug}/chapters/{chapter-slug}
  -> {data: {chapter: {pages: [{url, width, height}]}}}

Gambar (cover + halaman) berasal dari cdn.asurascans.com dan dimuat frontend
lewat proksi gambar /api/img (validasi SSRF di main.py, tanpa pelonggaran).

Batasan keamanan yang dijaga di sini (bukan di frontend):
- Judul bergenre dewasa (hentai/adult/erotica/pornographic) tidak pernah
  dikembalikan. Mode Aman (default) juga menyaring ecchi/smut.
- Satu host API yang teramati (api.asurascans.com); _api() tetap ditulis
  sebagai loop atas API_BASES supaya mirror bisa ditambahkan bila muncul.
  Domain situs lama asuracomic.net saat ini me-redirect ke asurascans.com,
  dan asuracomic.co.uk tidak terjangkau dari sandbox — keduanya BUKAN
  mirror API, jadi tidak dipakai sebagai basis API.
"""

import html as htmlmod
import re
import urllib.parse

try:
    from curl_cffi import requests as _creq
except Exception:  # pragma: no cover - lingkungan tanpa curl_cffi
    _creq = None

import httpx

API_BASES = ["https://api.asurascans.com"]
_AS_UA = "Lumio/2.27 (Asura reader)"
_BROWSER_UA = ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36")

_ADULT_ALWAYS = ("hentai", "adult", "erotica", "pornographic", "porn")
_ADULT_SAFE = ("ecchi", "smut")


def _norm(s):
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _strip_parenthetical(s):
    # "Solo Leveling (2021)" -> "Solo Leveling"; dipakai untuk pencocokan
    # lintas provider supaya sufiks tahun tidak menggagalkan match.
    return re.sub(r"\s*\([^)]*\)\s*$", "", s or "").strip()


def _title_score(query, title, alt_titles=None):
    """Skor kecocokan judul: exact 100 > diawali 75 > semua token 60 >
    substring 40 > overlap token (maks 30). Dipakai untuk mengurutkan hasil
    cari supaya judul asli mengalahkan sekuelnya."""
    q = _norm(query)
    if not q:
        return 0
    q_tokens = set(q.split())
    best = 0
    cands = [title] + list(alt_titles or [])
    for cand in cands:
        if isinstance(cand, dict):
            cand = next(iter(cand.values()), "")
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


def _genre_names(item):
    out = []
    for g in item.get("genres") or []:
        if isinstance(g, dict):
            out.append(str(g.get("name") or ""))
        else:
            out.append(str(g))
    return out


def _blocked_genres(item, safe_mode):
    names = " ".join(_genre_names(item)).lower()
    if any(k in names for k in _ADULT_ALWAYS):
        return True
    if safe_mode and any(k in names for k in _ADULT_SAFE):
        return True
    return False


def _clean_html_text(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = htmlmod.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _api(path, params=None):
    """GET JSON dari API Asura. curl_cffi (impersonasi Chrome) dulu seperti
    pola Otakudesu, lalu httpx. trust_env default: egress sandbox wajib
    lewat proxy lingkungan (JANGAN set trust_env=False)."""
    last_err = None
    for base in API_BASES:
        url = base + path
        if _creq is not None:
            try:
                r = _creq.get(url, params=params, impersonate="chrome124",
                              timeout=25, headers={"Accept": "application/json",
                                                   "User-Agent": _BROWSER_UA})
                if r.status_code == 200:
                    return r.json()
                last_err = "Asura HTTP %s" % r.status_code
                continue
            except Exception as e:
                last_err = str(e)
        try:
            with httpx.Client(timeout=25.0, follow_redirects=True) as client:
                r = client.get(url, params=params,
                               headers={"Accept": "application/json",
                                        "User-Agent": _AS_UA})
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = str(e)
    raise ValueError("Asura tidak terjangkau: %s" % last_err)


def search_manga(query, limit=24, safe_mode=1):
    """Cari judul. Hasil diurutkan skor judul dulu, lalu jumlah bab."""
    query = (query or "").strip()
    if len(query) < 2:
        raise ValueError("Kata kunci minimal 2 karakter")
    data = _api("/api/search", params={"q": query})
    items = []
    for m in data.get("data") or []:
        if _blocked_genres(m, safe_mode):
            continue
        alt = []
        for t in m.get("alt_titles") or []:
            if isinstance(t, dict):
                alt.extend(str(v) for v in t.values() if v)
            elif t:
                alt.append(str(t))
        items.append({
            "id": "as:" + str(m.get("slug") or ""),
            "title": m.get("title") or "Tanpa judul",
            "cover": m.get("cover") or "",
            "status": m.get("status") or "",
            "year": "",
            "description": _clean_html_text(m.get("description") or ""),
            "score": _title_score(query, m.get("title") or "",
                                  m.get("alt_titles") or []),
            "followed": m.get("chapter_count") or 0,
            "source": "Asura",
            "lang": "en",
            "alt_titles": alt,
        })
    items.sort(key=lambda it: (-it["score"], -it["followed"]))
    return {"items": items[:max(1, min(int(limit), 50))],
            "total": len(items)}


def get_series(slug):
    """Detail satu judul dari API series."""
    slug = (slug or "").strip()
    if not slug:
        raise ValueError("Slug Asura kosong")
    data = _api("/api/series/%s" % urllib.parse.quote(slug, safe=""))
    series = data.get("series") or {}
    if not series or data.get("error"):
        raise ValueError("Judul Asura tidak ditemukan")
    if _blocked_genres(series, 1):
        # Detail langsung: genre dewasa selalu ditolak, apa pun safe_mode.
        raise ValueError("Judul dewasa diblokir")
    return series


def get_series_chapters(slug, safe_mode=1):
    """Daftar bab: satu entri per nomor bab, diurut menaik. Bab premium
    tetap dicantumkan; halaman bab premium yang digerbang akan gagal di
    get_chapter_pages dengan pesan yang jelas (bukan halaman kosong)."""
    series = get_series(slug)
    if safe_mode and _blocked_genres(series, 1):
        raise ValueError("Judul dewasa diblokir")
    chapters = []
    offset = 0
    # Paging limit=100: limit=500 ditolak API dengan 403 (terverifikasi
    # 27 Okt 2026); limit=200 lolos, 100 dipakai supaya aman dari batas.
    while True:
        data = _api("/api/series/%s/chapters" % urllib.parse.quote(slug, safe=""),
                    params={"limit": 100, "offset": offset})
        batch = data.get("data") or []
        for ch in batch:
            num = ch.get("number")
            num_s = "" if num is None else str(num)
            chapters.append({
                "id": "as:%s:%s" % (slug, ch.get("slug") or ""),
                "chapter": num_s,
                "title": ch.get("title") or "",
                "lang": "en",
                "source": "Asura",
                "page_count": ch.get("page_count") or 0,
            })
        if len(batch) < 100:
            break
        offset += 100
    chapters.sort(key=lambda c: _chapter_sort_key(c["chapter"]))
    alt = []
    for key in ("alt_titles", "alternative_titles"):
        for t in series.get(key) or []:
            if isinstance(t, dict):
                alt.extend(str(v) for v in t.values() if v)
            elif t:
                alt.append(str(t))
    return {
        "id": "as:" + slug,
        "title": series.get("title") or "Tanpa judul",
        "cover": series.get("cover") or "",
        "status": series.get("status") or "",
        "year": "",
        "description": _clean_html_text(series.get("description") or ""),
        "chapters": chapters,
        "total": len(chapters),
        "alt_titles": alt,
    }


def _chapter_sort_key(num_s):
    try:
        return (0, float(num_s))
    except (TypeError, ValueError):
        return (1, 0.0)


def get_chapter_pages(series_slug, chapter_slug):
    """Daftar URL halaman satu bab (CDN Asura)."""
    series_slug = (series_slug or "").strip()
    chapter_slug = (chapter_slug or "").strip()
    if not series_slug or not chapter_slug:
        raise ValueError("ID bab Asura kosong")
    data = _api("/api/series/%s/chapters/%s" % (
        urllib.parse.quote(series_slug, safe=""),
        urllib.parse.quote(chapter_slug, safe="")))
    chapter = (data.get("data") or {}).get("chapter") or {}
    pages = []
    for p in chapter.get("pages") or []:
        u = p.get("url") if isinstance(p, dict) else p
        if u and str(u).startswith("http"):
            pages.append(str(u))
    if not pages:
        raise ValueError("Halaman bab ini tidak tersedia di Asura")
    return {"id": "as:%s:%s" % (series_slug, chapter_slug),
            "pages": pages, "total": len(pages)}
