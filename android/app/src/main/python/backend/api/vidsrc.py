"""Resolver stream Movie/Series via embed provider (pola POC 13 Nov 2026).

Rantai (dari kode publik OpenStream/CinePulse, MIT — pola, bukan salinan):
  embed shell -> /vs_src.php (token SEGAR tiap resolve) -> player URL
  (domain player DINAMIS dari token endpoint, bukan konstanta) -> halaman
  player -> iframe bersarang (1 level) -> hoster -> unpack p.a.c.k.e.r /
  parse jwplayer / Voe / ok.ru -> URL .m3u8 -> validasi manifest.

BATAS KERAS: playback client-side saja (pola resolver anime yang sudah ada);
TIDAK ada download/rip/re-host (validasi hanya 8KB pertama manifest, segmen
.ts tidak pernah diunduh); TIDAK ada circumvention DRM; TIDAK ada ekstraksi
kredensial/key. Token ?vs= diambil segar tiap resolve, tidak di-hardcode.

Bentuk kembalian resolve_stream: dict yang sama dengan resolver HiAnime
{master, variants, sub, sub_lang, referer, subtitles, intro, outro, server}
sehingga endpoint /api/stream/resolve_movie|resolve_series dan player HLS +
panel kualitas + engine subtitle dipakai ulang tanpa perubahan.

STATUS SANDBOX (13 Nov 2026): hop token lolos, hop player 403 (blokir IP
datacenter, sekelas AniList). Hop terakhir HANYA terbukti di perangkat user.
"""
import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36")

EMBED_BASE = "https://vidsrc.me"
TOKEN_URL = EMBED_BASE + "/vs_src.php"

HOSTER_FAMILIES = {
    "filemoon": "packed_file", "moonplayer": "packed_file",
    "kerapoxy": "packed_file", "fmoonembed": "packed_file",
    "moflix": "packed_file", "cinegrab": "packed_file",
    "streamwish": "packed_links", "vidhide": "packed_links",
    "filelions": "packed_links", "streamhg": "packed_links",
    "dhcplay": "packed_links", "hglink": "packed_links",
    "voe": "voe", "vidmoly": "jwplayer", "mp4upload": "mp4upload",
    "ok.ru": "okru",
}


def _fetch(url, referer=None, timeout=20, max_bytes=None):
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read(max_bytes) if max_bytes else resp.read()
        return resp.status, body


def _unpack_packer(packed_js):
    """Unpacker p.a.c.k.e.r Dean Edwards generik (dekompresi standar)."""
    m = re.search(
        r"eval\(function\(p,a,c,k,e,d\).*?\}\('(.*?)',(\d+),(\d+),'(.*?)'\.split",
        packed_js, re.S)
    if not m:
        return ""
    payload, radix, dictionary = m.group(1), int(m.group(2)), m.group(4).split("|")

    def base(n, b):
        digits = "0123456789abcdefghijklmnopqrstuvwxyz"
        out = ""
        while n > 0:
            out = digits[n % b] + out
            n //= b
        return out or "0"

    lookup = {}
    for i, word in enumerate(dictionary):
        if word:
            lookup[base(i, radix)] = word

    def repl(match):
        return lookup.get(match.group(0), match.group(0))

    return re.sub(r"\b\w+\b", repl, payload)


def _rot13(s):
    return s.translate(str.maketrans(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
        "NOPQRSTUVWXYZABCDEFGHIJKLMnopqrstuvwxyzabcdefghijklm"))


