"""Scraper dan resolver stream untuk Samehadaku (https://samehadaku.or.at,
cadangan https://samehadaku.st).

Situs streaming anime subtitle Indonesia (WordPress): katalog via WP REST
search, halaman anime statis berisi daftar episode + daftar mirror berupa
<select> berisi iframe base64 (tanpa butuh JS / admin-ajax). Mirror bervariasi
per episode: vkspeed.com (tanpa Cloudflare, mp4 langsung) atau upbolt.to
(sering 403 Cloudflare dari IP datacenter — dicoba juga, gagal = lanjut ke
mirror berikutnya). Rantai resolve: cari judul -> halaman anime -> daftar
episode -> halaman episode (ambil mirror) -> halaman embed (gali stream) ->
probe media (v66) sebelum diserahkan ke player.

Pola mengikuti animeindo.py (fetch + desync + diagnosa host-only) dan
otakudesu.py (kontrak dict terstandar). Subtitle: video hardsub Indonesia
seperti Otakudesu; softsub dipanen main.py dari HiAnime bila tersedia.

PENTING — dua jebakan Samehadaku yang sudah diverifikasi:
1. Pencarian `/?s=` TIDAK memfilter (halaman selalu menampilkan anime terbaru
   apa pun query-nya). Pencarian yang benar adalah WP REST:
   `/wp-json/wp/v2/search?search=<kata>&per_page=10` (menyaring di sisi
   server; subtype "anime" di .or.at, "series" di .st).
2. Kata "hentai" muncul di SETIAP halaman anime (widget filter genre situs +
   kartu anime lain) — guard hentai HANYA boleh membaca blok Genres MILIK
   anime itu sendiri (`<b>Genres</b>:`), bukan `in html.lower()` mentah.
"""

import base64
import re
import time
import html as htmlmod
import urllib.parse

from api.hianime import _probe_media as _v66_probe_media
from api.hianime import _host_from as _host_from_url

SAMEHADAKU_BASES = ["https://samehadaku.or.at", "https://samehadaku.st"]

# Kata yang tidak pernah dipakai sebagai kata kunci pencarian tunggal.
_STOPWORDS = {
    "the", "and", "under", "with", "from", "into", "season", "part",
    "movie", "film", "episode", "special", "ova", "ona", "no", "wa",
    "ga", "wo", "ni", "de", "to", "a", "an", "of", "in", "on",
}


def _fetch(url: str, referer: str = None, timeout: int = 12, deadline=None) -> str:
    """Fetch via otakudesu._fetch (curl_cffi chrome124 -> httpx -> desync
    v1/v2, lengkap dengan pencatatan diagnosa host-only). Pesan gagal
    ditulis ulang tanpa URL lengkap (hanya host) supaya tidak bocor ke
    pesan akhir / log."""
    from api.otakudesu import _fetch as _otaku_fetch
    try:
        return _otaku_fetch(url, referer=referer, timeout=timeout, deadline=deadline)
    except RuntimeError as e:
        msg = str(e or "")
        if "Samehadaku" in msg:
            raise
        raise RuntimeError(f"Gagal memuat halaman Samehadaku ({_host_from_url(url)})")
    except Exception as e:
        raise RuntimeError(f"Gagal memuat halaman Samehadaku ({_host_from_url(url)}): {e}")


def _wp_search(query: str, base: str, deadline=None) -> list:
    """Cari anime via WP REST search (menyaring di sisi server).
    Return [{title, url, id}] — hanya subtype anime/series.
    Return [] bila pencarian valid tapi 0 hasil; None bila fetch/JSON
    gagal (pembedaan ini penting: mirror lain hanya dicoba bila basis
    utama ERROR, bukan bila 0 hasil — 0 hasil di mirror lain hampir
    selalu juga 0/wrong, dan tiap fetch menelan ~2 dtk anggaran)."""
    import json as _json
    url = (f"{base}/wp-json/wp/v2/search?search="
           f"{urllib.parse.quote_plus(query)}&per_page=10")
    try:
        raw = _fetch(url, deadline=deadline)
    except Exception:
        return None
    try:
        data = _json.loads(raw)
    except Exception:
        return None
    out = []
    for it in data if isinstance(data, list) else []:
        if not isinstance(it, dict):
            continue
        if it.get("subtype") not in ("anime", "series"):
            continue
        title = htmlmod.unescape(str(it.get("title") or "").strip())
        link = str(it.get("url") or "").strip()
        if not title or not link:
            continue
        slug = link.rstrip("/").split("/")[-1]
        out.append({"title": title, "url": link, "id": slug, "poster": ""})
    return out


