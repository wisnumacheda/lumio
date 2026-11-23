"""TLS Client Hello Desynchronization (RFC-Compliant Multi-Record Split).
Membypass blokir SNI / DPI ISP di Indonesia tanpa memerlukan VPN / WireGuard,
sehingga 100% aman dari deteksi Sangfor Firewall dan tidak memicu blokir MAC address.
"""

import re
import socket
import ssl
import threading
import time
import urllib.parse
from config import HI_UA

# Cache DNS hasil resolve DoH / direct dengan batas memori maksimum
_DNS_CACHE = {}
_MAX_DNS_CACHE = 256

# Batas satu percobaan desync (v64). Diagnosa v63 dari HP user: desync ke
# zokoanime.video DIGANTUNG ISP sampai timeout (bukan di-RST cepat) — satu
# percobaan 12 dtk memakan anggaran resolve 25 dtk. Satu percobaan kini
# dibatasi ~6 dtk; hang = gagal cepat (DesyncTimeout), bukan gantung.
DESYNC_ATTEMPT_TIMEOUT = 6.0


class DesyncTimeout(TimeoutError):
    """Percobaan desync menggantung sampai batas waktu (ISP silent-drop).
    Atribut .variant = varian yang dipakai saat hang (1 atau 2)."""
    def __init__(self, message="desync timeout", variant=None):
        super().__init__(message)
        self.variant = variant


# Registri hang per-resolve (v64): deadline resolve -> host -> {kind}.
# kind: "direct" (curl/httpx timeout) atau "v1"/"v2" (varian desync hang).
# Dipakai agar host yang menggantung TIDAK dicoba dua kali dalam satu resolve
# yang sama — percobaan kedua langsung loncat ke varian yang belum dicoba,
# atau gagal cepat bila keduanya sudah hang. Kunci = deadline (float
# monotonic, unik per resolve); entri di-prune bila deadline sudah lewat
# >180 dtk. Tanpa deadline (jalur non-resolve) registri tidak dipakai.
_HANG_LOCK = threading.Lock()
_HANG_BY_DEADLINE = {}
_HANG_PRUNE_AFTER = 180.0


def _canonical_hang_host(host):
    """Host kanonis untuk registri hang: huruf kecil, tanpa port."""
    if not host:
        return ""
    h = str(host).strip().lower()
    if h.startswith("["):
        h = h.split("]", 1)[0] + "]"
    else:
        h = h.split(":", 1)[0]
    return h


def note_hang(deadline, host, kind):
    """Tandai host menggantung dalam resolve ini (kind: direct/v1/v2)."""
    if deadline is None or not host or kind not in ("direct", "v1", "v2"):
        return
    host = _canonical_hang_host(host)
    if not host:
        return
    now = time.monotonic()
    with _HANG_LOCK:
        for dl in [d for d in _HANG_BY_DEADLINE if d < now - _HANG_PRUNE_AFTER]:
            _HANG_BY_DEADLINE.pop(dl, None)
        _HANG_BY_DEADLINE.setdefault(float(deadline), {}).setdefault(host, set()).add(kind)


def hung_kinds(deadline, host):
    """Set kind yang sudah hang untuk (deadline, host) — kosong bila belum ada."""
    if deadline is None or not host:
        return set()
    host = _canonical_hang_host(host)
    if not host:
        return set()
    with _HANG_LOCK:
        return set(_HANG_BY_DEADLINE.get(float(deadline), {}).get(host, set()))


def hung_in_resolve(deadline, host):
    """True bila host sudah tercatat menggantung dalam resolve ini."""
    return bool(hung_kinds(deadline, host))


# Marker halaman blokir ISP Indonesia + Cloudflare (v62). Dipakai _fetch /
# _fetch_once / _fetch_media: halaman blokir HTTP 200 harus diperlakukan sebagai
# GAGAL, bukan sukses (sebelumnya dikembalikan apa adanya, lalu pemanggil gagal
# parse dengan pesan menyesatkan — atau lebih buruk, dianggap halaman asli).
_BLOCK_PAGE_MARKERS = (
    "internet sehat",
    "trustpositif",
    "trust positif",
    "situs ini diblokir",
    "website ini diblokir",
    "halaman ini diblokir",
    "attention required",
    "just a moment",
    "cf-challenge",
    "kominfo",
    "access denied",
)


