"""Compile the production mode handlers with host-side boundary fakes.

This exercises state transitions and FTS mask/error handling without an Android
build tree; generated Binder interfaces and hardware remain build/device checks.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

DEVICE = Path(__file__).absolute().parents[1]
KERNEL = Path(os.environ.get("CRUX_KERNEL_SOURCE", DEVICE.parents[2] / "kernel/xiaomi/crux"))


def function(source, signature):
    start = source.index(signature)
    opening = source.index("{", start)
    depth = 1
    end = opening + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


def compile_and_run(code, language):
    compiler = shutil.which("c++" if language == "c++" else "cc")
    if compiler is None:
        raise unittest.SkipTest("Host compiler unavailable")
    with tempfile.TemporaryDirectory(prefix="crux-power-test-") as temporary:
        source = Path(temporary) / ("test.cpp" if language == "c++" else "test.c")
        executable = Path(temporary) / "test"
        source.write_text(code)
        subprocess.run([compiler, "-std=c++17" if language == "c++" else "-std=gnu11",
                        str(source), "-o", str(executable)], check=True)
        subprocess.run([str(executable)], check=True)


class PowerModeTest(unittest.TestCase):
    def test_sustained_performance_and_double_tap_errors(self):
        source = (DEVICE / "power-libperfmgr/aidl/Power.cpp").read_text()
        handlers = function(source, "ndk::ScopedAStatus Power::setMode(")
        handlers += function(source, "ndk::ScopedAStatus Power::setBoost(")
        compile_and_run(r"""
#include <cassert>
#include <chrono>
#include <memory>
#include <sstream>
#include <string>
#include <vector>
#define LOG(x) std::ostringstream()
#define PLOG(x) std::ostringstream()
#define ATRACE_INT(a,b) ((void)0)
#define EX_ILLEGAL_STATE 5
#define TARGET_TAP_TO_WAKE_NODE "touch-node"
namespace ndk {
struct ScopedAStatus {
 int error;
 static ScopedAStatus ok() { return {0}; }
 static ScopedAStatus fromExceptionCode(int error) { return {error}; }
};
}
enum class Mode {DOUBLE_TAP_TO_WAKE, LOW_POWER, SUSTAINED_PERFORMANCE, LAUNCH,
 FIXED_PERFORMANCE, EXPENSIVE_RENDERING, INTERACTIVE, DEVICE_IDLE,
 DISPLAY_INACTIVE, AUDIO_STREAMING_LOW_LATENCY};
