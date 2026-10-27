"""Klien scraping Komikcast untuk kategori Manhwa Lumio (Bahasa Indonesia).

PERINGATAN — MODUL INI BELUM TERVERIFIKASI LIVE (27 Okt 2026):
situs komikcast.asia memblokir IP datacenter sandbox dengan
Cloudflare ("One moment, please...") dan Imunify360 bot-protection
("Access denied by Imunify360 bot-protection") — baik lewat httpx,
curl_cffi impersonasi Chrome, maupun fetch teks browser. Blokir itu
TIDAK diakali di sini. Parser di bawah ditulis defensif berlapis
berdasarkan pola yang TERAMATI, bukan markup yang terverifikasi:
- pola URL bab: /chapter/<slug>-chapter-<nomor> (contoh teramati di
  hasil pencarian: the-beginning-after-the-end-chapter-99,
  eleceed-chapter-261, martial-god-regressed-to-level-2-chapter-04,
  seoul-station-druid-chapter-1, i-became-an-evolving-space-monster-chapter-30-2)
- situs WordPress (pencarian ?s=, halaman judul /komik/<slug>/)
- domain utama komikcast.asia (klaim situsnya sendiri: "Simpan domain
  utama kami di komikcast.asia")
Bila situs memblokir, fungsi-fungsi di sini melempar ValueError
berawalan "Komikcast diblokir" (bukan hasil kosong diam-diam), supaya
endpoint gabungan bisa mencatatnya sebagai catatan, bukan data palsu.
Verifikasi HP oleh user tetap WAJIB sebelum modul ini dianggap jalan.

Struktur mirror mengikuti pola Otakudesu (backend/api/otakudesu.py):
KC_MIRRORS di-loop; mirror tambahan HANYA ditambahkan bila teramati
dari sumber tepercaya — jangan mengarang domain mirror.

Batasan keamanan yang dijaga di sini (bukan di frontend):
- Judul bergenre dewasa tidak pernah dikembalikan: kata kunci dewasa
  pada judul selalu disaring; blok genre diperiksa bila blok genre
  berhasil diparsing dari halaman judul.
- Gambar bab dimuat frontend lewat proksi gambar /api/img (validasi
  SSRF di main.py, tanpa pelonggaran).
"""

import html as htmlmod
import re
import urllib.parse

try:
    from curl_cffi import requests as _creq
except Exception:  # pragma: no cover - lingkungan tanpa curl_cffi
    _creq = None

import httpx

# Hanya domain yang teramati (klaim situs sendiri). Jangan tambah mirror
# tanpa bukti dari hasil pencarian/situs.
KC_MIRRORS = ["https://komikcast.asia"]
_BROWSER_UA = ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36")
_KC_UA = "Lumio/2.27 (Komikcast reader)"

_CHALLENGE_MARKERS = ("One moment, please", "challenges.cloudflare",
                      "cf-challenge", "Imunify360", "Attention Required")
_ADULT_ALWAYS = ("hentai", "adult", "erotica", "pornographic", "porn")
_ADULT_SAFE = ("ecchi", "smut")
_JUNK_IMG = ("logo", "banner", "avatar", "icon", "ads", "advertisement",
             "placeholder", "spinner", "loading", "emoji", "wp-includes",
             "data:image")


class KCBlocked(ValueError):
    """Situs memblokir akses otomatis (Cloudflare/Imunify360)."""
    pass


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


def _is_blocked_body(html):
    return any(m in html for m in _CHALLENGE_MARKERS)


def _fetch(path):
    """Ambil HTML lewat rotasi mirror. curl_cffi dulu (pola Otakudesu),
    lalu httpx. trust_env default: egress sandbox wajib lewat proxy
    lingkungan (JANGAN set trust_env=False)."""
    last_err = None
    for base in KC_MIRRORS:
        url = base + path
        html = None
        if _creq is not None:
            try:
                r = _creq.get(url, impersonate="chrome124", timeout=12,
                              headers={"User-Agent": _BROWSER_UA,
                                       "Accept": "text/html,application/xhtml+xml"})
                if r.status_code == 200:
                    html = r.text
                else:
                    last_err = "Komikcast HTTP %s" % r.status_code
            except Exception as e:
                last_err = str(e)
        if html is None:
            try:
                with httpx.Client(timeout=12.0, follow_redirects=True) as client:
                    r = client.get(url, headers={"User-Agent": _KC_UA,
                                                 "Accept": "text/html,application/xhtml+xml"})
                if r.status_code == 200:
                    html = r.text
                else:
                    last_err = "Komikcast HTTP %s" % r.status_code
            except Exception as e:
                last_err = str(e)
        if html is None:
            continue
        if _is_blocked_body(html):
            raise KCBlocked(
                "Komikcast diblokir: situs menolak akses otomatis dari "
                "server ini (bot-protection). Coba lagi dari HP.")
        return html
    raise ValueError("Komikcast tidak terjangkau: %s" % last_err)


def _adult_in_title(title):
    t = _norm(title)
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


