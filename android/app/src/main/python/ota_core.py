"""OTA payload core untuk Lumio — stdlib murni, tanpa dependensi Android.

Dipakai oleh server_launcher (pemilihan backend saat boot) dan backend
(endpoint /api/payload/*). Payload = zip berisi backend/ + frontend/ +
PAYLOAD.json yang diunduh dari rilis GitHub repo Lumio; memungkinkan
perbaikan & fitur baru sampai ke pengguna TANPA update APK.

Tata letak di <data_dir>/ota/:
  state.json    {active, prev, pending, attempts{}, ok{}, blacklist[]}
  payload/      payload aktif (backend/, frontend/, PAYLOAD.json)
  payload.prev/ payload bagus terakhir (cadangan rollback)
  incoming/     payload terstaging, dipromosikan saat boot berikutnya
  staging/      area kerja ekstrak (sementara)

Pengaman: payload baru harus lolos py_compile seluruh backend; payload
yang gagal boot sehat 2x berturut-turut otomatis di-rollback ke prev /
bawaan APK dan versinya masuk blacklist.
"""
import hashlib
import io
import json
import os
import py_compile
import shutil
import zipfile

OTA_DIRNAME = "ota"


def ota_dir(data_dir):
    return os.path.join(data_dir, OTA_DIRNAME)


def _state_path(d):
    return os.path.join(d, "state.json")


def load_state(d):
    try:
        with open(_state_path(d), "r", encoding="utf-8") as f:
            s = json.load(f)
        if not isinstance(s, dict):
            s = {}
    except Exception:
        s = {}
    s.setdefault("active", 0)
    s.setdefault("prev", 0)
    s.setdefault("pending", None)
    s.setdefault("attempts", {})
    s.setdefault("ok", {})
    s.setdefault("blacklist", [])
    return s


def save_state(d, s):
    os.makedirs(d, exist_ok=True)
    tmp = _state_path(d) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f)
    os.replace(tmp, _state_path(d))


def payload_meta(pdir):
    try:
        with open(os.path.join(pdir, "PAYLOAD.json"), "r", encoding="utf-8") as f:
            m = json.load(f)
        return m if isinstance(m, dict) else None
    except Exception:
        return None


def payload_valid(pdir):
    m = payload_meta(pdir)
    return bool(
        m
        and isinstance(m.get("version"), int)
        and os.path.isfile(os.path.join(pdir, "backend", "main.py"))
        and os.path.isdir(os.path.join(pdir, "frontend"))
    )


def prepare_boot(data_dir):
    """Jalankan SEBELUM backend dipilih. Mengembalikan dict
    {version, backend_dir, frontend_dir, dir} bila payload OTA layak jalan,
    atau None untuk memakai isi bawaan APK. Menangani promosi pending dan
    rollback payload yang gagal boot sehat 2x."""
    if not data_dir:
        return None
    d = ota_dir(data_dir)
    s = load_state(d)
    changed = False

    # 1) Promosi pending -> aktif (yang lama disimpan sebagai prev)
    pend = s.get("pending")
    inc = os.path.join(d, "incoming")
    if pend:
        meta = payload_meta(inc) if os.path.isdir(inc) else None
        if (
            int(pend) not in s["blacklist"]
            and payload_valid(inc)
            and meta
            and int(meta["version"]) == int(pend)
        ):
            cur = os.path.join(d, "payload")
            prev = os.path.join(d, "payload.prev")
            shutil.rmtree(prev, ignore_errors=True)
            if os.path.isdir(cur):
                os.replace(cur, prev)
                s["prev"] = s["active"]
            os.replace(inc, cur)
            s["active"] = int(pend)
            s["pending"] = None
            s["attempts"][str(pend)] = 0
            s["ok"][str(pend)] = False
            changed = True
        else:
            s["pending"] = None
            changed = True
            shutil.rmtree(inc, ignore_errors=True)

    # 2) Rollback bila payload aktif belum terbukti sehat setelah 2x boot
    act = int(s.get("active") or 0)
    if act > 0 and not s["ok"].get(str(act)):
        n = int(s["attempts"].get(str(act), 0)) + 1
        s["attempts"][str(act)] = n
        changed = True
        if n >= 2:
            if act not in s["blacklist"]:
                s["blacklist"].append(act)
            cur = os.path.join(d, "payload")
            prevd = os.path.join(d, "payload.prev")
            shutil.rmtree(cur, ignore_errors=True)
            pv = int(s.get("prev") or 0)
            if pv > 0 and payload_valid(prevd):
                os.replace(prevd, cur)
                s["active"] = pv
            else:
                s["active"] = 0
            act = int(s["active"])

    if changed:
        save_state(d, s)

    if act > 0:
        pdir = os.path.join(d, "payload")
        if payload_valid(pdir):
            return {
                "version": act,
                "backend_dir": os.path.join(pdir, "backend"),
                "frontend_dir": os.path.join(pdir, "frontend"),
                "dir": pdir,
            }
    return None


def mark_boot_ok(data_dir, version):
    """Dipanggil backend (lifespan) begitu server terbukti hidup."""
    d = ota_dir(data_dir)
    s = load_state(d)
    s["ok"][str(int(version))] = True
    s["attempts"][str(int(version))] = 0
    save_state(d, s)


def stage_payload(data_dir, zip_bytes, expected_sha256, expected_version):
    """Verifikasi (sha256, struktur, versi, py_compile) lalu jadikan
    incoming. Tidak melempar; hasil berupa dict {ok, ...}."""
    d = ota_dir(data_dir)
    os.makedirs(d, exist_ok=True)
    sha = hashlib.sha256(zip_bytes).hexdigest()
    if expected_sha256 and sha.lower() != str(expected_sha256).lower():
        return {"ok": False, "error": "SHA-256 payload tidak cocok"}
    stg = os.path.join(d, "staging")
    shutil.rmtree(stg, ignore_errors=True)
    os.makedirs(stg, exist_ok=True)
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        for name in zf.namelist():
            parts = name.replace("\\", "/").split("/")
            if name.startswith("/") or ".." in parts:
                return {"ok": False, "error": "Isi zip tidak aman"}
        zf.extractall(stg)
    except Exception as e:
        return {"ok": False, "error": f"Zip rusak: {e}"}
    if not payload_valid(stg):
        return {"ok": False, "error": "Struktur payload tidak lengkap"}
    meta = payload_meta(stg)
    if int(meta["version"]) != int(expected_version):
        return {"ok": False, "error": "Versi payload tidak sesuai manifest"}
    for root, _dirs, files in os.walk(os.path.join(stg, "backend")):
        for fn in files:
            if fn.endswith(".py"):
                try:
                    py_compile.compile(os.path.join(root, fn), doraise=True)
                except Exception as e:
                    return {"ok": False, "error": f"Kompilasi gagal di {fn}: {e}"}
    inc = os.path.join(d, "incoming")
    shutil.rmtree(inc, ignore_errors=True)
    os.replace(stg, inc)
    s = load_state(d)
    s["pending"] = int(expected_version)
    save_state(d, s)
    return {"ok": True, "version": int(expected_version), "label": meta.get("label", "")}
