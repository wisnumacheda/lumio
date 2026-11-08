"""Klien scraping Komiku untuk kategori Manhwa Lumio (Bahasa Indonesia).

Sumber: komiku.org (situs baca komik Indonesia). Diverifikasi live dari
sandbox pada 8 Nov 2026 — fetch statis tanpa JS challenge/Cloudflare:
- cari judul:   GET https://api.komiku.org/?s=<query>&post_type=manga
  (halaman komiku.org sendiri memuat hasil secara lazy lewat HTMX dari
  host API ini, jadi modul memanggil API-nya langsung) -> fragmen HTML
  kartu .bge: <a href="/manga/<slug>/"> + <img thumbnail.komiku.org>
  + <h3>Judul</h3> + <b>Tipe</b> Genre + tautan bab Awal/Terbaru.
- detail judul:  GET https://komiku.org/manga/<slug>/ -> H1 "Komik <Judul>",
  og:image (thumbnail.komiku.to), og:description, genre lewat
  <meta itemprop="genre">, status/rating dari tabel info, daftar bab
  href="/<slug>-chapter-<N>/".
- halaman bab:   GET https://komiku.org/<slug>-chapter-<N>/ -> gambar di
  dalam #Baca_Komik, host img.komiku.org (satu halaman bisa terpecah
  jadi _part1/_part2, diurutkan per nomor halaman lalu nomor part).

Gambar (cover + halaman) dimuat frontend lewat proksi gambar /api/img
(validasi SSRF di main.py, tanpa pelonggaran).

Batasan keamanan yang dijaga di sini (bukan di frontend):
- Judul bergenre dewasa tidak pernah dikembalikan: genre diperiksa di
  halaman judul (meta itemprop="genre" + Rating 18+); di pencarian,
  kata kunci dewasa pada judul/genre disaring. Mode Aman (default)
  juga menyaring ecchi/smut.
- Judul yang tidak ditemukan melempar ValueError biasa (bukan hasil
  kosong diam-diam) supaya penggabung bisa mencatat kegagalan provider
  secara jujur.
"""

import html as htmlmod
import re
import urllib.parse

try:
    from curl_cffi import requests as _creq
except Exception:  # pragma: no cover - lingkungan tanpa curl_cffi
    _creq = None

import httpx

KM_BASE = "https://komiku.org"
KM_API = "https://api.komiku.org"
_BROWSER_UA = ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36")
_KM_UA = "Lumio/2.28 (Komiku reader)"

_ADULT_ALWAYS = ("hentai", "adult", "erotica", "pornographic", "porn",
                 "dewasa")
_ADULT_SAFE = ("ecchi", "smut")
_JUNK_IMG = ("logo", "banner", "avatar", "icon", "advertisement",
             "placeholder", "spinner", "loading", "emoji", "wp-includes",
             "data:image", "promosi")
# CATATAN: "ads" sengaja TIDAK ada di daftar — sebagai substring ia cocok
# dengan "uploads" (uplo**ads**) sehingga hampir semua URL gambar ikut
# terbuang.


def _norm(s):
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _title_score(query, title):
    q = _norm(query)
    c = _norm(title)
    if not q or not c:
        return 0
    q_tokens = set(q.split())
    if c == q:
        return 100
    if c.startswith(q):
        return 75
    if q_tokens and q_tokens <= set(c.split()):
        return 60
    if q in c:
        return 40
    overlap = len(q_tokens & set(c.split())) / max(1, len(q_tokens))
    return round(overlap * 30)