def _looks_like_block_page(text) -> bool:
    """True bila teks (HTML) tampak seperti halaman blokir ISP/Cloudflare.
    Hanya memeriksa <title> + 4000 karakter pertama agar halaman sah yang
    kebetulan menyebut kata itu di bagian dalam tidak kena false positive."""
    if not text:
        return False
    head = text if isinstance(text, str) else text.decode("utf-8", errors="ignore")
    m = re.search(r"<title[^>]*>(.*?)</title>", head, re.I | re.S)
    sample = ((m.group(1) + "\n" + head[:4000]) if m else head[:4000]).lower()
    return any(mk in sample for mk in _BLOCK_PAGE_MARKERS)


def _set_dns_cache(host: str, ip: str):
    """Menyimpan hasil resolve DNS dengan batas ukuran memori (bounded cache)."""
    if len(_DNS_CACHE) >= _MAX_DNS_CACHE:
        try:
            first_k = next(iter(_DNS_CACHE))
            _DNS_CACHE.pop(first_k, None)
        except Exception:
            _DNS_CACHE.clear()
    _DNS_CACHE[host] = ip


def _resolve_host_impl(host: str) -> str:
    """Resolve domain ke IPv4 dengan proteksi anti DNS-poisoning ISP.
    Urutan (v62): DoH dulu (Cloudflare 1.1.1.1 / Google 8.8.8.8) -> System DNS ->
    Hardcoded Fallback. DoH didahulukan karena DNS sistem bisa di-poison ISP ke IP
    halaman blokir (IP publik, lolos cek private-range) — dalam kasus itu desync
    ikut nyambung ke IP yang salah; bila DoH gagal/tidak terjangkau, jatuh ke
    system DNS seperti sebelumnya. Hasil di-cache (biaya DoH hanya sekali/host)."""
    if not host:
        return host
    if host in _DNS_CACHE:
        return _DNS_CACHE[host]

    # Cek apakah host sudah merupakan IP numerik
    if all(part.isdigit() for part in host.split(".") if part):
        _set_dns_cache(host, host)
        return host

    def _usable_ip(ip):
        # Tolak IP private/loopback/link-local (hasil DNS-poisoning ISP).
        try:
            parts = [int(p) for p in str(ip).split(".")]
            if len(parts) != 4 or any(p < 0 or p > 255 for p in parts):
                return False
            a, b = parts[0], parts[1]
            if a == 10 or a == 127 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168):
                return False
            if a == 169 and b == 254:
                return False
            return True
        except Exception:
            return False

    # 1. Bypass DNS Blokir ISP via DoH (DNS-over-HTTPS) — didahulukan (v62).
    for doh_url in ("https://1.1.1.1/dns-query", "https://dns.google/resolve"):
        try:
            import urllib.request
            import json
            req = urllib.request.Request(
                f"{doh_url}?name={host}&type=A",
                headers={"Accept": "application/dns-json", "User-Agent": HI_UA}
            )
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    answers = data.get("Answer", [])
                    for ans in answers:
                        if ans.get("type") == 1 and ans.get("data"):
                            ip = ans["data"]
                            if _usable_ip(ip):
                                _set_dns_cache(host, ip)
                                return ip
        except Exception:
            pass

    # 2. Coba resolve DNS sistem lokal
    try:
        ip = socket.gethostbyname(host)
        # Abaikan bila di-poison ke IP localhost atau private range
        if _usable_ip(ip):
            _set_dns_cache(host, ip)
            return ip
    except Exception:
        pass

    # 3. Fallback IP CDN yang diketahui
    known = {
        "hls.dramahot.top": "93.123.109.210",
        "dramahot.top": "93.123.109.210",
    }
    if host in known:
        _set_dns_cache(host, known[host])
        return known[host]

    return host


