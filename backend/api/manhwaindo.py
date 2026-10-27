"""Klien scraping Manhwaindo untuk kategori Manhwa Lumio (Bahasa Indonesia).

DIVERIFIKASI LIVE dari sandbox pada 27 Okt 2026 (situs TIDAK memblokir
IP datacenter — berbeda dengan Komikcast):
- cari judul:   GET /?s=<query> (server-rendered; kartu hasil: anchor
  class="series" ke /series/<slug>/ + badge "Ch. NNN" + sinopsis +
  span genre; poster di anchor sebelumnya, img thumbnail manga-images)
- detail judul:  GET /series/<slug>/ (og:title/og:image/og:description,
  sinopsis setelah heading "Sinopsis", "Tahun Rilis", status
  "Manhwa Ongoing/Completed", genre links, alt-title tepat setelah
  <h1>, dan blok agcChapterListConfig {seriesId, endpoint})
- daftar bab:    GET <endpoint>?series=<seriesId> (endpoint default
  /wp-json/agc/v1/chapters; JSON list [{id, url, chapter, time}],
  urutan terbaru dulu) — contoh nyata: Eleceed seriesId 13 = 424
  entri, bab 1..423 kontinu + 1 entri ekstra (nilai non-integer).
- halaman bab:   GET /<slug>-chapter-<num>/ (server-rendered; gambar
  halaman di CDN imgx-id.gmbr.pro, pola
  /uploads/manga-images/<huruf>/<slug>/chapter-<num>/<n>.jpg;
  contoh nyata: Eleceed ch 423 = 27 gambar 1.jpg..27.jpg)

Struktur mirror mengikuti pola Otakudesu/Komikcast: MI_MIRRORS di-loop;
mirror tambahan HANYA ditambahkan bila teramati — situs saudari
beta.mwland.my BUKAN mirror sampai terbukti satu struktur.

Batasan keamanan yang dijaga di sini (bukan di frontend):
- Judul bergenre dewasa tidak pernah dikembalikan: kata kunci dewasa
  pada judul/genre selalu disaring; Mode Aman juga menyaring ecchi/smut.
- Gambar bab dimuat frontend lewat proksi gambar /api/img (validasi
  SSRF di main.py, tanpa pelonggaran). URL gambar imgx-id.gmbr.pro
  dinormalisasi http -> https (host yang sama terbukti melayani https
  lewat og:image); host lain tidak diubah.
- Iklan situs (banner judi dkk.) TIDAK PERNAH diambil: filter halaman
  hanya menerima URL yang mengandung /manga-images/ + /chapter-.
"""

import html as htmlmod
import json
import re
import urllib.parse

try:
    from curl_cffi import requests as _creq
except Exception:  # pragma: no cover - lingkungan tanpa curl_cffi
    _creq = None

import httpx

# Hanya domain yang terverifikasi live. Jangan tambah mirror tanpa bukti.
MI_MIRRORS = ["https://www.manhwaindo.my"]
_BROWSER_UA = ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36")
_MI_UA = "Lumio/2.28 (Manhwaindo reader)"

_CHALLENGE_MARKERS = ("One moment, please", "challenges.cloudflare",
                      "cf-challenge", "Imunify360", "Attention Required")
_ADULT_ALWAYS = ("hentai", "adult", "erotica", "pornographic", "porn")
_ADULT_SAFE = ("ecchi", "smut")


class MIBlocked(ValueError):
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
    for base in MI_MIRRORS:
        url = base + path
        html = None
        if _creq is not None:
            try:
                r = _creq.get(url, impersonate="chrome124", timeout=15,
                              headers={"User-Agent": _BROWSER_UA,
                                       "Accept": "text/html,application/xhtml+xml"})
                if r.status_code == 200:
                    html = r.text
                else:
                    last_err = "Manhwaindo HTTP %s" % r.status_code
            except Exception as e:
                last_err = str(e)
        if html is None:
            try:
                with httpx.Client(timeout=15.0, follow_redirects=True) as client:
                    r = client.get(url, headers={"User-Agent": _MI_UA,
                                                 "Accept": "text/html,application/xhtml+xml"})
                if r.status_code == 200:
                    html = r.text
                else:
                    last_err = "Manhwaindo HTTP %s" % r.status_code
            except Exception as e:
                last_err = str(e)
        if html is None:
            continue
        if _is_blocked_body(html):
            raise MIBlocked(
                "Manhwaindo diblokir: situs menolak akses otomatis dari "
                "server ini (bot-protection). Coba lagi dari HP.")
        return html
    raise ValueError("Manhwaindo tidak terjangkau: %s" % last_err)