def _clean_text(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = htmlmod.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _adult_in_text(text):
    t = _norm(text)
    return any(k in t.split() or k in t for k in _ADULT_ALWAYS)


def _img_from_tag(tag):
    """Ambil URL gambar dari satu tag <img>: utamakan data-src bila src
    berupa placeholder, lalu src biasa. Abaikan data: URI."""
    m = re.search(r'data-src="([^"]+)"', tag)
    src = re.search(r'\ssrc="([^"]+)"', tag)
    src = src.group(1) if src else ""
    if m and (not src or "placeholder" in src or src.startswith("data:")):
        return m.group(1)
    return src


def _fetch(url):
    """Ambil HTML. curl_cffi dulu (pola Otakudesu/Komikcast), lalu httpx.
    trust_env default: egress sandbox wajib lewat proxy lingkungan
    (JANGAN set trust_env=False)."""
    last_err = None
    if _creq is not None:
        try:
            r = _creq.get(url, impersonate="chrome124", timeout=15,
                          headers={"User-Agent": _BROWSER_UA,
                                   "Accept": "text/html,application/xhtml+xml"})
            if r.status_code == 200:
                return r.text
            last_err = "Komiku HTTP %s" % r.status_code
        except Exception as e:
            last_err = str(e)
    try:
        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            r = client.get(url, headers={"User-Agent": _KM_UA,
                                         "Accept": "text/html,application/xhtml+xml"})
        if r.status_code == 200:
            return r.text
        last_err = "Komiku HTTP %s" % r.status_code
    except Exception as e:
        last_err = str(e)
    raise ValueError("Komiku tidak terjangkau: %s" % last_err)


def search_manga(query, limit=24, safe_mode=1):
    """Cari judul lewat API pencarian Komiku (fragmen HTML kartu .bge).
    Kartu berisi slug, cover thumbnail.komiku.org, judul, tipe + genre,
    serta nomor bab terbaru (dipakai sebagai 'followed' untuk urutan)."""
    query = (query or "").strip()
    if len(query) < 2:
        raise ValueError("Kata kunci minimal 2 karakter")
    url = KM_API + "/?s=" + urllib.parse.quote_plus(query) + "&post_type=manga"
    html = _fetch(url)
    items = []
    seen = set()
    for block in re.finditer(r'<div class="bge">(.*?)</div>\s*</div>', html,
                             re.S | re.I):
        inner = block.group(1)
        slug_m = re.search(r'href="/manga/([^"/\?#]+)/?"', inner, re.I)
        if not slug_m:
            continue
        slug = slug_m.group(1).strip()
        if slug in seen:
            continue
        seen.add(slug)
        title_m = re.search(r"<h3[^>]*>(.*?)</h3>", inner, re.S | re.I)
        title = _clean_text(title_m.group(1)) if title_m else ""
        if not title:
            alt = re.search(r'alt="([^"]+)"', inner)
            title = alt.group(1).strip() if alt else ""
        if not title or _adult_in_text(title):
            continue
        # Tipe + genre: <div class="tpe1_inf"><b>Manhua</b> Aksi</div>
        genre_text = ""
        gm = re.search(r'<div class="tpe1_inf">(.*?)</div>', inner, re.S | re.I)
        if gm:
            genre_text = _clean_text(gm.group(1))
        if _adult_in_text(genre_text):
            continue
        if safe_mode and any(k in _norm(genre_text).split()
                             for k in _ADULT_SAFE):
            continue
        cover = ""
        for tag in re.finditer(r"<img[^>]+>", inner):
            u = _img_from_tag(tag.group(0))
            # URL cover memakai entitas HTML (&#038;) — unescape supaya
            # proksi /api/img menerima query utuh (& bukan fragment #).
            u = htmlmod.unescape(u)
            if u.startswith("http") and not any(j in u.lower() for j in _JUNK_IMG):
                cover = u
                break
        latest = 0
        for num_m in re.finditer(r"-chapter-(\d+)", inner):
            try:
                latest = max(latest, int(num_m.group(1)))
            except ValueError:
                pass
        items.append({
            "id": "km:" + slug,
            "title": title,
            "cover": cover,
            "status": "",
            "year": "",
            "description": "",
            "score": _title_score(query, title),
            "followed": latest,
            "source": "Komiku",
            "lang": "id",
            "alt_titles": [],
        })
    items.sort(key=lambda it: (-it["score"], -it["followed"]))
    return {"items": items[:max(1, min(int(limit), 50))],
            "total": len(items)}


def _meta_content(html, prop):
    m = re.search(
        r'<meta[^>]+(?:property|name)="%s"[^>]+content="([^"]*)"' % prop,
        html, re.I)
    if m:
        return htmlmod.unescape(m.group(1)).strip()
    m = re.search(
        r'<meta[^>]+content="([^"]*)"[^>]+(?:property|name)="%s"' % prop,
        html, re.I)
    return htmlmod.unescape(m.group(1)).strip() if m else ""


def get_series_chapters(slug, safe_mode=1):
    """Daftar bab dari halaman judul /manga/<slug>/. Nomor bab diambil
    dari pola href -chapter-<nomor> (termasuk bernol depan, mis. -03);
    ID bab memakai slug mentah situs, nomor tampil dinormalisasi."""
    slug = (slug or "").strip()
    if not slug:
        raise ValueError("Slug Komiku kosong")
    html = _fetch(KM_BASE + "/manga/%s/" % urllib.parse.quote(slug, safe=""))
    title = _meta_content(html, "og:title") or ""
    if not title:
        h1 = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S | re.I)
        title = _clean_text(h1.group(1)) if h1 else slug.replace("-", " ").title()
    # H1 situs berawalan "Komik " — buang supaya cocok ketat lintas provider.
    title = re.sub(r"^Komik\s+", "", title).strip()
    cover = htmlmod.unescape(_meta_content(html, "og:image") or "")
    description = _clean_text(_meta_content(html, "og:description"))
    status = ""
    sm = re.search(r"<td>Status:</td>\s*<td>([^<]+)</td>", html, re.I)
    if sm:
        status = sm.group(1).strip()
    # Blok genre dewasa: meta itemprop="genre" + Rating 18+.
    genres = [g.strip() for g in re.findall(
        r'<meta[^>]+itemprop="genre"[^>]+content="([^"]*)"', html, re.I)]
    genre_block = " ".join(genres).lower()
    if any(k in genre_block for k in _ADULT_ALWAYS):
        raise ValueError("Judul dewasa diblokir")
    if safe_mode and any(k in genre_block for k in _ADULT_SAFE):
        raise ValueError("Judul dewasa diblokir")
    rm = re.search(r"<td>Rating:</td>\s*<td>([^<]+)</td>", html, re.I)
    if rm and rm.group(1).strip().startswith("18"):
        raise ValueError("Judul dewasa diblokir")
    chapters = []
    seen = set()
    for m in re.finditer(
            r'href="(/[^"]*-chapter-([^"/\?#]+)/?)"[^>]*>(.*?)</a>',
            html, re.S | re.I):
        ch_href = m.group(1).strip()
        ch_slug = ch_href.strip("/")
        if ch_slug in seen:
            continue
        seen.add(ch_slug)
        raw_num = m.group(2).strip()
        try:
            num = str(int(raw_num))
        except ValueError:
            # Bab pecahan situs ("26-5" = bab 26.5): normalisasi ke desimal
            # supaya urutan + merge lintas provider benar; ID bab tetap
            # memakai slug mentah situs.
            dm = re.match(r"^(\d+)-(\d+)$", raw_num)
            num = "%s.%s" % (dm.group(1), dm.group(2)) if dm else raw_num
        chapters.append({
            "id": "km:" + ch_slug,
            "chapter": num,
            "title": "",
            "lang": "id",
            "source": "Komiku",
        })
    if not chapters:
        raise ValueError("Daftar bab Komiku tidak ditemukan")
    chapters.sort(key=lambda c: _chapter_sort_key(c["chapter"]))
    return {
        "id": "km:" + slug,
        "title": title,
        "cover": cover,
        "status": status,
        "year": "",
        "description": description,
        "chapters": chapters,
        "total": len(chapters),
        "alt_titles": [],
    }