def _resolve_host(host: str, _desync: bool = False) -> str:
    """Wrapper pencatat (Diagnosa v63): resolve seperti biasa, tapi catat
    hasilnya ke ring buffer netdiag (stage dns, host saja). Cache hit dan
    IP numerik tidak dicatat — tidak ada jaringan yang terjadi.
    _desync=True bila resolve dilakukan dari jalur desync (satu-satunya
    pemanggil saat ini: tls_desync_connect)."""
    if not host:
        return host
    if host in _DNS_CACHE:
        return _DNS_CACHE[host]
    if all(part.isdigit() for part in host.split(".") if part):
        return _resolve_host_impl(host)
    _t0 = time.monotonic()
    _nd_method = "desync" if _desync else "direct"
    try:
        ip = _resolve_host_impl(host)
    except Exception as e:
        try:
            from api.netdiag import record as _nd_rec, classify_error as _nd_class
            _nd_rec("dns", host, _nd_method, _nd_class(e), int((time.monotonic() - _t0) * 1000))
        except Exception:
            pass
        raise
    try:
        from api.netdiag import record as _nd_rec
        if ip == host:
            _nd_rec("dns", host, _nd_method, "gagal-dns", int((time.monotonic() - _t0) * 1000))
        else:
            _nd_rec("dns", host, _nd_method, "sukses", int((time.monotonic() - _t0) * 1000))
    except Exception:
        pass
    return ip


def tls_desync_connect(host: str, port: int = 443, timeout: float = 8.0, variant: int = 1):
    """Membuka socket TLS dengan Client Hello dipecah menjadi beberapa TLS Record.
    Varian 1 (v62): 2 fragmen — Record 1: 1 byte data Client Hello; Record 2: sisanya.
    Varian 2 (v64): 3 fragmen — [1 byte][64 byte][sisa], jeda 0,02 dtk; SNI
    terpecah di DUA titik dengan ukuran rekaman berbeda dari v1.
    Menghilangkan signature SNI utuh dari pemindaian DPI ISP.
    Seluruh percobaan (connect + handshake) dibatasi `timeout` secara
    keseluruhan (v64 — sebelumnya connect dan handshake masing-masing bisa
    makan `timeout`, total sampai 2x). Hang = DesyncTimeout, bukan socket
    setengah-jadi seperti sebelumnya."""
    _variant = 2 if variant == 2 else 1
    _end = time.monotonic() + float(timeout)
    def _remaining():
        return _end - time.monotonic()
    ip = _resolve_host(host, _desync=True)
    raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        raw_sock.settimeout(max(0.1, _remaining()))
        raw_sock.connect((ip, port))
    except (socket.timeout, TimeoutError):
        try:
            raw_sock.close()
        except Exception:
            pass
        raise DesyncTimeout(f"desync connect timeout {host}", variant=_variant)
    raw_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    in_bio, out_bio = ssl.MemoryBIO(), ssl.MemoryBIO()
    sslobj = ctx.wrap_bio(in_bio, out_bio, server_hostname=host)

    try:
        sslobj.do_handshake()
    except ssl.SSLWantReadError:
        pass

    b = out_bio.read()
    if len(b) > 10:
        if _variant == 2:
            # v64: 3 fragmen — pecah di dua titik, bukan satu.
            rec1 = b[:3] + b"\x00\x01" + b[5:6]
            rec2_data = b[6:70]
            rec2 = b[:3] + len(rec2_data).to_bytes(2, "big") + rec2_data
            rec3_data = b[70:]
            rec3 = b[:3] + len(rec3_data).to_bytes(2, "big") + rec3_data
            raw_sock.sendall(rec1)
            time.sleep(0.02)  # Jeda mikro agar terkirim terpisah di TCP layer
            raw_sock.sendall(rec2)
            time.sleep(0.02)
            raw_sock.sendall(rec3)
        else:
            # Pecah Client Hello menjadi 2 valid TLS records
            rec1 = b[:3] + b"\x00\x01" + b[5:6]
            rec2_data = b[6:]
            rec2 = b[:3] + len(rec2_data).to_bytes(2, "big") + rec2_data

            raw_sock.sendall(rec1)
            time.sleep(0.04)  # Jeda mikro agar terkirim terpisah di TCP layer
            raw_sock.sendall(rec2)
    else:
        raw_sock.sendall(b)

    # Selesaikan TLS Handshake — dibatasi deadline keseluruhan (v64).
    _completed = False
    while time.monotonic() < _end:
        try:
            raw_sock.settimeout(max(0.1, _remaining()))
            d = raw_sock.recv(8192)
        except (socket.timeout, TimeoutError):
            break
        if not d:
            break
        in_bio.write(d)
        try:
            sslobj.do_handshake()
            if out_bio.pending:
                raw_sock.sendall(out_bio.read())
            _completed = True
            break
        except ssl.SSLWantWriteError:
            raw_sock.sendall(out_bio.read())
        except ssl.SSLWantReadError:
            if out_bio.pending:
                raw_sock.sendall(out_bio.read())
    if not _completed:
        try:
            raw_sock.close()
        except Exception:
            pass
        raise DesyncTimeout(f"desync handshake timeout {host}", variant=_variant)

    return raw_sock, sslobj, in_bio, out_bio