def search(query: str, limit: int = 10, deadline=None) -> list:
    """Cari anime di Samehadaku. Return [{title, url, id, poster}].
    Jalur: judul penuh dulu, lalu kata-kata tunggal yang khas (kata pertama
    yang mengembalikan hasil biasanya sudah cukup — verifikasi ketat
    judul terjadi di resolve_stream via blok judul alternatif)."""
    from api.otakudesu import _is_title_match as _match
    clean_q = re.sub(r"[^\w\s-]", "", query).strip()
    results = []
    seen = set()

    def _add(cands):
        for c in cands:
            if c["url"] in seen:
                continue
            seen.add(c["url"])
            results.append(c)
            if len(results) >= limit:
                return True
        return False

    for base in SAMEHADAKU_BASES:
        if _add(_wp_search(clean_q, base, deadline=deadline) or []):
            break
        # Kata tunggal yang khas, sesuai urutan kemunculan di judul.
        words = [w for w in re.split(r"[\s-]+", clean_q.lower())
                 if len(w) >= 3 and w not in _STOPWORDS]
        for w in words[:5]:
            if _add(_wp_search(w, base, deadline=deadline) or []):
                break
        if results:
            break
    # Saring ringan: buang kandidat yang judul romajinya jelas tidak
    # berhubungan sama sekali (verifikasi ketat tetap di resolve_stream).
    if results and clean_q:
        kept = [r for r in results if _match(clean_q, r.get("title", ""))]
        return kept or results
    return results


def _anime_page_info(anime_url: str, deadline=None) -> dict:
    """Ambil info halaman anime: judul, judul alternatif (blok alter),
    genre MILIK anime ini saja, dan daftar episode. Raise bila 404/gagal."""
    html = _fetch(anime_url, deadline=deadline)
    title_m = re.search(r"<title>(.*?)</title>", html, re.S)
    title = htmlmod.unescape(title_m.group(1).strip()) if title_m else ""
    # Judul situs berakhir "... Sub Indo - Samehadaku" (.or.at) atau
    # "... Subtitle Indonesia Samehadaku" (.st) — buang suffix situsnya.
    title = re.sub(r"(\s*[-–|]\s*Samehadaku|\s+Samehadaku)\s*$", "", title).strip()
    alter = ""
    am = re.search(r'<span class="alter">([^<]*)</span>', html)
    if am:
        alter = htmlmod.unescape(am.group(1).strip())
    genres = []
    gm = re.search(r"<b>Genres</b>:(.*?)</span>", html, re.S)
    if gm:
        genres = [htmlmod.unescape(g).strip().lower()
                  for g in re.findall(r'rel="tag">([^<]+)</a>', gm.group(1))]
    episodes = []
    seen = set()
    for href, ep_no in re.findall(
        r'href="((?:https://[^"/]+)?/[^"/]*episode-(\d+)-subtitle-indonesi[a]?/?)"',
        html,
    ):
        full = urllib.parse.urljoin(anime_url, href)
        if full in seen:
            continue
        seen.add(full)
        episodes.append({"ep": int(ep_no), "title": f"Episode {ep_no}", "url": full})
    episodes.sort(key=lambda x: x["ep"])
    return {"title": title, "alter": alter, "genres": genres,
            "episodes": episodes, "url": anime_url}


def get_episodes(anime_url_or_slug: str, deadline=None) -> list:
    """Ambil daftar episode dari halaman anime Samehadaku.
    Return [{ep, title, url}] urut ep kecil ke besar."""
    if anime_url_or_slug.startswith("http"):
        url = anime_url_or_slug
    else:
        slug = anime_url_or_slug.strip().strip("/")
        url = f"{SAMEHADAKU_BASES[0]}/anime/{slug}/"
    info = _anime_page_info(url, deadline=deadline)
    return info["episodes"]