enum class Boost {INTERACTION, DISPLAY_UPDATE_IMMINENT, ML_ACC, AUDIO_LAUNCH};
std::string toString(Mode mode) {
 return mode == Mode::SUSTAINED_PERFORMANCE ? "SUSTAINED_PERFORMANCE" : "mode";
}
std::string toString(Boost) { return "boost"; }
namespace android::base {
static bool writeOk = true;
static std::string lastValue;
bool WriteStringToFile(const std::string &value, const char *, bool) {
 lastValue = value; return writeOk;
}
}
struct HintManager {
 std::vector<std::string> started, ended;
 void DoHint(const std::string &hint) { started.push_back(hint); }
 void DoHint(const std::string &hint, std::chrono::milliseconds) { DoHint(hint); }
 void EndHint(const std::string &hint) { ended.push_back(hint); }
};
struct InteractionHandler { int count = 0; void Acquire(int) { ++count; } };
struct PowerSessionManager {
 static PowerSessionManager *getInstance() { static PowerSessionManager p; return &p; }
 void updateHintMode(const std::string &, bool) {}
};
struct Power {
 std::shared_ptr<HintManager> mHintManager = std::make_shared<HintManager>();
 std::shared_ptr<InteractionHandler> mInteractionHandler = std::make_shared<InteractionHandler>();
 bool mSustainedPerfModeOn = false;
 ndk::ScopedAStatus setMode(Mode, bool);
 ndk::ScopedAStatus setBoost(Boost, int32_t);
};
""" + handlers + r"""
int main() {
 Power p;
 assert(p.setMode(Mode::SUSTAINED_PERFORMANCE, true).error == 0);
 assert(p.mSustainedPerfModeOn);
 assert(p.mHintManager->started.back() == "SUSTAINED_PERFORMANCE");
 p.setBoost(Boost::INTERACTION, 100);
 p.setMode(Mode::LAUNCH, true);
 assert(p.mInteractionHandler->count == 0);
 assert(p.mHintManager->started.size() == 1);
 assert(p.setMode(Mode::SUSTAINED_PERFORMANCE, false).error == 0);
 assert(!p.mSustainedPerfModeOn);
 assert(p.mHintManager->ended.back() == "SUSTAINED_PERFORMANCE");
 p.setBoost(Boost::INTERACTION, 100);
 p.setMode(Mode::LAUNCH, true);
 assert(p.mInteractionHandler->count == 1);
 assert(p.mHintManager->started.size() == 2);
 assert(p.setMode(Mode::DOUBLE_TAP_TO_WAKE, true).error == 0);
 assert(android::base::lastValue == "1");
 assert(p.setMode(Mode::DOUBLE_TAP_TO_WAKE, false).error == 0);
 assert(android::base::lastValue == "0");
 android::base::writeOk = false;
 assert(p.setMode(Mode::DOUBLE_TAP_TO_WAKE, true).error == EX_ILLEGAL_STATE);
}
""", "c++")

    def test_fts_sysfs_publication_and_teardown_order(self):
        if not KERNEL.is_dir():
            self.skipTest("Crux kernel checkout unavailable")
        source = (KERNEL / "drivers/input/touchscreen/fts_521/fts.c").read_text()
        probe = source[source.index("static int fts_probe("):source.index("static int fts_remove(")]
        remove = source[source.index("static int fts_remove("):]
        publication = probe.index("device_create_with_groups(")
        registration = probe.index("xiaomitouch_register_modedata(")
        self.assertLess(probe.index("mutex_init(&(info->fod_mutex))"), publication)
        self.assertLess(publication, registration)
        # Attribute creation failure must use the existing pre-registration unwind.
        self.assertNotIn("goto ", probe[registration:probe.index("return OK;", registration)])
        self.assertIn("fts_touch_groups", probe[publication:registration])
        self.assertNotRegex(source, r"sysfs_create_file\([^;]*dev_attr_double_tap")
        self.assertNotRegex(source, r"sysfs_remove_file\([^;]*dev_attr_double_tap")
        unwind = probe[probe.index("ProbeErrorExit_8:"):probe.index("ProbeErrorExit_7:")]
        self.assertIn("if (info->fts_touch_dev)", unwind)
        self.assertIn("device_unregister(info->fts_touch_dev)", unwind)
        self.assertLess(remove.index("get_device(info->fts_touch_dev)"),
                        remove.index("device_unregister(info->fts_touch_dev)"))
        self.assertLess(remove.index("destroy_workqueue(info->event_wq)"),
                        remove.index("put_device(touch_dev)"))
        self.assertLess(remove.index("destroy_workqueue(info->touch_feature_wq)"),
                        remove.index("put_device(touch_dev)"))
        self.assertLess(remove.index("device_unregister(info->fts_touch_dev)"),
                        remove.index("destroy_workqueue("))
        self.assertLess(remove.index("device_unregister(info->fts_touch_dev)"),
                        remove.index("kfree(info);"))

    def test_fts_probe_dma_cleanup_handles_null_and_frees_children_first(self):
        if not KERNEL.is_dir():
            self.skipTest("Crux kernel checkout unavailable")
        source = (KERNEL / "drivers/input/touchscreen/fts_521/fts.c").read_text()
        unwind = source.split("ProbeErrorExit_7:", 1)[1]
        cleanup = unwind.split("#ifdef CONFIG_I2C_BY_DMA", 1)[1].split("#endif", 1)[0]
        compile_and_run(r"""
