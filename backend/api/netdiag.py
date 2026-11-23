"""Perekam kejadian jaringan — Diagnosa Jaringan (payload v63).

Ring buffer in-memory (50 kejadian terakhir, tanpa persist ke disk) yang
mencatat tiap percobaan jaringan di jalur anime: tahap, HOST saja, metode
(direct/desync), hasil, latensi ms, waktu. Alat ukur saja: tidak mengubah
perilaku jaringan apa pun.

Tahap server: resolve-fetch / resolve-hasil / probe-master / master-fetch /
segmen / ajax-otakudesu / subtitle / dns.
Tahap klien (v65, via POST /api/net/diag/event): native-gagal / web-muat /
web-gagal / web-ok — hanya tahap itu yang diterima dari klien.

BATAS KERAS: yang dicatat HANYA host (tanpa port) — TIDAK PERNAH URL
lengkap, isi body, query, atau kredensial. Modul ini hanya memakai stdlib
(tanpa import dari api.*) supaya tidak ada import cycle: semua pemanggil
mengimpornya secara lazy di dalam fungsi, seperti pola desync yang sudah ada.
"""

import collections
import re
import threading
import time
import urllib.parse

_MAX_EVENTS = 50

# Hasil yang diizinkan — di luar daftar ini dipetakan ke "lainnya".
# "timeout-gantung" (v64): percobaan desync digantung ISP sampai batas waktu
# (silent-drop), bukan timeout direct biasa.
# "mulai" (v65): penanda MULAI muat dari klien (web-muat) — belum ada hasil;
# bila hanya ada "mulai" tanpa hasil susulan, muatannya menggantung.
RESULTS = {
    "sukses", "timeout", "timeout-gantung", "rst-putus", "halaman-blokir", "http-403",
    "http-404", "http-5xx", "bukan-m3u8", "chunked", "gagal-dns", "lainnya", "mulai",
}

# Tahap yang boleh dilaporkan KLIEN (JS player) lewat POST /api/net/diag/event.
# Tahap server (resolve-fetch / probe-master / master-fetch / segmen /
# ajax-otakudesu / subtitle / dns / resolve-hasil) HANYA dicatat server —
# klien yang mengirimnya ditolak.
CLIENT_STAGES = {"native-gagal", "web-muat", "web-gagal", "web-ok"}

# Catatan klien yang diizinkan: label pendek dari set yang dikenal
# (kategori error native / detail hls.js / penanda muat) — TIDAK PERNAH
# teks bebas dari URL/pesan error mentah.
_NOTE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _\-.]{0,39}$")

_EVENTS = collections.deque(maxlen=_MAX_EVENTS)
_LOCK = threading.Lock()
_SEQ = [0]


def host_of(url_or_host):
    """Host kanonis dari URL atau string host: huruf kecil, tanpa port.
    Return "" bila tidak bisa dipetik — pemanggil tetap mencatat kejadiannya
    dengan host kosong daripada membocorkan URL mentah."""
    if not url_or_host:
        return ""
    s = str(url_or_host).strip()
    if "://" in s:
        try:
            netloc = urllib.parse.urlsplit(s).netloc.lower()
        except Exception:
            return ""
        host = netloc
    else:
        host = s.lower()
    if host.startswith("["):  # literal IPv6 [::1]:8080
        host = host.split("]", 1)[0] + "]"
    else:
        host = host.split(":", 1)[0]
    host = host.strip()
    if not re.match(r"^[a-z0-9._\-\[\]:]+$", host):
        return ""
    return host


def classify_error(err):
    """Petakan exception/pesan error ke salah satu RESULTS. Menerima
    exception atau string. Tidak pernah mengembalikan teks asal — hanya
    label hasil — supaya pesan error yang memuat URL tidak bocor ke log."""
    s = str(err or "").lower()
    if "timeout-gantung" in s or "desync timeout" in s or "desync handshake timeout" in s or "desync response timeout" in s or "desync connect timeout" in s:
        return "timeout-gantung"
    if any(t in s for t in ("timeout", "timed out", "timedout", "deadline exceeded")):
        return "timeout"
    if any(t in s for t in ("connection reset", "reset by peer", "connectionreseterror",
                            "broken pipe", "connection aborted", "rst")):
        return "rst-putus"
    if any(t in s for t in ("getaddrinfo", "name or service not known",
                            "nodename nor servname", "temporary failure in name resolution",
                            "dns", "gagal-dns")):
        return "gagal-dns"
    if "block page" in s or "dpi block" in s or "halaman blokir" in s:
        return "halaman-blokir"
    if "not m3u8" in s or "not a valid m3u8" in s or "bukan-m3u8" in s:
        return "bukan-m3u8"
    if "chunked" in s:
        return "chunked"
    if "upstream 403" in s or "http 403" in s or " 403 " in s or "status 403" in s:
        return "http-403"
    # v66 — bedakan 404 (konten memang hilang) dari 403/5xx: probe master
    # yang 404 tidak boleh dicoba lagi via desync.
    if "upstream 404" in s or "http 404" in s or " 404 " in s or "status 404" in s:
        return "http-404"
    if ("upstream 5" in s or "http 5" in s or "status 5" in s
            or " 500 " in s or " 502 " in s or " 503 " in s or " 504 " in s):
        return "http-5xx"
    return "lainnya"