def _fetch_json(path):
    """Ambil JSON dari endpoint REST situs (dipakai daftar bab)."""
    last_err = None
    for base in MI_MIRRORS:
        url = base + path
        if _creq is not None:
            try:
                r = _creq.get(url, impersonate="chrome124", timeout=20,
                              headers={"User-Agent": _BROWSER_UA,
                                       "Accept": "application/json"})
                if r.status_code == 200:
                    return r.json()
                last_err = "Manhwaindo HTTP %s" % r.status_code
            except Exception as e:
                last_err = str(e)
        try:
            with httpx.Client(timeout=20.0, follow_redirects=True) as client:
                r = client.get(url, headers={"User-Agent": _MI_UA,
                                             "Accept": "application/json"})
            if r.status_code == 200:
                return r.json()
            last_err = "Manhwaindo HTTP %s" % r.status_code
        except Exception as e:
            last_err = str(e)
    raise ValueError("Manhwaindo tidak terjangkau: %s" % last_err)


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


def search_manga(query, limit=24, safe_mode=1):
    """Cari judul lewat pencarian WordPress (?s=). Tiap kartu hasil
    terdiri dari dua anchor ke /series/<slug>/ (poster lalu tautan
    judul); kartu dibatasi dari anchor pertama tiap slug. Di dalam
    kartu: poster manga-images, badge "Ch. NNN", sinopsis, span genre."""
    query = (query or "").strip()
    if len(query) < 2:
        raise ValueError("Kata kunci minimal 2 karakter")
    html = _fetch("/?s=" + urllib.parse.quote_plus(query))
    items = []
    # Kartu hasil terdiri dari DUA anchor per judul (poster dulu, lalu
    # tautan judul) — keduanya mengarah ke /series/<slug>/. Jadi kartu
    # dibatasi dari anchor PERTAMA tiap slug sampai anchor pertama slug
    # berikutnya; seluruh isi kartu (poster, badge bab, sinopsis, genre)
    # ada di dalam region itu.
    all_anchors = list(re.finditer(
        r'<a[^>]+href="[^"]*/series/([^"/\?#]+)/?"[^>]*>', html, re.I))
    starts = []
    seen = set()
    for m in all_anchors:
        slug = m.group(1).strip()
        if slug not in seen:
            seen.add(slug)
            starts.append((m.start(), slug))
    for idx, (start, slug) in enumerate(starts):
        end = starts[idx + 1][0] if idx + 1 < len(starts) else len(html)
        region = html[start:end]
        tm = re.search(r'title="([^"]+)"', region)
        title = tm.group(1).strip() if tm else ""
        if not title:
            dm = re.search(r'font-semibold[^>]*>([^<]+)</div>', region)
            title = _clean_text(dm.group(1)) if dm else ""
        if not title or _adult_in_title(title):
            continue
        # Sinopsis: <p class="line-clamp-2 ..."> pertama di kartu.
        syn = ""
        sm = re.search(r'<p[^>]*line-clamp-2[^>]*>(.*?)</p>', region, re.S | re.I)
        if sm:
            syn = _clean_text(sm.group(1))
        # Genre: span genre di kartu.
        genres = []
        for gm in re.finditer(
                r'<span[^>]*text-indigo-100[^>]*>([^<]+)</span>', region, re.I):
            g = _clean_text(gm.group(1))
            if g:
                genres.append(g)
        genre_text = " ".join(genres)
        if _adult_in_text(genre_text):
            continue
        if safe_mode and any(k in _norm(genre_text).split()
                             for k in _ADULT_SAFE):
            continue
        # Poster: <img> manga-images pertama di kartu.
        cover = ""
        for tag in re.finditer(r"<img[^>]+>", region):
            u = _img_from_tag(tag.group(0))
            if not u or u.startswith("data:"):
                ssm = re.search(r'srcset="([^"]+)"', tag.group(0))
                if ssm:
                    u = ssm.group(1).split(",")[0].split()[0]
            if "manga-images" in u and u.startswith("http"):
                cover = u
                break
        # Bab terbaru: badge "Ch. NNN" pertama di kartu.
        latest = ""
        lm = re.findall(r"Ch\.\s*(\d+(?:\.\d+)?)", region)
        if lm:
            latest = lm[0]
        try:
            followed = int(float(latest)) if latest else 0
        except ValueError:
            followed = 0
        items.append({
            "id": "mi:" + slug,
            "title": title,
            "cover": cover,
            "status": "",
            "year": "",
            "description": syn,
            "score": _title_score(query, title),
            "followed": followed,
            "source": "Manhwaindo",
            "lang": "id",
            "alt_titles": [],
        })
    items.sort(key=lambda it: (-it["score"], -it["followed"]))
    return {"items": items[:max(1, min(int(limit), 50))], "total": len(items)}