#include <assert.h>
#include <stddef.h>
struct dma_buffers { void *rdBuf; void *wrBuf; };
struct fts_ts_info { struct dma_buffers *dma_buf; };
static void *freed[3];
static int count;
static void kfree(void *pointer) { freed[count++] = pointer; }
static void cleanup(struct fts_ts_info *info) {
""" + cleanup + r"""
}
int main(void) {
 struct fts_ts_info info = {NULL};
 cleanup(&info);
 assert(count == 0);
 char read_buffer, write_buffer;
 struct dma_buffers buffers = {&read_buffer, &write_buffer};
 info.dma_buf = &buffers;
 cleanup(&info);
 assert(count == 3 && freed[0] == &read_buffer && freed[1] == &write_buffer);
 assert(freed[2] == &buffers);
}
""", "c")

    def test_fts_double_tap_preserves_other_gestures_and_reports_errors(self):
        if not KERNEL.is_dir():
            self.skipTest("Crux kernel checkout unavailable")
        source = (KERNEL / "drivers/input/touchscreen/fts_521/fts.c").read_text()
        handler = function(source, "static int fts_set_double_tap(")
        gestures = (KERNEL / "drivers/input/touchscreen/fts_521/fts_lib/ftsGesture.c").read_text()
        getter = function(gestures, "int isGestureActive(")
        event_gate = source[source.index("case GEST_ID_DBLTAP:"):source.index("case GEST_ID_AT:")]
        self.assertIn("!isGestureActive(GEST_ID_DBLTAP)", event_gate)
        mode = function(source, "static int fts_mode_handler(struct fts_ts_info *info, int force)\n{")
        self.assertIn("double_tap_enabled = isGestureActive(GEST_ID_DBLTAP)", mode)
        self.assertIn("if (double_tap_enabled)", mode)
        compile_and_run(r"""
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <errno.h>
#include <stddef.h>
typedef uint8_t u8;
#define GESTURE_MASK_SIZE 4
#define GEST_ID_DBLTAP 5
#define FEAT_SEL_GESTURE 0
#define FEAT_ENABLE 1
#define FEAT_DISABLE 0
#define OK 0
struct fts_ts_info { int gesture_enabled; };
static int fts_double_tap_lock, lock_depth, cover_mode, update_error, mode_error, applies;
static int gestureMask_mutex;
static u8 gesture_mask[GESTURE_MASK_SIZE];
#define active_mask gesture_mask[0]
static void mutex_lock(int *lock) { assert(!lock_depth); lock_depth = 1; }
static void mutex_unlock(int *lock) { assert(lock_depth); lock_depth = 0; }
static int check_feature_feasibility(struct fts_ts_info *info, int feat) { return cover_mode ? -1 : 0; }
static void fromIDtoMask(int id, u8 *mask, size_t size) { mask[id / 8] |= 1U << (id % 8); }
static int updateGestureMask(u8 *mask, size_t size, int enabled) {
 assert(lock_depth);
 if(update_error) return -1;
 if(enabled) active_mask |= mask[0]; else active_mask &= ~mask[0];
 return 0;
}
static int isAnyGestureActive(void) { return active_mask != 0; }
static int fts_mode_handler(struct fts_ts_info *info, int force) {
 assert(lock_depth); ++applies; return mode_error ? -1 : 0;
}
""" + getter + handler + r"""
int main(void) {
 struct fts_ts_info info = {0};
 active_mask = 2;
 assert(fts_set_double_tap(&info, true) == 0);
 assert(active_mask == 0x22 && info.gesture_enabled && applies == 1);
 assert(isGestureActive(GEST_ID_DBLTAP));
 assert(!isGestureActive(32));
 assert(fts_set_double_tap(&info, false) == 0);
 assert(active_mask == 2 && info.gesture_enabled && applies == 2);
 assert(!isGestureActive(GEST_ID_DBLTAP));
 assert(isGestureActive(1));
 active_mask = 0x20;
 assert(fts_set_double_tap(&info, false) == 0);
 assert(!active_mask && !info.gesture_enabled);
 cover_mode = 1;
 assert(fts_set_double_tap(&info, true) == -EBUSY);
 assert(!lock_depth && !active_mask);
 assert(fts_set_double_tap(&info, false) == 0);
 cover_mode = 0; update_error = 1;
 assert(fts_set_double_tap(&info, true) == -EIO);
 assert(!lock_depth);
 update_error = 0; mode_error = 1;
 assert(fts_set_double_tap(&info, true) == -EIO);
 assert(!lock_depth);
}
""", "c")


if __name__ == "__main__":
    unittest.main()
