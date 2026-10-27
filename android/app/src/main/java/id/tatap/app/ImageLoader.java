package id.tatap.app;

import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.os.Handler;
import android.os.Looper;
import android.util.LruCache;
import android.widget.ImageView;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ThreadFactory;
import java.util.concurrent.atomic.AtomicInteger;

public class ImageLoader {
    // Batasi 2 thread background dengan THREAD_PRIORITY_BACKGROUND agar tidak merebut CPU dari UI thread dan ExoPlayer
    private static final ExecutorService executor = Executors.newFixedThreadPool(2, new ThreadFactory() {
        private final AtomicInteger count = new AtomicInteger(1);
        @Override
        public Thread newThread(Runnable r) {
            Thread t = new Thread(() -> {
                android.os.Process.setThreadPriority(android.os.Process.THREAD_PRIORITY_BACKGROUND);
                r.run();
            }, "LumioImageLoader-" + count.getAndIncrement());
            return t;
        }
    });
    private static final Handler mainHandler = new Handler(Looper.getMainLooper());
    private static final LruCache<String, Bitmap> memoryCache;

    static {
        // Alokasikan maksimal 1/12 dari memori heap (sangat ramah STB/TV RAM 1GB-2GB)
        final int maxMemory = (int) (Runtime.getRuntime().maxMemory() / 1024);
        final int cacheSize = Math.max(4096, maxMemory / 12);
        memoryCache = new LruCache<String, Bitmap>(cacheSize) {
            @Override
            protected int sizeOf(String key, Bitmap bitmap) {
                return bitmap.getByteCount() / 1024;
            }
        };
    }

    public static void trimMemory() {
        if (memoryCache != null) {
            memoryCache.evictAll();
        }
    }

    public static void load(final String urlString, final ImageView imageView) {
        if (urlString == null || urlString.isEmpty()) {
            imageView.setImageDrawable(null);
            return;
        }

        Bitmap cached = memoryCache.get(urlString);
        if (cached != null) {
            imageView.setAlpha(1.0f);
            imageView.setImageBitmap(cached);
            return;
        }

        imageView.setTag(urlString);
        imageView.setAlpha(1.0f);
        imageView.setImageDrawable(new android.graphics.drawable.ColorDrawable(0xFF151822));
        final boolean isTv = TvFocusHelper.isTv(imageView.getContext());
        executor.execute(() -> {
            try {
                if (!urlString.equals(imageView.getTag())) {
                    return;
                }
                URL url = new URL(urlString);
                HttpURLConnection conn = (HttpURLConnection) url.openConnection();
                conn.setConnectTimeout(7000);
                conn.setReadTimeout(7000);
                conn.setRequestProperty("User-Agent", "Mozilla/5.0");
                conn.connect();

                InputStream input = conn.getInputStream();
                byte[] data = readFully(input);
                conn.disconnect();

                if (!urlString.equals(imageView.getTag())) {
                    return;
                }

                if (data != null && data.length > 0) {
                    BitmapFactory.Options opts = new BitmapFactory.Options();
                    opts.inJustDecodeBounds = true;
                    BitmapFactory.decodeByteArray(data, 0, data.length, opts);

                    // Downsample target: TV 180x260 (hemat 63% RAM), Mobile 300x420
                    int reqW = isTv ? 180 : 300;
                    int reqH = isTv ? 260 : 420;
                    opts.inSampleSize = calculateInSampleSize(opts, reqW, reqH);
                    opts.inJustDecodeBounds = false;
                    // Gunakan format 16-bit RGB_565 (2 byte/pixel) alih-alih ARGB_8888 (4 byte/pixel)
                    opts.inPreferredConfig = Bitmap.Config.RGB_565;

                    Bitmap bitmap = BitmapFactory.decodeByteArray(data, 0, data.length, opts);
                    if (bitmap != null) {
                        memoryCache.put(urlString, bitmap);
                        mainHandler.post(() -> {
                            if (urlString.equals(imageView.getTag())) {
                                if (isTv) {
                                    // Di Android TV: direct bitmap assignment (zero property animation overhead di UI thread)
                                    imageView.setAlpha(1.0f);
                                    imageView.setImageBitmap(bitmap);
                                } else {
                                    imageView.animate().cancel();
                                    imageView.setAlpha(0f);
                                    imageView.setImageBitmap(bitmap);
                                    imageView.animate().alpha(1.0f).setDuration(160).start();
                                }
                            }
                        });
                    }
                }
            } catch (Exception ignored) {}
        });
    }

    private static int calculateInSampleSize(BitmapFactory.Options options, int reqWidth, int reqHeight) {
        final int height = options.outHeight;
        final int width = options.outWidth;
        int inSampleSize = 1;

        if (height > reqHeight || width > reqWidth) {
            final int halfHeight = height / 2;
            final int halfWidth = width / 2;

            while ((halfHeight / inSampleSize) >= reqHeight && (halfWidth / inSampleSize) >= reqWidth) {
                inSampleSize *= 2;
            }
        }
        return Math.max(1, inSampleSize);
    }

    private static byte[] readFully(InputStream is) {
        try {
            ByteArrayOutputStream baos = new ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            int n;
            while ((n = is.read(buf)) != -1) {
                baos.write(buf, 0, n);
            }
            return baos.toByteArray();
        } catch (Exception e) {
            return null;
        }
    }
}