def record(stage, host, method, result, ms, note=None, dedupe_success=False, variant=None):
    """Catat satu kejadian. stage: resolve-fetch / resolve-hasil /
    probe-master / master-fetch / segmen / ajax-otakudesu / subtitle / dns
    (server) atau native-gagal / web-muat / web-gagal / web-ok (klien v65).
    method: direct / desync. ms: latensi milidetik (int).
    note: hanya string tetap yang sudah ditentukan pemanggil
    (cache / anggaran-habis / host-mati / url-tidak-valid / body-kosong /
    tanpa-chunk / host-gantung) — JANGAN isi teks dinamis dari error/URL.
    variant (v64): 1/2 bila method desync — varian desync yang dipakai.
    dedupe_success=True: kejadian sukses yang identik (stage+host+method)
    hanya dicatat sekali — dipakai segmen agar buffer tidak dibanjiri."""
    h = host_of(host)
    res = result if result in RESULTS else "lainnya"
    m = method if method in ("direct", "desync") else "direct"
    try:
        ms_i = max(0, int(ms or 0))
    except Exception:
        ms_i = 0
    with _LOCK:
        if dedupe_success and res == "sukses":
            for ev in _EVENTS:
                if (ev["stage"] == stage and ev["host"] == h and ev["method"] == m
                        and ev["result"] == "sukses"):
                    return None
        _SEQ[0] += 1
        ev = {
            "seq": _SEQ[0],
            "t": time.strftime("%H:%M:%S", time.localtime()),
            "stage": str(stage or ""),
            "host": h,
            "method": m,
            "result": res,
            "ms": ms_i,
        }
        if variant is not None:
            try:
                ev["variant"] = int(variant)
            except Exception:
                pass
        if note:
            ev["note"] = str(note)[:40]
        _EVENTS.append(ev)
        return ev


def events():
    """Daftar kejadian, TERBARU dulu. Salinan — pemanggil tidak bisa
    mengubah buffer internal."""
    with _LOCK:
        return [dict(ev) for ev in reversed(_EVENTS)]


def validate_client_event(payload):
    """Validasi laporan kejadian dari klien (JS player) untuk
    POST /api/net/diag/event. Return dict bersih siap record().

    TOLAK (ValueError, pesan tetap tanpa gema payload) bila:
    - bukan dict / tahap bukan tahap klien / hasil bukan RESULTS,
    - host mengandung URL lengkap (\"://\", \"/\", spasi) atau port —
      klien WAJIB mengirim host telanjang (host_of di JS),
    - ms/variant di luar batas, atau note mengandung karakter di luar
      label yang dikenal (huruf/angka/spasi/_/-/.).
    Tahap server TIDAK PERNAH diterima dari klien — pemalsuan tahap
    resolve/probe dari klien ditolak di sini."""
    if not isinstance(payload, dict):
        raise ValueError("body harus objek JSON")
    stage = payload.get("stage")
    if stage not in CLIENT_STAGES:
        raise ValueError("tahap tidak diizinkan dari klien")
    raw_host = payload.get("host")
    raw_host = "" if raw_host is None else str(raw_host).strip()
    if raw_host:
        if "://" in raw_host or "/" in raw_host or " " in raw_host:
            raise ValueError("host harus telanjang, tanpa URL")
        if host_of(raw_host) != raw_host.lower():
            raise ValueError("host harus telanjang, tanpa port")
    method = payload.get("method") or "direct"
    if method not in ("direct", "desync"):
        raise ValueError("metode tidak dikenal")
    result = payload.get("result")
    if result not in RESULTS:
        raise ValueError("hasil tidak dikenal")
    ms = payload.get("ms", 0)
    if isinstance(ms, bool) or not isinstance(ms, int):
        try:
            ms = int(ms)
        except Exception:
            raise ValueError("ms harus bilangan bulat")
    if ms < 0 or ms > 3600000:
        raise ValueError("ms di luar batas")
    note = payload.get("note")
    if note is not None:
        note = str(note)
        if len(note) > 40 or not _NOTE_RE.match(note) or "://" in note \
                or "http" in note.lower():
            raise ValueError("catatan tidak diizinkan")
    variant = payload.get("variant")
    if variant is not None:
        if isinstance(variant, bool) or variant not in (1, 2):
            raise ValueError("varian harus 1 atau 2")
    return {"stage": stage, "host": raw_host, "method": method,
            "result": result, "ms": ms, "note": note, "variant": variant}


def clear():
    """Kosongkan buffer."""
    with _LOCK:
        _EVENTS.clear()


def has_success(stage, host, method):
    """True bila sudah ada kejadian sukses identik di buffer."""
    h = host_of(host)
    with _LOCK:
        return any(ev["stage"] == stage and ev["host"] == h
                   and ev["method"] == method and ev["result"] == "sukses"
                   for ev in _EVENTS)
