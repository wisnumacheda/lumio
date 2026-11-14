"""Daftar subtitle statis untuk judul TMDB via rezesubs (payload v50).

KONTEKS (bukti on-device 14 Nov 2026): hidden resolver v49 menangkap
manifest Viduki dan video jalan di player Lumio, TAPI nol subtitle. Riset
FASE 0 resolver v2 membuktikan mekanismenya dari chunk JS player Viduki
(chunk 945, modul 27, tidak terobfuscasi): player mengambil DAFTAR subtitle
dari API JSON rezesubs, lalu file .vtt baru di-fetch browser saat trek
dimuat (saat/setelah playback) — sehingga intersepsi request pasif di
hidden WebView tidak pernah melihat satu pun request subtitle. Solusinya
bukan pelebaran pola tangkap, melainkan fetch statis endpoint yang sama
dari backend — tanpa injeksi JS, tanpa kredensial.

Endpoint (dipakai player Viduki sendiri):
  GET https://rezesubs.com/data/subs/movie/{tmdb}
  GET https://rezesubs.com/data/subs/tv/{tmdb}/{season}/{episode}
  -> {"count": N, "status": "present", "subtitles": [{lang, file, url}]}
URL subtitle relatif -> absolutkan ke https://rezesubs.com.

DISIPLIN REFERER (terbukti 403 tanpa Referer, untuk endpoint daftar
MAUPUN file .vtt — proteksi hotlink Cloudflare): setiap fetch ke rezesubs
HARUS mengirim Referer URL embed Viduki. Entri subtitle yang dikembalikan
membawa field `referer` yang sama, agar /api/player/sub mengirimkannya
saat mengambil file (frontend memakai referer per-trek).

BATAS KERAS: fetch daftar JSON saja; TIDAK mengunduh/menyimpan segmen
video; TIDAK ada kredensial/key; gagal apa pun -> daftar kosong (jujur),
tidak pernah melempar.
"""

import json
import time
import urllib.error
import urllib.request

from api import embed_providers as epmod

_REZE_BASE = "https://rezesubs.com"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
_TIMEOUT = 10.0
_CACHE_TTL = 6 * 3600
_CACHE = {}

# Nama bahasa (rezesubs memakai nama panjang Inggris + sesekali kode 3
# huruf) -> kode ISO 639-1 dua huruf yang dipakai mesin subtitle Lumio
# (pemilih trek sumber terjemahan + label). Bahasa tak dikenal diteruskan
# apa adanya (huruf kecil) — trek mentahnya tetap bisa dipilih.
_LANG_MAP = {
    "english": "en", "eng": "en",
    "indonesian": "id", "ind": "id",
    "malay": "ms", "may": "ms", "msa": "ms",
    "chinese": "zh", "chi": "zho", "zho": "zh", "mandarin": "zh",
    "cantonese": "zh",
    "japanese": "ja", "jpn": "ja",
    "korean": "ko", "kor": "ko",
    "spanish": "es", "spa": "es",
    "french": "fre", "fre": "fr", "fra": "fr",
    "german": "de", "ger": "de", "deu": "de",
    "portuguese": "pt", "por": "pt", "pob": "pt",
    "russian": "ru", "rus": "ru",
    "arabic": "ar", "ara": "ar",
    "turkish": "tr", "tur": "tr",
    "italian": "it", "ita": "it",
    "dutch": "nl", "dut": "nld", "nld": "nl",
    "polish": "pl", "pol": "pl",
    "czech": "cs", "cze": "ces", "ces": "cs",
    "slovak": "sk", "slo": "slk", "slk": "sk",
    "hungarian": "hu", "hun": "hu",
    "greek": "el", "gre": "ell", "ell": "el",
    "hebrew": "he", "heb": "he",
    "swedish": "sv", "swe": "sv",
    "danish": "da", "dan": "da",
    "finnish": "fin", "fin": "fi",
    "norwegian": "no", "nor": "no",
    "romanian": "ro", "rum": "ron", "ron": "ro",
    "bulgarian": "bg", "bul": "bg",
    "ukrainian": "uk", "ukr": "uk",
    "hindi": "hi", "hin": "hi",
    "bengali": "bn", "ben": "bn",
    "tamil": "ta", "tam": "ta",
    "thai": "th", "tha": "th",
    "vietnamese": "vi", "vie": "vi",
    "filipino": "tl",
    "persian": "fa", "per": "fas", "fas": "fa",
    "urdu": "ur",
    "punjabi": "pa",
    "croatian": "hr", "scr": "hr",
    "slovenian": "sl", "slv": "sl",
    "icelandic": "is", "ice": "isl", "isl": "is",
    "estonian": "et", "est": "et",
    "latvian": "lv", "lav": "lv",
    "lithuanian": "lt",
    "serbian": "sr", "srp": "sr",
    "albanian": "sq", "alb": "sqi", "sqi": "sq",
    "armenian": "hy", "arm": "hye", "hye": "hy",
    "georgian": "ka", "geo": "kat", "kat": "ka",
    "macedonian": "mk", "mac": "mkd", "mkd": "mk",
    "basque": "eu", "baq": "eus", "eus": "eu",
    "catalan": "ca",
    "galician": "gl",
    "welsh": "cy", "wel": "cym", "cym": "cy",
    "irish": "ga",
    "maltese": "mt",
    "esperanto": "eo",
    "latin": "la",
    "burmese": "my", "bur": "mya", "mya": "my",
    "khmer": "km",
    "lao": "lo",
    "nepali": "ne",
    "sinhala": "si",
    "azerbaijani": "az",
    "kazakh": "kk",
    "uzbek": "uz",
    "tibetan": "bo", "tib": "bod", "bod": "bo",
    "maori": "mi", "mao": "mri", "mri": "mi",
    "afrikaans": "af",
    "amharic": "am",
    "assamese": "as",
    "belarusian": "be",
    "bosnian": "bs",
    "gujarati": "gu",
    "hausa": "ha",
    "igbo": "ig",
    "javanese": "jv",
    "kannada": "kn",
    "kurdish": "ku",
    "kyrgyz": "ky",
    "luxembourgish": "lb",
    "malayalam": "ml",
    "marathi": "mr",
    "mongolian": "mn",
    "oriya": "or",
    "pashto": "ps",
    "sindhi": "sd",
    "somali": "so",
    "sundanese": "su",
    "swahili": "sw",
    "tajik": "tg",
    "telugu": "te",
    "turkmen": "tk",
    "uyghur": "ug",
    "yoruba": "yo",
    "zulu": "zu",
    "unknown": "xx",
}