def _extract_candidates(html):
    """Kandidat URL stream dari halaman player/hoster. Urutan: unpack
    packed-script dulu (Filemoon/StreamWish/VidHide), lalu jwplayer/Voe/
    Mp4Upload/ok.ru, lalu .m3u8 telanjang. Return [(url, how)] dedup."""
    found = []
    seen = set()

    def add(url, how):
        if url and url not in seen and len(url) < 2000:
            seen.add(url)
            found.append((url, how))

    texts = [html]
    for packed in re.findall(r"eval\(function\(p,a,c,k,e,d\).*?\}\(.*?\.split\('\|'\)[^)]*\)\)", html, re.S):
        texts.append(_unpack_packer(packed))

    for text in texts:
        for m in re.findall(r'file\s*:\s*"([^"]+\.m3u8[^"]*)"', text):
            add(m, "file:")
        for key in ("hls4", "hls2", "hls3"):
            for m in re.findall(r'"' + key + r'"\s*:\s*"([^"]+)"', text):
                add(m, "links." + key)
        for m in re.findall(r"'hls'\s*:\s*'([^']+)'", text):
            if m.startswith("http"):
                add(m, "voe.hls")
            else:
                try:
                    dec = base64.b64decode(m).decode("utf-8", "replace")
                    if ".m3u8" in dec:
                        add(dec, "voe.hls.b64")
                except Exception:
                    pass
        for m in re.findall(r"rot13\(['\"]([^'\"]{20,})['\"]\)", text):
            try:
                dec = base64.b64decode(_rot13(m)).decode("utf-8", "replace")
                for u in re.findall(r"https?://[^\s\"'\\]+\.m3u8[^\s\"'\\]*", dec):
                    add(u, "voe.rot13")
            except Exception:
                pass
        for m in re.findall(r"sources\s*:\s*\[([^\]]+)\]", text):
            for u in re.findall(r'file\s*:\s*"([^"]+)"', m):
                add(u, "jwplayer.sources")
        for m in re.findall(r'player\.src\(\{[^}]*src\s*:\s*"([^"]+)"', text):
            add(m, "mp4upload.mp4")
        for m in re.findall(r'data-options="([^"]+)"', text):
            try:
                opts = json.loads(m.replace("&quot;", '"'))
                meta = opts.get("flashvars", {}).get("metadata", {})
                if meta.get("hlsManifestUrl"):
                    add(meta["hlsManifestUrl"], "okru.hls")
            except Exception:
                pass
        for u in re.findall(r"https?://[^\s\"'\\<>]+\.m3u8(?:\?[^\s\"'\\<>]*)?", text):
            add(u, "bare.m3u8")
    return found


def _extract_subtitles(html):
    """Panen subtitle best-effort dari halaman player/hoster: URL .vtt
    (langsung atau dari unpacked script) + label di sekitarnya. Kosong
    bila tidak ada — panel subtitle Lumio menampilkannya dengan jujur."""
    found = []
    seen = set()
    texts = [html]
    for packed in re.findall(r"eval\(function\(p,a,c,k,e,d\).*?\}\(.*?\.split\('\|'\)[^)]*\)\)", html, re.S):
        texts.append(_unpack_packer(packed))
    for text in texts:
        for m in re.finditer(r"https?://[^\s\"'\\<>]+\.vtt(?:\?[^\s\"'\\<>]*)?", text):
            url = m.group(0)
            if url in seen or len(url) > 2000:
                continue
            seen.add(url)
            window = text[max(0, m.start() - 160):m.start()].lower()
            if "indones" in window:
                label, lang = "Indonesian", "id"
            elif "english" in window or '"en"' in window or "'en'" in window:
                label, lang = "English", "en"
            else:
                label, lang = "Subtitle", "en"
            found.append({"label": label, "lang": lang, "url": url, "default": False})
    if found:
        found[0]["default"] = True
    return found


def _hoster_family(url):
    host = urllib.parse.urlparse(url).netloc.lower()
    for kw, fam in HOSTER_FAMILIES.items():
        if kw in host:
            return fam
    return "unknown"


def _validate_manifest(url, referer=None, timeout=20):
    """Fetch HANYA 8KB pertama manifest. Return (ok, status, detail,
    variants). variants diparse dari #EXT-X-STREAM-INF di head (pola
    hianime._validate_master_playlist); kosong bila single rendition."""
    try:
        status, head = _fetch(url, referer=referer, timeout=timeout, max_bytes=8192)
    except urllib.error.HTTPError as e:
        return False, e.code, "HTTP %d" % e.code, []
    except Exception as e:
        return False, 0, "%s: %s" % (type(e).__name__, str(e)[:80]), []
    text = head.decode("utf-8", "replace")
    if "#EXTM3U" not in text[:4096]:
        return False, status, "200 but no #EXTM3U in first 8KB", []
    variants = []
    lines = text.splitlines()
    base = url.rsplit("/", 1)[0] + "/"
    for i, ln in enumerate(lines):
        if ln.startswith("#EXT-X-STREAM-INF"):
            rm = re.search(r"RESOLUTION=\d+x(\d+)", ln)
            q = (rm.group(1) + "p") if rm else "auto"
            nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
            if nxt and not nxt.startswith("#"):
                if not nxt.startswith("http"):
                    nxt = urllib.parse.urljoin(base, nxt)
                variants.append({"q": q, "url": nxt})
    variants.sort(key=lambda v: int(re.sub(r"\D", "", v["q"]) or 0), reverse=True)
    return True, status, "valid m3u8, %d variant(s) in head" % len(variants), variants


