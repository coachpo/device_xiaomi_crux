/*
 * crux-installer: the crux-install/1 installer core for Crux PE13.
 *
 * B, TWRP update-binary (protected B RAM TWRP, clean install):
 *     update-binary <api-version> <pipe-fd> <zip-path>
 * The ZIP holds STORED entries: crux-install.txt (manifest), crux-install.sig
 * (P-256 ECDSA r||s over the manifest bytes), zstd raw system/vendor images and
 * both PE FITs. The key is /crux/install-keys/b-p256.pub in the B TWRP ramdisk.
 *
 * A, PE ROM (the privileged OTA helper), after the release assets are downloaded:
 *     crux-installer --a-prepare <asset-dir> <txn> [<key>]
 * verifies the signed manifest, the running start build and every asset, maps
 * each asset with FIEMAP, writes its plaintext to those raw peuserdata blocks
 * (the uncrypt technique, so Recovery needs no keys), reads them back, and
 * writes /metadata/crux_ota/txn/<txn>/ and the U-Boot request. Then the caller
 * reboots to recovery. As the init services of the updater app:
 *     crux-installer --a-service prepare   (transaction in /data/ota_package/crux/pending,
 *                                          status in /data/ota_package/crux/<txn>/status)
 *     crux-installer --a-service result    (at boot: /data/ota_package/crux/last-result,
 *                                          then the finished request and download are removed)
 *
 * A, PE Recovery (data-preserving Dev OTA):
 *     crux-installer --a-recovery <txn>
 * /metadata/crux_ota/txn/<txn>/ holds the release's crux-ota-manifest.txt and
 * .sig and one uncrypt block map per downloaded asset; the asset bytes are read
 * as plaintext from the raw peuserdata blocks. The key is
 * /system/etc/crux-ota/a-p256.pub in the PE Recovery ramdisk.
 *
 * Before any write both modes check the signature, the serial, the Recovery
 * container U-Boot marker, the recovery guard and protected partitions, LUN0
 * geometry, every payload digest (as delivered and expanded) and the FIT size;
 * A also checks the installed start build. B then asks for a volume-key
 * confirmation. Writes go to pevendor and pesystem with O_DIRECT readback (B
 * also formats peuserdata and pemetadata), then the FIT(s) are staged in the
 * pesystem tail and the "land" state block is written last, so an interrupted
 * run never lands a FIT. An A refusal before writes records "abort", which makes
 * U-Boot start the unchanged ROM. Exit status: 0 ready to land, 1 failed (B, or
 * A after writes), 2 A refused before writes. The protocol is described in
 * docs/pe13-install-protocol-2026-11-07.md.
 *
 * The host test build (-DCRUX_TEST) takes --root DIR, which redirects /dev,
 * /sys, /proc, /metadata, /crux*, /system and the tools into a fixture, and
 * --confirm yes|no; the A start build is read from DIR/start/system/build.prop.
 */

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <poll.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>
#ifndef CRUX_TEST
#include <dirent.h>
#include <linux/fiemap.h>
#include <linux/hdreg.h>
#include <linux/input.h>
#include <sys/ioctl.h>
#include <sys/mount.h>

#ifndef FS_IOC_FIEMAP
#define FS_IOC_FIEMAP _IOWR('f', 11, struct fiemap)
#endif
#ifndef BLKROGET
#define BLKROGET _IO(0x12, 94)
#endif
#ifndef BLKGETSIZE64
#define BLKGETSIZE64 _IOR(0x12, 114, size_t)
#endif
#endif

#include "mincrypt/p256.h"
#include "mincrypt/p256_ecdsa.h"
#include "mincrypt/sha256.h"
#include "zstd.h"

#define BLK 4096ULL
#define CHUNK (4ULL << 20)
#define STATE_REL 0xf0000ULL
#define ROM_STAGE_REL 0xf1000ULL
#define REC_STAGE_REL 0xf8000ULL
#define ROM_WINDOW_BLKS 0x3100ULL
#define REC_WINDOW_BLKS 0x3b00ULL
#define CONFIRM_SECONDS 60

#ifdef CRUX_TEST
#define DIRECT 0
#else
#define DIRECT O_DIRECT
#endif

enum { MODE_B, MODE_A, MODE_PREPARE };

struct volume {
	const char *name;
	uint64_t start;		/* LUN0 4096-byte block */
	uint64_t blocks;
};

static const struct volume volumes[] = {
	{ "pesystem", 0x29e4000, 0x100000 },
	{ "pevendor", 0x2ae4000, 0x70000 },
	{ "peuserdata", 0x2b54000, 0x1000000 },
	{ "pemetadata", 0x3b54000, 0x1000 },
	{ "cache", 0x30000, 0x18000 },
};

static const char *const protected_names[] = {
	"userdata", "metadata", "misc", "cache", "system", "vendor",
	"boot", "recovery", "dtbo", "vbmeta",
};

struct zentry {
	char name[96];
	uint64_t offset;
	uint64_t size;
};

struct kv {
	char key[64];
	char val[256];
};

struct range {
	uint64_t off;
	uint64_t len;
};

/* Payload bytes: one ZIP entry, or the blocks of an uncrypt map on peuserdata. */
struct source {
	char name[96];
	int fd;
	uint64_t size;
	int n;
	struct range *r;
};

static const char *root = "";
static FILE *ui_pipe;
static int mode = MODE_B;
static int confirm_mode = -1;
static int zfd = -1;
static struct zentry zents[32];
static int nzents;
static struct kv manifest[96];
static int nmanifest;
static int writes_started;
static char a_txn[48];
static char status_path[640];
#define R_COUNT_A 1

static void at(char *out, size_t n, const char *fmt, ...)
{
	char tail[384];
	va_list ap;

	va_start(ap, fmt);
	vsnprintf(tail, sizeof(tail), fmt, ap);
	va_end(ap);
	snprintf(out, n, "%s%s", root, tail);
}

static void ui(const char *fmt, ...)
{
	char msg[512];
	va_list ap;

	va_start(ap, fmt);
	vsnprintf(msg, sizeof(msg), fmt, ap);
	va_end(ap);
	if (ui_pipe) {
		fprintf(ui_pipe, "ui_print %s\nui_print\n", msg);
		fflush(ui_pipe);
	}
	fprintf(stderr, "crux-installer: %s\n", msg);
	fflush(stderr);
}

static void progress(double fraction, int seconds)
{
	if (ui_pipe) {
		fprintf(ui_pipe, "progress %.3f %d\n", fraction, seconds);
		fflush(ui_pipe);
	}
}

static void write_state_fields(const char *const *fields);
static void a_result(const char *status, const char *message);

static void die(const char *fmt, ...)
{
	char msg[512];
	va_list ap;

	va_start(ap, fmt);
	vsnprintf(msg, sizeof(msg), fmt, ap);
	va_end(ap);
	ui("ERROR: %s", msg);
	if (mode == MODE_PREPARE) {
		ui("The update was not prepared; nothing was requested from U-Boot.");
		if (status_path[0]) {
			FILE *f = fopen(status_path, "w");

			if (f) {
				fprintf(f, "failed: %s\n", msg);
				fclose(f);
				chmod(status_path, 0644); /* the app (uid system) reads it */
			}
		}
		exit(1);
	}
	if (mode == MODE_A && a_txn[0]) {
		static int dying;

		if (dying++)
			exit(1);
		a_result(writes_started ? "failed-after-write" : "refused", msg);
		if (!writes_started) {
			char txn[80];
			const char *fields[] = { "crux_s_ver=1", txn, "crux_s_stage=abort", "crux_s_tries=0",
						 "crux_s_msg=refused-before-write", NULL };

			snprintf(txn, sizeof(txn), "crux_s_txn=%s", a_txn);
			write_state_fields(fields);
			ui("Nothing was written; U-Boot will start the unchanged PE ROM.");
			exit(2);
		}
		ui("pesystem/pevendor may be partly written. Returning to U-Boot retries this update once.");
		exit(1);
	}
	if (writes_started)
		ui("PE volumes may be partly written and no FIT was staged for landing. "
		   "Re-run this ZIP from the B TWRP; it rewrites everything.");
	else
		ui("Nothing was written.");
	exit(1);
}

static void read_exact(int fd, void *buf, size_t n, uint64_t off)
{
	uint8_t *p = buf;

	while (n) {
		ssize_t r = pread(fd, p, n, (off_t)off);

		if (r <= 0)
			die("read failed at %" PRIu64 ": %s", off, r < 0 ? strerror(errno) : "short file");
		p += r;
		n -= (size_t)r;
		off += (uint64_t)r;
	}
}

static void write_exact(int fd, const void *buf, size_t n, uint64_t off)
{
	const uint8_t *p = buf;

	while (n) {
		ssize_t w = pwrite(fd, p, n, (off_t)off);

		if (w <= 0)
			die("write failed at %" PRIu64 ": %s", off, w < 0 ? strerror(errno) : "short write");
		p += w;
		n -= (size_t)w;
		off += (uint64_t)w;
	}
}

static void *aligned(size_t n)
{
	void *p = NULL;

	if (posix_memalign(&p, BLK, n))
		die("out of memory for %zu bytes", n);
	memset(p, 0, n);
	return p;
}

static uint32_t crc32_update(uint32_t crc, const uint8_t *p, size_t n)
{
	static uint32_t table[256];
	size_t i;

	if (!table[1])
		for (i = 0; i < 256; i++) {
			uint32_t c = (uint32_t)i;
			int k;

			for (k = 0; k < 8; k++)
				c = c & 1 ? 0xedb88320U ^ (c >> 1) : c >> 1;
			table[i] = c;
		}
	crc = ~crc;
	for (i = 0; i < n; i++)
		crc = table[(crc ^ p[i]) & 0xff] ^ (crc >> 8);
	return ~crc;
}

