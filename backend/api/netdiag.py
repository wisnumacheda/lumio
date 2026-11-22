"""Perekam kejadian jaringan — Diagnosa Jaringan (payload v63).

Ring buffer in-memory (50 kejadian terakhir, tanpa persist ke disk) yang
mencatat tiap percobaan jaringan di jalur anime: tahap, HOST saja, metode
(direct/desync), hasil, latensi ms, waktu. Alat ukur saja: tidak mengubah
perilaku jaringan apa pun.

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
RESULTS = {
    "sukses", "timeout", "rst-putus", "halaman-blokir", "http-403",
    "http-5xx", "bukan-m3u8", "chunked", "gagal-dns", "lainnya",
}

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
    if ("upstream 5" in s or "http 5" in s or "status 5" in s
            or " 500 " in s or " 502 " in s or " 503 " in s or " 504 " in s):
        return "http-5xx"
    return "lainnya"


def record(stage, host, method, result, ms, note=None, dedupe_success=False):
    """Catat satu kejadian. stage: resolve-fetch / probe-master /
    master-fetch / segmen / ajax-otakudesu / subtitle / dns.
    method: direct / desync. ms: latensi milidetik (int).
    note: hanya string tetap yang sudah ditentukan pemanggil
    (cache / anggaran-habis / host-mati / url-tidak-valid / body-kosong /
    tanpa-chunk) — JANGAN isi teks dinamis dari error/URL.
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
        if note:
            ev["note"] = str(note)[:40]
        _EVENTS.append(ev)
        return ev


def events():
    """Daftar kejadian, TERBARU dulu. Salinan — pemanggil tidak bisa
    mengubah buffer internal."""
    with _LOCK:
        return [dict(ev) for ev in reversed(_EVENTS)]


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