def _chapter_sort_key(num_s):
    try:
        return (0, float(num_s))
    except (TypeError, ValueError):
        return (1, 0.0)


def _page_sort_key(url):
    """Urutkan gambar bab: nomor halaman dulu, lalu nomor part
    (1_part1.webp < 1_part2.webp < 2_part1.webp < 31.webp)."""
    m = re.search(r"/(\d+)(?:_part(\d+))?\.(?:jpe?g|png|webp)(?:\?|#|$)",
                  url, re.I)
    if not m:
        return (1, 0, 0)
    try:
        page = int(m.group(1))
    except ValueError:
        page = 0
    try:
        part = int(m.group(2)) if m.group(2) else 0
    except ValueError:
        part = 0
    return (0, page, part)


def get_chapter_pages(series_slug, chapter_slug):
    """Daftar URL gambar satu bab. Gambar diambil dari container
    #Baca_Komik (bila ada), lalu seluruh <img> halaman dengan filter
    junk; hanya host img.komiku.org yang dipakai sebagai halaman."""
    series_slug = (series_slug or "").strip()
    chapter_slug = (chapter_slug or "").strip()
    if not chapter_slug:
        raise ValueError("ID bab Komiku kosong")
    html = _fetch(KM_BASE + "/%s/" % urllib.parse.quote(chapter_slug, safe=""))
    container = None
    mm = re.search(r'<div[^>]*id="Baca_Komik"[^>]*>(.*)',
                   html, re.S | re.I)
    if mm:
        container = mm.group(1)
    scope = container if container is not None else html
    pages = []
    for tag in re.finditer(r"<img[^>]+>", scope):
        u = htmlmod.unescape(_img_from_tag(tag.group(0)))
        if not u or u.startswith("data:"):
            continue
        if "img.komiku.org" not in u:
            continue
        if not u.startswith("http"):
            continue
        low = u.lower()
        if not re.search(r"\.(jpe?g|png|webp)(\?|#|$)", low):
            continue
        if any(j in low for j in _JUNK_IMG):
            continue
        if u not in pages:
            pages.append(u)
    pages.sort(key=_page_sort_key)
    if not pages:
        raise ValueError("Halaman bab Komiku tidak ditemukan")
    return {"id": "km:" + chapter_slug, "pages": pages, "total": len(pages)}
