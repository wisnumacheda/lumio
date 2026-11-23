"""Scraper dan resolver stream untuk Animeindo (https://animeindo.cloud).

Situs streaming anime subtitle Indonesia (Nuxt SSR): halaman tontonan
statis berisi daftar episode + embedUrl Desustream di HTML, tanpa butuh
JS. Rantai resolve: cari judul -> daftar episode -> halaman episode
(ambil embedUrl) -> halaman pemutar Desustream (gali m3u8) -> probe
master (v66) sebelum diserahkan ke player. Pola mengikuti
otakudesu.py (fetch + desync + diagnosa host-only) dan anichan.py
(kontrak dict terstandar). Subtitle: video hardsub Indonesia seperti
Otakudesu; softsub dipanen main.py dari HiAnime bila tersedia.
"""

import re
import time
import html as htmlmod
import urllib.parse

from api.hianime import _probe_master as _v66_probe_master
from api.hianime import _host_from as _host_from_url

ANIMEINDO_BASE = "https://animeindo.cloud"

# Cache halaman Daftar Anime A-Z (~1850 judul, ~650 KB): diambil sekali
# per jam supaya tiap resolve tidak mengunduh ulang.
_DAFTAR_CACHE = {"html": "", "exp": 0.0}
_DAFTAR_TTL = 3600.0


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
        if "Animeindo" in msg:
            raise
        raise RuntimeError(f"Gagal memuat halaman Animeindo ({_host_from_url(url)})")
    except Exception as e:
        raise RuntimeError(f"Gagal memuat halaman Animeindo ({_host_from_url(url)}): {e}")


def _daftar_html(deadline=None) -> str:
    """Halaman Daftar Anime A-Z (satu halaman, seluruh katalog). Di-cache
    1 jam di memori."""
    now = time.monotonic()
    if _DAFTAR_CACHE["html"] and _DAFTAR_CACHE["exp"] > now:
        return _DAFTAR_CACHE["html"]
    html = _fetch(f"{ANIMEINDO_BASE}/anime-indo/daftar-anime", deadline=deadline)
    _DAFTAR_CACHE["html"] = html
    _DAFTAR_CACHE["exp"] = now + _DAFTAR_TTL
    return html


def _search_cards(query: str, limit: int = 10, deadline=None) -> list:
    """Fallback: kartu anime di homepage (/?s=). Halaman ini mengabaikan
    query dan menampilkan daftar terbaru — hanya dipakai bila halaman
    Daftar Anime gagal diunduh."""
    clean_q = re.sub(r"[^\w\s-]", "", query).strip()
    url = f"{ANIMEINDO_BASE}/?s={urllib.parse.quote_plus(clean_q)}"
    html = _fetch(url, deadline=deadline)
    results = []
    seen = set()
    for m in re.finditer(
        r'<a href="(/nonton/[^"]+)"[^>]*class="anime-card".*?'
        r'<h3 class="anime-card-title">([^<]+)</h3>',
        html, re.S,
    ):
        href, title = m.group(1), htmlmod.unescape(m.group(2).strip())
        if href in seen:
            continue
        seen.add(href)
        full = urllib.parse.urljoin(ANIMEINDO_BASE, href)
        slug = href.strip("/").split("/")[-1]
        results.append({"title": title, "url": full, "id": slug, "poster": ""})
        if len(results) >= limit:
            break
    return results


