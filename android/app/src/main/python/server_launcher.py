import os, re, sys, threading


def _ver_tuple(v):
    """'2.27.0' -> (2, 27, 0); segmen non-angka dihitung 0."""
    parts = []
    for p in str(v).strip().split("."):
        m = re.match(r"(\d+)", p)
        parts.append(int(m.group(1)) if m else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def _backend_version(backend_dir):
    """Baca __version__ dari backend/version.py tanpa mengimpor modul
    (impor akan bentrok antara isi bawaan dan payload). None bila tak
    terbaca."""
    try:
        with open(os.path.join(backend_dir, "version.py"), "r", encoding="utf-8") as f:
            m = re.search(r"""__version__\s*=\s*["']([^"']+)["']""", f.read())
        return m.group(1) if m else None
    except Exception:
        return None


def _payload_is_stale(payload_backend_dir, bundled_backend_dir, data_dir):
    """True bila payload terpilih harus DIABAIKAN boot ini: versinya
    lebih rendah dari backend bawaan APK, atau versinya tak terbaca
    (payload korup). Dalam kedua kasus, status aktif dibersihkan secara
    aman agar payload usang/rusak tidak terpilih lagi di boot
    berikutnya — sampai ada payload baru (promosi pending tetap berjalan
    normal). Mengembalikan False bila payload layak dipakai (versi >=
    bawaan) atau bila versi bawaan sendiri tak terbaca (jangan merusak
    perilaku lama). Tidak pernah melempar.

    SUMBER VERSI BAWAAN (v2.28.1): env LUMIO_APP_VERSION DIDAHULUKAN —
    diisi sisi Java dari PackageManager versionName. Ini SATU-SATUNYA
    sumber yang benar di produksi: APK hanya berisi .pyc hasil compile
    dan TIDAK ADA version.py di disk, sehingga pembacaan file selalu
    gagal dan aturan stale v2.28.0 tidak pernah aktif di HP pengguna
    (payload v18/backend 2.27.0 terus menutupi APK 2.28.0). Bila env
    tidak ada, fallback ke pembacaan version.py (jalur sandbox/dev,
    di mana tree sumber berisi .py)."""
    bundled = os.environ.get("LUMIO_APP_VERSION") or _backend_version(bundled_backend_dir)
    if bundled is None:
        print("[LUMIO-OTA] Versi bawaan tak terbaca (env kosong + version.py tak ada); payload dipakai apa adanya (fail-open)", flush=True)
        return False
    pv = _backend_version(payload_backend_dir)
    if pv is None or _ver_tuple(pv) < _ver_tuple(bundled):
        try:
            import ota_core
            d = ota_core.ota_dir(data_dir)
            s = ota_core.load_state(d)
            if int(s.get("active") or 0) > 0:
                s["active"] = 0
                ota_core.save_state(d, s)
        except Exception:
            pass
        return True
    return False

def trim_memory():
    """Trigger Python garbage collection explicitly on memory trim (Android TV only)."""
    if os.environ.get("TATAP_IS_TV") == "1":
        try:
            import gc
            gc.collect()
        except Exception:
            pass

def start_server(data_dir=None, is_tv=False, app_version=None):
    """Jalankan server Uvicorn Tatap di background thread Android.

    app_version (opsional, backward-compatible): versionName APK dari
    sisi Java (PackageManager). Bila diisi, disimpan ke env
    LUMIO_APP_VERSION SEBELUM pemilihan payload, agar aturan stale
    memakai versi bawaan yang benar di produksi (tanpa version.py di
    disk). Pemanggil lama tanpa argumen ini tetap jalan lewat fallback
    pembacaan version.py."""
    try:
        if app_version:
            os.environ["LUMIO_APP_VERSION"] = str(app_version)
        if is_tv or os.environ.get("TATAP_IS_TV") == "1":
            os.environ["TATAP_IS_TV"] = "1"
            # Jangan override gc.set_threshold ke angka kecil karena memicu CPU thrashing & thermal throttling di TV Stick!

        base_dir = os.path.dirname(os.path.abspath(__file__))
        backend_dir = os.path.join(base_dir, "backend")
        frontend_dir = os.path.join(base_dir, "frontend")
        if base_dir not in sys.path:
            sys.path.insert(0, base_dir)

        # --- OTA payload: isi unduhan (backend+frontend dari rilis GitHub)
        # menimpa isi bawaan APK bila ada dan sehat; gagal -> bawaan.
        # ATURAN v2.28.0: payload yang backend-nya LEBIH TUA dari bawaan
        # APK diabaikan (kasus nyata: payload v16/backend 2.26.0 menutupi
        # APK 2.27.0 sehingga aplikasi tampil 2.26.0). Payload usang itu
        # juga dibersihkan dari status aktif agar tidak terpilih lagi. ---
        payload_version = 0
        if data_dir:
            try:
                import ota_core
                sel = ota_core.prepare_boot(data_dir)
                if sel:
                    if _payload_is_stale(sel["backend_dir"], backend_dir, data_dir):
                        print("[LUMIO-OTA] Payload usang/rusak diabaikan; pakai bawaan APK", flush=True)
                    else:
                        backend_dir = sel["backend_dir"]
                        frontend_dir = sel["frontend_dir"]
                        payload_version = int(sel["version"])
                        print(f"[LUMIO-OTA] Memakai payload isi v{payload_version}", flush=True)
            except Exception as ota_err:
                print(f"[LUMIO-OTA] Lewati payload ({ota_err}); pakai bawaan APK", flush=True)
        os.environ["LUMIO_PAYLOAD_VERSION"] = str(payload_version)
        if data_dir:
            os.environ["LUMIO_DATA_DIR"] = data_dir

        if backend_dir not in sys.path:
            sys.path.insert(0, backend_dir)

        if data_dir:
            os.environ["TATAP_DATABASE_PATH"] = os.path.join(data_dir, "anime.db")
            os.environ["TATAP_CACHE_DIR"] = os.path.join(data_dir, "cache")
            os.makedirs(data_dir, exist_ok=True)
            os.makedirs(os.path.join(data_dir, "cache"), exist_ok=True)

        os.environ["APP_HOST"] = "127.0.0.1"
        os.environ["APP_PORT"] = "8767"

        # Import main module setelah environment siap
        import main
        main.frontend_dir = frontend_dir

        import uvicorn
        print("[TATAP-SERVER] Menyalakan Uvicorn pada 127.0.0.1:8767...", flush=True)

        class AndroidServer(uvicorn.Server):
            def install_signal_handlers(self):
                # Pass karena dijalankan di secondary thread Android
                pass

        is_tv_mode = (os.environ.get("TATAP_IS_TV") == "1")
        config = uvicorn.Config(
            main.app,
            host="127.0.0.1",
            port=8767,
            loop="asyncio",
            http="h11",
            log_level="warning" if is_tv_mode else "info",
            access_log=False
        )
        server = AndroidServer(config=config)
        server.run()
    except Exception as e:
        import traceback
        print(f"[TATAP-SERVER-ERROR] {e}\n{traceback.format_exc()}", flush=True)

def run_in_background(data_dir=None, is_tv=False, app_version=None):
    t = threading.Thread(target=start_server, args=(data_dir, is_tv, app_version), daemon=True, name="TatapBackendThread")
    t.start()
    return True
