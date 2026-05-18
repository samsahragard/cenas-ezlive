package com.cenaskitchen.gps;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.location.Location;
import android.os.Build;
import android.os.IBinder;
import android.os.Looper;
import android.util.Log;

import androidx.core.app.NotificationCompat;
import androidx.core.content.ContextCompat;

import com.google.android.gms.location.FusedLocationProviderClient;
import com.google.android.gms.location.LocationCallback;
import com.google.android.gms.location.LocationRequest;
import com.google.android.gms.location.LocationResult;
import com.google.android.gms.location.LocationServices;
import com.google.android.gms.location.Priority;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;

/**
 * GpsTrackerService — runs as a foreground service while a driver shift is
 * active. Captures GPS via FusedLocationProvider, POSTs each fix directly
 * to the configured /driver/track URL on a background executor. No JS
 * round-trip in the data path, so it survives WebView suspend.
 */
public class GpsTrackerService extends Service {

    public static final String ACTION_START = "com.cenaskitchen.gps.START";
    public static final String ACTION_STOP = "com.cenaskitchen.gps.STOP";

    public static final String EXTRA_URL = "url";
    public static final String EXTRA_INTERVAL_MS = "interval_ms";
    public static final String EXTRA_DISTANCE_M = "distance_m";
    public static final String EXTRA_COOKIE = "cookie";

    private static final String TAG = "CenasGpsService";
    private static final String CHANNEL_ID = "cenas_gps_shift";
    private static final int NOTIFICATION_ID = 91100;

    private FusedLocationProviderClient fused;
    private LocationCallback callback;
    private ScheduledExecutorService httpPool;
    private String url;
    private String cookie;

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent == null) {
            return START_NOT_STICKY;
        }
        String action = intent.getAction();
        if (ACTION_STOP.equals(action)) {
            stopTracking();
            stopSelf();
            return START_NOT_STICKY;
        }

        if (ACTION_START.equals(action)) {
            this.url = intent.getStringExtra(EXTRA_URL);
            this.cookie = intent.getStringExtra(EXTRA_COOKIE);
            int intervalMs = intent.getIntExtra(EXTRA_INTERVAL_MS, 5000);
            int distanceM = intent.getIntExtra(EXTRA_DISTANCE_M, 10);

            startInForeground();
            startTracking(intervalMs, distanceM);
        }
        return START_STICKY;
    }

    private void startInForeground() {
        ensureChannel();

        Intent launch = getPackageManager().getLaunchIntentForPackage(getPackageName());
        PendingIntent pi = null;
        if (launch != null) {
            int piFlags = PendingIntent.FLAG_UPDATE_CURRENT;
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                piFlags |= PendingIntent.FLAG_IMMUTABLE;
            }
            pi = PendingIntent.getActivity(this, 0, launch, piFlags);
        }

        Notification n = new NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.ic_menu_mylocation)
            .setContentTitle("On Shift")
            .setContentText("Cenas Kitchen driver tracking active")
            .setOngoing(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setContentIntent(pi)
            .build();

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            startForeground(NOTIFICATION_ID, n,
                android.content.pm.ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION);
        } else {
            startForeground(NOTIFICATION_ID, n);
        }
    }

    private void ensureChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return;
        NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        if (nm == null) return;
        if (nm.getNotificationChannel(CHANNEL_ID) != null) return;
        NotificationChannel ch = new NotificationChannel(
            CHANNEL_ID,
            "Cenas Kitchen shift tracking",
            NotificationManager.IMPORTANCE_LOW
        );
        ch.setDescription("Shows while a driver shift is active.");
        nm.createNotificationChannel(ch);
    }

    @SuppressWarnings("MissingPermission")
    private void startTracking(int intervalMs, int distanceM) {
        if (ContextCompat.checkSelfPermission(this, android.Manifest.permission.ACCESS_FINE_LOCATION)
            != PackageManager.PERMISSION_GRANTED) {
            Log.w(TAG, "ACCESS_FINE_LOCATION not granted - cannot start tracking");
            return;
        }
        fused = LocationServices.getFusedLocationProviderClient(this);
        httpPool = Executors.newSingleThreadScheduledExecutor();

        LocationRequest req = new LocationRequest.Builder(Priority.PRIORITY_HIGH_ACCURACY, intervalMs)
            .setMinUpdateIntervalMillis(Math.max(2000, intervalMs / 2))
            .setMinUpdateDistanceMeters(distanceM)
            .setWaitForAccurateLocation(false)
            .build();

        callback = new LocationCallback() {
            @Override
            public void onLocationResult(LocationResult result) {
                if (result == null) return;
                for (Location loc : result.getLocations()) {
                    submitFix(loc);
                }
            }
        };

        fused.requestLocationUpdates(req, callback, Looper.getMainLooper());
        Log.i(TAG, "tracking started: interval=" + intervalMs + "ms distance=" + distanceM + "m");
    }

    private void submitFix(Location loc) {
        if (httpPool == null || url == null) return;
        final double lat = loc.getLatitude();
        final double lng = loc.getLongitude();
        final float acc = loc.getAccuracy();
        final float speed = loc.hasSpeed() ? loc.getSpeed() : Float.NaN;
        final float bearing = loc.hasBearing() ? loc.getBearing() : Float.NaN;
        httpPool.submit(() -> postFix(lat, lng, acc, speed, bearing));
    }

    private void postFix(double lat, double lng, float acc, float speed, float bearing) {
        StringBuilder body = new StringBuilder(256);
        body.append("{\"lat\":").append(lat)
            .append(",\"lng\":").append(lng)
            .append(",\"accuracy_m\":").append(acc);
        if (!Float.isNaN(speed)) body.append(",\"speed_mps\":").append(speed);
        if (!Float.isNaN(bearing)) body.append(",\"heading_deg\":").append(bearing);
        body.append(",\"is_native\":true,\"source\":\"cenas-gps-native\"}");

        HttpURLConnection conn = null;
        try {
            URL u = new URL(url);
            conn = (HttpURLConnection) u.openConnection();
            conn.setRequestMethod("POST");
            conn.setConnectTimeout(8000);
            conn.setReadTimeout(8000);
            conn.setDoOutput(true);
            conn.setRequestProperty("Content-Type", "application/json");
            conn.setRequestProperty("User-Agent", "CenasGpsNative/0.1");
            if (cookie != null && !cookie.isEmpty()) {
                conn.setRequestProperty("Cookie", cookie);
            }
            try (OutputStream os = conn.getOutputStream()) {
                os.write(body.toString().getBytes(StandardCharsets.UTF_8));
            }
            int code = conn.getResponseCode();
            if (code >= 400) {
                Log.w(TAG, "fix POST HTTP " + code);
            }
        } catch (Exception e) {
            Log.w(TAG, "fix POST failed: " + e.getMessage());
        } finally {
            if (conn != null) conn.disconnect();
        }
    }

    private void stopTracking() {
        if (fused != null && callback != null) {
            try { fused.removeLocationUpdates(callback); } catch (Exception ignored) {}
        }
        if (httpPool != null) {
            httpPool.shutdownNow();
            httpPool = null;
        }
        callback = null;
        fused = null;
        Log.i(TAG, "tracking stopped");
    }

    @Override
    public void onDestroy() {
        stopTracking();
        super.onDestroy();
    }
}