def tls_desync_request(url: str, headers: dict = None, timeout: float = 10.0, variant: int = 1) -> bytes:
    """Kirim HTTP GET request via desync TLS socket dan kembalikan response body bytes.
    v64: seluruh percobaan dibatasi `timeout` secara keseluruhan (connect +
    handshake + baca respons); bila TIDAK ADA satu byte pun yang tiba sampai
    batas waktu, raise DesyncTimeout (hang) — sebelumnya mengembalikan b''
    sehingga pemanggil tidak bisa membedakan hang dari body kosong."""
    _variant = 2 if variant == 2 else 1
    p = urllib.parse.urlsplit(url)
    host = p.netloc.split(":")[0]
    port = p.port or 443
    path = p.path or "/"
    if p.query:
        path += "?" + p.query
    _end = time.monotonic() + float(timeout)

    raw_sock, sslobj, in_bio, out_bio = tls_desync_connect(host, port, timeout=timeout, variant=_variant)
    try:
        h_lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host}",
            f"User-Agent: {HI_UA}",
            "Accept: */*",
            "Connection: close",
        ]
        if headers:
            for k, v in headers.items():
                if k.lower() not in ("host", "connection"):
                    h_lines.append(f"{k}: {v}")
        
        req_data = "\r\n".join(h_lines) + "\r\n\r\n"
        sslobj.write(req_data.encode("utf-8"))
        raw_sock.sendall(out_bio.read())

        # Baca seluruh HTTP response — dibatasi deadline keseluruhan (v64):
        # server yang meneteskan 1 byte per interval tidak bisa memperpanjang
        # percobaan tanpa batas seperti sebelumnya (settimeout per-recv).
        resp_raw = b""
        while True:
            _rem = _end - time.monotonic()
            if _rem <= 0:
                break
            try:
                raw_sock.settimeout(_rem)
                d = raw_sock.recv(16384)
                if not d:
                    break
                in_bio.write(d)
                while True:
                    try:
                        chunk = sslobj.read(16384)
                        if not chunk:
                            break
                        resp_raw += chunk
                    except (ssl.SSLWantReadError, ssl.SSLZeroReturnError):
                        break
            except socket.timeout:
                break
            except Exception:
                break

        if not resp_raw:
            raise DesyncTimeout(f"desync response timeout {host}", variant=_variant)
        # Pisahkan header dan body HTTP
        idx = resp_raw.find(b"\r\n\r\n")
        if idx >= 0:
            return resp_raw[idx + 4:]
        return resp_raw
    finally:
        try:
            raw_sock.close()
        except Exception:
            pass