static uint16_t le16(const uint8_t *p) { return (uint16_t)(p[0] | p[1] << 8); }
static uint32_t le32(const uint8_t *p) { return (uint32_t)le16(p) | (uint32_t)le16(p + 2) << 16; }

static void read_file(const char *path, char *buf, size_t n, size_t *got)
{
	int fd = open(path, O_RDONLY);
	ssize_t r;

	if (fd < 0)
		die("cannot read %s: %s", path, strerror(errno));
	r = read(fd, buf, n - 1);
	close(fd);
	if (r < 0)
		die("cannot read %s", path);
	buf[r] = '\0';
	if (got)
		*got = (size_t)r;
}

/* ---- payload sources ---- */

static void source_read(const struct source *s, uint64_t pos, void *buf, size_t n)
{
	uint8_t *p = buf;

	if (pos + n > s->size)
		die("%s: read past its end", s->name);
	while (n) {
		uint64_t base = 0, within, avail;
		size_t take;
		int i;

		for (i = 0; i < s->n && pos >= base + s->r[i].len; i++)
			base += s->r[i].len;
		if (i == s->n)
			die("%s: block map is shorter than the asset", s->name);
		within = pos - base;
		avail = s->r[i].len - within;
		take = n < avail ? n : (size_t)avail;
		read_exact(s->fd, p, take, s->r[i].off + within);
		p += take;
		pos += take;
		n -= take;
	}
}

static void source_sha256(const struct source *s, uint8_t out[32])
{
	uint8_t *buf = malloc(1 << 20);
	uint64_t off = 0;
	SHA256_CTX c;

	if (!buf)
		die("out of memory");
	SHA256_init(&c);
	while (off < s->size) {
		size_t n = s->size - off < (1 << 20) ? (size_t)(s->size - off) : (1 << 20);

		source_read(s, off, buf, n);
		SHA256_update(&c, buf, (int)n);
		off += n;
	}
	free(buf);
	memcpy(out, SHA256_final(&c), 32);
}

/* ---- ZIP (STORED entries only) ---- */

static void zip_open(const char *path)
{
	uint8_t tail[65557], *cd, *p;
	struct stat st;
	size_t n;
	uint32_t cd_size, cd_off;
	int i, count;
	long eocd = -1;

	zfd = open(path, O_RDONLY);
	if (zfd < 0 || fstat(zfd, &st))
		die("cannot open %s: %s", path, strerror(errno));
	n = st.st_size < (off_t)sizeof(tail) ? (size_t)st.st_size : sizeof(tail);
	read_exact(zfd, tail, n, (uint64_t)st.st_size - n);
	for (i = (int)n - 22; i >= 0; i--)
		if (le32(tail + i) == 0x06054b50) {
			eocd = i;
			break;
		}
	if (eocd < 0)
		die("not a ZIP file");
	count = le16(tail + eocd + 10);
	cd_size = le32(tail + eocd + 12);
	cd_off = le32(tail + eocd + 16);
	if (count > (int)(sizeof(zents) / sizeof(zents[0])) || (uint64_t)cd_off + cd_size > (uint64_t)st.st_size)
		die("unexpected ZIP directory");
	cd = malloc(cd_size);
	if (!cd)
		die("out of memory");
	read_exact(zfd, cd, cd_size, cd_off);
	for (p = cd, i = 0; i < count; i++) {
		uint8_t local[30];
		uint16_t name_len, extra_len, comment_len;
		struct zentry *e = &zents[nzents++];

		if (p + 46 > cd + cd_size || le32(p) != 0x02014b50)
			die("bad ZIP directory entry");
		name_len = le16(p + 28);
		extra_len = le16(p + 30);
		comment_len = le16(p + 32);
		if (p + 46 + name_len > cd + cd_size || name_len >= sizeof(e->name))
			die("bad ZIP entry name");
		memcpy(e->name, p + 46, name_len);
		e->name[name_len] = '\0';
		if (le16(p + 10) != 0 || le32(p + 20) != le32(p + 24))
			die("ZIP entry %s is not STORED", e->name);
		e->size = le32(p + 24);
		read_exact(zfd, local, sizeof(local), le32(p + 42));
		if (le32(local) != 0x04034b50)
			die("bad ZIP local header for %s", e->name);
		e->offset = (uint64_t)le32(p + 42) + 30 + le16(local + 26) + le16(local + 28);
		if (e->offset + e->size > (uint64_t)st.st_size)
			die("ZIP entry %s exceeds the file", e->name);
		p += 46 + name_len + extra_len + comment_len;
	}
	free(cd);
}

static void zip_source(const char *name, struct source *s)
{
	int i;

	for (i = 0; i < nzents; i++)
		if (!strcmp(zents[i].name, name)) {
			snprintf(s->name, sizeof(s->name), "%s", name);
			s->fd = zfd;
			s->size = zents[i].size;
			s->n = 1;
			s->r = malloc(sizeof(*s->r));
			if (!s->r)
				die("out of memory");
			s->r[0].off = zents[i].offset;
			s->r[0].len = zents[i].size;
			return;
		}
	die("ZIP lacks %s", name);
}

/* ---- manifest ---- */

static void parse_manifest(const char *text, size_t n)
{
	const char *p = text, *end = text + n;

	while (p < end) {
		const char *nl = memchr(p, '\n', (size_t)(end - p)), *eq;
		size_t len = nl ? (size_t)(nl - p) : (size_t)(end - p);
		struct kv *kv;

		eq = memchr(p, '=', len);
		if (len && *p != '#') {
			if (!eq || (size_t)(eq - p) >= sizeof(kv->key) || len - (size_t)(eq - p) - 1 >= sizeof(kv->val) ||
			    nmanifest == (int)(sizeof(manifest) / sizeof(manifest[0])))
				die("malformed manifest line");
			kv = &manifest[nmanifest++];
			memcpy(kv->key, p, (size_t)(eq - p));
			kv->key[eq - p] = '\0';
			memcpy(kv->val, eq + 1, len - (size_t)(eq - p) - 1);
			kv->val[len - (size_t)(eq - p) - 1] = '\0';
		}
		p += len + 1;
	}
}

static const char *mget(const char *key)
{
	int i;

	for (i = 0; i < nmanifest; i++)
		if (!strcmp(manifest[i].key, key))
			return manifest[i].val;
	die("manifest lacks %s", key);
	return NULL;
}

static uint64_t mnum(const char *key)
{
	char *end;
	unsigned long long v = strtoull(mget(key), &end, 0);

	if (*end)
		die("manifest %s is not a number", key);
	return v;
}

static void mdigest(const char *key, uint8_t out[32])
{
	const char *v = mget(key);
	int i;

	if (strlen(v) != 64)
		die("manifest %s is not a SHA-256", key);
	for (i = 0; i < 32; i++) {
		unsigned int byte;

		if (sscanf(v + 2 * i, "%2x", &byte) != 1)
			die("manifest %s is not a SHA-256", key);
		out[i] = (uint8_t)byte;
	}
}

/* Verify the P-256 signature of the manifest text with a raw X||Y key file. */
static void verify_and_parse(const char *text, size_t len, const uint8_t sig[64], const char *key_path)
{
	uint8_t key[64], digest[32];
	char keybuf[160];
	p256_int x, y, msg, r, s;
	size_t got, i;

	/* Raw 64-byte X||Y, or the same as 128 hex digits (optionally newline-terminated). */
	read_file(key_path, keybuf, sizeof(keybuf), &got);
	if (got == sizeof(key)) {
		memcpy(key, keybuf, sizeof(key));
	} else if ((got == 128 || (got == 129 && keybuf[128] == '\n')) &&
		   strspn(keybuf, "0123456789abcdef") == 128) {
		for (i = 0; i < sizeof(key); i++) {
			unsigned int byte;

			sscanf(keybuf + 2 * i, "%2x", &byte);
			key[i] = (uint8_t)byte;
		}
	} else {
		die("install key %s is missing or malformed", key_path);
	}
	SHA256_hash(text, (int)len, digest);
	p256_from_bin(key, &x);
	p256_from_bin(key + 32, &y);
	p256_from_bin(digest, &msg);
	p256_from_bin(sig, &r);
	p256_from_bin(sig + 32, &s);
	if (!p256_ecdsa_verify(&x, &y, &msg, &r, &s))
		die("manifest signature does not verify with %s", key_path);
	parse_manifest(text, len);
}

/* ---- live device checks ---- */

static int cmdline_value(const char *cmdline, const char *key, char *out, size_t n)
{
	size_t klen = strlen(key);
	const char *p = cmdline;

	while ((p = strstr(p, key))) {
		if ((p == cmdline || p[-1] == ' ') && p[klen] == '=') {
			size_t len = strcspn(p + klen + 1, " \n");

			if (len >= n)
				return -1;
			memcpy(out, p + klen + 1, len);
			out[len] = '\0';
			return 0;
		}
		p += klen;
	}
	return -1;
}

static void node_of(const char *name, char *node, size_t n)
{
	char path[512], target[512];
	ssize_t len;
	const char *base;

	at(path, sizeof(path), "/dev/block/by-name/%s", name);
	len = readlink(path, target, sizeof(target) - 1);
	if (len <= 0)
		die("no by-name link for %s", name);
	target[len] = '\0';
	base = strrchr(target, '/');
	snprintf(node, n, "%s", base ? base + 1 : target);
}

static uint64_t sysfs_number(const char *node, const char *field)
{
	char path[512], buf[64];

	at(path, sizeof(path), "/sys/class/block/%s/%s", node, field);
	read_file(path, buf, sizeof(buf), NULL);
	return strtoull(buf, NULL, 10);
}

/*
 * Live facts of one partition: its read-only flag (BLKROSET state) and its extent
 * in 512-byte sectors. B (TWRP) reads sysfs. A runs in PE Recovery, a coredomain
 * that SELinux keeps out of unlabeled sysfs, so it asks the block device instead.
 */