def search_manga(query, limit=24, safe_mode=1):
    """Cari judul lewat pencarian WordPress (?s=). Kartu hasil diparsing
    dari anchor /komik/<slug>/; gambar dari <img> di dalam anchor."""
    query = (query or "").strip()
    if len(query) < 2:
        raise ValueError("Kata kunci minimal 2 karakter")
    html = _fetch("/?s=" + urllib.parse.quote_plus(query))
    items = []
    seen = set()
    for m in re.finditer(
            r'<a[^>]+href="[^"]*/komik/([^"/\?#]+)/?"[^>]*>(.*?)</a>',
            html, re.S | re.I):
        slug = m.group(1).strip()
        inner = m.group(2) or ""
        if slug in seen:
            continue
        seen.add(slug)
        title = _clean_text(inner)
        if not title:
            alt = re.search(r'alt="([^"]+)"', inner)
            title = alt.group(1).strip() if alt else ""
        if not title or _adult_in_title(title):
            continue
        cover = ""
        for tag in re.finditer(r"<img[^>]+>", inner):
            u = _img_from_tag(tag.group(0))
            if u.startswith("http") and not any(j in u.lower() for j in _JUNK_IMG):
                cover = u
                break
        items.append({
            "id": "kc:" + slug,
            "title": title,
            "cover": cover,
            "status": "",
            "year": "",
            "description": "",
            "score": _title_score(query, title),
            "followed": 0,
            "source": "Komikcast",
            "lang": "id",
            "alt_titles": [],
        })
    items.sort(key=lambda it: -it["score"])
    return {"items": items[:max(1, min(int(limit), 50))], "total": len(items)}


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
    """Daftar bab dari halaman judul /komik/<slug>/. Nomor bab diambil
    dari pola URL -chapter-<nomor> yang teramati (termasuk bentuk 30-2)."""
    slug = (slug or "").strip()
    if not slug:
        raise ValueError("Slug Komikcast kosong")
    html = _fetch("/komik/%s/" % urllib.parse.quote(slug, safe=""))
    title = _meta_content(html, "og:title") or ""
    if not title:
        h1 = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S | re.I)
        title = _clean_text(h1.group(1)) if h1 else slug.replace("-", " ").title()
    cover = _meta_content(html, "og:image") or ""
    description = _clean_text(_meta_content(html, "og:description"))
    # Blok genre dewasa bila blok genre berhasil diparsing.
    genre_block = ""
    gm = re.search(r'Genre[s]?[:<][^<]{0,500}', html, re.I)
    if gm:
        genre_block = _norm(_clean_text(gm.group(0)))
    if any(k in genre_block for k in _ADULT_ALWAYS):
        raise ValueError("Judul dewasa diblokir")
    if safe_mode and any(k in genre_block for k in _ADULT_SAFE):
        raise ValueError("Judul dewasa diblokir")
    chapters = []
    seen = set()
    for m in re.finditer(
            r'<a[^>]+href="[^"]*/chapter/([^"/\?#]+)/?"[^>]*>(.*?)</a>',
            html, re.S | re.I):
        ch_slug = m.group(1).strip()
        if ch_slug in seen:
            continue
        seen.add(ch_slug)
        num_m = re.search(r"-chapter-(\d+(?:\.\d+)?(?:-\d+)?)$", ch_slug)
        num = num_m.group(1) if num_m else ""
        ch_title = _clean_text(m.group(2) or "")
        chapters.append({
            "id": "kc:" + ch_slug,
            "chapter": num,
            "title": ch_title,
            "lang": "id",
            "source": "Komikcast",
        })
    if not chapters:
        raise ValueError("Daftar bab Komikcast tidak ditemukan")
    chapters.sort(key=lambda c: _chapter_sort_key(c["chapter"]))
    return {
        "id": "kc:" + slug,
        "title": title,
        "cover": cover,
        "status": "",
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


def get_chapter_pages(chapter_slug):
    """Daftar URL gambar satu bab. Strategi berlapis: dulu container
    reader yang umum di tema komik WordPress, lalu seluruh <img> halaman
    dengan filter junk. URL relatif di-resolve ke mirror aktif."""
    chapter_slug = (chapter_slug or "").strip()
    if not chapter_slug:
        raise ValueError("ID bab Komikcast kosong")
    html = _fetch("/chapter/%s/" % urllib.parse.quote(chapter_slug, safe=""))
    base = KC_MIRRORS[0]
    pages = []
    # 1) Container reader (bila ada): ambil isinya dulu.
    container = None
    for pat in (r'<div[^>]*id="readerarea"[^>]*>(.*)',
                r'<div[^>]*class="[^"]*reading-content[^"]*"[^>]*>(.*)',
                r'<div[^>]*class="[^"]*main-reading-area[^"]*"[^>]*>(.*)'):
        mm = re.search(pat, html, re.S | re.I)
        if mm:
            container = mm.group(1)
            break
    scope = container if container is not None else html
    for tag in re.finditer(r"<img[^>]+>", scope):
        u = _img_from_tag(tag.group(0))
        if not u or u.startswith("data:"):
            continue
        u = urllib.parse.urljoin(base + "/", u)
        if not u.startswith("http"):
            continue
        low = u.lower()
        if not re.search(r"\.(jpe?g|png|webp)(\?|#|$)", low):
            continue
        if any(j in low for j in _JUNK_IMG):
            continue
        if u not in pages:
            pages.append(u)
    if not pages:
        raise ValueError("Halaman bab Komikcast tidak ditemukan")
    return {"id": "kc:" + chapter_slug, "pages": pages, "total": len(pages)}
