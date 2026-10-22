package id.tatap.app;

import android.app.Activity;
import android.content.Intent;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
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

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
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
    private FrameLayout fullContainer;
    private View customView;
    private WebChromeClient.CustomViewCallback customCallback;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private boolean pageLoaded = false;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

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
                    splashText.setVisibility(View.GONE);
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
        splashText.setOnClickListener(v -> {
            splashText.setText("Menyiapkan Lumio…");
            waitForBackend(0);
        });

        setContentView(root);
        waitForBackend(0);
    }

    /** Jembatan JS -> native: unduh & pasang APK update (dari banner UI web). */
    public class UpdateBridge {
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
                Toast.makeText(WebUiActivity.this, "Mengunduh update…", Toast.LENGTH_SHORT).show();
                new Thread(() -> downloadAndInstall(apkUrl)).start();
            });
        }
    }

    private void downloadAndInstall(String apkUrl) {
        File out = null;
        try {
            File dir = getExternalCacheDir();
            if (dir == null) dir = getCacheDir();
            out = new File(dir, "lumio-update.apk");
            HttpURLConnection c = (HttpURLConnection) new URL(apkUrl).openConnection();
            c.setConnectTimeout(15000);
            c.setReadTimeout(30000);
            c.setRequestProperty("User-Agent", "Lumio-Android");
            c.connect();
            if (c.getResponseCode() != 200) throw new Exception("HTTP " + c.getResponseCode());
            InputStream in = c.getInputStream();
            FileOutputStream fos = new FileOutputStream(out);
            byte[] buf = new byte[65536];
            int n;
            while ((n = in.read(buf)) != -1) fos.write(buf, 0, n);
            fos.flush();
            fos.close();
            in.close();
            c.disconnect();
            final File apkFile = out;
            handler.post(() -> {
                try {
                    Uri uri = FileProvider.getUriForFile(WebUiActivity.this,
                            getPackageName() + ".fileprovider", apkFile);
                    Intent it = new Intent(Intent.ACTION_VIEW);
                    it.setDataAndType(uri, "application/vnd.android.package-archive");
                    it.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_ACTIVITY_NEW_TASK);
                    startActivity(it);
                } catch (Exception e) {
                    Toast.makeText(WebUiActivity.this,
                            "Gagal membuka installer: " + e.getMessage(), Toast.LENGTH_LONG).show();
                }
            });
        } catch (Exception e) {
            handler.post(() -> Toast.makeText(WebUiActivity.this,
                    "Unduh update gagal: " + e.getMessage(), Toast.LENGTH_LONG).show());
        }
    }

    private boolean handleUrl(Uri uri) {
        String u = uri.toString();
        if (u.startsWith("lumio://classic")) {
            finish(); // kembali ke UI bawaan (MainActivity) untuk sesi ini
            return true;
        }
        String host = uri.getHost();
        if (host == null || host.equals("127.0.0.1") || host.equals("localhost")) return false;
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, uri));
        } catch (Exception ignored) {
        }
        return true;
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
                    if (!pageLoaded) web.loadUrl(HOME_URL);
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
                        if (!"true".equals(value)) finish();
                    });
        } else {
            finish();
        }
    }

    @Override
    protected void onDestroy() {
        if (web != null) {
            web.destroy();
            web = null;
        }
        super.onDestroy();
    }
}