struct part {
	char node[512];
	int ro;
	uint64_t start, size;
};

static void part_facts(const char *name, struct part *p, int extent)
{
	char path[640], buf[16];

	node_of(name, p->node, sizeof(p->node));
#ifndef CRUX_TEST
	if (mode == MODE_A) {
		struct hd_geometry geo;
		uint64_t bytes;
		int fd;

		at(path, sizeof(path), "/dev/block/%s", p->node);
		fd = open(path, O_RDONLY | O_CLOEXEC);
		if (fd < 0 || ioctl(fd, BLKROGET, &p->ro) ||
		    (extent && (ioctl(fd, BLKGETSIZE64, &bytes) || ioctl(fd, HDIO_GETGEO, &geo))))
			die("cannot query %s (%s): %s", name, p->node, strerror(errno));
		close(fd);
		if (extent) {
			p->start = geo.start;
			p->size = bytes / 512;
		}
		return;
	}
#endif
	at(path, sizeof(path), "/sys/class/block/%s/ro", p->node);
	read_file(path, buf, sizeof(buf), NULL);
	p->ro = buf[0] == '1' ? 1 : buf[0] == '0' ? 0 : -1;
	if (extent) {
		p->start = sysfs_number(p->node, "start");
		p->size = sysfs_number(p->node, "size");
	}
}

/* Checks shared by both modes; the A recovery has /data and /metadata mounted. */
static void check_device(void)
{
	char path[512], cmdline[4096], value[128], uevent[512], want[640];
	char mounts[16384];
	struct part part;
	size_t i;

	at(path, sizeof(path), "/proc/cmdline");
	read_file(path, cmdline, sizeof(cmdline), NULL);
	if (cmdline_value(cmdline, "androidboot.serialno", value, sizeof(value)))
		die("kernel command line has no serial number");
	if (strcmp(mget("serialno"), "any") && strcmp(mget("serialno"), value))
		die("this package is for serial %s, not %s", mget("serialno"), value);
	if (cmdline_value(cmdline, "androidboot.crux_uboot", value, sizeof(value)) ||
	    strcmp(value, mget("uboot_role")))
		die("not started by the declared Recovery container U-Boot (%s)", mget("uboot_role"));
	if (mode == MODE_A) {
		if (cmdline_value(cmdline, "androidboot.crux_ota_txn", value, sizeof(value)) || strcmp(value, a_txn))
			die("U-Boot did not start this recovery for transaction %s", a_txn);
		at(path, sizeof(path), "/dev/crux-pe-storage-guard.active");
		if (access(path, F_OK))
			die("the PE storage guard is not active");
	} else {
		char profile[128], ready[128];

		at(path, sizeof(path), "/crux-ram-twrp-profile");
		read_file(path, profile, sizeof(profile), NULL);
		at(path, sizeof(path), "/crux-ram-twrp-guard.ready");
		read_file(path, ready, sizeof(ready), NULL);
		profile[strcspn(profile, "\n")] = '\0';
		ready[strcspn(ready, "\n")] = '\0';
		if (!profile[0] || strcmp(profile, ready) || !strstr(mget("twrp_profiles"), profile))
			die("not running in a declared, guarded B TWRP profile");
	}
	for (i = 0; i < sizeof(protected_names) / sizeof(protected_names[0]); i++) {
		part_facts(protected_names[i], &part, 0);
		if (part.ro != 1)
			die("protected partition %s is not read-only", protected_names[i]);
	}
	at(path, sizeof(path), "/proc/mounts");
	read_file(path, mounts, sizeof(mounts), NULL);
	for (i = 0; i < sizeof(volumes) / sizeof(volumes[0]); i++) {
		const struct volume *v = &volumes[i];
		int written = mode == MODE_B || !strcmp(v->name, "pesystem") || !strcmp(v->name, "pevendor") ||
			      !strcmp(v->name, "cache");

		part_facts(v->name, &part, 1);
		if (part.start != v->start * 8 || part.size != v->blocks * 8)
			die("live GPT extent of %s (%s) differs", v->name, part.node);
		/* ueventd names the by-name links after PARTNAME; B also reads it back. */
		if (mode == MODE_B) {
			at(path, sizeof(path), "/sys/class/block/%s/uevent", part.node);
			read_file(path, uevent, sizeof(uevent), NULL);
			snprintf(want, sizeof(want), "PARTNAME=%s\n", v->name);
			if (!strstr(uevent, want))
				die("%s does not carry PARTNAME %s", part.node, v->name);
		}
		if (written) {
			snprintf(want, sizeof(want), "/dev/block/%s ", part.node);
			if (strstr(mounts, want))
				die("%s is mounted", v->name);
			snprintf(want, sizeof(want), "/by-name/%s ", v->name);
			if (strstr(mounts, want))
				die("%s is mounted", v->name);
		}
		if (strncmp(v->name, "pe", 2) == 0 && part.ro != 0)
			die("%s is not writable", v->name);
	}
	ui("Device %s: serial, container U-Boot, recovery guard and LUN0 geometry checked", mget("serialno"));
}

/* ---- expansion: raw zstd, zstd-compressed sparse, or plain sparse ---- */

typedef void (*sink_fn)(void *ctx, const uint8_t *data, size_t n, uint64_t offset);

/* Collects expanded bytes into aligned 4 MiB chunks for the sink. */
struct acc {
	const char *what;
	uint8_t *buf;
	size_t fill;
	uint64_t produced, limit;
	sink_fn sink;
	void *ctx;
};

static void acc_flush(struct acc *a)
{
	if (!a->fill)
		return;
	if (a->produced + a->fill > a->limit)
		die("%s expands beyond its declared size", a->what);
	a->sink(a->ctx, a->buf, a->fill, a->produced);
	a->produced += a->fill;
	a->fill = 0;
}

static void acc_push(struct acc *a, const uint8_t *p, size_t n)
{
	while (n) {
		size_t take = CHUNK - a->fill < n ? CHUNK - a->fill : n;

		memcpy(a->buf + a->fill, p, take);
		a->fill += take;
		p += take;
		n -= take;
		if (a->fill == CHUNK)
			acc_flush(a);
	}
}

static void acc_repeat(struct acc *a, uint32_t pattern, uint64_t n)
{
	uint8_t word[4] = { (uint8_t)pattern, (uint8_t)(pattern >> 8), (uint8_t)(pattern >> 16),
			    (uint8_t)(pattern >> 24) };

	while (n) {
		size_t take = CHUNK - a->fill < n ? CHUNK - a->fill : (size_t)n, i;

		if (pattern == 0)
			memset(a->buf + a->fill, 0, take);
		else
			for (i = 0; i < take; i++)
				a->buf[a->fill + i] = word[(a->produced + a->fill + i) & 3];
		a->fill += take;
		n -= take;
		if (a->fill == CHUNK)
			acc_flush(a);
	}
}

typedef void (*bytes_fn)(void *ctx, const uint8_t *p, size_t n);

static void raw_feed(void *ctx, const uint8_t *p, size_t n)
{
	acc_push(ctx, p, n);
}

/* Android sparse image (v1.0) stream parser; DONT_CARE expands to zeros. */
struct sparse {
	struct acc *out;
	int state;		/* 0 file header, 1 chunk header, 2 raw data, 3 fill word, 4 crc, 5 done */
	uint8_t hdr[64];
	size_t have, need;
	uint32_t blk_sz, total_blks, chunks_left, chunk_hdr_sz;
	uint64_t left, chunk_bytes;
};

static void sparse_chunk(struct sparse *sp)
{
	uint16_t type = le16(sp->hdr);
	uint32_t blocks = le32(sp->hdr + 4), total = le32(sp->hdr + 8);

	sp->chunk_bytes = (uint64_t)blocks * sp->blk_sz;
	sp->have = 0;
	if (type == 0xcac1 && total == sp->chunk_hdr_sz + sp->chunk_bytes) {
		sp->state = 2;
		sp->left = sp->chunk_bytes;
	} else if (type == 0xcac2 && total == sp->chunk_hdr_sz + 4) {
		sp->state = 3;
		sp->need = 4;
	} else if (type == 0xcac3 && total == sp->chunk_hdr_sz) {
		acc_repeat(sp->out, 0, sp->chunk_bytes);
		sp->state = 1;
	} else if (type == 0xcac4 && total == sp->chunk_hdr_sz + 4) {
		sp->state = 4;
		sp->need = 4;
	} else {
		die("%s: bad sparse chunk %04x", sp->out->what, type);
	}
	if (sp->state == 1) {
		sp->need = sp->chunk_hdr_sz;
		if (!--sp->chunks_left)
			sp->state = 5;
	}
}