def _extract_mirrors(ep_html: str) -> list:
    """Ambil daftar mirror dari halaman episode: <select class="mirror">
    berisi <option value="<iframe base64>">. Return [(label, embed_url)]."""
    out = []
    for val, label in re.findall(
        r'<option value="([^"]+)"[^>]*>([^<]*)</option>', ep_html
    ):
        val = val.strip()
        if not val:
            continue
        try:
            dec = base64.b64decode(val).decode("utf-8", errors="ignore")
        except Exception:
            continue
        m = re.search(r'src="([^"]+)"', dec)
        if not m:
            continue
        u = m.group(1)
        if u.startswith("//"):
            u = "https:" + u
        if u.startswith("http"):
            out.append((label.strip() or "Video", u))
    return out


def _unpack_packed_js(packed_call: str) -> str:
    """Bongkar Dean Edwards packer `eval(function(p,a,c,k,e,d){...})`
    (dipakai halaman embed vkspeed) — operasi string murni, tanpa
    mengeksekusi JS halaman. Return kode hasil bongkaran."""
    def _read_sq(s, i):
        assert s[i] == "'"
        out = []
        i += 1
        while i < len(s):
            c = s[i]
            if c == "\\" and i + 1 < len(s):
                out.append(s[i:i + 2])
                i += 2
                continue
            if c == "'":
                return "".join(out), i + 1
            out.append(c)
            i += 1
        raise ValueError("unterminated string")

    def _js_unescape(s):
        return s.replace("\\'", "'").replace('\\"', '"').replace("\\\\", "\\")

    def _base36(n):
        d = "0123456789abcdefghijklmnopqrstuvwxyz"
        if n == 0:
            return "0"
        r = ""
        while n:
            r = d[n % 36] + r
            n //= 36
        return r

    i = packed_call.index("}('") + 2
    payload_raw, i = _read_sq(packed_call, i)
    m = re.match(r",\s*(\d+)\s*,\s*(\d+)\s*,\s*'", packed_call[i:])
    if not m:
        raise ValueError("packed args tidak dikenali")
    a, c = int(m.group(1)), int(m.group(2))
    i += m.end() - 1
    k_raw, _i = _read_sq(packed_call, i)
    k = _js_unescape(k_raw).split("|")
    p = _js_unescape(payload_raw)
    for n in range(c - 1, -1, -1):
        if n < len(k) and k[n]:
            p = re.sub(r"\b" + _base36(n) + r"\b", k[n], p)
    return p


def _extract_stream_from_player(player_html: str) -> list:
    """Gali URL stream dari halaman embed. Return [(url, kind, label)]
    dengan kind 'm3u8' atau 'mp4' — m3u8 didahulukan. vkspeed
    menyembunyikan URL di JS packer (jwplayer sources + label kualitas);
    host lain biasanya menaruhnya langsung."""
    out = []

    def _add(u, kind, label=""):
        if u.startswith("//"):
            u = "https:" + u
        if u.startswith("http") and (u, kind) not in [(x[0], x[1]) for x in out]:
            out.append((u, kind, label))

    for u in re.findall(r'https?://[^"\'\s\\]+\.m3u8[^"\'\s\\]*', player_html):
        _add(u, "m3u8")
    if out:
        return out
    # JS packer (vkspeed): bongkar, lalu baca jwplayer sources + label.
    start = player_html.find("eval(function(p,a,c,k,e,d)")
    if start >= 0:
        end = player_html.find("</script>", start)
        try:
            unpacked = _unpack_packed_js(player_html[start:end if end > 0 else None])
            for m in re.finditer(
                r'file\s*:\s*"(https?://[^"]+)"\s*,\s*label\s*:\s*"([^"]+)"',
                unpacked,
            ):
                kind = "m3u8" if ".m3u8" in m.group(1) else "mp4"
                _add(m.group(1), kind, m.group(2))
            if out:
                return out
        except Exception:
            pass
    # Blogger (video.g): URL videoplayback ter-encode unicode (\u003d dst)
    # di dalam data halaman — bongkar escape-nya lalu ambil URL-nya.
    # itag 18=360p mp4, 22=720p, 37=1080p. URL-nya terikat IP + expire,
    # tapi di-resolve fresh tiap request dan dipakai langsung oleh
    # backend yang sama (probe + proxy), jadi konsisten.
    if "videoplayback" in player_html and "googlevideo.com" in player_html:
        _unesc = re.sub(r"\\u([0-9a-fA-F]{4})",
                        lambda m: chr(int(m.group(1), 16)), player_html)
        _unesc = htmlmod.unescape(_unesc)
        for _m in re.finditer(
            r"(https?://[^\s\"'\\<>]*googlevideo\.com/videoplayback[^\s\"'\\<>]*)",
            _unesc,
        ):
            _u = _m.group(1).rstrip(",")
            _itag = re.search(r"[?&]itag=(\d+)", _u)
            _lbl = {"18": "360p", "22": "720p", "37": "1080p",
                    "43": "360p"}.get(_itag.group(1) if _itag else "", "HD")
            _add(_u, "mp4", _lbl)
        if out:
            return out
    for pat in (r'file\s*:\s*["\']([^"\']+)',
                r'<source[^>]+src="([^"]+)"',
                r'https?://[^"\'\s]+\.mp4[^"\'\s]*'):
        for mm in re.finditer(pat, player_html):
            u = mm.group(1) if mm.lastindex else mm.group(0)
            kind = "m3u8" if ".m3u8" in u else "mp4"
            _add(u, kind)
        if out:
            return out
    return out


