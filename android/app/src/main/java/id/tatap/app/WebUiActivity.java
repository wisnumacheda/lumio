package id.tatap.app;

import android.app.Activity;
import android.app.Dialog;
import android.app.DownloadManager;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.ActivityInfo;
import android.content.SharedPreferences;
import android.database.Cursor;
import android.util.Log;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.os.Environment;
import android.os.Handler;
import android.os.Looper;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.Window;
import android.provider.Settings;
import android.webkit.JavascriptInterface;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;

import android.widget.Toast;

import androidx.core.content.FileProvider;

import org.json.JSONObject;

import java.io.File;
import java.net.HttpURLConnection;
import java.net.URL;

/**
 * Lumio (fork) — WebView shell.
 * Menampilkan UI web Lumio (prototype) dari backend lokal aplikasi
 * (http://127.0.0.1:8767/prototype) sebagai tampilan utama.
 * UI native (MainActivity) tetap hidup di belakang sebagai cadangan:
 * tombol Back pada layar utama web, atau tautan "UI Klasik" di
 * Pengaturan web, menutup activity ini dan kembali ke UI bawaan.
 */
public class WebUiActivity extends Activity {

    private static final String HOME_URL = "http://127.0.0.1:8767/prototype";
    private static final String PING_URL = "http://127.0.0.1:8767/api/ping";

    private WebView web;
    private TextView splashText;
    private View splashView;
    private FrameLayout fullContainer;
    private View customView;
    private WebChromeClient.CustomViewCallback customCallback;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private boolean pageLoaded = false;
    private String homeUrl = HOME_URL;

    // Unduhan update APK via DownloadManager (menggantikan streaming
    // HttpURLConnection yang gagal diam-diam: tanpa progres, tanpa kabar).
    private static final String UPDATE_APK_FILE = "Lumio-update.apk";
    // Resolver embed WebView tersembunyi (APK 2.30.0): dibuat malas saat
    // pertama dipakai, dihancurkan di onDestroy.
    private EmbedResolver embedResolver;