static void sparse_feed(void *ctx, const uint8_t *p, size_t n)
{
	struct sparse *sp = ctx;

	while (n) {
		if (sp->state == 5)
			die("%s: data after the last sparse chunk", sp->out->what);
		if (sp->state == 2) {
			size_t take = sp->left < n ? (size_t)sp->left : n;

			acc_push(sp->out, p, take);
			p += take;
			n -= take;
			sp->left -= take;
			if (!sp->left) {
				sp->state = 1;
				sp->need = sp->chunk_hdr_sz;
				sp->have = 0;
				if (!--sp->chunks_left)
					sp->state = 5;
			}
			continue;
		}
		{
			size_t take = sp->need - sp->have < n ? sp->need - sp->have : n;

			memcpy(sp->hdr + sp->have, p, take);
			sp->have += take;
			p += take;
			n -= take;
		}
		if (sp->have < sp->need)
			continue;
		if (sp->state == 0) {
			uint16_t file_hdr = le16(sp->hdr + 8);

			if (sp->need == 28) {
				if (le32(sp->hdr) != 0xed26ff3a || le16(sp->hdr + 4) != 1 || file_hdr < 28 ||
				    file_hdr > sizeof(sp->hdr) || le16(sp->hdr + 10) < 12 || le16(sp->hdr + 10) > 32)
					die("%s is not an Android sparse image", sp->out->what);
				sp->chunk_hdr_sz = le16(sp->hdr + 10);
				sp->blk_sz = le32(sp->hdr + 12);
				sp->total_blks = le32(sp->hdr + 16);
				sp->chunks_left = le32(sp->hdr + 20);
				if (sp->blk_sz != BLK || (uint64_t)sp->total_blks * sp->blk_sz != sp->out->limit)
					die("%s sparse geometry differs from the manifest", sp->out->what);
				if (file_hdr > 28) {
					sp->need = file_hdr;
					continue;
				}
			}
			sp->state = sp->chunks_left ? 1 : 5;
			sp->need = sp->chunk_hdr_sz;
			sp->have = 0;
		} else if (sp->state == 1) {
			sparse_chunk(sp);
		} else if (sp->state == 3) {
			acc_repeat(sp->out, le32(sp->hdr), sp->chunk_bytes);
			sp->state = 1;
			sp->need = sp->chunk_hdr_sz;
			sp->have = 0;
			if (!--sp->chunks_left)
				sp->state = 5;
		} else if (sp->state == 4) {
			sp->state = 1;
			sp->need = sp->chunk_hdr_sz;
			sp->have = 0;
			if (!--sp->chunks_left)
				sp->state = 5;
		}
	}
}

static void stream(const struct source *s, int zstd, bytes_fn fn, void *ctx)
{
	uint8_t *in = malloc(1 << 20), *out = zstd ? malloc(1 << 20) : NULL;
	ZSTD_DCtx *d = zstd ? ZSTD_createDCtx() : NULL;
	uint64_t consumed = 0;
	size_t ret = 1;

	if (!in || (zstd && (!out || !d)))
		die("out of memory for %s", s->name);
	if (d)
		ZSTD_DCtx_setParameter(d, ZSTD_d_windowLogMax, 27);
	while (consumed < s->size) {
		size_t n = s->size - consumed < (1 << 20) ? (size_t)(s->size - consumed) : (1 << 20);
		ZSTD_inBuffer ib = { in, n, 0 };

		source_read(s, consumed, in, n);
		consumed += n;
		if (!zstd) {
			fn(ctx, in, n);
			continue;
		}
		while (ib.pos < ib.size) {
			ZSTD_outBuffer ob = { out, 1 << 20, 0 };

			ret = ZSTD_decompressStream(d, &ob, &ib);
			if (ZSTD_isError(ret))
				die("%s: zstd: %s", s->name, ZSTD_getErrorName(ret));
			if (ob.pos)
				fn(ctx, out, ob.pos);
		}
	}
	if (zstd && ret != 0)
		die("%s: truncated zstd stream", s->name);
	if (d)
		ZSTD_freeDCtx(d);
	free(in);
	free(out);
}

enum { FORMAT_ZSTD_RAW, FORMAT_ZSTD_SPARSE, FORMAT_SPARSE };

struct image {
	const char *part;
	struct source src;
	int format;
	uint64_t bytes;		/* expanded raw size */
	uint8_t sha[32];	/* expanded raw SHA-256 */
};

static uint64_t expand(const struct image *img, sink_fn sink, void *ctx)
{
	struct acc a = { img->part, aligned(CHUNK), 0, 0, img->bytes, sink, ctx };
	struct sparse sp = { &a, 0, { 0 }, 0, 28, 0, 0, 0, 0, 0, 0 };

	if (img->format == FORMAT_ZSTD_RAW)
		stream(&img->src, 1, raw_feed, &a);
	else
		stream(&img->src, img->format == FORMAT_ZSTD_SPARSE, sparse_feed, &sp);
	if (img->format != FORMAT_ZSTD_RAW && sp.state != 5)
		die("%s: truncated sparse image", img->part);
	acc_flush(&a);
	free(a.buf);
	return a.produced;
}

static void hash_sink(void *ctx, const uint8_t *data, size_t n, uint64_t offset)
{
	(void)offset;
	SHA256_update(ctx, data, (int)n);
}

static void check_expanded(const struct image *img)
{
	SHA256_CTX c;

	SHA256_init(&c);
	if (expand(img, hash_sink, &c) != img->bytes || memcmp(SHA256_final(&c), img->sha, 32))
		die("expanded %s image differs from the manifest", img->part);
}

struct fit {
	const char *role;
	struct source src;
	uint64_t window_blocks, stage_rel;
	uint8_t *padded;
	uint32_t crc;
};

static void load_fit(struct fit *f, const char *key_prefix)
{
	char key[96];
	uint8_t want[32], got[32];
	uint64_t bytes;

	snprintf(key, sizeof(key), "%s.bytes", key_prefix);
	bytes = mnum(key);
	if (bytes != f->src.size || bytes > f->window_blocks * BLK || bytes < 64)
		die("%s FIT size does not fit its window", f->role);
	f->padded = aligned(f->window_blocks * BLK);
	source_read(&f->src, 0, f->padded, bytes);
	snprintf(key, sizeof(key), "%s.sha256", key_prefix);
	mdigest(key, want);
	SHA256_hash(f->padded, (int)bytes, got);
	if (memcmp(got, want, 32))
		die("%s FIT differs from the manifest", f->role);
	if (memcmp(f->padded, "\xd0\x0d\xfe\xed", 4) ||
	    ((uint64_t)f->padded[4] << 24 | f->padded[5] << 16 | f->padded[6] << 8 | f->padded[7]) > bytes)
		die("%s FIT is not a complete FIT", f->role);
	f->crc = crc32_update(0, f->padded, f->window_blocks * BLK);
}

/* ---- confirmation (B) ---- */

static int confirm(void)
{
	if (confirm_mode >= 0)
		return confirm_mode;
#ifdef CRUX_TEST
	return 0;
#else
	struct pollfd fds[16];
	int n = 0, i, waited;
	DIR *dir = opendir("/dev/input");
	struct dirent *de;

	while (dir && (de = readdir(dir)) && n < 16) {
		char path[300];

		if (strncmp(de->d_name, "event", 5))
			continue;
		snprintf(path, sizeof(path), "/dev/input/%s", de->d_name);
		fds[n].fd = open(path, O_RDONLY | O_NONBLOCK);
		fds[n].events = POLLIN;
		if (fds[n].fd >= 0)
			n++;
	}
	if (dir)
		closedir(dir);
	if (!n)
		return 0;
	for (i = 0; i < n; i++) {
		struct input_event ev;

		while (read(fds[i].fd, &ev, sizeof(ev)) == sizeof(ev))
			;
	}
	ui("Press Volume Up to install, Volume Down to cancel (%d s).", CONFIRM_SECONDS);
	for (waited = 0; waited < CONFIRM_SECONDS * 10; waited++) {
		if (poll(fds, (nfds_t)n, 100) <= 0)
			continue;
		for (i = 0; i < n; i++) {
			struct input_event ev;

			while (read(fds[i].fd, &ev, sizeof(ev)) == sizeof(ev))
				if (ev.type == EV_KEY && ev.value == 1 &&
				    (ev.code == KEY_VOLUMEUP || ev.code == KEY_VOLUMEDOWN))
					return ev.code == KEY_VOLUMEUP;
		}
	}
	return 0;
#endif
}

/* ---- writes ---- */

static int open_volume(const char *name, int flags)
{
	char path[512];
	int fd;

	at(path, sizeof(path), "/dev/block/by-name/%s", name);
	fd = open(path, flags | DIRECT);
	if (fd < 0)
		die("cannot open %s: %s", name, strerror(errno));
#if defined(CRUX_TEST) && defined(F_NOCACHE)
	fcntl(fd, F_NOCACHE, 1);
#endif
	return fd;
}

static void readback(const char *name, uint64_t offset, uint64_t bytes, const uint8_t want[32])
{
	int fd = open_volume(name, O_RDONLY);
	uint8_t *buf = aligned(CHUNK);
	uint64_t done = 0;
	SHA256_CTX c;

	SHA256_init(&c);
	while (done < bytes) {
		size_t n = bytes - done < CHUNK ? (size_t)(bytes - done) : CHUNK;

		read_exact(fd, buf, n, offset + done);
		SHA256_update(&c, buf, (int)n);
		done += n;
	}
	close(fd);
	free(buf);
	if (memcmp(SHA256_final(&c), want, 32))
		die("%s readback differs", name);
}

struct write_ctx {
	int fd;
	uint64_t total;
	double base, span;
};

static void write_sink(void *ctx, const uint8_t *data, size_t n, uint64_t offset)
{
	struct write_ctx *w = ctx;

	write_exact(w->fd, data, n, offset);
	progress(w->base + w->span * (double)(offset + n) / (double)w->total, 0);
}

static void write_image(const struct image *img, double base, double span)
{
	struct write_ctx w = { open_volume(img->part, O_WRONLY), img->bytes, base, span };

	ui("Writing %s (%" PRIu64 " bytes)...", img->part, img->bytes);
	if (expand(img, write_sink, &w) != img->bytes)
		die("%s write length differs", img->part);
	if (fsync(w.fd))
		die("%s sync failed", img->part);
	close(w.fd);
	readback(img->part, 0, img->bytes, img->sha);
	ui("%s written and read back", img->part);
}