def _slug_variants(clean_slug: str) -> list:
    """Varian slug untuk percobaan halaman anime langsung. Samehadaku
    memakai 'season-2' sementara Lumio memakai '2nd-season' (dan
    sebaliknya) — coba kedua bentuk + tanpa penanda musim."""
    out = []
    for s in (clean_slug,
              re.sub(r"-(\d+)(?:nd|rd|th|st)-season$", r"-season-\1", clean_slug),
              re.sub(r"-season-(\d+)$", r"-\1nd-season", clean_slug),
              re.sub(r"-(?:\d+(?:nd|rd|th|st)-season|season-\d+|s\d+)$", "", clean_slug)):
        if s and s not in out:
            out.append(s)
    return out


def _season_num(t: str):
    """Nomor musim EKSPLISIT dari judul/slug ('Season 2', '2nd Season',
    'S2') — None bila tidak ada penanda. Dipakai _verify_candidate
    untuk menolak kandidat yang musimnya jelas-jelas beda (mis. halaman
    Season 3 untuk query Season 2). Fail-open: bila salah satu sisi
    tidak berpenanda, tidak ditolak (banyak halaman S1 tanpa penanda)."""
    m = re.search(r"(?:season[-\s]*(\d+)|(\d+)(?:nd|rd|th|st)[-\s]*season|\bs(\d+)\b)",
                  t or "", re.I)
    if not m:
        return None
    for g in m.groups():
        if g:
            return int(g)
    return None


def _verify_candidate(clean_title: str, info: dict) -> bool:
    """Verifikasi ketat: judul EN cocok dengan judul alternatif halaman
    anime, atau cocok romaji via _is_title_match. Mencegah fallback buta
    ke hasil pertama (judul Season lain / judul mirip)."""
    from api.otakudesu import _is_title_match as _match
    # Penjaga musim: kedua sisi berpenanda eksplisit tapi beda -> tolak.
    _cs = _season_num(clean_title)
    _ps = _season_num(info.get("title", ""))
    if _cs is not None and _ps is not None and _cs != _ps:
        return False
    if _match(clean_title, info.get("title", "")):
        return True
    alter = info.get("alter", "") or ""
    if alter:
        norm_title = re.sub(r"[^a-z0-9]", "", clean_title.lower())
        norm_alter = re.sub(r"[^a-z0-9]", "", alter.lower())
        if norm_title and norm_title in norm_alter:
            return True
        for alt in alter.split(","):
            if _match(clean_title, alt.strip()):
                return True
    return False


