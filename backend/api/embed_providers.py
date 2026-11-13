"""Provider embed Movie/Series + failover (payload v45).

KONTEKS (bukti on-device 14 Nov 2026, HP user, IP residensial): rantai
statis VidSrc MATI — hop player HTTP 403. v45 memberi jalan putar lain:
mode embed iframe — JS provider berjalan di WebView perangkat, bukan
fetch statis backend.

HASIL RISET RANTAI (13-14 Nov 2026; fetch HTTP standar + dokumentasi
publik provider; TANPA eksekusi JS, TANPA pembobolan enkripsi):
- VidSrc (vidsrc.me): SATU-SATUNYA rantai statis yang pernah jalan.
  Dipertahankan, gagal cepat + jujur bila player host menolak.
- VidLink / VidFast / VidRock: shell JS (Next.js/SPA); HTML statis
  tidak memuat data stream; API statisnya terenkripsi — TIDAK
  diimplementasikan statis (browser-only, sesuai batas tugas).
- Viduki (vidsrc.wtf): API iframe-only by design (dokumentasi resmi
  di beranda: "Drop these URLs into an iframe").
- VidJoy: template embed tidak terdokumentasi + gerbang challenge JS +
  flaky dari sandbox (disconnect/401) — TIDAK dimasukkan.
- SpencerDevs: halaman coming-soon — mati, TIDAK dimasukkan.

Template embed di bawah berasal dari dokumentasi resmi tiap provider /
pola terverifikasi probe (HTTP 200 dari sandbox, 14 Nov 2026). Mode
embed TIDAK memakai atribut sandbox pada iframe: VidFast/VidLink
menolak iframe ter-sandbox (dokumentasi publik).

BATAS KERAS: playback client-side saja; TANPA download/rip/re-host;
TANPA circumvention DRM; TANPA ekstraksi kredensial/key.
"""

# Urutan = urutan fallback mode embed. VidSrc terakhir: rantai statisnya
# sudah terbukti 403, tapi iframe-nya (JS di WebView, header browser
# lengkap) adalah fingerprint berbeda — mungkin jalan, belum terbukti.
PROVIDERS = [
    {
        "id": "vidlink",
        "name": "VidLink",
        "movie": "https://vidlink.pro/movie/{tmdb}",
        "tv": "https://vidlink.pro/tv/{tmdb}/{season}/{episode}",
    },
    {
        "id": "vidfast",
        "name": "VidFast",
        "movie": "https://vidfast.pro/movie/{tmdb}?autoPlay=true",
        "tv": "https://vidfast.pro/tv/{tmdb}/{season}/{episode}?autoPlay=true",
    },
    {
        "id": "vidrock",
        "name": "VidRock",
        "movie": "https://vidrock.net/movie/{tmdb}",
        "tv": "https://vidrock.net/tv/{tmdb}/{season}/{episode}",
    },
    {
        "id": "viduki",
        "name": "Viduki",
        "movie": "https://viduki.net/1/movie/{tmdb}?color=e01621",
        "tv": "https://viduki.net/1/tv/{tmdb}/{season}/{episode}?color=e01621",
    },
    {
        "id": "vidsrc",
        "name": "VidSrc",
        "movie": "https://vidsrc.me/embed/movie?tmdb={tmdb}",
        "tv": "https://vidsrc.me/embed/tv?tmdb={tmdb}&season={season}&episode={episode}",
    },
]


def embed_urls(tmdb_id, media_type, season=1, episode=1):
    """Konstruksi URL embed murni (tanpa fetch). media_type: movie|tv."""
    key = "movie" if media_type == "movie" else "tv"
    out = []
    for p in PROVIDERS:
        url = p[key].format(tmdb=tmdb_id, season=season, episode=episode)
        out.append({"id": p["id"], "name": p["name"], "url": url})
    return out


def note_reason(provider, exc):
    """Ringkas alasan gagal untuk fail_reasons (pola v24, maks 140
    karakter) + dedupe prefix ganda (cacat v44): pesan resolver yang
    sudah diawali nama provider ("VidSrc hop player gagal...") tidak
    boleh digabung lagi menjadi "VidSrc: VidSrc hop player gagal...".
    """
    msg = " ".join(str(exc or "").split())
    low = msg.lower()
    prov = provider.lower()
    for _ in range(3):
        if low.startswith(prov + ":"):
            msg = msg[len(provider) + 1:].strip()
        elif low.startswith(prov + " "):
            msg = msg[len(provider) + 1:].strip()
        else:
            break
        low = msg.lower()
    msg = msg[:140]
    return msg or "gagal tanpa pesan"