static int run_tool(char *const argv[], char *out, size_t n)
{
	int pipe_fd[2], status;
	pid_t pid;
	size_t got = 0;

	if (pipe(pipe_fd))
		die("pipe failed");
	pid = fork();
	if (pid < 0)
		die("fork failed");
	if (!pid) {
		dup2(pipe_fd[1], 1);
		dup2(pipe_fd[1], 2);
		close(pipe_fd[0]);
		execv(argv[0], argv);
		_exit(127);
	}
	close(pipe_fd[1]);
	for (;;) {
		ssize_t r = read(pipe_fd[0], out + got, n - 1 - got);

		if (r <= 0 || got + (size_t)r >= n - 1) {
			if (r > 0)
				got += (size_t)r;
			break;
		}
		got += (size_t)r;
	}
	out[got] = '\0';
	close(pipe_fd[0]);
	if (waitpid(pid, &status, 0) < 0)
		return -1;
	return WIFEXITED(status) ? WEXITSTATUS(status) : -1;
}

static void format_volume(const char *name, uint64_t blocks)
{
	char mke2fs[512], e2fsck[512], tune2fs[512], dev[512], count[32], out[16384], *p;
	int data = !strcmp(name, "peuserdata");
	char *mk_data[] = { mke2fs, "-F", "-t", "ext4", "-b", "4096", "-m", "0", "-I", "512", "-O",
			    "encrypt,quota,project", "-E", "nodiscard,quotatype=usr:grp:prj", dev, count, NULL };
	char *mk_meta[] = { mke2fs, "-F", "-t", "ext4", "-b", "4096", "-m", "0", "-I", "256", "-E",
			    "nodiscard", dev, count, NULL };
	char *fsck[] = { e2fsck, "-fn", dev, NULL };
	char *tune[] = { tune2fs, "-l", dev, NULL };
	uint64_t got_blocks, inode;

	at(mke2fs, sizeof(mke2fs), "/system/bin/mke2fs");
	at(e2fsck, sizeof(e2fsck), "/system/bin/e2fsck");
	at(tune2fs, sizeof(tune2fs), "/system/bin/tune2fs");
	at(dev, sizeof(dev), "/dev/block/by-name/%s", name);
	snprintf(count, sizeof(count), "%" PRIu64, blocks);
	ui("Formatting %s...", name);
	if (run_tool(data ? mk_data : mk_meta, out, sizeof(out)))
		die("mke2fs %s failed: %.200s", name, out);
	if (run_tool(fsck, out, sizeof(out)))
		die("e2fsck %s failed: %.200s", name, out);
	if (run_tool(tune, out, sizeof(out)))
		die("tune2fs %s failed", name);
	p = strstr(out, "Block count:");
	got_blocks = p ? strtoull(p + 12, NULL, 10) : 0;
	p = strstr(out, "Inode size:");
	inode = p ? strtoull(p + 11, NULL, 10) : 0;
	if (got_blocks != blocks || inode != (data ? 512 : 256) || !strstr(out, "has_journal") ||
	    (data && (!strstr(out, "encrypt") || !strstr(out, "project") || !strstr(out, "quota"))))
		die("%s filesystem parameters differ after formatting", name);
	ui("%s formatted and checked", name);
}

static void stage_fit(const struct fit *f)
{
	int fd = open_volume("pesystem", O_WRONLY);
	uint8_t digest[32];
	uint64_t bytes = f->window_blocks * BLK;

	write_exact(fd, f->padded, bytes, f->stage_rel * BLK);
	if (fsync(fd))
		die("pesystem sync failed");
	close(fd);
	SHA256_hash(f->padded, (int)bytes, digest);
	readback("pesystem", f->stage_rel * BLK, bytes, digest);
	ui("%s FIT staged for U-Boot (window CRC %08x)", f->role, f->crc);
}

/* Write a crux-install/1 state block (env -c format) and read it back. */
static void write_state_fields(const char *const *fields)
{
	uint8_t *blob = aligned(BLK), digest[32];
	char *p = (char *)blob + 4;
	uint32_t crc;
	int fd, i;

	for (i = 0; fields[i]; i++)
		p += sprintf(p, "%s", fields[i]) + 1;
	crc = crc32_update(0, blob + 4, BLK - 4);
	blob[0] = (uint8_t)crc;
	blob[1] = (uint8_t)(crc >> 8);
	blob[2] = (uint8_t)(crc >> 16);
	blob[3] = (uint8_t)(crc >> 24);
	fd = open_volume("pesystem", O_WRONLY);
	write_exact(fd, blob, BLK, STATE_REL * BLK);
	if (fsync(fd))
		die("pesystem sync failed");
	close(fd);
	SHA256_hash(blob, (int)BLK, digest);
	readback("pesystem", STATE_REL * BLK, BLK, digest);
	free(blob);
}

static void write_land_state(const char *txn, const struct fit *rec, const struct fit *rom)
{
	char t[80], rc[40], recrc[40];
	const char *fields[] = { "crux_s_ver=1", t, "crux_s_stage=land", "crux_s_tries=0", rc,
				 rec ? "crux_s_rec=1" : NULL, recrc, NULL };

	snprintf(t, sizeof(t), "crux_s_txn=%s", txn);
	snprintf(rc, sizeof(rc), "crux_s_rom_crc=%08x", rom->crc);
	if (rec)
		snprintf(recrc, sizeof(recrc), "crux_s_rec_crc=%08x", rec->crc);
	write_state_fields(fields);
}

/* ---- A: PE Recovery Dev OTA ---- */

static void a_result(const char *status, const char *message)
{
	char dir[512], path[600];
	FILE *f;

	at(dir, sizeof(dir), "/metadata/crux_ota/result");
	mkdir(dir, 0700);
	snprintf(path, sizeof(path), "%s/%s.txt", dir, a_txn);
	f = fopen(path, "w");
	if (!f)
		return;
	fprintf(f, "txn=%s\nstatus=%s\nmessage=%s\n", a_txn, status, message);
	fsync(fileno(f));
	fclose(f);
}

static void map_source(const char *dir, const char *asset, struct source *s, int datafd)
{
	char path[640], *text = malloc(1 << 20), *line, *save = NULL, node[512], want[600];
	uint64_t size = 0, block = 0, total = 0;
	size_t got;
	int count = 0, i;

	if (!text)
		die("out of memory");
	snprintf(path, sizeof(path), "%s/%s.map", dir, asset);
	read_file(path, text, 1 << 20, &got);
	line = strtok_r(text, "\n", &save);
	node_of("peuserdata", node, sizeof(node));
	snprintf(want, sizeof(want), "/%s", node);
	if (!line || (!strstr(line, "/by-name/peuserdata") && !strstr(line, want)))
		die("%s block map does not name peuserdata", asset);
	line = strtok_r(NULL, "\n", &save);
	if (!line || sscanf(line, "%" SCNu64 " %" SCNu64, &size, &block) != 2 || block != BLK)
		die("%s block map has no size", asset);
	line = strtok_r(NULL, "\n", &save);
	if (!line || sscanf(line, "%d", &count) != 1 || count <= 0 || count > 1 << 20)
		die("%s block map has no ranges", asset);
	snprintf(s->name, sizeof(s->name), "%s", asset);
	s->fd = datafd;
	s->size = size;
	s->n = count;
	s->r = calloc((size_t)count, sizeof(*s->r));
	if (!s->r)
		die("out of memory");
	for (i = 0; i < count; i++) {
		uint64_t a = 0, b = 0;

		line = strtok_r(NULL, "\n", &save);
		if (!line || sscanf(line, "%" SCNu64 " %" SCNu64, &a, &b) != 2 || b <= a ||
		    b > volumes[2].blocks)
			die("%s block map range %d is invalid", asset, i);
		s->r[i].off = a * BLK;
		s->r[i].len = (b - a) * BLK;
		total += s->r[i].len;
	}
	if (total < size || total - size >= BLK)
		die("%s block map covers %" PRIu64 " bytes for a %" PRIu64 "-byte asset", asset, total, size);
	s->r[count - 1].len -= total - size;
	free(text);
}

static void a_asset(const char *dir, const char *role, struct source *s, int datafd)
{
	char key[96];
	uint8_t want[32], got[32];

	snprintf(key, sizeof(key), "asset.%s.name", role);
	map_source(dir, mget(key), s, datafd);
	snprintf(key, sizeof(key), "asset.%s.bytes", role);
	if (mnum(key) != s->size)
		die("%s size differs from the manifest", s->name);
	snprintf(key, sizeof(key), "asset.%s.sha256", role);
	mdigest(key, want);
	source_sha256(s, got);
	if (memcmp(got, want, 32))
		die("downloaded %s differs from the release manifest", s->name);
}

static void a_image(const char *dir, const char *role, const char *part, uint64_t limit, struct image *img,
		    int datafd)
{
	char key[96];
	const char *format;

	img->part = part;
	a_asset(dir, role, &img->src, datafd);
	snprintf(key, sizeof(key), "asset.%s.format", role);
	format = mget(key);
	img->format = !strcmp(format, "zstd-sparse") ? FORMAT_ZSTD_SPARSE :
		      !strcmp(format, "sparse") ? FORMAT_SPARSE : -1;
	if (img->format < 0)
		die("%s has unsupported format %s", role, format);
	snprintf(key, sizeof(key), "asset.%s.raw_bytes", role);
	img->bytes = mnum(key);
	if (!img->bytes || img->bytes % BLK || img->bytes > limit)
		die("%s image size %" PRIu64 " does not fit its volume", part, img->bytes);
	snprintf(key, sizeof(key), "asset.%s.raw_sha256", role);
	mdigest(key, img->sha);
	check_expanded(img);
}

static void prop_value(const char *props, const char *key, char *out, size_t n)
{
	size_t klen = strlen(key);
	const char *p = props;

	out[0] = '\0';
	while ((p = strstr(p, key))) {
		if ((p == props || p[-1] == '\n') && p[klen] == '=') {
			size_t len = strcspn(p + klen + 1, "\n");

			if (len < n) {
				memcpy(out, p + klen + 1, len);
				out[len] = '\0';
			}
			return;
		}
		p += klen;
	}
}