def resolve_stream(title_or_slug: str, ep: int = 1, deadline=None) -> dict:
    """Selesaikan URL stream Samehadaku untuk anime & episode tertentu.
    Return dict terstandar (master, variants, referer, server, ...).
    Master bisa m3u8 ATAU mp4 progresif (vkspeed hanya menyediakan mp4) —
    keduanya diprobe sebelum diserahkan (v66, via _probe_media)."""
    if "hentai" in (title_or_slug or "").lower():
        raise RuntimeError("Konten Hentai tidak tersedia di Samehadaku")

    from api.otakudesu import _is_title_match as _match

    # 1. Temukan halaman anime: WP search DULU, tebakan slug langsung
    # belakangan. Alasannya anggaran: tiap tebakan slug yang 404 memicu
    # kaskade desync di _fetch_once (direct curl + httpx + desync v1+v2,
    # sampai ~15 dtk per URL) — 16 tebakan bisa menghabiskan seluruh
    # anggaran 25 dtk sebelum search sempat jalan (terbukti di uji live
    # v72: 2 tebakan 404 -> 2 gantungan desync -> anggaran habis).
    # WP search menyaring di sisi server (1 fetch per kata) dan langsung
    # mengembalikan URL halaman anime yang benar.
    anime_url = None
    info = None
    if "samehadaku" in title_or_slug and ("/anime/" in title_or_slug or "/series/" in title_or_slug):
        anime_url = title_or_slug
    else:
        slug_in = title_or_slug.strip().strip("/")
        clean_str = re.sub(r"-(?=[a-z0-9]{5,10}$)[a-z0-9]*\d[a-z0-9]*$", "", slug_in)
        clean_slug = re.sub(r"-\d+$", "", clean_str)
        clean_title = clean_slug.replace("-", " ").strip()
        # 1a. WP REST search + verifikasi ketat via halaman anime.
        # Kata tunggal diurutkan PANJANG-DULU (kata khas cenderung panjang;
        # 'zero'/'goddess' mengalahkan 'full'/'world') dan dibatasi 6 kata.
        # Per query HANYA kandidat teratas yang diverifikasi (peringkat-1
        # untuk kata khas hampir selalu benar; tiap verifikasi = 1 fetch
        # ~2 dtk, jadi verifikasi massal menghabiskan anggaran — terbukti
        # di uji live v72: 15 fetch sukses tapi anggaran habis sebelum
        # judul ketemu). Mirror (.st) dicoba HANYA bila basis utama ERROR
        # (None) — bila basis utama mengembalikan 0 hasil, mirror hampir
        # selalu juga 0/salah, dan tiap fetch menelan ~2 dtk.
        # Pencarian kata dijalankan PARALEL (3 worker): tiap fetch ke
        # Samehadaku menelan 1,5-3,5 dtk dari sandbox (IP datacenter kena
        # scrutiny Cloudflare); 7 query sekuensial = ~18 dtk hanya untuk
        # fase search. Paralel memangkasnya jadi ~5-6 dtk. Verifikasi
        # tetap sekuensial menurut prioritas kata (kandidat pertama yang
        # lolos menghentikan pencarian).
        _words = [w for w in re.split(r"[\s-]+", clean_title.lower())
                  if len(w) >= 3 and w not in _STOPWORDS]
        _words = sorted(_words, key=len, reverse=True)[:6]
        _seen_q = []
        for _q in [clean_title] + _words:
            if _q and _q not in _seen_q:
                _seen_q.append(_q)
        queries = _seen_q

        def _search_one(_q):
            for _b in SAMEHADAKU_BASES:
                _r = _wp_search(_q, _b, deadline=deadline)
                if _r is None:
                    continue  # error -> coba mirror
                return _r  # hasil (kosong/tidak) -> cukup
            return None

        import concurrent.futures as _cf
        _ex = _cf.ThreadPoolExecutor(max_workers=3)
        _futs = {}
        try:
            for _q in queries:
                _futs[_q] = _ex.submit(_search_one, _q)
            verified = 0
            for _q in queries:
                try:
                    _cands = _futs[_q].result()
                except Exception:
                    continue
                if not _cands:
                    continue
                try:
                    _info = _anime_page_info(_cands[0]["url"], deadline=deadline)
                except Exception:
                    continue
                verified += 1
                if _info["episodes"] and _verify_candidate(clean_title, _info):
                    anime_url, info = _cands[0]["url"], _info
                    break
                if verified >= 6:
                    break
        finally:
            # Batalkan pencarian yang belum jalan; yang sedang berjalan
            # dibiarkan selesai sendiri (dibatasi deadline).
            _ex.shutdown(wait=False, cancel_futures=True)
        # 1b. Tebakan slug langsung (fallback bila search tidak menemukan
        # apa-apa). DIBATASI 6 percobaan — tiap 404 bisa menelan ~15 dtk
        # lewat kaskade desync, jadi jangan biarkan tebakan menghabiskan
        # anggaran yang dibutuhkan episode/embed/probe.
        if not anime_url:
            _tries = 0
            for base in SAMEHADAKU_BASES:
                for variant in _slug_variants(clean_slug):
                    for path in ("anime", "series"):
                        if _tries >= 6:
                            break
                        _tries += 1
                        try:
                            cand = f"{base}/{path}/{variant}/"
                            _info = _anime_page_info(cand, deadline=deadline)
                            if _info["episodes"] and _verify_candidate(clean_title, _info):
                                anime_url, info = cand, _info
                                break
                        except Exception:
                            continue
                        if anime_url:
                            break
                    if anime_url:
                        break
                if anime_url:
                    break
        if not anime_url:
            raise RuntimeError(
                f"Anime '{title_or_slug}' tidak memiliki judul yang cocok di Samehadaku"
            )

    if info is None:
        info = _anime_page_info(anime_url, deadline=deadline)
    # Guard genre: HANYA blok Genres milik anime ini (bukan seluruh HTML).
    if any(g == "hentai" for g in info.get("genres", [])):
        raise RuntimeError("Konten genre Hentai diblokir di Samehadaku")

    episodes = info.get("episodes") or []
    if not episodes:
        raise RuntimeError("Tidak ada daftar episode di Samehadaku")
    target_ep = next((e for e in episodes if e["ep"] == int(ep)), None)
    if not target_ep:
        idx = int(ep) - 1
        if 0 <= idx < len(episodes):
            target_ep = episodes[idx]
        else:
            raise RuntimeError(f"Episode {ep} belum rilis di Samehadaku")

    # 2. Halaman episode -> daftar mirror -> halaman embed -> stream.
    ep_html = _fetch(target_ep["url"], referer=anime_url, deadline=deadline)
    mirrors = _extract_mirrors(ep_html)
    if not mirrors:
        raise RuntimeError("Pemutar Samehadaku tidak ditemukan untuk episode ini")
    last_err = None
    for _label, embed_url in mirrors:
        try:
            origin = f"{urllib.parse.urlparse(embed_url).scheme}://{urllib.parse.urlparse(embed_url).netloc}"
            player_html = _fetch(embed_url, referer=target_ep["url"], deadline=deadline)
            streams = _extract_stream_from_player(player_html)
            if not streams:
                last_err = RuntimeError(f"Stream tidak ditemukan dari pemutar ({_host_from_url(embed_url)})")
                continue
            # Utamakan m3u8; bila hanya ada mp4, ambil kualitas tertinggi
            # (label 360p > 192p) — JANGAN urut alfabet URL (v192 < v360
            # secara alfabet, padahal 360p yang tertinggi).
            def _qnum(label):
                m = re.search(r"(\d+)", label or "")
                return int(m.group(1)) if m else 0
            streams.sort(key=lambda s: (0 if s[1] == "m3u8" else 1,
                                        -_qnum(s[2]), s[0]))
            # 3. Validasi media (v66): master mati tidak boleh sampai ke player.
            # mp4 divalidasi dengan probe range (ftyp + video/mp4), bukan
            # sebagai m3u8 — _probe_media memilih jalur sesuai jenis media.
            best_url, best_kind, _best_label = streams[0]
            _v66_probe_media(best_url, referer=origin + "/", deadline=deadline)
            mp4s = [(u, lb) for (u, k, lb) in streams if k == "mp4"]
            m3u8s = [u for (u, k, _lb) in streams if k == "m3u8"]
            variants = []
            if m3u8s:
                variants = [{"q": "auto", "url": m3u8s[0]}]
            else:
                # Urutkan mp4 dari kualitas tertinggi (label 360p > 192p).
                ordered = sorted(mp4s, key=lambda t: _qnum(t[1]), reverse=True)
                for u, lb in ordered:
                    q = lb if re.match(r"^\d+p$", (lb or "").strip()) else (
                        f"{_qnum(lb)}p" if _qnum(lb) else "HD")
                    variants.append({"q": q, "url": u})
                if not variants:
                    variants = [{"q": "HD", "url": best_url}]
            return {
                "master": best_url,
                "variants": variants,
                "sub": None,
                "sub_lang": "Indonesian (Hardsub)",
                "referer": origin + "/",
                "server": "Samehadaku (Sub Indo)",
                "source": "samehadaku",
                "tracks": [],
                "subtitles": [],
            }
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(str(last_err or "Semua mirror Samehadaku gagal"))