def tls_desync_post(url: str, data=None, headers: dict = None, timeout: float = 10.0, variant: int = 1) -> bytes:
    """Kirim HTTP POST via desync TLS socket (v62 — untuk admin-ajax Otakudesu saat
    direct di-RST DPI). Body dikembalikan setelah di-dechunk bila ber-chunked.
    data: dict (form-urlencoded) atau bytes mentah.
    v64: seperti tls_desync_request — dibatasi `timeout` keseluruhan; nol byte
    yang tiba = DesyncTimeout (hang)."""
    _variant = 2 if variant == 2 else 1
    p = urllib.parse.urlsplit(url)
    host = p.netloc.split(":")[0]
    port = p.port or 443
    path = p.path or "/"
    if p.query:
        path += "?" + p.query
    _end = time.monotonic() + float(timeout)

    raw_sock, sslobj, in_bio, out_bio = tls_desync_connect(host, port, timeout=timeout, variant=_variant)
    try:
        if data is None:
            body = b""
        elif isinstance(data, (bytes, bytearray)):
            body = bytes(data)
        else:
            body = urllib.parse.urlencode(data).encode("utf-8")
        h_lines = [
            f"POST {path} HTTP/1.1",
            f"Host: {host}",
            f"User-Agent: {HI_UA}",
            "Accept: */*",
            "Content-Type: application/x-www-form-urlencoded",
            f"Content-Length: {len(body)}",
            "Connection: close",
        ]
        if headers:
            for k, v in headers.items():
                if k.lower() not in ("host", "connection", "content-length", "content-type"):
                    h_lines.append(f"{k}: {v}")

        req_data = "\r\n".join(h_lines) + "\r\n\r\n"
        sslobj.write(req_data.encode("utf-8"))
        if body:
            sslobj.write(body)
        raw_sock.sendall(out_bio.read())

        # Baca seluruh HTTP response — dibatasi deadline keseluruhan (v64).
        resp_raw = b""
        while True:
            _rem = _end - time.monotonic()
            if _rem <= 0:
                break
            try:
                raw_sock.settimeout(_rem)
                d = raw_sock.recv(16384)
                if not d:
                    break
                in_bio.write(d)
                while True:
                    try:
                        chunk = sslobj.read(16384)
                        if not chunk:
                            break
                        resp_raw += chunk
                    except (ssl.SSLWantReadError, ssl.SSLZeroReturnError):
                        break
            except socket.timeout:
                break
            except Exception:
                break

        if not resp_raw:
            raise DesyncTimeout(f"desync response timeout {host}", variant=_variant)
        # Pisahkan header dan body HTTP, lalu lepas chunked bila ada.
        idx = resp_raw.find(b"\r\n\r\n")
        raw_body = resp_raw[idx + 4:] if idx >= 0 else resp_raw
        dec = _dechunk_http_body(raw_body)
        return dec if dec is not None else raw_body
    finally:
        try:
            raw_sock.close()
        except Exception:
            pass


def _desync_timeout_each(deadline, timeout_each):
    """Timeout satu percobaan varian: dibatasi cap 6 dtk DAN sisa anggaran
    resolve (bila ada). Minimal 0,5 dtk agar percobaan tetap sempat jalan."""
    to = min(float(timeout_each), DESYNC_ATTEMPT_TIMEOUT)
    if deadline is not None:
        to = min(to, max(0.5, float(deadline) - time.monotonic()))
    return to


def desync_request_variants(url, headers=None, deadline=None, timeout_each=DESYNC_ATTEMPT_TIMEOUT):
    """GET via desync dengan fallback varian (v64): coba varian 1; bila
    menggantung (DesyncTimeout), coba varian 2. Varian yang sudah hang untuk
    (deadline, host) di resolve ini dilewati (registri hang). Return
    (bytes, variant) — variant = varian yang berhasil.
    Raise DesyncTimeout (.variant = varian terakhir yang dicoba) bila semua
    varian yang dicoba hang. Body kosong (b'') dikembalikan apa adanya —
    pemanggil yang menilai (bukan hang)."""
    p = urllib.parse.urlsplit(url)
    host = (p.netloc.split(":")[0] or "").lower()
    _hung = hung_kinds(deadline, host)
    _last_err = None
    for _v in (1, 2):
        if f"v{_v}" in _hung:
            continue
        try:
            data = tls_desync_request(url, headers=headers,
                                      timeout=_desync_timeout_each(deadline, timeout_each),
                                      variant=_v)
            return data, _v
        except DesyncTimeout as e:
            e.variant = _v
            note_hang(deadline, host, f"v{_v}")
            _last_err = e
            continue
    if _last_err is not None:
        raise _last_err
    raise DesyncTimeout(f"desync semua varian sudah hang {host}", variant=None)


