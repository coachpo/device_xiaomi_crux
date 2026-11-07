package org.coachpo.cruxupdater;

import android.os.Build;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.math.BigInteger;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.security.AlgorithmParameters;
import java.security.KeyFactory;
import java.security.MessageDigest;
import java.security.PublicKey;
import java.security.Signature;
import java.security.spec.ECGenParameterSpec;
import java.security.spec.ECParameterSpec;
import java.security.spec.ECPoint;
import java.security.spec.ECPublicKeySpec;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** The crux-ota/1 Dev channel: GitHub prereleases, or a test base URL in channel.url. */
final class Channel {
    static final File ROOT = new File("/data/ota_package/crux");
    static final String RELEASES =
            "https://api.github.com/repos/coachpo/crux-pe13-distributed/releases?per_page=30";
    static final String MANIFEST = "crux-ota-manifest.txt";
    static final String SIGNATURE = "crux-ota-manifest.sig";
    static final String[] ROLES = {"system", "vendor", "rom_fit"};

    /** One applicable, signature-verified release. */
    static final class Release {
        String tag;
        byte[] manifestBytes;
        byte[] signature;
        Map<String, String> manifest;
        final Map<String, String> urls = new LinkedHashMap<>();

        String get(String key) throws IOException {
            String value = manifest.get(key);
            if (value == null) throw new IOException("manifest lacks " + key);
            return value;
        }

        long totalBytes() throws IOException {
            long total = 0;
            for (String role : ROLES) total += Long.parseLong(get("asset." + role + ".bytes"));
            return total;
        }
    }

    /**
     * The phone's serial number. ro.boot.serialno is serialno_prop, which system_app may not
     * read; system_server returns the same value (ro.serialno) to the system uid.
     */
    static String serial() {
        try {
            return Build.getSerial();
        } catch (SecurityException e) {
            return "";
        }
    }

    /**
     * A system property as init set it. PE's PixelPropsUtils rewrites Build.TYPE to "user" in
     * every app process, so the build variant is read here instead.
     */
    static String prop(String key) throws IOException {
        try {
            return (String) Class.forName("android.os.SystemProperties")
                    .getMethod("get", String.class).invoke(null, key);
        } catch (ReflectiveOperationException e) {
            throw new IOException("cannot read " + key, e);
        }
    }

    static void setProp(String key, String value) throws IOException {
        try {
            Class.forName("android.os.SystemProperties")
                    .getMethod("set", String.class, String.class).invoke(null, key, value);
        } catch (ReflectiveOperationException e) {
            throw new IOException("cannot set " + key, e);
        }
    }

    static byte[] get(String url, long limit) throws IOException {
        HttpURLConnection connection = (HttpURLConnection) new URL(url).openConnection();
        connection.setConnectTimeout(20000);
        connection.setReadTimeout(60000);
        connection.setRequestProperty("Accept", "application/vnd.github+json");
        try (InputStream in = connection.getInputStream()) {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buffer = new byte[65536];
            int n;
            while ((n = in.read(buffer)) > 0) {
                out.write(buffer, 0, n);
                if (out.size() > limit) throw new IOException("response too large: " + url);
            }
            return out.toByteArray();
        } finally {
            connection.disconnect();
        }
    }

    static Map<String, String> parse(byte[] text) throws IOException {
        Map<String, String> values = new LinkedHashMap<>();
        for (String line : new String(text, StandardCharsets.UTF_8).split("\n")) {
            if (line.isEmpty() || line.startsWith("#")) continue;
            int eq = line.indexOf('=');
            if (eq <= 0) throw new IOException("malformed manifest line");
            values.put(line.substring(0, eq), line.substring(eq + 1));
        }
        return values;
    }

    static byte[] unhex(String hex) {
        byte[] out = new byte[hex.length() / 2];
        for (int i = 0; i < out.length; i++) {
            out[i] = (byte) Integer.parseInt(hex.substring(2 * i, 2 * i + 2), 16);
        }
        return out;
    }

