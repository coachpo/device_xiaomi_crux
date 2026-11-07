package org.coachpo.cruxupdater;

import android.app.Activity;
import android.app.AlertDialog;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.os.StatFs;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.TextView;

import java.io.File;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.security.SecureRandom;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** Check, download, verify and hand the update to PE Recovery through U-Boot. */
public class UpdateActivity extends Activity {
    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private TextView status;
    private ProgressBar bar;
    private Button check;
    private Button download;
    private Button install;
    private Channel.Release release;
    private String txn;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        int pad = (int) (16 * getResources().getDisplayMetrics().density);
        layout.setPadding(pad, pad, pad, pad);
        status = new TextView(this);
        status.setTextIsSelectable(true);
        bar = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        bar.setMax(1000);
        check = new Button(this);
        check.setText("Check for update");
        download = new Button(this);
        download.setText("Download");
        install = new Button(this);
        install.setText("Install and restart");
        layout.addView(status);
        layout.addView(bar);
        layout.addView(check);
        layout.addView(download);
        layout.addView(install);
        ScrollView scroll = new ScrollView(this);
        scroll.addView(layout, new ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT));
        setContentView(scroll);
        check.setOnClickListener(v -> checkForUpdate());
        download.setOnClickListener(v -> downloadUpdate());
        install.setOnClickListener(v -> confirmInstall());
        download.setEnabled(false);
        install.setEnabled(false);
        show(header() + lastResult());
        checkForUpdate();
    }

    private String header() {
        return "Installed: " + Build.VERSION.INCREMENTAL + "\n" + Build.FINGERPRINT + "\n\n";
    }

    static String lastResult() {
        File file = new File(Channel.ROOT, "last-result");
        if (!file.exists()) return "";
        try {
            return "Last update:\n" + new String(Files.readAllBytes(file.toPath()), StandardCharsets.UTF_8) + "\n";
        } catch (Exception e) {
            return "";
        }
    }

    private void show(String text) {
        main.post(() -> status.setText(text));
    }

    private void buttons(boolean canCheck, boolean canDownload, boolean canInstall) {
        main.post(() -> {
            check.setEnabled(canCheck);
            download.setEnabled(canDownload);
            install.setEnabled(canInstall);
        });
    }

    private void checkForUpdate() {
        buttons(false, false, false);
        show(header() + "Checking the Crux Dev channel...");
        worker.execute(() -> {
            List<String> notes = new ArrayList<>();
            try {
                release = Channel.find(notes);
                StringBuilder text = new StringBuilder(header());
                if (release == null) {
                    text.append("No applicable update.\n");
                } else {
                    text.append("Update: build ").append(release.get("target_build")).append(" (")
                            .append(release.tag).append(")\n")
                            .append(release.get("target_fingerprint")).append("\n")
                            .append("Download: ").append(release.totalBytes() / (1 << 20)).append(" MiB\n")
                            .append("Keeps your apps and data. The phone restarts twice to install.\n");
                }
                for (String note : notes) text.append("\n").append(note);
                show(text.append("\n\n").append(lastResult()).toString());
                buttons(true, release != null, false);
            } catch (Exception e) {
                show(header() + "Check failed: " + e);
                buttons(true, false, false);
            }
        });
    }

    private void downloadUpdate() {
        buttons(false, false, false);
        worker.execute(() -> {
            try {
                byte[] random = new byte[4];
                new SecureRandom().nextBytes(random);
                txn = "a" + release.get("target_build") + "-" + Channel.hex(random);
                File dir = new File(Channel.ROOT, txn);
                if (!dir.mkdirs()) throw new Exception("cannot create " + dir);
                // The root prepare service reaches it through group system.
                android.system.Os.chmod(dir.getPath(), 0770);
                long total = release.totalBytes();
                StatFs fs = new StatFs("/data");
                if (fs.getAvailableBytes() < total + (1L << 30)) {
                    throw new Exception("needs " + (total >> 20) + " MiB plus 1 GiB free in /data");
                }
                long[] done = {0};
                for (String role : Channel.ROLES) {
                    show(header() + "Downloading " + release.get("asset." + role + ".name") + "...");
                    Channel.download(release, role, dir, done, total,
                            (d, t) -> main.post(() -> bar.setProgress((int) (1000 * d / t))));
                }
                write(new File(dir, Channel.MANIFEST), release.manifestBytes);
                write(new File(dir, Channel.SIGNATURE), release.signature);
                show(header() + "Build " + release.get("target_build") + " downloaded and verified.\n"
                        + "Install keeps your apps and data.");
                buttons(false, false, true);
            } catch (Exception e) {
                show(header() + "Download failed: " + e);
                buttons(true, false, false);
            }
        });
    }

    private static void write(File file, byte[] data) throws Exception {
        try (FileOutputStream out = new FileOutputStream(file)) {
            out.write(data);
            out.getFD().sync();
        }
        file.setReadable(true, false);
    }

    private void confirmInstall() {
        new AlertDialog.Builder(this)
                .setTitle("Install the Crux update?")
                .setMessage("The phone restarts into PE Recovery, writes the new system and vendor, "
                        + "and restarts into the updated system. Apps and data are kept.")
                .setPositiveButton("Install", (d, w) -> installUpdate())
                .setNegativeButton("Cancel", null)
                .show();
    }

    private void installUpdate() {
        buttons(false, false, false);
        show(header() + "Preparing the update for PE Recovery...");
        worker.execute(() -> {
            try {
                File status = new File(new File(Channel.ROOT, txn), "status");
                status.delete();
                write(new File(Channel.ROOT, "pending"), (txn + "\n").getBytes(StandardCharsets.UTF_8));
                Channel.setProp("sys.crux_ota.request", "idle");
                Channel.setProp("sys.crux_ota.request", "prepare");
                long deadline = System.currentTimeMillis() + 15 * 60 * 1000;
                String result = "";
                while (System.currentTimeMillis() < deadline) {
                    if (status.exists()) {
                        result = new String(Files.readAllBytes(status.toPath()), StandardCharsets.UTF_8).trim();
                        if (!result.isEmpty()) break;
                    }
                    Thread.sleep(1000);
                }
                Channel.setProp("sys.crux_ota.request", "idle");
                if (!"prepared".equals(result)) {
                    throw new Exception(result.isEmpty() ? "the prepare service did not answer" : result);
                }
                getSharedPreferences("crux", MODE_PRIVATE).edit().putString("awaiting", txn).commit();
                show(header() + "Restarting into PE Recovery to install...");
                Thread.sleep(1500);
                ((PowerManager) getSystemService(POWER_SERVICE)).reboot("recovery");
            } catch (Exception e) {
                show(header() + "Install was not started: " + e.getMessage());
                buttons(true, false, true);
            }
        });
    }
}