def search(query: str, limit: int = 10, deadline=None) -> list:
    """Cari anime di Animeindo. Return [{title, url, id, poster}].
    Jalur utama: Daftar Anime A-Z (cocokkan judul via data-title);
    fallback: kartu homepage."""
    from api.otakudesu import _is_title_match as _match
    clean_q = re.sub(r"[^\w\s-]", "", query).strip()
    results = []
    try:
        html = _daftar_html(deadline=deadline)
        for m in re.finditer(
            r'<a href="(/nonton/[^"]+)" class="daftar-anime-item" data-title="([^"]*)"[^>]*>([^<]+)',
            html,
        ):
            href = m.group(1)
            dtitle = htmlmod.unescape(m.group(2).strip())
            text = htmlmod.unescape(m.group(3).strip())
            if _match(clean_q, dtitle or text) or _match(clean_q, text):
                full = urllib.parse.urljoin(ANIMEINDO_BASE, href)
                results.append({
                    "title": text,
                    "url": full,
                    "id": href.strip("/").split("/")[-1],
                    "poster": "",
                })
                if len(results) >= limit:
                    break
    except Exception:
        results = []
    if not results:
        try:
            results = _search_cards(query, limit=limit, deadline=deadline)
        except Exception:
            results = []
    return results


def get_episodes(anime_url_or_slug: str, deadline=None) -> list:
    """Ambil daftar episode dari halaman tontonan Animeindo.
    Return [{ep: 1, title: 'Episode 1', url: 'https://...'}] urut ep kecil
    ke besar. Slug episode dipakai apa adanya dari href (situs ini punya
    typo di sebagian slug, mis. 'asohia') — jangan ditebak ulang."""
    if anime_url_or_slug.startswith("http"):
        url = anime_url_or_slug
    else:
        url = f"{ANIMEINDO_BASE}/nonton/{anime_url_or_slug.strip('/')}/"
    html = _fetch(url, deadline=deadline)
    out = []
    seen = set()
    for href, ep_no in re.findall(
        r'href="((?:https://animeindo\.cloud)?/nonton/[^"/]+/[^"/]*episode-(\d+)[^"/]*)"',
        html,
    ):
        full = urllib.parse.urljoin(ANIMEINDO_BASE, href)
        if full in seen:
            continue
        seen.add(full)
        out.append({"ep": int(ep_no), "title": f"Episode {ep_no}", "url": full})
    out.sort(key=lambda x: x["ep"])
    return out


def _extract_embed_url(ep_html: str):
    """Ambil embedUrl dari halaman episode (JSON-LD / payload Nuxt).
    Bentuknya bisa lolos-unicode (https:\\u002F\\u002F...) — normalisasi
    dulu. Utamakan Desustream; bila tidak ada, pakai URL https pertama."""
    norm = ep_html.replace("\\u002F", "/").replace("\\u0026", "&")
    found = re.findall(r'"embedUrl"\s*:\s*"((?:https?:)?//[^"]+)"', norm)
    if not found:
        return None
    for u in found:
        if u.startswith("//"):
            u = "https:" + u
        if "desustream" in u:
            return u
    u = found[0]
    if u.startswith("//"):
        u = "https:" + u
    return u if u.startswith("http") else None


def _extract_stream_from_player(player_html: str):
    """Gali URL stream dari halaman pemutar embed (pola yang sama dengan
    otakudesu._extract_animestream_stream). Utamakan m3u8."""
    for pat in (
        r'https?://[^"\'\s]+\.m3u8[^"\'\s]*',
        r'file\s*:\s*["\']([^"\']+)',
        r'<source[^>]+src="([^"]+)"',
        r'https?://[^"\'\s]+\.mp4[^"\'\s]*',
    ):
        mm = re.search(pat, player_html)
        if mm:
            stream = mm.group(1) if mm.lastindex else mm.group(0)
            if stream.startswith("//"):
                stream = "https:" + stream
            return stream
    return None