/* Read the installed system's build identity from pesystem, read-only. */
static void a_check_start(void)
{
	char path[512], props[65536], build[64], fingerprint[192];
	const char *allowed = mget("start_builds");
#ifndef CRUX_TEST
	char dev[512];

	at(dev, sizeof(dev), "/dev/block/by-name/pesystem");
	mkdir("/tmp/crux-start", 0700);
	if (mount(dev, "/tmp/crux-start", "ext4", MS_RDONLY | MS_NOATIME, NULL))
		die("cannot mount the installed pesystem read-only: %s", strerror(errno));
	snprintf(path, sizeof(path), "/tmp/crux-start/system/build.prop");
	read_file(path, props, sizeof(props), NULL);
	if (umount("/tmp/crux-start"))
		die("cannot unmount the installed pesystem: %s", strerror(errno));
#else
	at(path, sizeof(path), "/start/system/build.prop");
	read_file(path, props, sizeof(props), NULL);
#endif
	prop_value(props, "ro.build.version.incremental", build, sizeof(build));
	prop_value(props, "ro.build.fingerprint", fingerprint, sizeof(fingerprint));
	if (!build[0])
		die("the installed system has no build number");
	if (strcmp(allowed, "any")) {
		char list[300], *tok, *save = NULL;
		int ok = 0;

		snprintf(list, sizeof(list), "%s", allowed);
		for (tok = strtok_r(list, ", ", &save); tok; tok = strtok_r(NULL, ", ", &save))
			ok |= !strcmp(tok, build);
		if (!ok)
			die("installed build %s is not an allowed starting point (%s)", build, allowed);
	}
	if (!strcmp(build, mget("target_build")))
		ui("Installed build %s equals the target; reinstalling it with data kept", build);
	ui("Start: %s (%s)", build, fingerprint);
}

static int a_main(const char *txn)
{
	struct image vendor_img, system_img;
	struct fit rom = { "rom", { "", -1, 0, 0, NULL }, ROM_WINDOW_BLKS, ROM_STAGE_REL, NULL, 0 };
	char dir[512], mpath[600], spath[600], key[512], *text;
	uint8_t sig[64];
	size_t got;
	int datafd;

	mode = MODE_A;
	if (strlen(txn) >= sizeof(a_txn) || strspn(txn, "abcdefghijklmnopqrstuvwxyz0123456789-._ABCDEFGHIJKLMNOPQRSTUVWXYZ") !=
	    strlen(txn))
		die("invalid transaction ID");
	snprintf(a_txn, sizeof(a_txn), "%s", txn);
	ui("Crux PE13 Dev OTA (crux-install/1), transaction %s", txn);
	at(dir, sizeof(dir), "/metadata/crux_ota/txn/%s", txn);
	snprintf(mpath, sizeof(mpath), "%s/crux-ota-manifest.txt", dir);
	snprintf(spath, sizeof(spath), "%s/crux-ota-manifest.sig", dir);
	text = malloc(16384);
	if (!text)
		die("out of memory");
	read_file(mpath, text, 16384, &got);
	{
		char sigbuf[80];
		size_t sgot;

		read_file(spath, sigbuf, sizeof(sigbuf), &sgot);
		if (sgot != sizeof(sig))
			die("release signature is not 64 bytes");
		memcpy(sig, sigbuf, sizeof(sig));
	}
	at(key, sizeof(key), "/system/etc/crux-ota/a-p256.pub");
	verify_and_parse(text, got, sig, key);
	free(text);
	if (strcmp(mget("schema"), "crux-ota/1") || strcmp(mget("kind"), "a-ota") || strcmp(mget("device"), "crux") ||
	    strcmp(mget("data_policy"), "preserve"))
		die("this release is not a crux-ota/1 data-preserving Dev OTA");
	ui("Release: %s build %s (signature verified)", mget("product"), mget("target_build"));
	check_device();
	a_check_start();
	at(key, sizeof(key), "/dev/block/by-name/peuserdata");
	datafd = open(key, O_RDONLY);
	if (datafd < 0)
		die("cannot read peuserdata: %s", strerror(errno));
	ui("Checking the downloaded payloads...");
	a_image(dir, "vendor", "pevendor", volumes[1].blocks * BLK, &vendor_img, datafd);
	a_image(dir, "system", "pesystem", STATE_REL * BLK, &system_img, datafd);
	a_asset(dir, "rom_fit", &rom.src, datafd);
	load_fit(&rom, "asset.rom_fit");
	ui("Target: %s", mget("target_fingerprint"));
	ui("Keeps PE user data. Writes pevendor and pesystem, then stages the ROM FIT.");
	a_result("writing", "pevendor and pesystem are being written");
	writes_started = 1;
	write_image(&vendor_img, 0.0, 0.3);
	write_image(&system_img, 0.3, 0.6);
	stage_fit(&rom);
	write_land_state(txn, NULL, &rom);
	a_result("staged", "U-Boot lands the ROM FIT and starts the new PE ROM");
	ui("Done: U-Boot lands the ROM FIT for transaction %s.", txn);
	close(datafd);
	return 0;
}

/* ---- A: ROM-side preparation (the OTA helper) ---- */

struct extent {
	uint64_t logical, physical, len;
};

/*
 * Physical extents of a complete regular file on peuserdata, relative to the
 * PE userdata partition (dm-default-key maps it one to one).
 */
static int file_extents(int fd, uint64_t size, const char *name, struct extent **out)
{
#ifdef CRUX_TEST
	/* The fixture lays every asset out as two extents at fixed, distinct blocks. */
	static uint64_t next = 2000;
	uint64_t blocks = (size + BLK - 1) / BLK, first = blocks / 2 ? blocks / 2 : 1;
	struct extent *e = calloc(2, sizeof(*e));
	int n = 0;

	(void)fd;
	(void)name;
	e[n++] = (struct extent){ 0, next * BLK, first * BLK };
	if (blocks > first)
		e[n++] = (struct extent){ first * BLK, (next + first + 41) * BLK, (blocks - first) * BLK };
	next += blocks + 100;
	*out = e;
	return n;
#else
	size_t max = 4096;
	struct fiemap *fm = calloc(1, sizeof(*fm) + max * sizeof(struct fiemap_extent));
	struct extent *e = calloc(max, sizeof(*e));
	uint64_t expect = 0;
	unsigned int i;
	int n = 0;

	if (!fm || !e)
		die("out of memory");
	fm->fm_start = 0;
	fm->fm_length = ~0ULL;
	fm->fm_flags = FIEMAP_FLAG_SYNC;
	fm->fm_extent_count = (uint32_t)max;
	if (ioctl(fd, FS_IOC_FIEMAP, fm))
		die("%s: FIEMAP failed: %s", name, strerror(errno));
	if (fm->fm_mapped_extents == max)
		die("%s has too many extents", name);
	for (i = 0; i < fm->fm_mapped_extents; i++) {
		struct fiemap_extent *x = &fm->fm_extents[i];
		uint32_t bad = FIEMAP_EXTENT_UNKNOWN | FIEMAP_EXTENT_DELALLOC | FIEMAP_EXTENT_ENCODED |
			       FIEMAP_EXTENT_NOT_ALIGNED | FIEMAP_EXTENT_DATA_INLINE | FIEMAP_EXTENT_DATA_TAIL |
			       FIEMAP_EXTENT_UNWRITTEN;

		if (x->fe_flags & bad || x->fe_logical != expect || x->fe_physical % BLK || x->fe_length % BLK)
			die("%s has an extent that cannot be read from raw blocks (flags %x)", name, x->fe_flags);
		e[n++] = (struct extent){ x->fe_logical, x->fe_physical, x->fe_length };
		expect += x->fe_length;
	}
	if (expect < size)
		die("%s is not fully allocated", name);
	free(fm);
	*out = e;
	return n;
#endif
}

/* Rewrite one asset's plaintext into its own raw blocks and write its uncrypt map. */
static void prepare_asset(const char *asset_dir, const char *txn_dir, const char *role, int rawfd)
{
	char key[96], path[640], map[640], line[96];
	const char *name;
	uint8_t want[32], got[32], *buf = aligned(CHUNK);
	struct extent *e = NULL;
	struct stat st;
	struct source src;
	uint64_t size;
	FILE *f;
	int fd, n, i, ranges = 0;

	snprintf(key, sizeof(key), "asset.%s.name", role);
	name = mget(key);
	if (strchr(name, '/'))
		die("asset name %s is not a file name", name);
	snprintf(key, sizeof(key), "asset.%s.bytes", role);
	size = mnum(key);
	snprintf(key, sizeof(key), "asset.%s.sha256", role);
	mdigest(key, want);
	snprintf(path, sizeof(path), "%s/%s", asset_dir, name);
	fd = open(path, O_RDONLY);
	if (fd < 0 || fstat(fd, &st) || !S_ISREG(st.st_mode) || (uint64_t)st.st_size != size)
		die("%s is missing or has the wrong size", name);
	src = (struct source){ "", fd, size, 1, calloc(1, sizeof(struct range)) };
	snprintf(src.name, sizeof(src.name), "%s", name);
	src.r[0].len = size;
	source_sha256(&src, got);
	if (memcmp(got, want, 32))
		die("downloaded %s differs from the release manifest", name);
	n = file_extents(fd, size, name, &e);
	for (i = 0; i < n; i++) {
		uint64_t done = 0;

		while (done < e[i].len) {
			uint64_t left = e[i].len - done;
			size_t step = left < CHUNK ? (size_t)left : CHUNK, data = step;

			if (e[i].logical + done >= size)
				break;
			if (e[i].logical + done + step > size)
				data = (size_t)(size - e[i].logical - done);
			memset(buf, 0, step);
			read_exact(fd, buf, data, e[i].logical + done);
			write_exact(rawfd, buf, step, e[i].physical + done);
			done += step;
		}
	}
	if (fsync(rawfd))
		die("peuserdata sync failed");
	close(fd);
	snprintf(map, sizeof(map), "%s/%s.map", txn_dir, name);
	f = fopen(map, "w");
	if (!f)
		die("cannot write %s: %s", map, strerror(errno));
	for (i = 0; i < n; i++)
		if (!i || e[i].physical != e[i - 1].physical + e[i - 1].len)
			ranges++;
	fprintf(f, "/dev/block/bootdevice/by-name/peuserdata\n%" PRIu64 " %" PRIu64 "\n%d\n", size, (uint64_t)BLK, ranges);
	for (i = 0; i < n; ) {
		uint64_t start = e[i].physical, end = e[i].physical + e[i].len;

		for (i++; i < n && e[i].physical == end; i++)
			end += e[i].len;
		snprintf(line, sizeof(line), "%" PRIu64 " %" PRIu64 "\n", (uint64_t)(start / BLK), (uint64_t)(end / BLK));
		fputs(line, f);
	}
	if (fflush(f) || fsync(fileno(f)))
		die("cannot write %s", map);
	fclose(f);
	free(e);
	free(src.r);
	free(buf);
	map_source(txn_dir, name, &src, rawfd);
	source_sha256(&src, got);
	if (memcmp(got, want, 32))
		die("raw blocks of %s do not read back as the asset", name);
	free(src.r);
	ui("%s mapped to raw peuserdata and read back", name);
}