def _adult_in_title(title):
    return _adult_in_text(title)


def get_series_chapters(slug, safe_mode=1):
    """Detail + daftar bab satu judul. Metadata dari halaman series;
    daftar bab dari endpoint REST agc (seriesId dari blok
    agcChapterListConfig di halaman yang sama)."""
    slug = (slug or "").strip()
    if not slug:
        raise ValueError("Slug Manhwaindo kosong")
    html = _fetch("/series/%s/" % urllib.parse.quote(slug, safe=""))
    title = _meta_content(html, "og:title") or ""
    title = re.sub(r"\s*[—–-]\s*Baca Online di ManhwaIndo\s*$", "", title).strip()
    if not title:
        h1 = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S | re.I)
        title = _clean_text(h1.group(1)) if h1 else slug.replace("-", " ").title()
    cover = _meta_content(html, "og:image") or ""
    description = _meta_content(html, "og:description") or ""
    # Sinopsis: teks setelah heading "Sinopsis" (sebelum label Penulis).
    sm = re.search(r"Sinopsis(.{0,3000}?)Penulis\s*:", html, re.S | re.I)
    if sm:
        syn = _clean_text(sm.group(1))
        if len(syn) > 80:
            description = syn
    # Tahun rilis + status.
    year = ""
    ym = re.search(r"Tahun Rilis[^0-9]{0,80}(\d{4})", html)
    if ym:
        year = ym.group(1)
    status = ""
    stm = re.search(r"(Manhwa|Manga|Manhua)(?:<[^>]+>|\s)+(Ongoing|Completed)",
                    html)
    if stm:
        status = stm.group(2)
    # Genre (untuk filter dewasa) dari genre links.
    genres = [g.strip() for g in re.findall(
        r'href="[^"]*(?:genre|genres)[^"]*"[^>]*>([^<]+)</a>', html, re.I)]
    genre_text = " ".join(genres)
    if _adult_in_text(title) or _adult_in_text(genre_text):
        raise ValueError("Judul dewasa diblokir")
    if safe_mode and any(k in _norm(genre_text).split()
                         for k in _ADULT_SAFE):
        raise ValueError("Judul dewasa diblokir")
    # Alt-title: teks tepat setelah </h1>, sebelum label "Manhwa Ongoing".
    alt_titles = []
    h1 = re.search(r"<h1[^>]*>.*?</h1>", html, re.S | re.I)
    if h1:
        tail = _clean_text(html[h1.end():h1.end() + 800])
        tail = re.split(r"\bManhwa\s+(?:Ongoing|Completed)\b", tail)[0].strip()
        if tail and ("," in tail or "，" in tail):
            for part in re.split(r"[,，]", tail):
                part = part.strip()
                if part:
                    alt_titles.append(part)
    # SeriesId + endpoint daftar bab.
    cfg = re.search(r"agcChapterListConfig\s*=\s*\{([^}]*)\}", html, re.S)
    series_id = None
    endpoint = "/wp-json/agc/v1/chapters"
    if cfg:
        body = cfg.group(1)
        im = re.search(r"seriesId:\s*(\d+)", body)
        if im:
            series_id = im.group(1)
        em = re.search(r'endpoint:\s*"((?:[^"\\]|\\.)*)"', body)
        if em:
            ep = em.group(1).replace("\\/", "/")
            # Endpoint di config berupa URL penuh; normalisasi jadi path
            # agar _fetch_json (yang memakai rotasi mirror) tetap benar.
            ep = re.sub(r"^https?://[^/]+", "", ep)
            if ep.startswith("/"):
                endpoint = ep
    if not series_id:
        raise ValueError("Daftar bab Manhwaindo tidak ditemukan")
    data = _fetch_json(endpoint + "?series=" + urllib.parse.quote(series_id))
    if not isinstance(data, list) or not data:
        raise ValueError("Daftar bab Manhwaindo tidak ditemukan")
    chapters = []
    seen = set()
    for it in data:
        if not isinstance(it, dict):
            continue
        url = str(it.get("url") or "").strip()
        num = str(it.get("chapter") or "").strip()
        if not url or not num:
            continue
        ch_slug = url.rstrip("/").split("/")[-1]
        if not ch_slug or ch_slug in seen:
            continue
        seen.add(ch_slug)
        chapters.append({
            "id": "mi:" + ch_slug,
            "chapter": num,
            "title": "",
            "time": str(it.get("time") or ""),
            "lang": "id",
            "source": "Manhwaindo",
        })
    if not chapters:
        raise ValueError("Daftar bab Manhwaindo tidak ditemukan")
    chapters.sort(key=lambda c: _chapter_sort_key(c["chapter"]))
    return {
        "id": "mi:" + slug,
        "title": title,
        "cover": cover,
        "status": status,
        "year": year,
        "description": description,
        "chapters": chapters,
        "total": len(chapters),
        "alt_titles": alt_titles,
    }