def _map_lang(raw):
    name = (raw or "").strip().lower()
    if not name:
        return "xx", "Subtitle"
    # Resolusi berantai (maks 3 lompatan): sebagian entri map memakai kode
    # ISO 639-2 sebagai perantara (mis. french -> fre -> fr). Hasil akhir
    # selalu kode 2 huruf atau nama asli bila tak dikenal.
    code = name
    for _ in range(3):
        nxt = _LANG_MAP.get(code)
        if not nxt or nxt == code:
            break
        code = nxt
    # Label tampil: nama kapital (ikuti konvensi player Viduki).
    label = name[:1].upper() + name[1:] if name else "Subtitle"
    return code, label


def _viduki_referer(tmdb_id, media_type, season, episode):
    """URL embed Viduki untuk judul ini — dipakai sebagai Referer saat
    fetch daftar maupun file subtitle (wajib, kalau tidak 403)."""
    try:
        for p in epmod.embed_urls(tmdb_id, media_type, season, episode):
            if p.get("id") == "viduki":
                return p.get("url") or ""
    except Exception:
        pass
    return ""


def fetch_embed_subtitles(tmdb_id, media_type, season=1, episode=1):
    """Kembalikan [{label, lang, url, referer}] atau [] (gagal apa pun).

    Dedupe: trek pertama per kode bahasa (berkas dasar "{lang}.vtt" selalu
    terdaftar sebelum varian "{lang}_N.vtt") agar panel subtitle tidak
    dibanjiri puluhan varian bahasa yang sama.
    """
    try:
        tmdb_id = int(tmdb_id)
    except (TypeError, ValueError):
        return []
    if tmdb_id <= 0 or media_type not in ("movie", "tv"):
        return []
    key = (media_type, tmdb_id, int(season or 1), int(episode or 1))
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < _CACHE_TTL:
        return [dict(s) for s in hit[1]]
    referer = _viduki_referer(tmdb_id, media_type, key[2], key[3])
    if media_type == "movie":
        api_url = "%s/data/subs/movie/%d" % (_REZE_BASE, tmdb_id)
    else:
        api_url = "%s/data/subs/tv/%d/%d/%d" % (_REZE_BASE, tmdb_id, key[2], key[3])
    out = []
    try:
        req = urllib.request.Request(api_url, headers={
            "User-Agent": _UA,
            "Accept": "application/json, */*",
            "Referer": referer or "https://www.viduki.net/",
        })
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            if resp.status != 200:
                _CACHE[key] = (now, [])
                return []
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        subs = payload.get("subtitles") if isinstance(payload, dict) else None
        if not isinstance(subs, list):
            _CACHE[key] = (now, [])
            return []
        seen = set()
        for s in subs:
            if not isinstance(s, dict):
                continue
            url = (s.get("url") or "").strip()
            lang_raw = (s.get("lang") or "").strip()
            if not url or not lang_raw:
                continue
            if not url.startswith("http"):
                url = _REZE_BASE + url
            code, label = _map_lang(lang_raw)
            if code in seen:
                continue
            seen.add(code)
            out.append({"label": label, "lang": code, "url": url,
                        "referer": referer})
    except Exception:
        return []
    _CACHE[key] = (now, [dict(s) for s in out])
    return out