static void copy_file(const char *from, const char *to)
{
	char *buf = malloc(16384);
	size_t got;
	FILE *f;

	if (!buf)
		die("out of memory");
	read_file(from, buf, 16384, &got);
	f = fopen(to, "w");
	if (!f || fwrite(buf, 1, got, f) != got || fflush(f) || fsync(fileno(f)))
		die("cannot write %s", to);
	fclose(f);
	free(buf);
}

static void write_request(const char *txn)
{
	uint8_t blob[BLK];
	char *p = (char *)blob + 4, path[512], tmp[540];
	uint32_t crc;
	FILE *f;

	memset(blob, 0, sizeof(blob));
	p += sprintf(p, "crux_r_ver=1") + 1;
	p += sprintf(p, "crux_r_txn=%s", txn) + 1;
	sprintf(p, "crux_r_action=install");
	crc = crc32_update(0, blob + 4, BLK - 4);
	blob[0] = (uint8_t)crc;
	blob[1] = (uint8_t)(crc >> 8);
	blob[2] = (uint8_t)(crc >> 16);
	blob[3] = (uint8_t)(crc >> 24);
	at(path, sizeof(path), "/metadata/crux_ota/request");
	snprintf(tmp, sizeof(tmp), "%s.tmp", path);
	f = fopen(tmp, "w");
	if (!f || fwrite(blob, 1, sizeof(blob), f) != sizeof(blob) || fflush(f) || fsync(fileno(f)))
		die("cannot write the U-Boot request");
	fclose(f);
	if (rename(tmp, path))
		die("cannot publish the U-Boot request: %s", strerror(errno));
}

static int prepare_main(const char *asset_dir, const char *txn, const char *key_arg)
{
	char mpath[640], spath[640], key[512], txn_dir[512], dir[768], cmdline[4096], value[128];
	char props[65536], build[64], *text;
	uint8_t sig[64];
	size_t got;
	int rawfd;

	mode = MODE_PREPARE;
	if (strlen(txn) >= sizeof(a_txn) ||
	    strspn(txn, "abcdefghijklmnopqrstuvwxyz0123456789-._ABCDEFGHIJKLMNOPQRSTUVWXYZ") != strlen(txn))
		die("invalid transaction ID");
	ui("Preparing Crux PE13 Dev OTA transaction %s", txn);
	snprintf(mpath, sizeof(mpath), "%s/crux-ota-manifest.txt", asset_dir);
	snprintf(spath, sizeof(spath), "%s/crux-ota-manifest.sig", asset_dir);
	text = malloc(16384);
	if (!text)
		die("out of memory");
	read_file(mpath, text, 16384, &got);
	{
		char sigbuf[80];
		size_t sgot;

		read_file(spath, sigbuf, sizeof(sigbuf), &sgot);
		if (sgot != sizeof(sig))
			die("release signature is not 64 bytes");
		memcpy(sig, sigbuf, sizeof(sig));
	}
	if (key_arg)
		snprintf(key, sizeof(key), "%s", key_arg);
	else
		at(key, sizeof(key), "/system/etc/crux-ota/a-p256.pub");
	verify_and_parse(text, got, sig, key);
	free(text);
	if (strcmp(mget("schema"), "crux-ota/1") || strcmp(mget("kind"), "a-ota") || strcmp(mget("device"), "crux") ||
	    strcmp(mget("data_policy"), "preserve"))
		die("this release is not a crux-ota/1 data-preserving Dev OTA");
	at(dir, sizeof(dir), "/proc/cmdline");
	read_file(dir, cmdline, sizeof(cmdline), NULL);
	if (cmdline_value(cmdline, "androidboot.serialno", value, sizeof(value)) ||
	    (strcmp(mget("serialno"), "any") && strcmp(mget("serialno"), value)))
		die("this release is not for serial %s", value);
	at(dir, sizeof(dir), "/system/build.prop");
	read_file(dir, props, sizeof(props), NULL);
	prop_value(props, "ro.build.version.incremental", build, sizeof(build));
	{
		char list[300], *tok, *save = NULL;
		int ok = !strcmp(mget("start_builds"), "any");

		snprintf(list, sizeof(list), "%s", mget("start_builds"));
		for (tok = strtok_r(list, ", ", &save); tok; tok = strtok_r(NULL, ", ", &save))
			ok |= !strcmp(tok, build);
		if (!ok)
			die("running build %s is not an allowed starting point", build);
	}
	ui("Release %s for running build %s (signature verified)", mget("target_build"), build);
	at(dir, sizeof(dir), "/metadata/crux_ota");
	mkdir(dir, 0700);
	at(dir, sizeof(dir), "/metadata/crux_ota/txn");
	mkdir(dir, 0700);
	/* PE Recovery may add files here but not create directories (SELinux). */
	at(dir, sizeof(dir), "/metadata/crux_ota/result");
	mkdir(dir, 0700);
	at(txn_dir, sizeof(txn_dir), "/metadata/crux_ota/txn/%s", txn);
	if (mkdir(txn_dir, 0700) && errno != EEXIST)
		die("cannot create %s: %s", txn_dir, strerror(errno));
	at(dir, sizeof(dir), "/dev/block/by-name/peuserdata");
	rawfd = open(dir, O_RDWR);
	if (rawfd < 0)
		die("cannot open peuserdata: %s", strerror(errno));
	prepare_asset(asset_dir, txn_dir, "system", rawfd);
	prepare_asset(asset_dir, txn_dir, "vendor", rawfd);
	prepare_asset(asset_dir, txn_dir, "rom_fit", rawfd);
	close(rawfd);
	snprintf(dir, sizeof(dir), "%s/crux-ota-manifest.txt", txn_dir);
	copy_file(mpath, dir);
	snprintf(dir, sizeof(dir), "%s/crux-ota-manifest.sig", txn_dir);
	copy_file(spath, dir);
	write_request(txn);
	sync();
	ui("Prepared: reboot to recovery to install transaction %s.", txn);
	return 0;
}

/* ---- A: init services of the updater app ---- */

static void write_text(const char *path, const char *text)
{
	char tmp[700];
	FILE *f;

	snprintf(tmp, sizeof(tmp), "%s.tmp", path);
	f = fopen(tmp, "w");
	if (!f || fputs(text, f) < 0 || fflush(f) || fsync(fileno(f)))
		die("cannot write %s", path);
	fclose(f);
	chmod(tmp, 0644);
	if (rename(tmp, path))
		die("cannot publish %s", path);
}

static void remove_tree(const char *dir)
{
#ifndef CRUX_TEST
	DIR *d = opendir(dir);
	struct dirent *de;
	char path[1024];

	while (d && (de = readdir(d))) {
		if (!strcmp(de->d_name, ".") || !strcmp(de->d_name, ".."))
			continue;
		snprintf(path, sizeof(path), "%s/%s", dir, de->d_name);
		unlink(path);
	}
	if (d)
		closedir(d);
	rmdir(dir);
#else
	char cmd[1100];

	snprintf(cmd, sizeof(cmd), "rm -rf '%s'", dir);
	if (system(cmd))
		ui("cannot remove %s", dir);
#endif
}

static int service_prepare(void)
{
	char path[512], txn[64], dir[600];
	size_t got;

	at(path, sizeof(path), "/data/ota_package/crux/pending");
	read_file(path, txn, sizeof(txn), &got);
	txn[strcspn(txn, "\n")] = '\0';
	at(dir, sizeof(dir), "/data/ota_package/crux/%s", txn);
	snprintf(status_path, sizeof(status_path), "%s/status", dir);
	unlink(status_path);
	prepare_main(dir, txn, NULL);
	write_text(status_path, "prepared\n");
	unlink(path);
	return 0;
}