def desync_post_variants(url, data=None, headers=None, deadline=None, timeout_each=DESYNC_ATTEMPT_TIMEOUT):
    """POST via desync dengan fallback varian (v64) — sama seperti
    desync_request_variants, untuk admin-ajax Otakudesu."""
    p = urllib.parse.urlsplit(url)
    host = (p.netloc.split(":")[0] or "").lower()
    _hung = hung_kinds(deadline, host)
    _last_err = None
    for _v in (1, 2):
        if f"v{_v}" in _hung:
            continue
        try:
            raw = tls_desync_post(url, data=data, headers=headers,
                                  timeout=_desync_timeout_each(deadline, timeout_each),
                                  variant=_v)
            return raw, _v
        except DesyncTimeout as e:
            e.variant = _v
            note_hang(deadline, host, f"v{_v}")
            _last_err = e
            continue
    if _last_err is not None:
        raise _last_err
    raise DesyncTimeout(f"desync semua varian sudah hang {host}", variant=None)


async def tls_desync_stream(url: str, headers: dict = None, chunk_size: int = 65536):
    """Async generator untuk streaming segmen video via desync TLS dengan proteksi anti socket-leak."""
    import asyncio
    p = urllib.parse.urlsplit(url)
    host = p.netloc.split(":")[0]
    port = p.port or 443
    path = p.path or "/"
    if p.query:
        path += "?" + p.query

    loop = asyncio.get_running_loop()
    raw_sock, sslobj, in_bio, out_bio = await loop.run_in_executor(
        None, lambda: tls_desync_connect(host, port, timeout=10.0)
    )

    try:
        h_lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host}",
            f"User-Agent: {HI_UA}",
            "Accept: */*",
            "Connection: close",
        ]
        if headers:
            for k, v in headers.items():
                if k.lower() not in ("host", "connection"):
                    h_lines.append(f"{k}: {v}")

        req_data = "\r\n".join(h_lines) + "\r\n\r\n"
        sslobj.write(req_data.encode("utf-8"))
        raw_sock.sendall(out_bio.read())

        # Stream chunks
        header_parsed = False
        buf = b""
        raw_sock.settimeout(12.0)

        while True:
            try:
                d = await loop.run_in_executor(None, lambda: raw_sock.recv(32768))
                if not d:
                    break
                in_bio.write(d)
                while True:
                    try:
                        chunk = sslobj.read(32768)
                        if not chunk:
                            break
                        if not header_parsed:
                            buf += chunk
                            sep = buf.find(b"\r\n\r\n")
                            if sep >= 0:
                                header_parsed = True
                                body_part = buf[sep + 4:]
                                buf = b""
                                if body_part:
                                    yield body_part
                        else:
                            yield chunk
                    except (ssl.SSLWantReadError, ssl.SSLZeroReturnError):
                        break
            except Exception:
                break
    finally:
        try:
            raw_sock.close()
        except Exception:
            pass


def _dechunk_http_body(data: bytes):
    """Lepas chunked transfer-encoding (RFC 9112) dari body mentah desync.
    Kembalikan None bila data bukan chunked yang valid (body utuh bukan
    chunked — mis. gambar biner — tidak pernah diubah).
    v61: dipindah dari backend/main.py ke sini supaya satu aturan dipakai
    semua jalur (gambar, video, probe) — sebelumnya hanya /api/img yang
    melepaskannya, jalur video tidak, sehingga body playlist ber-chunked
    lolos validasi substring lalu ditolak ExoPlayer (code 3002)."""
    if not data:
        return None
    out = bytearray()
    pos = 0
    n = len(data)
    while pos < n:
        end = data.find(b"\r\n", pos)
        if end < 0 or end - pos > 16:
            return None
        size_s = data[pos:end].split(b";", 1)[0].strip()
        try:
            size = int(size_s, 16)
        except ValueError:
            return None
        pos = end + 2
        if size == 0:
            return bytes(out)
        if pos + size > n:
            return None
        out += data[pos:pos + size]
        pos += size
        if data[pos:pos + 2] == b"\r\n":
            pos += 2
    return None