def _player_url(tmdb_id, media_type, season=1, episode=1):
    """Hop token: mint player URL segar dari endpoint publik tiap resolve."""
    if media_type == "movie":
        q = {"type": "movie", "id": tmdb_id}
        embed = "%s/embed/movie?tmdb=%s" % (EMBED_BASE, tmdb_id)
    else:
        q = {"type": "tv", "id": tmdb_id, "season": season, "episode": episode}
        embed = "%s/embed/tv?tmdb=%s&season=%s&episode=%s" % (EMBED_BASE, tmdb_id, season, episode)
    url = TOKEN_URL + "?" + urllib.parse.urlencode(q)
    status, body = _fetch(url, referer=embed)
    data = json.loads(body.decode("utf-8"))
    src = data.get("src") or ""
    if not src.startswith("http"):
        raise RuntimeError("token endpoint tidak mengembalikan player URL")
    return src, embed


def resolve_stream(tmdb_id, media_type, season=1, episode=1):
    """Rantai penuh embed -> token -> player -> hoster -> manifest.
    Melempar RuntimeError dengan detail hop yang gagal (untuk fail_reasons
    jujur di endpoint). Tidak pernah mengunduh segmen video."""
    try:
        src, embed = _player_url(tmdb_id, media_type, season, episode)
    except Exception as e:
        raise RuntimeError("VidSrc hop token gagal: %s" % str(e)[:120])
    try:
        _status, html_bytes = _fetch(src, referer=embed)
        html = html_bytes.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise RuntimeError("VidSrc hop player gagal: HTTP %d (player host memblokir IP ini?)" % e.code)
    except Exception as e:
        raise RuntimeError("VidSrc hop player gagal: %s" % str(e)[:120])
    pages = [(src, html)]
    frames = re.findall(r'<iframe[^>]+src="([^"]+)"', html)
    for fr in frames[:3]:
        absu = urllib.parse.urljoin(src, fr)
        if absu.startswith("http"):
            try:
                _s, fb = _fetch(absu, referer=src)
                pages.append((absu, fb.decode("utf-8", "replace")))
            except Exception:
                pass
    candidates = []
    subtitles = []
    for page_url, page_html in pages:
        for url, how in _extract_candidates(page_html):
            candidates.append((url, how, page_url))
        if not subtitles:
            subtitles = _extract_subtitles(page_html)
    if not candidates:
        fams = sorted({_hoster_family(u) for u, _ in pages})
        raise RuntimeError("VidSrc: tidak ada kandidat stream dari halaman player (hoster: %s)" % ",".join(fams))
    last_detail = ""
    for url, how, page_url in candidates:
        ok, status, detail, variants = _validate_manifest(url, referer=page_url)
        last_detail = detail
        if ok:
            if not variants:
                variants = [{"q": "auto", "url": url}]
            default_track = next((s for s in subtitles if s.get("default")), subtitles[0] if subtitles else None)
            return {
                "master": url,
                "variants": variants,
                "sub": default_track["url"] if default_track else None,
                "sub_lang": default_track["label"] if default_track else None,
                "referer": page_url,
                "mal_id": "",
                "subtitles": subtitles,
                "intro": {"start": 0, "end": 0},
                "outro": {"start": 0, "end": 0},
                "server": "VidSrc",
            }
    raise RuntimeError("VidSrc: semua kandidat manifest gagal (%s)" % last_detail[:100])


def resolve_movie(tmdb_id):
    return resolve_stream(tmdb_id, "movie")


def resolve_series(tmdb_id, season=1, episode=1):
    return resolve_stream(tmdb_id, "tv", season, episode)