def resolve_stream(title_or_slug: str, ep: int = 1, deadline=None) -> dict:
    """Selesaikan URL stream m3u8 Animeindo untuk anime & episode tertentu.
    Return dict terstandar (master, variants, referer, server, ...)."""
    if "hentai" in (title_or_slug or "").lower():
        raise RuntimeError("Konten Hentai tidak tersedia di Animeindo")

    # 1. Daftar episode: URL tontonan langsung, slug animeindo, atau cari.
    episodes = []
    if "animeindo" in title_or_slug and "/nonton/" in title_or_slug:
        episodes = get_episodes(title_or_slug, deadline=deadline)
    else:
        slug_in = title_or_slug.strip().strip("/")
        if "-" in slug_in and " " not in slug_in and slug_in.endswith("-sub-indo"):
            try:
                direct_eps = get_episodes(
                    f"{ANIMEINDO_BASE}/nonton/{slug_in}/", deadline=deadline
                )
                if direct_eps:
                    episodes = direct_eps
            except Exception:
                pass
        if not episodes:
            # Bersihkan hash suffix (cth: "...-jtgn9fq") dan angka suffix ID
            # HiAnime (cth: "ao-ashi-2nd-season-7197" -> "ao ashi 2nd season").
            # Hash HANYA dihapus bila mengandung digit: "-season" (6 huruf,
            # tanpa digit) bukan hash dan tidak boleh ikut terhapus.
            clean_str = re.sub(r"-(?=[a-z0-9]{5,10}$)[a-z0-9]*\d[a-z0-9]*$", "", title_or_slug)
            clean_title = re.sub(r"-\d+$", "", clean_str).replace("-", " ").strip()
            search_res = search(clean_title, deadline=deadline)
            if not search_res and clean_title != title_or_slug:
                raw_title = re.sub(r"-\d+$", "", title_or_slug).replace("-", " ").strip()
                if raw_title != clean_title:
                    search_res = search(raw_title, deadline=deadline)
            if not search_res:
                simplified = re.sub(
                    r"\b(?:season\s*\d+|\d+(?:nd|rd|th|st)\s*season|part\s*\d+|s\d+)\b",
                    "", clean_title, flags=re.I,
                ).strip()
                if simplified and simplified != clean_title:
                    search_res = search(simplified, deadline=deadline)
            if not search_res:
                raise RuntimeError(f"Anime '{title_or_slug}' tidak ditemukan di Animeindo")
            from api.otakudesu import _is_title_match as _match
            matched_item = None
            for cand in search_res:
                if _match(clean_title, cand.get("title", "")):
                    matched_item = cand
                    break
            if not matched_item:
                raise RuntimeError(
                    f"Anime '{title_or_slug}' tidak memiliki judul yang cocok di Animeindo"
                )
            episodes = get_episodes(matched_item["url"], deadline=deadline)

    if not episodes:
        raise RuntimeError("Tidak ada daftar episode di Animeindo")

    # Cari episode yang cocok
    target_ep = next((e for e in episodes if e["ep"] == int(ep)), None)
    if not target_ep:
        idx = int(ep) - 1
        if 0 <= idx < len(episodes):
            target_ep = episodes[idx]
        else:
            raise RuntimeError(f"Episode {ep} belum rilis di Animeindo")

    # 2. Halaman episode -> embedUrl Desustream -> halaman pemutar -> m3u8.
    ep_html = _fetch(target_ep["url"], referer=ANIMEINDO_BASE + "/", deadline=deadline)
    embed_url = _extract_embed_url(ep_html)
    if not embed_url:
        raise RuntimeError("Pemutar Animeindo tidak ditemukan untuk episode ini")
    origin = f"{urllib.parse.urlparse(embed_url).scheme}://{urllib.parse.urlparse(embed_url).netloc}"
    player_html = _fetch(embed_url, referer=target_ep["url"], deadline=deadline)
    hls_master = _extract_stream_from_player(player_html)
    if not hls_master:
        raise RuntimeError("Stream Animeindo tidak ditemukan dari pemutar")

    # 3. Validasi master (v66): master mati tidak boleh sampai ke player.
    _v66_probe_master(hls_master, referer=origin + "/", deadline=deadline)

    variants = [
        {"q": "720p", "url": hls_master},
        {"q": "auto", "url": hls_master},
    ]
    return {
        "master": hls_master,
        "variants": variants,
        "sub": None,
        "sub_lang": "Indonesian (Hardsub)",
        "referer": origin + "/",
        "server": "Animeindo (Sub Indo)",
        "source": "animeindo",
        "tracks": [],
        "subtitles": [],
    }
