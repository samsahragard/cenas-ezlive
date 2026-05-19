package com.cenaskitchen.gps;

import android.Manifest;
import android.app.ActivityManager;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.os.Build;
import android.os.PowerManager;
import android.provider.Settings;
import android.webkit.CookieManager;

import com.getcapacitor.JSObject;
import com.getcapacitor.PermissionState;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.getcapacitor.annotation.Permission;
import com.getcapacitor.annotation.PermissionCallback;

/**
 * CenasGpsNative — native Capacitor plugin that owns a foreground service
 * collecting GPS fixes and POSTing them directly via OkHttp on a background
 * thread. No JS callback in the data path. Survives WebView suspend, screen
 * lock, and app-switch — the foreground service notification keeps the
 * process alive per Android foreground-service contract.
 *
 * JS API:
 *   await CenasGpsNative.start({ url, interval_ms, distance_m });
 *   await CenasGpsNative.stop();
 *   await CenasGpsNative.checkPermissions();
 *   await CenasGpsNative.requestPermissions();
 *   await CenasGpsNative.openSettings();
 *   await CenasGpsNative.isRunning();
 *
 * Auth: the service grabs cookies for the target URL from Android's
 * WebView CookieManager. The session cookie planted by /keypad-login
 * propagates automatically — no token issuance needed.
 */
@CapacitorPlugin(
    name = "CenasGpsNative",
    permissions = {
        @Permission(
            alias = "location",
            strings = {
                Manifest.permission.ACCESS_FINE_LOCATION,
                Manifest.permission.ACCESS_COARSE_LOCATION
            }
        ),
        @Permission(
            alias = "backgroundLocation",
            strings = { Manifest.permission.ACCESS_BACKGROUND_LOCATION }
        ),
        @Permission(
            alias = "notifications",
            strings = { Manifest.permission.POST_NOTIFICATIONS }
        )
    }
)
public class CenasGpsPlugin extends Plugin {

    private static final String TAG = "CenasGpsPlugin";

    @PluginMethod
    public void start(PluginCall call) {
        String url = call.getString("url");
        if (url == null || url.isEmpty()) {
            call.reject("url is required");
            return;
        }
        int intervalMs = call.getInt("interval_ms", 5000);
        int distanceM = call.getInt("distance_m", 10);

        // Read cookies for the target URL from the WebView's cookie store
        // so the native HTTP client authenticates as the logged-in driver.
        String cookie = CookieManager.getInstance().getCookie(url);

        Context ctx = getContext();
        Intent svc = new Intent(ctx, GpsTrackerService.class);
        svc.setAction(GpsTrackerService.ACTION_START);
        svc.putExtra(GpsTrackerService.EXTRA_URL, url);
        svc.putExtra(GpsTrackerService.EXTRA_INTERVAL_MS, intervalMs);
        svc.putExtra(GpsTrackerService.EXTRA_DISTANCE_M, distanceM);
        svc.putExtra(GpsTrackerService.EXTRA_COOKIE, cookie);

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            ctx.startForegroundService(svc);
        } else {
            ctx.startService(svc);
        }

