package org.coachpo.cruxupdater;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;

import java.io.File;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;

/** After the boot that ends an update this app started, report the crux_ota_result outcome. */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        PendingResult pending = goAsync();
        new Thread(() -> {
            try {
                report(context);
            } finally {
                pending.finish();
            }
        }).start();
    }

    private static void report(Context context) {
        SharedPreferences prefs = context.getSharedPreferences("crux", Context.MODE_PRIVATE);
        String awaiting = prefs.getString("awaiting", "");
        if (awaiting.isEmpty()) return;
        File file = new File(Channel.ROOT, "last-result");
        long boot = System.currentTimeMillis() - android.os.SystemClock.elapsedRealtime();
        // crux_ota_result runs at sys.boot_completed, next to this broadcast.
        for (int i = 0; i < 30 && (!file.exists() || file.lastModified() < boot); i++) {
            try {
                Thread.sleep(1000);
            } catch (InterruptedException e) {
                return;
            }
        }
        if (!file.exists() || file.lastModified() < boot) return;
        String text;
        try {
            text = new String(Files.readAllBytes(file.toPath()), StandardCharsets.UTF_8);
        } catch (Exception e) {
            return;
        }
        String txn = field(text, "txn"), stage = field(text, "stage");
        // Only U-Boot's final stages end the update; anything else is still in progress.
        if (!awaiting.equals(txn) || !("done".equals(stage) || "failed".equals(stage))) return;
        prefs.edit().remove("awaiting").apply();
        String message = field(text, "message");
        boolean installed = "done".equals(stage) && "staged".equals(field(text, "status"));
        String title = installed ? "Crux update installed" : "Crux update not installed";
        String body = installed ? "Now running build " + field(text, "running_build")
                : "failed".equals(stage) ? "U-Boot stopped while installing the new system."
                : message.isEmpty() ? "Stage: " + stage : message;
        NotificationManager manager = context.getSystemService(NotificationManager.class);
        manager.createNotificationChannel(new NotificationChannel("crux_ota", "Crux system update",
                NotificationManager.IMPORTANCE_DEFAULT));
        PendingIntent open = PendingIntent.getActivity(context, 0, new Intent(context, UpdateActivity.class),
                PendingIntent.FLAG_IMMUTABLE);
        manager.notify(1, new Notification.Builder(context, "crux_ota")
                .setSmallIcon(android.R.drawable.stat_sys_download_done)
                .setContentTitle(title)
                .setContentText(body)
                .setContentIntent(open)
                .setAutoCancel(true)
                .build());
    }

    private static String field(String text, String key) {
        for (String line : text.split("\n")) {
            if (line.startsWith(key + "=")) return line.substring(key.length() + 1);
        }
        return "";
    }
}
