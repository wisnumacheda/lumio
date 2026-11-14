"""Provider embed Movie/Series + failover (payload v45, diperluas v52).

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

RISET PENYEDIA BARU (14 Nov 2026 malam; laporan +
~/workspace/.scratch-providers-v52/research.md; fetch statis + docs
publik; klaim inti VidRift terverifikasi mandiri):
- VidRift: SATU-SATUNYA kandidat yang lolos uji replay — manifest
  /api/orion/<hash32>/master.m3u8 merespons 200 + #EXTM3U hanya dengan
  Referer URL embed, tanpa cookie (tanpa Referer -> 403); hash stabil;
  kontrol negatif id palsu 0 stream. Masuk loop resolver PERTAMA.
- EmbedWave / VidNest.fun / CineSrc / Vidly / Vidy / vidsrc.pk:
  embed KUAT (terdokumentasi, player in-page terbukti) tetapi TIDAK
  ADA bukti replay (sinyal session-bound: staged issue, decrypt berat,
  endpoint bertoken) — embed-only, TIDAK PERNAH masuk loop resolver.
- Ditolak: VidStorm, vidsrc.pro/.cc/.icu (tak terjangkau), vid-src.top
  + vidnest.pro (skrip iklan), 2embed (judul salah), superembed
  (IMDb-only).

Template embed di bawah berasal dari dokumentasi resmi tiap provider /
pola terverifikasi probe (HTTP 200 dari sandbox, 14 Nov 2026). Mode
embed TIDAK memakai atribut sandbox pada iframe: VidFast/VidLink
menolak iframe ter-sandbox (dokumentasi publik).

BATAS KERAS: playback client-side saja; TANPA download/rip/re-host;
TANPA circumvention DRM; TANPA ekstraksi kredensial/key.
"""

