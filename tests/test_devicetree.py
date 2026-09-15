"""Build Crux DTs using the kernel's own dtc and apply each supported base."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

DEVICE = Path(__file__).absolute().parents[1]
KERNEL = Path(os.environ.get("CRUX_KERNEL_SOURCE", DEVICE.parents[2] / "kernel/xiaomi/crux"))
BASES = ("sm8150", "sm8150-v2", "sm8150p", "sm8150p-v2")


def run(command, **kwargs):
    result = subprocess.run(command, text=True, capture_output=True, **kwargs)
    if result.returncode:
        raise AssertionError(f"{command[0]} failed ({result.returncode}):\n{result.stderr}")
    return result


class CruxDeviceTreeTest(unittest.TestCase):
    def test_ramdisk_fstab_selected_on_every_supported_base(self):
        cc = shutil.which("cc")
        if not cc or not KERNEL.is_dir():
            self.skipTest("Host C compiler and Crux kernel checkout required")
        dtc_source = KERNEL / "scripts/dtc"
        libfdt = dtc_source / "libfdt"
        with tempfile.TemporaryDirectory(prefix="crux-dt-test-") as temporary:
            out = Path(temporary)
            for filename in ("dtc-parser.tab.c", "dtc-parser.tab.h", "dtc-lexer.lex.c"):
                shutil.copyfile(dtc_source / (filename + "_shipped"), out / filename)
            sources = "checks.c data.c dtc.c flattree.c fstree.c livetree.c srcpos.c treesource.c util.c".split()
            run([cc, "-O2", "-fcommon", f"-I{dtc_source}", f"-I{libfdt}", f"-I{out}"] +
                [str(dtc_source / name) for name in sources] +
                [str(out / name) for name in ("dtc-parser.tab.c", "dtc-lexer.lex.c")] +
                ["-o", str(out / "dtc")])
            for name in (*BASES, "crux-sm8150-overlay"):
                source = KERNEL / "arch/arm64/boot/dts/qcom" / (name + ".dts")
                preprocessed = out / (name + ".dts")
                run([cc, "-E", "-nostdinc", f"-I{dtc_source / 'include-prefixes'}",
                     "-undef", "-D__DTS__", "-x", "assembler-with-cpp",
                     str(source), "-o", str(preprocessed)])
                run([str(out / "dtc"), "-@", "-I", "dts", "-O", "dtb",
                     "-o", str(out / (name + ".dtb")), str(preprocessed)])
            check = out / "check_overlay.c"
            check.write_text(r"""
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "libfdt.h"
static void *load(const char *path) {
 FILE *f = fopen(path, "rb");
 if (!f) exit(2);
 fseek(f, 0, SEEK_END);
 long size = ftell(f);
 rewind(f);
 void *data = malloc(size);
 if (fread(data, 1, size, f) != (size_t)size) exit(2);
 fclose(f);
 return data;
}
int main(int argc, char **argv) {
 if (argc != 3) return 2;
 void *base = load(argv[1]), *overlay = load(argv[2]);
 void *merged = malloc(4 * 1024 * 1024);
 int result = fdt_open_into(base, merged, 4 * 1024 * 1024);
 if (!result) result = fdt_overlay_apply(merged, overlay);
 if (result) {
  fprintf(stderr, "Cannot apply Crux overlay: %s\n", fdt_strerror(result));
  return 1;
 }
 int node = fdt_path_offset(merged, "/firmware/android/fstab");
 const char *status = fdt_getprop(merged, node, "status", NULL);
 if (!status || strcmp(status, "disabled")) {
  fprintf(stderr, "DT fstab is still selected ahead of ramdisk fstab\n");
  return 1;
 }
 if (fdt_path_offset(merged, "/firmware/android/vbmeta") < 0) {
  fprintf(stderr, "AVB metadata node was removed\n");
  return 1;
 }
 if (fdt_path_offset(merged, "/soc/ufshc@1d84000") < 0) {
  fprintf(stderr, "Expected Crux UFS controller missing\n");
  return 1;
 }
 return 0;
}
""")
            sources = ("fdt.c fdt_ro.c fdt_wip.c fdt_sw.c fdt_rw.c fdt_strerror.c "
                       "fdt_empty_tree.c fdt_addresses.c fdt_overlay.c").split()
            run([cc, "-O2", f"-I{libfdt}", str(check)] +
                [str(libfdt / name) for name in sources] + ["-o", str(out / "check_overlay")])
            for name in BASES:
                with self.subTest(base=name):
                    run([str(out / "check_overlay"), str(out / (name + ".dtb")),
                         str(out / "crux-sm8150-overlay.dtb")])


if __name__ == "__main__":
    unittest.main()