/* At boot: report the last transaction to the app and retire it once it has finished. */
static int service_result(void)
{
	char path[600], request[BLK], vals[R_COUNT_A][64], props[65536], build[64], dir[600];
	char state_txt[512] = "", result[2048] = "", report[4096];
	uint8_t blk[BLK];
	size_t got;
	int fd, i;
	const char *stage = "none", *state_txn = "";
	char state_kv[8][2][64];
	int nstate = 0;

	mode = MODE_PREPARE;
	at(path, sizeof(path), "/metadata/crux_ota/request");
	fd = open(path, O_RDONLY);
	if (fd < 0)
		return 0;
	if (read(fd, request, sizeof(request)) != (ssize_t)sizeof(request)) {
		close(fd);
		return 0;
	}
	close(fd);
	memset(vals, 0, sizeof(vals));
	for (i = 4; i < (int)sizeof(request) && request[i]; i += (int)strlen(request + i) + 1) {
		if (!strncmp(request + i, "crux_r_txn=", 11))
			snprintf(vals[0], sizeof(vals[0]), "%s", request + i + 11);
	}
	if (!vals[0][0])
		return 0;
	at(path, sizeof(path), "/dev/block/by-name/pesystem");
	fd = open(path, O_RDONLY);
	if (fd >= 0 && pread(fd, blk, sizeof(blk), (off_t)(STATE_REL * BLK)) == (ssize_t)sizeof(blk) &&
	    crc32_update(0, blk + 4, BLK - 4) == le32(blk)) {
		for (i = 4; i < (int)BLK && blk[i] && nstate < 8; i += (int)strlen((char *)blk + i) + 1) {
			char *eq = strchr((char *)blk + i, '=');

			if (!eq)
				break;
			snprintf(state_kv[nstate][0], 64, "%.*s", (int)(eq - (char *)blk - i), (char *)blk + i);
			snprintf(state_kv[nstate][1], 64, "%s", eq + 1);
			nstate++;
		}
	}
	if (fd >= 0)
		close(fd);
	for (i = 0; i < nstate; i++) {
		if (!strcmp(state_kv[i][0], "crux_s_txn"))
			state_txn = state_kv[i][1];
		if (!strcmp(state_kv[i][0], "crux_s_stage"))
			stage = state_kv[i][1];
	}
	if (strcmp(state_txn, vals[0]))
		stage = "pending";
	snprintf(state_txt, sizeof(state_txt), "stage=%s\n", stage);
	at(path, sizeof(path), "/metadata/crux_ota/result/%s.txt", vals[0]);
	if (access(path, R_OK) == 0)
		read_file(path, result, sizeof(result), &got);
	at(path, sizeof(path), "/system/build.prop");
	read_file(path, props, sizeof(props), NULL);
	prop_value(props, "ro.build.version.incremental", build, sizeof(build));
	snprintf(report, sizeof(report), "txn=%s\n%srunning_build=%s\n%s", vals[0], state_txt, build, result);
	at(path, sizeof(path), "/data/ota_package/crux/last-result");
	write_text(path, report);
	if (!strcmp(stage, "done") || !strcmp(stage, "failed")) {
		at(path, sizeof(path), "/metadata/crux_ota/request");
		unlink(path);
		at(dir, sizeof(dir), "/data/ota_package/crux/%s", vals[0]);
		remove_tree(dir);
		ui("transaction %s finished (%s); request and download removed", vals[0], stage);
	}
	return 0;
}

/* ---- B: TWRP ZIP clean install ---- */

static void b_image(struct image *img, const char *part, uint64_t limit)
{
	char key[96];
	uint8_t want[32], got[32];

	img->part = part;
	img->format = FORMAT_ZSTD_RAW;
	snprintf(key, sizeof(key), "image.%s.entry", part);
	zip_source(mget(key), &img->src);
	snprintf(key, sizeof(key), "image.%s.compression", part);
	if (strcmp(mget(key), "zstd"))
		die("%s image is not zstd", part);
	snprintf(key, sizeof(key), "image.%s.stored_bytes", part);
	if (mnum(key) != img->src.size)
		die("%s stored size differs", part);
	snprintf(key, sizeof(key), "image.%s.stored_sha256", part);
	mdigest(key, want);
	source_sha256(&img->src, got);
	if (memcmp(got, want, 32))
		die("%s differs from the manifest", img->src.name);
	snprintf(key, sizeof(key), "image.%s.bytes", part);
	img->bytes = mnum(key);
	if (!img->bytes || img->bytes % BLK || img->bytes > limit)
		die("%s image size %" PRIu64 " does not fit its volume", part, img->bytes);
	snprintf(key, sizeof(key), "image.%s.sha256", part);
	mdigest(key, img->sha);
	check_expanded(img);
}

static void b_fit(struct fit *f)
{
	char key[96];

	snprintf(key, sizeof(key), "fit.%s.entry", f->role);
	zip_source(mget(key), &f->src);
	snprintf(key, sizeof(key), "fit.%s", f->role);
	load_fit(f, key);
}

#ifndef CRUX_TEST
static void request_reboot(void)
{
	char setprop[512];
	char *argv[] = { setprop, "sys.powerctl", "reboot,recovery", NULL };
	char out[256];

	at(setprop, sizeof(setprop), "/system/bin/setprop");
	ui("Rebooting to the Recovery container U-Boot, which lands both FITs and starts PE.");
	sleep(3);
	if (run_tool(argv, out, sizeof(out)))
		ui("Automatic reboot failed: choose Reboot > Recovery in TWRP to finish.");
}
#endif

static int b_main(const char *zip)
{
	struct image system_img, vendor_img;
	struct fit rec = { "recovery", { "", -1, 0, 0, NULL }, REC_WINDOW_BLKS, REC_STAGE_REL, NULL, 0 };
	struct fit rom = { "rom", { "", -1, 0, 0, NULL }, ROM_WINDOW_BLKS, ROM_STAGE_REL, NULL, 0 };
	struct source m, s;
	char txn[48], path[512], *text;
	uint8_t sig[64], random[4] = { 0 };
	int fd;

	ui("Crux PE13 B installer (crux-install/1)");
	zip_open(zip);
	zip_source("crux-install.txt", &m);
	zip_source("crux-install.sig", &s);
	if (m.size > 16384 || s.size != sizeof(sig))
		die("unexpected manifest or signature size");
	text = malloc(m.size + 1);
	if (!text)
		die("out of memory");
	source_read(&m, 0, text, m.size);
	source_read(&s, 0, sig, sizeof(sig));
	at(path, sizeof(path), "/crux/install-keys/b-p256.pub");
	verify_and_parse(text, m.size, sig, path);
	free(text);
	if (strcmp(mget("schema"), "crux-install/1") || strcmp(mget("kind"), "b-zip") ||
	    strcmp(mget("device"), "crux") || strcmp(mget("data_policy"), "discard"))
		die("this ZIP is not a crux-install/1 B clean-install package");
	ui("Package: %s build %s (signature verified)", mget("product"), mget("target_build"));
	check_device();
	ui("Checking payloads...");
	b_image(&system_img, "pesystem", STATE_REL * BLK);
	b_image(&vendor_img, "pevendor", volumes[1].blocks * BLK);
	b_fit(&rec);
	b_fit(&rom);
	ui("Target: %s", mget("target_fingerprint"));
	ui("Marker: %s", mget("target_marker"));
	ui("Writes pesystem and pevendor, ERASES PE userdata and metadata,");
	ui("stages both PE FITs for U-Boot. Original MIUI partitions are not touched.");
	if (!confirm())
		die("installation cancelled");
	writes_started = 1;
	progress(0.0, 0);
	write_image(&system_img, 0.0, 0.6);
	write_image(&vendor_img, 0.6, 0.25);
	format_volume("peuserdata", volumes[2].blocks);
	format_volume("pemetadata", volumes[3].blocks);
	stage_fit(&rec);
	stage_fit(&rom);
	fd = open("/dev/urandom", O_RDONLY);
	if (fd >= 0) {
		if (read(fd, random, sizeof(random)) != sizeof(random))
			memset(random, 0, sizeof(random));
		close(fd);
	}
	snprintf(txn, sizeof(txn), "b%s-%02x%02x%02x%02x", mget("target_build"),
		 random[0], random[1], random[2], random[3]);
	write_land_state(txn, &rec, &rom);
	progress(1.0, 0);
	ui("Done: transaction %s is ready for U-Boot to land.", txn);
#ifdef CRUX_TEST
	{
		FILE *f;

		at(path, sizeof(path), "/reboot-request");
		f = fopen(path, "w");
		if (f) {
			fprintf(f, "%s\n", txn);
			fclose(f);
		}
	}
#else
	request_reboot();
#endif
	return 0;
}

int main(int argc, char **argv)
{
#ifdef CRUX_TEST
	while (argc > 2 && !strncmp(argv[1], "--", 2) && strcmp(argv[1], "--a-recovery") &&
	       strcmp(argv[1], "--a-prepare") && strcmp(argv[1], "--a-service")) {
		if (!strcmp(argv[1], "--root"))
			root = argv[2];
		else if (!strcmp(argv[1], "--confirm"))
			confirm_mode = !strcmp(argv[2], "yes");
		argc -= 2;
		argv += 2;
	}
	if (argc == 3 && !strcmp(argv[1], "--a-recovery"))
		return a_main(argv[2]);
	if ((argc == 4 || argc == 5) && !strcmp(argv[1], "--a-prepare"))
		return prepare_main(argv[2], argv[3], argc == 5 ? argv[4] : NULL);
	if (argc == 3 && !strcmp(argv[1], "--a-service"))
		return !strcmp(argv[2], "prepare") ? service_prepare() : !strcmp(argv[2], "result") ? service_result() : 2;
	if (argc != 2)
		return 2;
	return b_main(argv[1]);
#else
	if (argc == 3 && !strcmp(argv[1], "--a-recovery"))
		return a_main(argv[2]);
	if ((argc == 4 || argc == 5) && !strcmp(argv[1], "--a-prepare"))
		return prepare_main(argv[2], argv[3], argc == 5 ? argv[4] : NULL);
	if (argc == 3 && !strcmp(argv[1], "--a-service"))
		return !strcmp(argv[2], "prepare") ? service_prepare() : !strcmp(argv[2], "result") ? service_result() : 2;
	if (argc != 4)
		return 2;
	ui_pipe = fdopen(atoi(argv[2]), "w");
	return b_main(argv[3]);
#endif
}