# Urutan PROVIDERS = urutan BAR EMBED; indeks 0 = default saat mode embed
# dibuka (frontend loadEmbedProvider(0)). Viduki tetap pertama sejak v46:
# preferensi user dari tes on-device 14 Nov 2026 (paling disukai dari
# 5 penyedia; embed-nya terbukti jalan + benar untuk Demon Slayer
# 1311031). VidRift #2. Keenam penyedia baru #3-#8. Urutan lama
# (VidLink/VidFast/VidRock/VidSrc) dipertahankan di belakang.
# Field "resolver": True HANYA bila ada bukti replay (VidRift + 4 lama).
# Frontend membangun urutan loop resolver dari RESOLVER_ORDER (allowlist
# eksplisit) — penyedia embed-only TIDAK PERNAH dipanggil resolver.
# VidRift "mirror": domain cadangan embed.vidrift.in (perilaku identik
# terverifikasi probe). Dipakai sebagai percobaan kedua OTOMATIS di dalam
# satu attempt resolver VidRift bila URL utama gagal (kegagalan di sana
# terdeteksi andal via hasil bridge); di mode embed TIDAK ada failover
# otomatis (deteksi gagal iframe lintas-origin tidak andal) dan TIDAK
# ada entri bar terpisah — bila .net mati, registry dialihkan via
# payload tanpa APK.
PROVIDERS = [
    {
        "id": "viduki",
        "name": "Viduki",
        "resolver": True,
        "movie": "https://viduki.net/1/movie/{tmdb}?color=e01621",
        "tv": "https://viduki.net/1/tv/{tmdb}/{season}/{episode}?color=e01621",
    },
    {
        "id": "vidrift",
        "name": "VidRift",
        "resolver": True,
        # exit=1: player menampilkan tombol keluar yang mengirim
        # postMessage {type:'vidrift:exit'} (kode player terverifikasi
        # dari HTML embed); frontend menanganinya seperti tombol
        # "Tutup embed". uiScale=1.5 untuk TV ditambahkan frontend
        # (hanya frontend yang tahu IS_TV).
        "movie": "https://embed.vidrift.net/embed/movie/{tmdb}?exit=1",
        "tv": "https://embed.vidrift.net/embed/tv/{tmdb}/{season}/{episode}?exit=1",
        "mirror_movie": "https://embed.vidrift.in/embed/movie/{tmdb}?exit=1",
        "mirror_tv": "https://embed.vidrift.in/embed/tv/{tmdb}/{season}/{episode}?exit=1",
    },
    {
        "id": "embedwave",
        "name": "EmbedWave",
        "resolver": False,
        # Param ?server= terdokumentasi (nxsha/cinesrc/videasy) tetapi
        # perilaku default tanpa param dibiarkan apa adanya — jangan
        # tebak nilai; wrapper atas nxsha, bukan backend independen.
        "movie": "https://embedwave.cc/embed/movie/{tmdb}",
        "tv": "https://embedwave.cc/embed/tv/{tmdb}/{season}/{episode}",
    },
    {
        "id": "vidnest",
        "name": "VidNest",
        "resolver": False,
        "movie": "https://vidnest.fun/movie/{tmdb}",
        "tv": "https://vidnest.fun/tv/{tmdb}/{season}/{episode}",
    },
    {
        "id": "cinesrc",
        "name": "CineSrc",
        "resolver": False,
        # Bentuk TV memakai query ?s=&e= (terdokumentasi di airtv).
        "movie": "https://cinesrc.st/embed/movie/{tmdb}",
        "tv": "https://cinesrc.st/embed/tv/{tmdb}?s={season}&e={episode}",
    },
    {
        "id": "vidly",
        "name": "Vidly",
        "resolver": False,
        # Query autoplay + audioPicker sesuai template airtv.
        "movie": "https://vidly.pro/movie/{tmdb}?autoplay=1&audioPicker=1",
        "tv": "https://vidly.pro/tv/{tmdb}/{season}/{episode}?autoplay=1&audioPicker=1",
    },
    {
        "id": "vidy",
        "name": "Vidy",
        "resolver": False,
        "movie": "https://vidy.st/movie/{tmdb}",
        "tv": "https://vidy.st/tv/{tmdb}/{season}/{episode}",
    },
    {
        "id": "vidsrcpk",
        "name": "VidSrc PK",
        "resolver": False,
        # Layanan BERBEDA dari VidSrc lama (vidsrc.me); id TMDB numerik
        # didukung (docs sendiri) — JANGAN pakai id IMDb.
        "movie": "https://embed.vidsrc.pk/movies/{tmdb}",
        "tv": "https://embed.vidsrc.pk/tv/{tmdb}/{season}/{episode}",
    },
    {
        "id": "vidlink",
        "name": "VidLink",
        "resolver": True,
        "movie": "https://vidlink.pro/movie/{tmdb}",
        "tv": "https://vidlink.pro/tv/{tmdb}/{season}/{episode}",
    },
    {
        "id": "vidfast",
        "name": "VidFast",
        "resolver": True,
        "movie": "https://vidfast.pro/movie/{tmdb}?autoPlay=true",
        "tv": "https://vidfast.pro/tv/{tmdb}/{season}/{episode}?autoPlay=true",
    },
    {
        "id": "vidrock",
        "name": "VidRock",
        "resolver": True,
        "movie": "https://vidrock.net/movie/{tmdb}",
        "tv": "https://vidrock.net/tv/{tmdb}/{season}/{episode}",
    },
    {
        "id": "vidsrc",
        "name": "VidSrc",
        "resolver": False,
        "movie": "https://vidsrc.me/embed/movie?tmdb={tmdb}",
        "tv": "https://vidsrc.me/embed/tv?tmdb={tmdb}&season={season}&episode={episode}",
    },
]

# Allowlist eksplisit loop resolver WebView tersembunyi, URUTAN RESOLVER
# (berbeda dari urutan bar embed): VidRift pertama — bukti replay
# Referer-only-nya terkuat dari semua penyedia yang pernah diteliti.
# Menggantikan mekanisme denylist HIDDEN_RESOLVER_SKIP (v49-v51): hanya
# id di daftar ini yang boleh dipanggil resolver, dalam urutan ini.
RESOLVER_ORDER = ["vidrift", "viduki", "vidlink", "vidfast", "vidrock"]


def embed_urls(tmdb_id, media_type, season=1, episode=1):
    """Konstruksi URL embed murni (tanpa fetch). media_type: movie|tv.

    Urutan keluaran = urutan bar embed. Tiap entri membawa flag
    "resolver" + "mirror" (bila ada) agar frontend memakai urutan yang
    benar per jalur tanpa menebak.
    """
    key = "movie" if media_type == "movie" else "tv"
    out = []
    for p in PROVIDERS:
        url = p[key].format(tmdb=tmdb_id, season=season, episode=episode)
        entry = {"id": p["id"], "name": p["name"], "url": url,
                 "resolver": bool(p.get("resolver"))}
        mirror_key = "mirror_" + key
        if p.get(mirror_key):
            entry["mirror"] = p[mirror_key].format(
                tmdb=tmdb_id, season=season, episode=episode)
        out.append(entry)
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
