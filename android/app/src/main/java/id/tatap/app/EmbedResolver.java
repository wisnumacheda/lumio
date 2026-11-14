package id.tatap.app;

import android.content.Context;
import android.net.Uri;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.webkit.CookieManager;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.Locale;
import java.util.Map;

/**
 * Resolver embed via WebView tersembunyi (APK 2.30.0).
 *
 * Masalah: di TV, D-pad tidak masuk ke iframe embed, sehingga play/pause/
 * seek di dalam player penyedia tidak terjangkau remote. Kelas ini memuat
 * halaman embed penyedia di WebView yang TIDAK PERNAH ditempel ke hierarki
 * view, mengintersepsi request manifest media (.m3u8) secara pasif lewat
 * WebViewClient.shouldInterceptRequest, lalu mengembalikan URL-nya ke UI
 * web agar diputar di player HLS Lumio sendiri (D-pad penuh, panel
 * kualitas, subtitle). Iframe embed tetap menjadi fallback terakhir.
 *
 * Pola ini diadaptasi dari OpenStream WebEmbedResolver (MIT License,
 * Copyright (c) 2026 Ivor, https://github.com/Ivorisnoob/OpenStream) —
 * desainnya (WebView detached, intersepsi pasif, timeout + settle,
 * penahanan navigasi) dipakai sebagai preseden; implementasi di bawah ini
 * ditulis ulang dalam Java untuk arsitektur Lumio dan TIDAK menyalin kode
 * OpenStream. DIVERGENSI KEAMANAN YANG DISENGAJA: OpenStream memakai
 * addJavascriptInterface + injeksi skrip ke halaman penyedia; Lumio TIDAK
 * melakukannya — hidden WebView di sini tidak mendapat JavascriptInterface
 * apa pun dan tidak ada evaluateJavascript ke halaman pihak ketiga.
 * Komunikasi dari halaman penyedia HANYA lewat intersepsi request.
 *
 * Batas keras: tanpa download segmen video (hanya URL manifest yang
 * diteruskan; validasi 8KB tetap di jalur proxy backend), tanpa
 * circumvention DRM, tanpa ekstraksi kredensial/key. URL lengkap TIDAK
 * PERNAH ditulis ke log (bisa membawa token) — hanya host yang dicatat.
 */
public class EmbedResolver {

    private static final String TAG = "EmbedResolver";

    /** Batas keras satu resolve (lebih pendek dari OpenStream 17 dtk karena
     * Lumio mencoba penyedia satu per satu; anggaran total dipegang JS). */
    private static final long TIMEOUT_MS = 15000;
    /** Jeda settle: selesai 1,5 dtk setelah stream terakhir terlihat, agar
     * subtitle yang datang belakangan ikut terkumpul. */
    private static final long SETTLE_MS = 1500;

    /** Penanda host iklan/telemetri yang tidak boleh dianggap stream
     * (diadaptasi dari daftar OpenStream). */
    private static final String[] BLOCKED_MARKERS = {
            "googleads", "doubleclick", "telemetry", "/ads/", "vast"
    };

    /** Hasil resolve, diteruskan ke pemanggil apa adanya sebagai payload
     * JSON: {ok, url?, subtitles?[{url, lang, label}], error?}. */
    public interface Callback {
        void onEmbedResolved(String requestId, JSONObject payload);
    }

    private final Context appContext;
    private final Handler handler;
    private final String userAgent;

    private WebView hidden;
    private Callback callback;
    private String activeRequestId;
    private String originalHost;
    private String currentHost;
    private String streamUrl;
    private final LinkedHashMap<String, JSONObject> subtitles = new LinkedHashMap<>();
    private boolean finished;
    private Runnable timeoutRunnable;
    private Runnable settleRunnable;

    public EmbedResolver(Context context, Handler handler, String userAgent) {
        this.appContext = context.getApplicationContext();
        this.handler = handler;
        this.userAgent = userAgent;
    }

