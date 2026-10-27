package id.tatap.app;

import android.app.Activity;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.os.Message;
import android.view.Gravity;
import android.view.ViewGroup;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

/**
 * Lumio — browser dalam aplikasi untuk tautan resmi (mis. bab Manhwa
 * resmi dari MangaDex yang hanya ditautkan ke situs penerbit).
 *
 * Sengaja TIDAK mengekspos JavascriptInterface apa pun ke halaman
 * pihak ketiga ini: situs penerbit adalah konten tak tepercaya, dan
 * WebView ini tidak boleh bisa memanggil fungsi native Lumio.
 *
 * Kejujuran produk: WebView ini TIDAK membypass login/paywall
 * penerbit (Tapas/Webnovel/dsb.) — pengguna tetap login/membayar di
 * situs penerbit, hanya saja tetap di dalam jendela Lumio, bukan
 * dilempar ke browser luar. Tampilannya tetap situs penerbit,
 * bukan reader Lumio.
 */
public class InAppBrowserActivity extends Activity {

    public static final String EXTRA_URL = "url";

    private WebView web;
    private TextView address;
    private ProgressBar progress;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        String rawUrl = getIntent() != null ? getIntent().getStringExtra(EXTRA_URL) : null;
        Uri uri = null;
        try {
            uri = rawUrl != null ? Uri.parse(rawUrl) : null;
        } catch (Exception ignored) {
        }
        // Hanya https + host jelas yang boleh dimuat. Skema lain
        // (intent://, file://, javascript:, dsb.) ditolak mentah-mentah.
        if (uri == null || !"https".equalsIgnoreCase(uri.getScheme()) || uri.getHost() == null) {
            Toast.makeText(this, "Tautan resmi tidak valid", Toast.LENGTH_SHORT).show();
            finish();
            return;
        }
        final String startUrl = uri.toString();

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Color.parseColor("#0B0B10"));

        LinearLayout bar = new LinearLayout(this);
        bar.setOrientation(LinearLayout.HORIZONTAL);
        bar.setGravity(Gravity.CENTER_VERTICAL);
        bar.setBackgroundColor(Color.parseColor("#14141E"));
        bar.setPadding(8, 8, 8, 8);

        Button back = new Button(this);
        back.setText("←");
        back.setOnClickListener(v -> {
            if (web != null && web.canGoBack()) web.goBack();
            else finish();
        });
        bar.addView(back);

        Button fwd = new Button(this);
        fwd.setText("→");
        fwd.setOnClickListener(v -> {
            if (web != null && web.canGoForward()) web.goForward();
        });
        bar.addView(fwd);

        address = new TextView(this);
        address.setTextColor(Color.parseColor("#9A9AB0"));
        address.setTextSize(13);
        address.setSingleLine(true);
        address.setEllipsize(android.text.TextUtils.TruncateAt.MIDDLE);
        LinearLayout.LayoutParams addrLp = new LinearLayout.LayoutParams(
                0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
        address.setLayoutParams(addrLp);
        address.setPadding(12, 0, 12, 0);
        bar.addView(address);

        Button close = new Button(this);
        close.setText("✕");
        close.setOnClickListener(v -> finish());
        bar.addView(close);

        root.addView(bar, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        progress = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        progress.setMax(100);
        root.addView(progress, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 8));

        web = new WebView(this);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        s.setSupportMultipleWindows(true);
        s.setJavaScriptCanOpenWindowsAutomatically(true);
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest req) {
                return !isHttpUrl(req.getUrl());
            }

            @SuppressWarnings("deprecation")
            @Override
            public boolean shouldOverrideUrlLoading(WebView v, String url) {
                return !isHttpUrl(Uri.parse(url));
            }

            @Override
            public void onPageFinished(WebView v, String url) {
                showAddress(url);
            }
        });
        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onProgressChanged(WebView v, int newProgress) {
                progress.setProgress(newProgress);
                progress.setVisibility(newProgress >= 100 ? ProgressBar.GONE : ProgressBar.VISIBLE);
            }

            @Override
            public boolean onCreateWindow(WebView view, boolean isDialog,
                                          boolean isUserGesture, Message resultMsg) {
                // Tautan target=_blank: muat di WebView yang sama, jangan
                // buka jendela baru (dan jangan lempar ke browser luar).
                WebView.HitTestResult hit = view.getHitTestResult();
                String url = hit != null ? hit.getExtra() : null;
                if (url != null && isHttpUrl(Uri.parse(url))) {
                    view.loadUrl(url);
                }
                return false;
            }
        });
        root.addView(web, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));

        setContentView(root);
        showAddress(startUrl);
        web.loadUrl(startUrl);
    }

    private static boolean isHttpUrl(Uri uri) {
        if (uri == null || uri.getHost() == null) return false;
        String scheme = uri.getScheme();
        return "https".equalsIgnoreCase(scheme) || "http".equalsIgnoreCase(scheme);
    }

    private void showAddress(String url) {
        if (address == null || url == null) return;
        Uri u = Uri.parse(url);
        String host = u.getHost() != null ? u.getHost() : url;
        String path = u.getPath() != null ? u.getPath() : "";
        String text = host + path;
        if (text.length() > 64) text = text.substring(0, 64) + "…";
        address.setText(text);
    }

    @Override
    public void onBackPressed() {
        if (web != null && web.canGoBack()) {
            web.goBack();
            return;
        }
        // Tidak ada riwayat: kembali ke reader (activity di bawah),
        // bukan keluar aplikasi.
        finish();
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