def _chapter_sort_key(num_s):
    try:
        return (0, float(num_s))
    except (TypeError, ValueError):
        return (1, 0.0)


def get_chapter_pages(chapter_slug):
    """Daftar URL gambar satu bab. Hanya gambar halaman yang diterima:
    URL mengandung /manga-images/ + /chapter- dan berakhiran gambar,
    bukan thumbnail dan bukan aset iklan (iklan situs memakai host lain
    dan tidak mengandung /manga-images/)."""
    chapter_slug = (chapter_slug or "").strip()
    if not chapter_slug:
        raise ValueError("ID bab Manhwaindo kosong")
    html = _fetch("/%s/" % urllib.parse.quote(chapter_slug, safe=""))
    pages = []
    for tag in re.finditer(r"<img[^>]+>", html):
        u = _img_from_tag(tag.group(0))
        if not u or u.startswith("data:"):
            continue
        if "/manga-images/" not in u or "/chapter-" not in u:
            continue
        if "thumbnail" in u:
            continue
        if not re.search(r"\.(jpe?g|png|webp)(\?|#|$)", u, re.I):
            continue
        # Normalisasi host gambar yang terverifikasi https -> https.
        if u.startswith("http://imgx-id.gmbr.pro/"):
            u = "https://" + u[len("http://"):]
        if u not in pages:
            pages.append(u)
    if not pages:
        raise ValueError("Halaman bab Manhwaindo tidak ditemukan")
    return {"id": "mi:" + chapter_slug, "pages": pages, "total": len(pages)}