    /** Mulai satu resolve. Harus dipanggil dari UI thread (jembatan JS
     * sudah mem-post ke handler); bila tidak, panggilan di-post ulang.
     * Hanya satu resolve aktif per waktu — panggilan kedua langsung
     * dijawab error "busy" lewat callback. */
    public void resolve(final String embedUrl, final String requestId, final Callback cb) {
        if (Looper.myLooper() != Looper.getMainLooper()) {
            handler.post(() -> resolve(embedUrl, requestId, cb));
            return;
        }
        if (requestId == null || requestId.isEmpty() || cb == null) {
            Log.w(TAG, "resolve ditolak: requestId/callback kosong");
            return;
        }
        if (activeRequestId != null) {
            postError(requestId, cb, "busy");
            return;
        }
        Uri uri = null;
        try { uri = Uri.parse(embedUrl); } catch (Exception ignored) {}
        if (uri == null || !"https".equalsIgnoreCase(uri.getScheme())
                || uri.getHost() == null || uri.getHost().isEmpty()) {
            postError(requestId, cb, "URL embed tidak valid");
            return;
        }
        activeRequestId = requestId;
        callback = cb;
        originalHost = uri.getHost().toLowerCase(Locale.US);
        currentHost = originalHost;
        streamUrl = null;
        subtitles.clear();
        finished = false;

        CookieManager cm = CookieManager.getInstance();
        try {
            cm.setAcceptCookie(true);
        } catch (Exception ignored) {}

        WebView wv = new WebView(appContext);
        try { cm.setAcceptThirdPartyCookies(wv, true); } catch (Exception ignored) {}
        WebSettings s = wv.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        s.setCacheMode(WebSettings.LOAD_NO_CACHE);
        if (userAgent != null && !userAgent.isEmpty()) {
            s.setUserAgentString(userAgent);
        }
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        s.setSupportMultipleWindows(false);
        s.setJavaScriptCanOpenWindowsAutomatically(false);
        // TIDAK ADA addJavascriptInterface di WebView ini (lihat header).
        wv.setWebViewClient(new WebViewClient() {
            @Override
            public android.webkit.WebResourceResponse shouldInterceptRequest(
                    WebView view, WebResourceRequest request) {
                // Berjalan di luar UI thread: hanya baca URL, mutasi state
                // di-post ke handler. Selalu pasif — tidak pernah memblokir
                // atau menulis ulang respons.
                try {
                    String url = request.getUrl() != null ? request.getUrl().toString() : null;
                    if (url != null) {
                        if (isMediaUrl(url)) {
                            final String found = url;
                            handler.post(() -> onStreamFound(found));
                        } else if (isSubtitleUrl(url)) {
                            final String found = url;
                            handler.post(() -> onSubtitleFound(found));
                        }
                    }
                } catch (Exception ignored) {}
                return super.shouldInterceptRequest(view, request);
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                // Subframe + redirect server selalu diizinkan (redirect
                // antar-host adalah perilaku normal: vidfast.pro -> .vc,
                // vidsrc.me -> .sh, apex viduki -> www). Navigasi main-frame
                // lain diblokir bila keluar root domain halaman embed
                // (menahan navigasi iklan di dalam hidden view).
                try {
                    if (!request.isForMainFrame()) return false;
                    if (request.isRedirect()) return false;
                    Uri u = request.getUrl();
                    if (u == null) return true;
                    String host = u.getHost() != null ? u.getHost().toLowerCase(Locale.US) : "";
                    if (sharesRoot(host, originalHost) || sharesRoot(host, currentHost)) {
                        currentHost = host;
                        return false;
                    }
                    return true;
                } catch (Exception ignored) {
                    return true;
                }
            }

            @SuppressWarnings("deprecation")
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, String url) {
                // Hanya dipakai API < 24; minSdk Lumio 24 sehingga jalur
                // ini praktis mati — izinkan agar tidak memecahkan apa pun.
                return false;
            }

            @Override
            public void onPageStarted(WebView view, String url, android.graphics.Bitmap favicon) {
                try {
                    Uri u = Uri.parse(url);
                    if (u.getHost() != null) currentHost = u.getHost().toLowerCase(Locale.US);
                } catch (Exception ignored) {}
                super.onPageStarted(view, url, favicon);
            }
        });
        wv.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onCreateWindow(WebView view, boolean isDialog,
                                          boolean isUserGesture, android.os.Message resultMsg) {
                // Tolak popup: window.open dari halaman penyedia tidak
                // boleh membuka apa pun.
                return false;
            }
        });
        hidden = wv;

        timeoutRunnable = () -> finishError("Waktu habis (15 detik) tanpa stream");
        handler.postDelayed(timeoutRunnable, TIMEOUT_MS);

        Map<String, String> headers = new HashMap<>();
        headers.put("Referer", originOf(uri));
        try {
            wv.loadUrl(embedUrl, headers);
        } catch (Exception e) {
            finishError("Gagal memuat halaman embed");
        }
    }

    /** Batalkan resolve berjalan (bila id cocok). Dipanggil saat player
     * ditutup / timeout sisi JS agar hidden WebView segera dihancurkan. */
    public void cancel(String requestId) {
        if (Looper.myLooper() != Looper.getMainLooper()) {
            handler.post(() -> cancel(requestId));
            return;
        }
        if (activeRequestId != null && activeRequestId.equals(requestId) && !finished) {
            finishError("dibatalkan");
        }
    }

    /** Hancurkan tanpa callback (activity sedang dihancurkan). */
    public void destroy() {
        if (Looper.myLooper() != Looper.getMainLooper()) {
            handler.post(this::destroy);
            return;
        }
        finished = true;
        activeRequestId = null;
        callback = null;
        cleanupHidden();
    }

    private void onStreamFound(String url) {
        if (finished || activeRequestId == null) return;
        if (!isValidNetworkUrl(url)) return;
        if (streamUrl == null) {
            streamUrl = url;
            try {
                Log.d(TAG, "stream ditemukan di host " + Uri.parse(url).getHost());
            } catch (Exception ignored) {}
        }
        // Tiap stream tambahan me-reset settle (kumpulkan subtitle dulu).
        if (settleRunnable != null) handler.removeCallbacks(settleRunnable);
        settleRunnable = this::finishOk;
        handler.postDelayed(settleRunnable, SETTLE_MS);
    }

    private void onSubtitleFound(String url) {
        if (finished || activeRequestId == null) return;
        if (!isValidNetworkUrl(url)) return;
        if (subtitles.containsKey(url)) return;
        try {
            JSONObject sub = new JSONObject();
            sub.put("url", url);
            String lang = guessLang(url);
            sub.put("lang", lang);
            sub.put("label", "id".equals(lang) ? "Indonesia" : "English");
            subtitles.put(url, sub);
        } catch (Exception ignored) {}
        // Subtitle saja TIDAK memicu selesai (seperti OpenStream).
    }

    private void finishOk() {
        if (finished || activeRequestId == null) return;
        if (streamUrl == null) {
            finishError("tidak menemukan stream");
            return;
        }
        finished = true;
        String id = activeRequestId;
        Callback cb = callback;
        try {
            JSONObject payload = new JSONObject();
            payload.put("ok", true);
            payload.put("url", streamUrl);
            JSONArray subs = new JSONArray();
            for (JSONObject sub : subtitles.values()) subs.put(sub);
            payload.put("subtitles", subs);
            if (cb != null) cb.onEmbedResolved(id, payload);
        } catch (Exception ignored) {}
        activeRequestId = null;
        callback = null;
        cleanupHidden();
    }

    private void finishError(String error) {
        if (finished && activeRequestId == null) return;
        finished = true;
        String id = activeRequestId;
        Callback cb = callback;
        activeRequestId = null;
        callback = null;
        if (id != null && cb != null) {
            postError(id, cb, error);
        }
        cleanupHidden();
    }

    private void postError(String requestId, Callback cb, String error) {
        try {
            JSONObject payload = new JSONObject();
            payload.put("ok", false);
            payload.put("error", error);
            cb.onEmbedResolved(requestId, payload);
        } catch (Exception ignored) {}
    }

    private void cleanupHidden() {
        if (timeoutRunnable != null) {
            handler.removeCallbacks(timeoutRunnable);
            timeoutRunnable = null;
        }
        if (settleRunnable != null) {
            handler.removeCallbacks(settleRunnable);
            settleRunnable = null;
        }
        if (hidden != null) {
            WebView wv = hidden;
            hidden = null;
            try { wv.stopLoading(); } catch (Exception ignored) {}
            try { wv.loadUrl("about:blank"); } catch (Exception ignored) {}
            try { wv.destroy(); } catch (Exception ignored) {}
        }
        try { CookieManager.getInstance().flush(); } catch (Exception ignored) {}
    }

    /** Pola media v1: HANYA .m3u8 (path sebelum '?', fragment dibuang).
     * .mp4/.mpd sengaja dikecualikan — frontend tidak punya player DASH dan
     * jalur bersama adalah HLS. Penanda iklan memblokir kandidat. */
    private static boolean isMediaUrl(String u) {
        String n = stripFragment(u.toLowerCase(Locale.US));
        for (String m : BLOCKED_MARKERS) {
            if (n.contains(m)) return false;
        }
        return pathBeforeQuery(n).endsWith(".m3u8");
    }

    private static boolean isSubtitleUrl(String u) {
        String n = stripFragment(u.toLowerCase(Locale.US));
        String path = pathBeforeQuery(n);
        return path.endsWith(".vtt") || path.endsWith(".srt") || n.contains("subtitle");
    }

    private static String stripFragment(String n) {
        int i = n.indexOf('#');
        return i >= 0 ? n.substring(0, i) : n;
    }

    private static String pathBeforeQuery(String n) {
        int i = n.indexOf('?');
        return i >= 0 ? n.substring(0, i) : n;
    }

    private static boolean isValidNetworkUrl(String url) {
        try {
            Uri u = Uri.parse(url);
            String scheme = u.getScheme();
            return ("https".equalsIgnoreCase(scheme) || "http".equalsIgnoreCase(scheme))
                    && u.getHost() != null && !u.getHost().isEmpty();
        } catch (Exception ignored) {
            return false;
        }
    }

    private static String originOf(Uri uri) {
        StringBuilder sb = new StringBuilder();
        sb.append(uri.getScheme()).append("://").append(uri.getHost());
        int port = uri.getPort();
        if (port != -1) sb.append(":").append(port);
        return sb.toString();
    }

    /** Tebakan bahasa dari URL subtitle; default "en" (mesin subtitle
     * backend memakai src default en bila tebakan meleset). */
    private static String guessLang(String url) {
        String n = url.toLowerCase(Locale.US);
        if (n.contains("indonesia") || n.contains(".id.") || n.contains("_id_")
                || n.contains("_id.") || n.contains("/id/") || n.contains("-id.")) {
            return "id";
        }
        return "en";
    }

    /** Root domain = dua label terakhir (aturan OpenStream; kasar untuk
     * suffix publik seperti co.uk — diterima, karena yang ditahan hanya
     * navigasi iklan). Host IP harus sama persis. */
    private static boolean sharesRoot(String a, String b) {
        if (a == null || b == null || a.isEmpty() || b.isEmpty()) return false;
        if (a.equals(b)) return true;
        if (isIpLiteral(a) || isIpLiteral(b)) return false;
        String ra = rootOf(a), rb = rootOf(b);
        return !ra.isEmpty() && ra.equals(rb);
    }

    private static String rootOf(String host) {
        String[] parts = host.split("\\.");
        if (parts.length >= 2) return parts[parts.length - 2] + "." + parts[parts.length - 1];
        return host;
    }

    private static boolean isIpLiteral(String host) {
        for (int i = 0; i < host.length(); i++) {
            char c = host.charAt(i);
            if (!(c >= '0' && c <= '9') && c != '.' && c != ':') return false;
        }
        return host.length() > 0;
    }
}