        JSObject ret = new JSObject();
        ret.put("started", true);
        ret.put("has_cookie", cookie != null && !cookie.isEmpty());
        call.resolve(ret);
    }

    @PluginMethod
    public void stop(PluginCall call) {
        Context ctx = getContext();
        Intent svc = new Intent(ctx, GpsTrackerService.class);
        svc.setAction(GpsTrackerService.ACTION_STOP);
        ctx.startService(svc);

        JSObject ret = new JSObject();
        ret.put("stopped", true);
        call.resolve(ret);
    }

    @PluginMethod
    public void isRunning(PluginCall call) {
        boolean running = false;
        ActivityManager am = (ActivityManager) getContext().getSystemService(Context.ACTIVITY_SERVICE);
        if (am != null) {
            for (ActivityManager.RunningServiceInfo svc : am.getRunningServices(Integer.MAX_VALUE)) {
                if (GpsTrackerService.class.getName().equals(svc.service.getClassName())) {
                    running = true;
                    break;
                }
            }
        }
        JSObject ret = new JSObject();
        ret.put("running", running);
        call.resolve(ret);
    }

    @PluginMethod
    public void checkPermissions(PluginCall call) {
        JSObject ret = new JSObject();
        ret.put("location", getPermissionState("location").toString());
        ret.put("backgroundLocation", getPermissionState("backgroundLocation").toString());
        ret.put("notifications", getPermissionState("notifications").toString());
        call.resolve(ret);
    }

    @PluginMethod
    public void requestPermissions(PluginCall call) {
        // Stage 1: foreground + notifications. Background must be requested
        // separately AFTER foreground is granted (Android 10+ requirement).
        String[] aliases;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            aliases = new String[]{"location", "notifications"};
        } else {
            aliases = new String[]{"location"};
        }
        requestPermissionForAliases(aliases, call, "foregroundGranted");
    }

    @PermissionCallback
    private void foregroundGranted(PluginCall call) {
        PermissionState locationState = getPermissionState("location");
        if (locationState != PermissionState.GRANTED) {
            JSObject ret = new JSObject();
            ret.put("location", locationState.toString());
            ret.put("backgroundLocation", "denied");
            call.resolve(ret);
            return;
        }
        // Stage 2: background — only valid on Android 10+. On older the
        // foreground grant covers background automatically.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            requestPermissionForAlias("backgroundLocation", call, "backgroundGranted");
        } else {
            JSObject ret = new JSObject();
            ret.put("location", "granted");
            ret.put("backgroundLocation", "granted");
            call.resolve(ret);
        }
    }

    @PermissionCallback
    private void backgroundGranted(PluginCall call) {
        JSObject ret = new JSObject();
        ret.put("location", getPermissionState("location").toString());
        ret.put("backgroundLocation", getPermissionState("backgroundLocation").toString());
        ret.put("notifications", getPermissionState("notifications").toString());
        call.resolve(ret);
    }

    @PluginMethod
    public void openSettings(PluginCall call) {
        Context ctx = getContext();
        Intent intent = new Intent(android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS);
        intent.setData(android.net.Uri.parse("package:" + ctx.getPackageName()));
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        ctx.startActivity(intent);
        call.resolve();
    }

    /**
     * Returns whether the app is currently whitelisted from battery
     * optimization (Doze / App Standby). Pure read — does not prompt.
     */
    @PluginMethod
    public void checkBatteryOptimizations(PluginCall call) {
        JSObject ret = new JSObject();
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.M) {
            // Pre-Doze (API < 23) — no battery optimization concept.
            ret.put("granted", true);
            ret.put("supported", false);
            call.resolve(ret);
            return;
        }
        Context ctx = getContext();
        PowerManager pm = (PowerManager) ctx.getSystemService(Context.POWER_SERVICE);
        boolean ignoring = pm != null && pm.isIgnoringBatteryOptimizations(ctx.getPackageName());
        ret.put("granted", ignoring);
        ret.put("supported", true);
        call.resolve(ret);
    }

    /**
     * Pops the system dialog asking the user to whitelist this app from
     * battery optimization (so the GPS foreground service is not killed
     * when the screen is off). If already granted, resolves immediately
     * with granted=true and prompted=false. Driver shift-start flow calls
     * this once on first start after install.
     */
    @PluginMethod
    public void requestIgnoreBatteryOptimizations(PluginCall call) {
        JSObject ret = new JSObject();
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.M) {
            ret.put("granted", true);
            ret.put("prompted", false);
            ret.put("supported", false);
            call.resolve(ret);
            return;
        }
        Context ctx = getContext();
        PowerManager pm = (PowerManager) ctx.getSystemService(Context.POWER_SERVICE);
        String pkg = ctx.getPackageName();
        if (pm != null && pm.isIgnoringBatteryOptimizations(pkg)) {
            ret.put("granted", true);
            ret.put("prompted", false);
            ret.put("supported", true);
            call.resolve(ret);
            return;
        }
        try {
            Intent intent = new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS);
            intent.setData(Uri.parse("package:" + pkg));
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            ctx.startActivity(intent);
            ret.put("granted", false);
            ret.put("prompted", true);
            ret.put("supported", true);
            call.resolve(ret);
        } catch (Exception e) {
            ret.put("granted", false);
            ret.put("prompted", false);
            ret.put("supported", true);
            ret.put("error", e.getMessage());
            call.resolve(ret);
        }
    }
}