    static String hex(byte[] data) {
        StringBuilder out = new StringBuilder();
        for (byte b : data) out.append(String.format("%02x", b & 0xff));
        return out.toString();
    }

    /** The release key: raw P-256 X||Y, or its 128 hex digits. */
    static PublicKey releaseKey() throws Exception {
        byte[] raw = Files.readAllBytes(new File("/system/etc/crux-ota/a-p256.pub").toPath());
        if (raw.length != 64) raw = unhex(new String(raw, StandardCharsets.US_ASCII).trim());
        AlgorithmParameters parameters = AlgorithmParameters.getInstance("EC");
        parameters.init(new ECGenParameterSpec("secp256r1"));
        ECParameterSpec spec = parameters.getParameterSpec(ECParameterSpec.class);
        ECPoint point = new ECPoint(new BigInteger(1, Arrays.copyOfRange(raw, 0, 32)),
                new BigInteger(1, Arrays.copyOfRange(raw, 32, 64)));
        return KeyFactory.getInstance("EC").generatePublic(new ECPublicKeySpec(point, spec));
    }

    static byte[] derInteger(byte[] value) {
        int start = 0;
        while (start < value.length - 1 && value[start] == 0) start++;
        byte[] trimmed = Arrays.copyOfRange(value, start, value.length);
        boolean pad = (trimmed[0] & 0x80) != 0;
        byte[] out = new byte[2 + trimmed.length + (pad ? 1 : 0)];
        out[0] = 0x02;
        out[1] = (byte) (trimmed.length + (pad ? 1 : 0));
        System.arraycopy(trimmed, 0, out, pad ? 3 : 2, trimmed.length);
        return out;
    }

    static boolean verify(byte[] text, byte[] raw) throws Exception {
        if (raw.length != 64) return false;
        byte[] r = derInteger(Arrays.copyOfRange(raw, 0, 32));
        byte[] s = derInteger(Arrays.copyOfRange(raw, 32, 64));
        byte[] der = new byte[2 + r.length + s.length];
        der[0] = 0x30;
        der[1] = (byte) (r.length + s.length);
        System.arraycopy(r, 0, der, 2, r.length);
        System.arraycopy(s, 0, der, 2 + r.length, s.length);
        Signature verifier = Signature.getInstance("SHA256withECDSA");
        verifier.initVerify(releaseKey());
        verifier.update(text);
        return verifier.verify(der);
    }

    /** Null when the release does not apply to this phone; the reason goes to notes. */
    static Release check(String tag, byte[] text, byte[] signature, Map<String, String> urls,
            List<String> notes) throws Exception {
        if (!verify(text, signature)) {
            notes.add(tag + ": signature does not verify");
            return null;
        }
        Release release = new Release();
        release.tag = tag;
        release.manifestBytes = text;
        release.signature = signature;
        release.manifest = parse(text);
        String current = Build.VERSION.INCREMENTAL;
        String serial = serial();
        if (!"crux-ota/1".equals(release.manifest.get("schema"))
                || !"a-ota".equals(release.manifest.get("kind"))
                || !"crux".equals(release.manifest.get("device"))
                || !("aosp_crux-" + prop("ro.build.type")).equals(release.manifest.get("product"))
                || !"preserve".equals(release.manifest.get("data_policy"))) {
            notes.add(tag + ": not a data-preserving Dev OTA for this product");
            return null;
        }
        String serialno = release.get("serialno");
        if (!"any".equals(serialno) && !serialno.equals(serial)) {
            notes.add(tag + ": for another phone");
            return null;
        }
        String starts = "," + release.get("start_builds") + ",";
        if (!"any".equals(release.get("start_builds")) && !starts.contains("," + current + ",")) {
            notes.add(tag + ": does not start from build " + current);
            return null;
        }
        boolean reinstall = new File(ROOT, "allow-reinstall").exists();
        int order = release.get("target_build").compareTo(current);
        if (order < 0 || (order == 0 && !reinstall)) {
            notes.add(tag + ": not newer than build " + current);
            return null;
        }
        for (String role : ROLES) {
            String name = release.get("asset." + role + ".name");
            if (!urls.containsKey(name)) {
                notes.add(tag + ": release lacks " + name);
                return null;
            }
            release.urls.put(name, urls.get(name));
        }
        return release;
    }

