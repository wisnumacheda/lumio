"""Resolver statis Movie/Series via VidRift (payload v53).

ALASAN (bukti on-device 15 Nov 2026, HP user): VidRift GAGAL lewat hidden
WebView — label "waktu habis", tidak ada manifest tertangkap dalam jatah
resolvernya (diduga fetch manifest butuh interaksi play/ad gate yang
tidak pernah terjadi di WebView tersembunyi). Tetapi shell halaman embed
VidRift SENDIRI berisi daftar stream: blob JSON `onStreams` memuat URL
bentuk /api/orion/<hash32>/master.m3u8, dan manifest itu replayable
hanya dengan Referer = URL embed, tanpa cookie (terverifikasi mandiri,
skrip recheck3.py: movie 1311031 = 5 hash, GoT S4E5 = 3 hash, replay
200 + #EXTM3U; id palsu 999999999 = 0 hash).

Rantai: fetch shell embed (URL POLOS, tanpa ?exit=1 — param itu hanya
untuk mode embed iframe) -> ekstrak hash orion -> validasi manifest
(8KB pertama, wajib #EXTM3U, Referer = URL embed) -> kembalikan master.
Mirror embed.vidrift.in dicoba HANYA bila fetch .net gagal / 0 hash
(konten keduanya identik — bila manifest .net ditolak, mirror tidak
membantu dan hanya membuang anggaran).

BATAS KERAS: playback client-side saja (pola resolver yang sudah ada);
TIDAK ada download/rip/re-host (validasi hanya 8KB pertama manifest,
segmen .ts tidak pernah diunduh); TIDAK ada circumvention DRM; TIDAK
ada ekstraksi kredensial/key; TIDAK ada eksekusi JS (ekstraksi hash
adalah regex atas HTML statis). Bentuk kembalian sama dengan resolver
VidSrc {master, variants, sub, sub_lang, referer, subtitles, intro,
outro, server} sehingga endpoint + player + engine subtitle dipakai
ulang tanpa perubahan.
"""
import re
import urllib.error
import urllib.parse
import urllib.request

# UA desktop Chrome — sama dengan yang dipakai verifikasi replay mandiri.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

EMBED_HOSTS = ("embed.vidrift.net", "embed.vidrift.in")
SHELL_TIMEOUT = 8
MANIFEST_TIMEOUT = 6
MAX_HASHES = 3
SHELL_MAX_BYTES = 1_000_000

_HASH_RE = re.compile(r"/api/orion/([A-Za-z0-9_-]{32})/master\.m3u8")


def _embed_url(host, tmdb_id, media_type, season=1, episode=1):
    """URL embed POLOS (tanpa ?exit=1 / uiScale — itu param mode iframe)."""
    if media_type == "movie":
        return "https://%s/embed/movie/%s" % (host, tmdb_id)
    return "https://%s/embed/tv/%s/%s/%s" % (host, tmdb_id, season, episode)


def _fetch(url, referer=None, timeout=10, max_bytes=None):
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read(max_bytes) if max_bytes else resp.read()
        return resp.status, body


def _validate_manifest(url, referer, timeout=MANIFEST_TIMEOUT):
    """Fetch HANYA 8KB pertama manifest. Return (ok, detail, variants).
    variants diparse dari #EXT-X-STREAM-INF di head (pola vidsrc);
    URI varian RELATIF (mis. v0.m3u8) di-resolve terhadap URL manifest."""
    try:
        _status, head = _fetch(url, referer=referer, timeout=timeout, max_bytes=8192)
    except urllib.error.HTTPError as e:
        return False, "HTTP %d" % e.code, []
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, str(e)[:80]), []
    text = head.decode("utf-8", "replace")
    if "#EXTM3U" not in text[:4096]:
        return False, "200 tetapi tanpa #EXTM3U di 8KB pertama", []
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
    return True, "manifest valid, %d varian di head" % len(variants), variants


def resolve_stream(tmdb_id, media_type, season=1, episode=1):
    """Shell embed -> hash orion -> manifest tervalidasi.
    Melempar RuntimeError dengan detail tahap yang gagal (untuk
    fail_reasons jujur di endpoint). Tidak pernah mengunduh segmen."""
    stage_errors = []
    for host in EMBED_HOSTS:
        embed = _embed_url(host, tmdb_id, media_type, season, episode)
        try:
            _status, shell = _fetch(embed, timeout=SHELL_TIMEOUT,
                                    max_bytes=SHELL_MAX_BYTES)
        except urllib.error.HTTPError as e:
            stage_errors.append("%s: shell HTTP %d" % (host, e.code))
            continue
        except Exception as e:
            stage_errors.append("%s: shell gagal (%s)" % (host, str(e)[:80]))
            continue
        text = shell.decode("utf-8", "replace")
        hashes = []
        for h in _HASH_RE.findall(text):
            if h not in hashes:
                hashes.append(h)
        if not hashes:
            # 0 hash = judul tanpa stream (kontrol negatif id palsu
            # juga 0) — coba mirror sekali, lalu gagal jujur.
            stage_errors.append("%s: tidak ada stream di halaman embed" % host)
            continue
        last_detail = ""
        for h in hashes[:MAX_HASHES]:
            manifest = "https://%s/api/orion/%s/master.m3u8" % (host, h)
            ok, detail, variants = _validate_manifest(manifest, referer=embed)
            last_detail = detail
            if ok:
                if not variants:
                    variants = [{"q": "auto", "url": manifest}]
                return {
                    "master": manifest,
                    "variants": variants,
                    "sub": None,
                    "sub_lang": None,
                    "referer": embed,
                    "mal_id": "",
                    "subtitles": [],
                    "intro": {"start": 0, "end": 0},
                    "outro": {"start": 0, "end": 0},
                    "server": "VidRift",
                }
        # Hash ada tetapi SEMUA manifest ditolak: mirror memuat konten
        # yang sama — jangan buang anggaran, gagalkan dengan detail.
        raise RuntimeError("VidRift: manifest ditolak (%s)" % last_detail[:100])
    raise RuntimeError("VidRift: " + "; ".join(stage_errors)[:200])


def resolve_movie(tmdb_id):
    return resolve_stream(tmdb_id, "movie")


def resolve_series(tmdb_id, season=1, episode=1):
    return resolve_stream(tmdb_id, "tv", season, episode)