def latest_updates(limit=24, safe_mode=1):
    """Feed bab terbaru dari bagian Latest Update beranda Manhwaindo.

    Beranda adalah server-rendered. Satu judul muncul dua kali di
    bagian ini sebagai dua anchor class=\"series\" berurutan: yang
    pertama membungkus poster (img manga-images, tanpa daftar bab),
    yang kedua membungkus judul + <ul> berisi <li> per bab (anchor ke
    /<slug>-chapter-<num>/ berlabel \"Ch. N\" + <span> waktu relatif
    seperti \"1 minute lalu\"). Parser menggabungkan kedua salinan
    jadi satu kartu logis. Rail hanya memakai bab TERBARU tiap judul
    (satu entri per judul, urutan beranda = terbaru dulu).
    Judul dewasa selalu disaring (kata kunci dewasa pada judul; Mode
    Aman juga menyaring ecchi/smut pada judul)."""
    html = _fetch("/")
    # Batas bagian dicari lewat heading h1/h2 (teks heading di HTML
    # mentah bisa diselingi tag/span, jadi pencarian string mentah
    # 'Latest Update' tidak bisa diandalkan — terbukti gagal pada
    # fetch yang heading-nya tetap ada setelah tag di-strip).
    heads = [(m.end(), _clean_text(m.group(1))) for m in
             re.finditer(r"<h[12][^>]*>(.*?)</h[12]>", html, re.S | re.I)]
    start = next((pos for pos, t in heads if t == "Latest Update"), None)
    if start is None:
        raise ValueError("Bagian Latest Update Manhwaindo tidak ditemukan")
    end = next((pos for pos, t in heads
                if t == "Blog Updates" and pos > start), None)
    section = html[start:end] if end else html[start:]
    series_anchors = list(re.finditer(
        r'<a[^>]+class="series"[^>]+href="[^"]*/series/([^"/\?#]+)/?"[^>]*>',
        section, re.I))
    if not series_anchors:
        raise ValueError("Bagian Latest Update Manhwaindo tidak ditemukan")
    # Satu judul muncul DUA kali di HTML (dua anchor class="series"
    # yang berurutan): salinan pertama membungkus poster (ada img,
    # TANPA daftar bab), salinan kedua membungkus judul + <ul> daftar
    # bab (tanpa img). Jadi kartu logis = gabungan semua salinan satu
    # slug: judul/cover/bab diisi dari salinan mana pun yang punya.
    cards = {}
    order = []
    for idx, m in enumerate(series_anchors):
        slug = m.group(1).strip()
        if not slug:
            continue
        end = series_anchors[idx + 1].start() if idx + 1 < len(series_anchors) \
            else len(section)
        region = section[m.start():end]
        if slug not in cards:
            cards[slug] = {"title": "", "cover": "", "chapter": "",
                           "chapter_slug": "", "time_text": ""}
            order.append(slug)
        entry = cards[slug]
        if not entry["title"]:
            tm = re.search(r'title="([^"]+)"', m.group(0))
            if tm:
                entry["title"] = htmlmod.unescape(tm.group(1)).strip()
            if not entry["title"]:
                dm = re.search(r"truncate[^>]*>([^<]+)</div>", region)
                entry["title"] = _clean_text(dm.group(1)) if dm else ""
        if not entry["cover"]:
            for tag in re.finditer(r"<img[^>]+>", region):
                u = _img_from_tag(tag.group(0))
                if u and "manga-images" in u and u.startswith("http"):
                    entry["cover"] = u
                    break
        if not entry["chapter"]:
            cm = re.search(
                r'<a[^>]+href="[^"]*/([^"/\?#]+-chapter-[^"/\?#]+)/?"[^>]*>'
                r"\s*Ch\.\s*([\d]+(?:\.[\d]+)?)",
                region, re.I)
            if cm:
                entry["chapter_slug"] = cm.group(1).strip()
                entry["chapter"] = cm.group(2).strip()
                time_m = re.search(
                    r'<span[^>]*class="text-xs"[^>]*>([^<]+)</span>',
                    region[cm.end():cm.end() + 800], re.S)
                entry["time_text"] = _clean_text(time_m.group(1)) \
                    if time_m else ""
    items = []
    for slug in order:
        e = cards[slug]
        if not e["title"] or not e["chapter"]:
            continue
        if _adult_in_title(e["title"]):
            continue
        if safe_mode and any(k in _norm(e["title"]).split()
                             for k in _ADULT_SAFE):
            continue
        items.append({
            "title": e["title"],
            "series_slug": slug,
            "chapter": e["chapter"],
            "chapter_slug": e["chapter_slug"],
            "time_text": e["time_text"],
            "cover": e["cover"],
            "lang": "id",
            "source": "Manhwaindo",
        })
    if not items:
        raise ValueError("Bagian Latest Update Manhwaindo tidak ditemukan")
    items = items[:max(1, limit)]
    return {"items": items, "total": len(items), "source": "Manhwaindo"}