    /** The newest applicable release, or null. */
    static Release find(List<String> notes) throws Exception {
        File local = new File(ROOT, "channel.url");
        List<Release> found = new ArrayList<>();
        if (local.exists()) {
            String base = new String(Files.readAllBytes(local.toPath()), StandardCharsets.UTF_8).trim();
            byte[] text = get(base + MANIFEST, 65536);
            byte[] signature = get(base + SIGNATURE, 1024);
            Map<String, String> manifest = parse(text);
            Map<String, String> urls = new LinkedHashMap<>();
            for (String role : ROLES) {
                String name = manifest.get("asset." + role + ".name");
                if (name != null) urls.put(name, base + name);
            }
            Release release = check("local", text, signature, urls, notes);
            if (release != null) found.add(release);
        } else {
            JSONArray releases = new JSONArray(new String(get(RELEASES, 8 << 20), StandardCharsets.UTF_8));
            for (int i = 0; i < releases.length(); i++) {
                JSONObject item = releases.getJSONObject(i);
                String tag = item.optString("tag_name");
                if (item.optBoolean("draft") || !item.optBoolean("prerelease") || !tag.startsWith("dev-pe13-")) {
                    continue;
                }
                Map<String, String> urls = new LinkedHashMap<>();
                JSONArray assets = item.getJSONArray("assets");
                for (int j = 0; j < assets.length(); j++) {
                    JSONObject asset = assets.getJSONObject(j);
                    urls.put(asset.getString("name"), asset.getString("browser_download_url"));
                }
                if (!urls.containsKey(MANIFEST) || !urls.containsKey(SIGNATURE)) continue;
                Release release = check(tag, get(urls.get(MANIFEST), 65536), get(urls.get(SIGNATURE), 1024),
                        urls, notes);
                if (release != null) found.add(release);
            }
        }
        Release best = null;
        for (Release release : found) {
            if (best == null || release.get("target_build").compareTo(best.get("target_build")) > 0) {
                best = release;
            }
        }
        return best;
    }

    interface Progress {
        void update(long done, long total);
    }

    /** Download one asset to dir/name, checking its size and SHA-256 from the manifest. */
    static void download(Release release, String role, File dir, long[] done, long total, Progress progress)
            throws Exception {
        String name = release.get("asset." + role + ".name");
        long size = Long.parseLong(release.get("asset." + role + ".bytes"));
        String sha256 = release.get("asset." + role + ".sha256");
        File target = new File(dir, name);
        File part = new File(dir, name + ".part");
        HttpURLConnection connection = (HttpURLConnection) new URL(release.urls.get(name)).openConnection();
        connection.setConnectTimeout(20000);
        connection.setReadTimeout(120000);
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        long received = 0;
        try (InputStream in = connection.getInputStream(); FileOutputStream out = new FileOutputStream(part)) {
            byte[] buffer = new byte[1 << 20];
            int n;
            while ((n = in.read(buffer)) > 0) {
                out.write(buffer, 0, n);
                digest.update(buffer, 0, n);
                received += n;
                done[0] += n;
                if (received > size) throw new IOException(name + " is larger than the manifest says");
                progress.update(done[0], total);
            }
            out.getFD().sync();
        } finally {
            connection.disconnect();
        }
        if (received != size || !hex(digest.digest()).equals(sha256)) {
            part.delete();
            throw new IOException(name + " differs from the release manifest");
        }
        if (!part.renameTo(target)) throw new IOException("cannot keep " + name);
        target.setReadable(true, false);
    }
}