    private long updateDownloadId = -1;
    private String lastUpdateUrl;
    private boolean updateRetried = false;
    private boolean updateReceiverRegistered = false;
    private BroadcastReceiver updateDownloadReceiver;
    private Dialog updateProgressDialog;
    private Runnable updateProgressRunnable;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        // Mode TV v1 (payload v39+): TV TIDAK lagi dialihkan ke MainActivity
        // (UI native lama). WebView memuat prototype dengan ?tv=1 agar layer
        // web mengaktifkan layout + navigasi D-pad TV. MainActivity tetap ada
        // sebagai "UI Klasik" cadangan.
        try {
            if (TvFocusHelper.isTv(this)) homeUrl = HOME_URL + "?tv=1";
        } catch (Exception ignored) {}


        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.parseColor("#0B0B10"));

        web = new WebView(this);
        web.setVisibility(View.INVISIBLE);
        web.setBackgroundColor(Color.parseColor("#0B0B10"));
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        s.setUserAgentString(s.getUserAgentString() + " LumioApp");
        web.addJavascriptInterface(new UpdateBridge(), "LumioNative");
        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onShowCustomView(View view, CustomViewCallback callback) {
                customView = view;
                customCallback = callback;
                fullContainer.addView(view, new FrameLayout.LayoutParams(
                        ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
                fullContainer.setVisibility(View.VISIBLE);
                web.setVisibility(View.INVISIBLE);
                setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE);
                getWindow().getDecorView().setSystemUiVisibility(
                        View.SYSTEM_UI_FLAG_FULLSCREEN | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                                | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                                | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION | View.SYSTEM_UI_FLAG_LAYOUT_STABLE);
            }

            @Override
            public void onHideCustomView() {
                if (customView != null) {
                    fullContainer.removeView(customView);
                    customView = null;
                }
                fullContainer.setVisibility(View.GONE);
                web.setVisibility(View.VISIBLE);
                setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED);
                getWindow().getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_VISIBLE);
                if (customCallback != null) {
                    customCallback.onCustomViewHidden();
                    customCallback = null;
                }
            }
        });
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest req) {
                return handleUrl(req.getUrl());
            }

            @SuppressWarnings("deprecation")
            @Override
            public boolean shouldOverrideUrlLoading(WebView v, String url) {
                return handleUrl(Uri.parse(url));
            }

            @Override
            public void onPageFinished(WebView v, String url) {
                if (!pageLoaded) {
                    pageLoaded = true;
                    if (splashView != null) splashView.setVisibility(View.GONE);
                    if (customView == null) web.setVisibility(View.VISIBLE);
                }
            }
        });
        root.addView(web, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        fullContainer = new FrameLayout(this);
        fullContainer.setBackgroundColor(Color.BLACK);
        fullContainer.setVisibility(View.GONE);
        root.addView(fullContainer, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        LinearLayout splash = new LinearLayout(this);
        splash.setOrientation(LinearLayout.VERTICAL);
        splash.setGravity(Gravity.CENTER);
        TextView brand = new TextView(this);
        brand.setText("Lumio");
        brand.setTextColor(Color.WHITE);
        brand.setTextSize(30);
        brand.setGravity(Gravity.CENTER);
        splash.addView(brand);
        ProgressBar bar = new ProgressBar(this);
        splash.addView(bar);
        splashText = new TextView(this);
        splashText.setText("Menyiapkan Lumio…");
        splashText.setTextColor(Color.parseColor("#9A9AB0"));
        splashText.setTextSize(14);
        splashText.setGravity(Gravity.CENTER);
        splashText.setPadding(0, 24, 0, 0);
        splash.addView(splashText);
        FrameLayout.LayoutParams splashLp = new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT);
        root.addView(splash, splashLp);
        splashView = splash;
        splashText.setOnClickListener(v -> {
            splashText.setText("Menyiapkan Lumio…");
            startEmbeddedBackend();
            waitForBackend(0);
        });

        setContentView(root);
        startEmbeddedBackend();
        waitForBackend(0);
    }

    /** Jembatan JS -> native: unduh & pasang APK update (dari banner UI web). */
    public class UpdateBridge {
        /** Mode TV: dipakai layer web (?tv=1 sudah disetel saat load) sebagai
         * sinyal kedua bila query hilang (mis. navigasi internal). */
        @JavascriptInterface
        public boolean isTv() {
            try {
                return TvFocusHelper.isTv(WebUiActivity.this);
            } catch (Exception ignored) {
                return false;
            }
        }

        @JavascriptInterface
        public void setKeepAwake(final boolean on) {
            handler.post(() -> {
                if (on) {
                    getWindow().addFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
                } else {
                    getWindow().clearFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
                }
            });
        }

        /** Buka tautan resmi (mis. bab Manhwa resmi) di browser dalam
         * aplikasi, bukan browser luar. Hanya https + host jelas yang
         * diteruskan; sisanya ditolak dengan toast. */
        @JavascriptInterface
        public void openInAppBrowser(final String url) {
            handler.post(() -> {
                Uri uri = null;
                try {
                    uri = url != null ? Uri.parse(url) : null;
                } catch (Exception ignored) {
                }
                if (uri == null || !"https".equalsIgnoreCase(uri.getScheme())
                        || uri.getHost() == null) {
                    Toast.makeText(WebUiActivity.this,
                            "Tautan resmi tidak valid", Toast.LENGTH_SHORT).show();
                    return;
                }
                Intent it = new Intent(WebUiActivity.this, InAppBrowserActivity.class);
                it.putExtra(InAppBrowserActivity.EXTRA_URL, uri.toString());
                startActivity(it);
            });
        }

        @JavascriptInterface
        public void installUpdate(final String apkUrl) {
            handler.post(() -> {
                if (android.os.Build.VERSION.SDK_INT >= 26
                        && !getPackageManager().canRequestPackageInstalls()) {
                    Toast.makeText(WebUiActivity.this,
                            "Izinkan dulu install dari aplikasi ini, lalu tap Update lagi ya",
                            Toast.LENGTH_LONG).show();
                    try {
                        Intent it = new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                                Uri.parse("package:" + getPackageName()));
                        startActivity(it);
                    } catch (Exception ignored) {
                    }
                    return;
                }
                startUpdateDownload(apkUrl);
            });
        }

        /** Resolver embed (APK 2.30.0): muat halaman embed di WebView
         * tersembunyi, intersepsi request manifest .m3u8, kembalikan
         * hasilnya ke UI web lewat callback evaluateJavascript
         * window.__lumioEmbedResolveResult(requestId, payload). Asinkron:
         * metode ini langsung kembali; hasilnya datang lewat callback.
         * Jembatan ini HANYA ada di WebView utama (halaman Lumio sendiri);
         * hidden WebView tidak mendapat JavascriptInterface apa pun. */
        @JavascriptInterface
        public void resolveEmbed(final String embedUrl, final String requestId) {
            handler.post(() -> {
                if (embedResolver == null) {
                    String ua = "";
                    try {
                        if (web != null) ua = web.getSettings().getUserAgentString();
                    } catch (Exception ignored) {
                    }
                    embedResolver = new EmbedResolver(getApplicationContext(), handler, ua);
                }
                embedResolver.resolve(embedUrl, requestId, (id, payload) -> {
                    if (web == null || isFinishing() || isDestroyed()) return;
                    try {
                        String js = "window.__lumioEmbedResolveResult("
                                + JSONObject.quote(id) + "," + payload.toString() + ")";
                        web.evaluateJavascript(js, null);
                    } catch (Exception ignored) {
                    }
                });
            });
        }

        /** Batalkan resolve embed yang sedang berjalan (dipanggil UI web
         * saat player ditutup / timeout). */
        @JavascriptInterface
        public void cancelEmbedResolve(final String requestId) {
            handler.post(() -> {
                if (embedResolver != null) embedResolver.cancel(requestId);
            });
        }
    }

    /** Unduh APK update via DownloadManager (bukan streaming):
     * berkas nyata di folder Download, progres di notifikasi + dialog
     * dalam aplikasi, ketuk notifikasi untuk install. Dipanggil dari
     * UI thread (installUpdate sudah berjalan di handler). */
    private void startUpdateDownload(String apkUrl) {
        if (updateDownloadId != -1) {
            Toast.makeText(this, "Pengunduhan pembaruan sedang berlangsung...", Toast.LENGTH_SHORT).show();
            return;
        }
        lastUpdateUrl = apkUrl;
        updateRetried = false;
        enqueueUpdateDownload(apkUrl);
    }

    private void enqueueUpdateDownload(String apkUrl) {
        File stale = new File(Environment.getExternalStoragePublicDirectory(
                Environment.DIRECTORY_DOWNLOADS), UPDATE_APK_FILE);
        try { if (stale.exists()) stale.delete(); } catch (Exception ignored) {}
        DownloadManager.Request req;
        try {
            req = new DownloadManager.Request(Uri.parse(apkUrl));
        } catch (Exception e) {
            Toast.makeText(this, "Tautan update tidak valid", Toast.LENGTH_LONG).show();
            return;
        }
        req.setTitle("Mengunduh update Lumio");
        req.setDescription("Lumio");
        req.setMimeType("application/vnd.android.package-archive");
        req.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
        req.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, UPDATE_APK_FILE);
        req.setAllowedOverMetered(true);
        req.setAllowedOverRoaming(false);
        DownloadManager dm = (DownloadManager) getSystemService(Context.DOWNLOAD_SERVICE);
        if (dm == null) {
            Toast.makeText(this, "Unduh update gagal: layanan unduhan tidak tersedia", Toast.LENGTH_LONG).show();
            return;
        }
        long id;
        try {
            id = dm.enqueue(req);
        } catch (Exception e) {
            Toast.makeText(this, "Unduh update gagal: " + e.getMessage(), Toast.LENGTH_LONG).show();
            return;
        }
        updateDownloadId = id;
        registerUpdateReceiver();
        showUpdateProgressDialog();
        scheduleUpdateProgressPoll();
    }

    private void registerUpdateReceiver() {
        if (updateReceiverRegistered) return;
        updateDownloadReceiver = new BroadcastReceiver() {
            @Override
            public void onReceive(Context context, Intent intent) {
                if (!DownloadManager.ACTION_DOWNLOAD_COMPLETE.equals(intent.getAction())) return;
                long id = intent.getLongExtra(DownloadManager.EXTRA_DOWNLOAD_ID, -1);
                if (id == updateDownloadId) onUpdateDownloadFinished();
            }
        };
        registerReceiver(updateDownloadReceiver, new IntentFilter(DownloadManager.ACTION_DOWNLOAD_COMPLETE));
        updateReceiverRegistered = true;
    }

    private void showUpdateProgressDialog() {
        dismissUpdateProgressDialog();
        Dialog d = new Dialog(this);
        d.requestWindowFeature(Window.FEATURE_NO_TITLE);
        d.setContentView(R.layout.dialog_app_download);
        d.setCancelable(false);
        if (d.getWindow() != null) {
            d.getWindow().setBackgroundDrawableResource(android.R.color.transparent);
            int w = getResources().getDisplayMetrics().widthPixels;
            int target = (int) (w * (TvFocusHelper.isTv(this) ? 0.65f : 0.90f));
            d.getWindow().setLayout(target, ViewGroup.LayoutParams.WRAP_CONTENT);
        }
        TextView tvTitle = d.findViewById(R.id.tv_download_title);
        if (tvTitle != null) tvTitle.setText("Mengunduh update Lumio");
        TextView tvTarget = d.findViewById(R.id.tv_download_target_version);
        if (tvTarget != null) tvTarget.setText("Mengunduh update Lumio");
        ProgressBar pb = d.findViewById(R.id.pb_download_horizontal);
        if (pb != null) { pb.setMax(100); pb.setProgress(0); }
        TextView tvStatus = d.findViewById(R.id.tv_download_status);
        if (tvStatus != null) tvStatus.setText("Menghubungkan ke server rilis...");
        TextView tvPercent = d.findViewById(R.id.tv_download_percent);
        if (tvPercent != null) tvPercent.setText("0%");
        View btnCancel = d.findViewById(R.id.btn_cancel_download);
        if (btnCancel != null) {
            btnCancel.setOnClickListener(v -> cancelUpdateDownload());
            if (TvFocusHelper.isTv(this)) {
                TvFocusHelper.attachFocusScale(btnCancel, 1.05f, 4f);
                btnCancel.requestFocus();
            }
        }
        updateProgressDialog = d;
        d.show();
    }

    private void scheduleUpdateProgressPoll() {
        if (updateProgressRunnable == null) {
            updateProgressRunnable = new Runnable() {
                @Override
                public void run() {
                    if (updateDownloadId == -1) return;
                    pollUpdateProgress();
                    if (updateDownloadId != -1) handler.postDelayed(this, 500);
                }
            };
        }
        handler.removeCallbacks(updateProgressRunnable);
        handler.postDelayed(updateProgressRunnable, 500);
    }

    private void pollUpdateProgress() {
        DownloadManager dm = (DownloadManager) getSystemService(Context.DOWNLOAD_SERVICE);
        if (dm == null || updateProgressDialog == null || !updateProgressDialog.isShowing()) return;
        Cursor c = null;
        try {
            c = dm.query(new DownloadManager.Query().setFilterById(updateDownloadId));
            if (c == null || !c.moveToFirst()) return;
            int status = c.getInt(c.getColumnIndexOrThrow(DownloadManager.COLUMN_STATUS));
            // Selesai/gagal ditangani penerima ACTION_DOWNLOAD_COMPLETE.
            if (status == DownloadManager.STATUS_SUCCESSFUL || status == DownloadManager.STATUS_FAILED) return;
            long soFar = c.getLong(c.getColumnIndexOrThrow(DownloadManager.COLUMN_BYTES_DOWNLOADED_SO_FAR));
            long total = c.getLong(c.getColumnIndexOrThrow(DownloadManager.COLUMN_TOTAL_SIZE_BYTES));
            ProgressBar pb = updateProgressDialog.findViewById(R.id.pb_download_horizontal);
            TextView tvStatus = updateProgressDialog.findViewById(R.id.tv_download_status);
            TextView tvPercent = updateProgressDialog.findViewById(R.id.tv_download_percent);
            if (status == DownloadManager.STATUS_PAUSED) {
                if (tvStatus != null) tvStatus.setText("Unduhan dijeda — menunggu jaringan…");
                return;
            }
            if (total > 0) {
                int progress = (int) ((soFar * 100) / total);
                if (pb != null) { pb.setIndeterminate(false); pb.setProgress(progress); }
                if (tvPercent != null) tvPercent.setText(progress + "%");
                if (tvStatus != null) {
                    tvStatus.setText(String.format(java.util.Locale.US, "%.1f MB / %.1f MB",
                            soFar / 1048576.0, total / 1048576.0));
                }
            } else {
                if (pb != null) pb.setIndeterminate(true);
                if (tvStatus != null) {
                    tvStatus.setText(String.format(java.util.Locale.US, "%.1f MB terunduh", soFar / 1048576.0));
                }
            }
        } catch (Exception ignored) {
        } finally {
            if (c != null) c.close();
        }
    }

    private void onUpdateDownloadFinished() {
        if (updateDownloadId == -1) return;
        long id = updateDownloadId;
        stopUpdateProgressPoll();
        DownloadManager dm = (DownloadManager) getSystemService(Context.DOWNLOAD_SERVICE);
        int status = DownloadManager.STATUS_FAILED;
        int reason = -1;
        String localUri = null;
        if (dm != null) {
            Cursor c = null;
            try {
                c = dm.query(new DownloadManager.Query().setFilterById(id));
                if (c != null && c.moveToFirst()) {
                    status = c.getInt(c.getColumnIndexOrThrow(DownloadManager.COLUMN_STATUS));
                    reason = c.getInt(c.getColumnIndexOrThrow(DownloadManager.COLUMN_REASON));
                    int uriIdx = c.getColumnIndex(DownloadManager.COLUMN_LOCAL_URI);
                    if (uriIdx >= 0) localUri = c.getString(uriIdx);
                }
            } catch (Exception ignored) {
            } finally { if (c != null) c.close(); }
        }
        if (status != DownloadManager.STATUS_SUCCESSFUL) {
            failUpdateDownload("Unduhan gagal (" + downloadFailReasonText(reason) + ")");
            return;
        }
        File apkFile = null;
        if (localUri != null) {
            try { apkFile = new File(Uri.parse(localUri).getPath()); } catch (Exception ignored) {}
        }
        if (apkFile == null || !apkFile.exists()) {
            apkFile = new File(Environment.getExternalStoragePublicDirectory(
                    Environment.DIRECTORY_DOWNLOADS), UPDATE_APK_FILE);
        }
        String problem = verifyApkFile(apkFile);
        if (problem != null) {
            try { if (apkFile.exists()) apkFile.delete(); } catch (Exception ignored) {}
            dismissUpdateProgressDialog();
            unregisterUpdateReceiver();
            updateDownloadId = -1;
            Toast.makeText(this, "File update tidak valid: " + problem + " — coba lagi",
                    Toast.LENGTH_LONG).show();
            return;
        }
        dismissUpdateProgressDialog();
        unregisterUpdateReceiver();
        updateDownloadId = -1;
        Uri installUri = null;
        if (dm != null) {
            try { installUri = dm.getUriForDownloadedFile(id); } catch (Exception ignored) {}
        }
        if (installUri == null) {
            try {
                installUri = FileProvider.getUriForFile(this,
                        getPackageName() + ".fileprovider", apkFile);
            } catch (Exception e) {
                Toast.makeText(this, "Gagal membuka installer: " + e.getMessage(), Toast.LENGTH_LONG).show();
                return;
            }
        }
        Toast.makeText(this, "Unduhan selesai! Memulai pemasangan...", Toast.LENGTH_SHORT).show();
        try {
            Intent it = new Intent(Intent.ACTION_VIEW);
            it.setDataAndType(installUri, "application/vnd.android.package-archive");
            it.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_ACTIVITY_NEW_TASK);
            startActivity(it);
        } catch (Exception e) {
            Toast.makeText(this, "Gagal membuka installer: " + e.getMessage(), Toast.LENGTH_LONG).show();
        }
    }

    /** Periksa berkas APK hasil unduhan: paket yang sama + versi lebih baru.
     * Mengembalikan null bila valid, atau alasan bila tidak valid. */
    private String verifyApkFile(File apkFile) {
        if (apkFile == null || !apkFile.exists() || apkFile.length() <= 0) return "berkas tidak ditemukan";
        android.content.pm.PackageInfo info;
        try {
            info = getPackageManager().getPackageArchiveInfo(apkFile.getAbsolutePath(), 0);
        } catch (Exception e) {
            return "tidak terbaca sebagai APK";
        }
        if (info == null || !getPackageName().equals(info.packageName)) return "bukan APK Lumio";
        try {
            android.content.pm.PackageInfo cur = getPackageManager().getPackageInfo(getPackageName(), 0);
            if (info.versionCode <= cur.versionCode) return "versi unduhan tidak lebih baru";
        } catch (Exception e) {
            return "tidak bisa memeriksa versi terpasang";
        }
        return null;
    }

    private void failUpdateDownload(String msg) {
        dismissUpdateProgressDialog();
        if (!updateRetried && lastUpdateUrl != null) {
            updateRetried = true;
            updateDownloadId = -1;
            unregisterUpdateReceiver();
            Toast.makeText(this, msg + " — mencoba lagi…", Toast.LENGTH_LONG).show();
            enqueueUpdateDownload(lastUpdateUrl);
            return;
        }
        updateDownloadId = -1;
        unregisterUpdateReceiver();
        Toast.makeText(this, msg + " — periksa koneksi lalu coba lagi", Toast.LENGTH_LONG).show();
    }

    private void cancelUpdateDownload() {
        if (updateDownloadId != -1) {
            try {
                DownloadManager dm = (DownloadManager) getSystemService(Context.DOWNLOAD_SERVICE);
                if (dm != null) dm.remove(updateDownloadId);
            } catch (Exception ignored) {}
            updateDownloadId = -1;
        }
        dismissUpdateProgressDialog();
        unregisterUpdateReceiver();
        Toast.makeText(this, "Pengunduhan pembaruan dibatalkan", Toast.LENGTH_SHORT).show();
    }

    private void dismissUpdateProgressDialog() {
        stopUpdateProgressPoll();
        try {
            if (updateProgressDialog != null && updateProgressDialog.isShowing()) updateProgressDialog.dismiss();
        } catch (Exception ignored) {}
        updateProgressDialog = null;
    }

    private void stopUpdateProgressPoll() {
        if (updateProgressRunnable != null) handler.removeCallbacks(updateProgressRunnable);
    }

    private void unregisterUpdateReceiver() {
        if (updateReceiverRegistered && updateDownloadReceiver != null) {
            try { unregisterReceiver(updateDownloadReceiver); } catch (Exception ignored) {}
        }
        updateReceiverRegistered = false;
        updateDownloadReceiver = null;
    }

    private static String downloadFailReasonText(int reason) {
        switch (reason) {
            case DownloadManager.ERROR_INSUFFICIENT_SPACE: return "ruang penyimpanan habis";
            case DownloadManager.ERROR_DEVICE_NOT_FOUND: return "penyimpanan tidak ditemukan";
            case DownloadManager.ERROR_CANNOT_RESUME: return "unduhan tidak bisa dilanjutkan";
            case DownloadManager.ERROR_FILE_ERROR: return "gagal menulis berkas";
            case DownloadManager.ERROR_HTTP_DATA_ERROR: return "data HTTP rusak";
            case DownloadManager.ERROR_TOO_MANY_REDIRECTS: return "terlalu banyak redirect";
            case DownloadManager.ERROR_UNHANDLED_HTTP_CODE: return "server merespons kode tak dikenal";
            default: return "kesalahan jaringan";
        }
    }
    private boolean handleUrl(Uri uri) {
        String u = uri.toString();
        if (u.startsWith("lumio://")) return true; // skema internal — UI klasik sudah tidak ada
        String host = uri.getHost();
        if (host == null || host.equals("127.0.0.1") || host.equals("localhost")) return false;
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, uri));
        } catch (Exception ignored) {
        }
        return true;
    }

    /** Menyalakan backend Python tertanam (dipindah dari MainActivity — activity ini kini pintu masuk aplikasi). */
    private void startEmbeddedBackend() {
        new Thread(() -> {
            try {
                Class<?> pyClass = Class.forName("com.chaquo.python.Python");
                if (!(Boolean) pyClass.getMethod("isStarted").invoke(null)) {
                    Class<?> androidPlatform = Class.forName("com.chaquo.python.android.AndroidPlatform");
                    Object platform = androidPlatform.getConstructor(android.content.Context.class).newInstance(getApplicationContext());
                    pyClass.getMethod("start", Class.forName("com.chaquo.python.Platform")).invoke(null, platform);
                }
                Object pyInstance = pyClass.getMethod("getInstance").invoke(null);
                Object launcherMod = pyClass.getMethod("getModule", String.class).invoke(pyInstance, "server_launcher");
                boolean isTv = false;
                try { isTv = TvFocusHelper.isTv(this); } catch (Exception ignored) {}
                String dataDir = getFilesDir().getAbsolutePath();
                // v2.28.1: versionName APK untuk aturan stale payload.
                // WAJIB dari PackageManager — di produksi tidak ada
                // version.py di disk (APK hanya berisi .pyc), jadi
                // launcher Python tidak bisa membaca versi bawaan sendiri.
                String appVersion = null;
                try { appVersion = getPackageManager().getPackageInfo(getPackageName(), 0).versionName; } catch (Exception ignored) {}
                launcherMod.getClass().getMethod("callAttr", String.class, Object[].class).invoke(launcherMod, "run_in_background", new Object[]{dataDir, isTv, appVersion});
            } catch (Throwable t) {
                Log.e("WebUiActivity", "Gagal start backend: " + t.getMessage(), t);
                handler.post(() -> splashText.setText("Gagal menyalakan mesin Lumio — sentuh untuk coba lagi"));
            }
        }).start();
    }

    private void waitForBackend(final int attempt) {
        new Thread(() -> {
            boolean ok = false;
            try {
                HttpURLConnection c = (HttpURLConnection) new URL(PING_URL).openConnection();
                c.setConnectTimeout(1200);
                c.setReadTimeout(1200);
                ok = c.getResponseCode() == 200;
                c.disconnect();
            } catch (Exception ignored) {
            }
            final boolean ready = ok;
            handler.post(() -> {
                if (isFinishing() || isDestroyed()) return;
                if (ready) {
                    if (!pageLoaded) {
                        try {
                            int vc = getPackageManager().getPackageInfo(getPackageName(), 0).versionCode;
                            SharedPreferences sp = getSharedPreferences("lumio_ui", MODE_PRIVATE);
                            if (sp.getInt("last_version_code", -1) != vc) {
                                web.clearCache(true);
                                sp.edit().putInt("last_version_code", vc).apply();
                            }
                        } catch (Exception ignored) {}
                        web.loadUrl(homeUrl);
                    }
                } else if (attempt < 120) {
                    handler.postDelayed(() -> waitForBackend(attempt + 1), 700);
                } else {
                    splashText.setText("Backend lama disiapkan — sentuh untuk coba lagi");
                }
            });
        }).start();
    }

    @Override
    public void onBackPressed() {
        if (customView != null) {
            web.getWebChromeClient().onHideCustomView();
            return;
        }
        if (web != null) {
            web.evaluateJavascript("(window.LUMIO_back ? window.LUMIO_back() : false)",
                    value -> {
                        if (!"true".equals(value)) finishAffinity();
                    });
        } else {
            finish();
        }
    }

    @Override
    protected void onDestroy() {
        // Unduhan DownloadManager tetap berjalan di sistem (notifikasi
        // menuntun ke installer); di sini hanya bersihkan UI + receiver
        // milik activity ini.
        stopUpdateProgressPoll();
        dismissUpdateProgressDialog();
        unregisterUpdateReceiver();
        if (embedResolver != null) {
            embedResolver.destroy();
            embedResolver = null;
        }
        if (web != null) {
            web.destroy();
            web = null;
        }
        super.onDestroy();
    }
}
