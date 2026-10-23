import os, sys, threading

def trim_memory():
    """Trigger Python garbage collection explicitly on memory trim (Android TV only)."""
    if os.environ.get("TATAP_IS_TV") == "1":
        try:
            import gc
            gc.collect()
        except Exception:
            pass

def start_server(data_dir=None, is_tv=False):
    """Jalankan server Uvicorn Tatap di background thread Android."""
    try:
        if is_tv or os.environ.get("TATAP_IS_TV") == "1":
            os.environ["TATAP_IS_TV"] = "1"
            # Jangan override gc.set_threshold ke angka kecil karena memicu CPU thrashing & thermal throttling di TV Stick!

        base_dir = os.path.dirname(os.path.abspath(__file__))
        backend_dir = os.path.join(base_dir, "backend")
        frontend_dir = os.path.join(base_dir, "frontend")
        if base_dir not in sys.path:
            sys.path.insert(0, base_dir)

        # --- OTA payload: isi unduhan (backend+frontend dari rilis GitHub)
        # menimpa isi bawaan APK bila ada dan sehat; gagal -> bawaan. ---
        payload_version = 0
        if data_dir:
            try:
                import ota_core
                sel = ota_core.prepare_boot(data_dir)
                if sel:
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

def run_in_background(data_dir=None, is_tv=False):
    t = threading.Thread(target=start_server, args=(data_dir, is_tv), daemon=True, name="TatapBackendThread")
    t.start()
    return True
