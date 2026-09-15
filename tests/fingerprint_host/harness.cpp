#include <atomic>
#include <cerrno>
#include <climits>
#include <condition_variable>
#include <cstdarg>
#include <cstring>
#include <fcntl.h>
#include <future>
#include <fstream>
#include <iostream>
#include <mutex>
#include <poll.h>
#include <string>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <unistd.h>
#include <vector>
#include "BiometricsFingerprint.h"

int fakeOpen(const char*, int, ...);
int fakeEventfd(unsigned int, int);
int fakePoll(struct pollfd*, nfds_t, int);
ssize_t fakeRead(int, void*, size_t);
ssize_t fakeWrite(int, const void*, size_t);
off_t fakeLseek(int, off_t, int);
int fakeIoctl(int, unsigned long, ...);
#define open fakeOpen
#define eventfd fakeEventfd
#define poll fakePoll
#define read fakeRead
#define write fakeWrite
#define lseek fakeLseek
#define ioctl fakeIoctl
#include "BiometricsFingerprint.cpp"
#undef open
#undef eventfd
#undef poll
#undef read
#undef write
#undef lseek
#undef ioctl

using namespace android::hardware::biometrics::fingerprint::V2_3::implementation;
using BF = BiometricsFingerprint;

namespace {
int passed = 0;
int lookupStatus = 0, openStatus = 0, notifyStatus = 0;
int enrollStatus = 0, authenticateStatus = 0;
char hbmFlag = '1';
std::mutex touchMutex;
std::vector<int> touchModes;
int lookups = 0, opens = 0, notifies = 0;
std::atomic<int> closes{0};
bool nullModule = false, nullDevice = false, synchronousCallback = false;
bool failEvent = false, failFodOpen = true, sendFodEvent = false;
int eventPair[2] = {-1, -1};
int fodPair[2] = {-1, -1};
std::atomic<bool> fodClosed{false};
std::atomic<bool> inExtCmd{false};
std::atomic<bool> closeWhileWorkerRunning{false};
std::mutex gateMutex;
std::condition_variable gate;
bool extEntered = false, releaseExt = false, stopWritten = false;
std::string vendorProperty;
fingerprint_device_t device;
hw_module_methods_t methods;
hw_module_t module;

void require(bool value, const char* message) {
    if (!value) throw std::runtime_error(message);
}
int closeDevice(hw_device_t*) {
    ++closes;
    if (inExtCmd || (!failFodOpen && !fodClosed)) closeWhileWorkerRunning = true;
    return 0;
}
int openDevice(const hw_module_t*, const char*, hw_device_t** out) {
    ++opens;
    *out = nullDevice ? nullptr : &device.common;
    return openStatus;
}
int setNotify(fingerprint_device_t*, fingerprint_notify_t notify) {
    ++notifies;
    device.notify = notify;
    if (synchronousCallback) { fingerprint_msg_t msg{}; msg.type = FINGERPRINT_ERROR; msg.data.error = FINGERPRINT_ERROR_CANCELED; notify(&msg); }
    return notifyStatus;
}
int extCommand(fingerprint_device_t*, int32_t, int32_t) {
    std::unique_lock<std::mutex> lock(gateMutex);
    inExtCmd = true;
    extEntered = true;
    gate.notify_all();
    gate.wait(lock, [] { return releaseExt; });
    inExtCmd = false;
    return 0;
}
void reset() {
    lookupStatus = openStatus = notifyStatus = enrollStatus = authenticateStatus = 0;
    hbmFlag = '1';
    touchModes.clear();
    lookups = opens = closes = notifies = 0;
    nullModule = nullDevice = synchronousCallback = false;
    failEvent = false;
    failFodOpen = true;
    sendFodEvent = false;
    fodClosed = false;
    inExtCmd = false;
    closeWhileWorkerRunning = false;
    extEntered = false;
    releaseExt = false;
    stopWritten = false;
    vendorProperty.clear();
    device = {};
    device.common.version = FINGERPRINT_MODULE_API_VERSION_2_1;
    device.common.close = closeDevice;
    device.set_notify = setNotify;
    device.extCmd = extCommand;
    device.pre_enroll = [](fingerprint_device_t*) -> uint64_t { return 1; };
    device.enroll = [](fingerprint_device_t*, const hw_auth_token_t*, uint32_t, uint32_t) { return enrollStatus; };
    device.post_enroll = [](fingerprint_device_t*) { return 0; };
    device.get_authenticator_id = [](fingerprint_device_t*) -> uint64_t { return 1; };
    device.cancel = [](fingerprint_device_t*) { return 0; };
    device.enumerate = [](fingerprint_device_t*) { return 0; };
    device.remove = [](fingerprint_device_t*, uint32_t, uint32_t) { return 0; };
    device.set_active_group = [](fingerprint_device_t*, uint32_t, const char*) { return 0; };
    device.authenticate = [](fingerprint_device_t*, uint64_t, uint32_t) { return authenticateStatus; };
    methods.open = openDevice;
    module.methods = &methods;
    BF::sInstance = nullptr;
}
template<class F> void test(const char* name, F run) {
    reset();
    run();
    if (eventPair[1] >= 0) { ::close(eventPair[1]); eventPair[1] = -1; }
    if (fodPair[1] >= 0) { ::close(fodPair[1]); fodPair[1] = -1; }
    ++passed;
    std::cout << "PASS " << name << '\n';
}
}

int hw_get_module_by_class(const char* id, const char* cls, const hw_module_t** out) {
    require(std::string(id) == "fingerprint", "wrong module ID");
    require(std::string(cls) == "goodix_fod", "wrong vendor class");
    ++lookups;
    *out = nullModule ? nullptr : &module;
    return lookupStatus;
}
int property_set(const char*, const char* value) { vendorProperty = value; return 0; }
int hostClose(int fd) {
    if (fd == fodPair[0]) { fodClosed = true; fodPair[0] = -1; }
    if (fd == eventPair[0]) eventPair[0] = -1;
    return ::close(fd);
}
int fakeOpen(const char* path, int, ...) {
    if (std::string(path) == "/dev/xiaomi-touch") return ::open("/dev/null", O_RDWR);
    if (std::string(path) != "/sys/class/drm/card0-DSI-1/fod_ui_ready") { errno = ENOENT; return -1; }
    if (failFodOpen) { errno = ENOENT; return -1; }
    require(socketpair(AF_UNIX, SOCK_STREAM, 0, fodPair) == 0, "FOD socketpair failed");
    return fodPair[0];
}
int fakeEventfd(unsigned int, int) {
    if (failEvent) { errno = EMFILE; return -1; }
    require(socketpair(AF_UNIX, SOCK_STREAM, 0, eventPair) == 0, "stop socketpair failed");
    return eventPair[0];
}
int fakePoll(struct pollfd* fds, nfds_t count, int timeout) {
    require(count == 2, "unexpected poll descriptor count");
    if (sendFodEvent) {
        sendFodEvent = false;
        fds[0].revents = POLLPRI;
        return 1;
    }
    struct pollfd stop{eventPair[1], POLLIN, 0};
    int rc = ::poll(&stop, 1, timeout);
    fds[0].revents = 0;
    fds[1].revents = stop.revents;
    return rc;
}
ssize_t fakeRead(int fd, void* ptr, size_t count) {
    if (fd == fodPair[0]) { require(count == 1, "unexpected FOD read size"); *static_cast<char*>(ptr) = hbmFlag; return 1; }
    return ::read(fd, ptr, count);
}
ssize_t fakeWrite(int fd, const void* ptr, size_t count) {
    auto result = ::write(fd, ptr, count);
    if (fd == eventPair[0]) {
        std::lock_guard<std::mutex> lock(gateMutex);
        stopWritten = true;
        gate.notify_all();
    }
    return result;
}
off_t fakeLseek(int fd, off_t offset, int mode) { return fd == fodPair[0] ? 0 : ::lseek(fd, offset, mode); }
int fakeIoctl(int fd, unsigned long request, ...) {
    require(fd >= 0 && request == 0, "wrong touch ioctl request");
    va_list varargs;
    va_start(varargs, request);
    auto* args = va_arg(varargs, int*);
    va_end(varargs);
    require(args[0] == 10, "wrong touch mode selector");
    for (int i = 2; i < 6; ++i) require(args[i] == 0, "touch ioctl unused word not initialized");
    {
        std::lock_guard<std::mutex> lock(touchMutex);
        touchModes.push_back(args[1]);
    }
    args[0] = 0;
    return 0;
}
std::vector<int> getTouchModes() {
    std::lock_guard<std::mutex> lock(touchMutex);
    return touchModes;
}
void acquired(int code) {
    fingerprint_msg_t msg{};
    msg.type = FINGERPRINT_ACQUIRED;
    msg.data.acquired.acquired_info = static_cast<fingerprint_acquired_info_t>(code);
    BF::notify(&msg);
}
void attachClient(BF& instance) {
    instance.setNotify(std::make_shared<android::hardware::biometrics::fingerprint::V2_1::IBiometricsFingerprintClientCallback>());
}

int main() {
    try {
        test("HAL lookup error returns null without open", [] {
            lookupStatus = -ENOENT;
            require(BF::openHal() == nullptr && opens == 0 && closes == 0, "lookup failure mishandled");
            require(vendorProperty == "none", "missing failure property");
        });
        test("null module and module open methods rejected", [] {
            nullModule = true;
            require(BF::openHal() == nullptr && opens == 0, "null module dereferenced");
            nullModule = false;
            module.methods = nullptr;
            require(BF::openHal() == nullptr && opens == 0, "null methods dereferenced");
            module.methods = &methods;
            methods.open = nullptr;
            require(BF::openHal() == nullptr && opens == 0, "null open dereferenced");
        });
        test("HAL open error returns null", [] {
            openStatus = -EIO;
            require(BF::openHal() == nullptr && opens == 1 && notifies == 0, "open failure mishandled");
        });
        test("null returned device and close rejected", [] {
            nullDevice = true;
            require(BF::openHal() == nullptr, "null device accepted");
            nullDevice = false;
            device.common.close = nullptr;
            require(BF::openHal() == nullptr && notifies == 0, "uncloseable device accepted");
        });
        test("HAL version mismatch closes rejected device once", [] {
            device.common.version = FINGERPRINT_MODULE_API_VERSION_2_0;
            require(BF::openHal() == nullptr && closes == 1 && notifies == 0, "bad version leaked");
        });
        test("missing set_notify closes rejected device once", [] {
            device.set_notify = nullptr;
            require(BF::openHal() == nullptr && closes == 1 && notifies == 0, "missing notify accepted");
        });
        test("missing extension closes rejected device once", [] {
            device.extCmd = nullptr;
            require(BF::openHal() == nullptr && closes == 1 && notifies == 0, "missing extension accepted");
        });
        const std::pair<const char*, void (*)()> missingCore[] = {
            {"missing pre_enroll rejected", [] { device.pre_enroll = nullptr; }},
            {"missing enroll rejected", [] { device.enroll = nullptr; }},
            {"missing post_enroll rejected", [] { device.post_enroll = nullptr; }},
            {"missing get_authenticator_id rejected", [] { device.get_authenticator_id = nullptr; }},
            {"missing cancel rejected", [] { device.cancel = nullptr; }},
            {"missing enumerate rejected", [] { device.enumerate = nullptr; }},
            {"missing remove rejected", [] { device.remove = nullptr; }},
            {"missing set_active_group rejected", [] { device.set_active_group = nullptr; }},
            {"missing authenticate rejected", [] { device.authenticate = nullptr; }},
        };
        for (auto entry : missingCore) test(entry.first, [&] {
            entry.second();
            require(BF::openHal() == nullptr && closes == 1 && notifies == 0, "missing core method accepted");
        });
        test("set_notify synchronous callback does not reopen HAL", [] {
            synchronousCallback = true;
            { BF instance; }
            require(lookups == 1 && opens == 1 && notifies == 1 && closes == 1, "synchronous callback recreated HAL");
        });
        test("set_notify failure closes rejected device once", [] {
            notifyStatus = -EIO;
            require(BF::openHal() == nullptr && notifies == 1 && closes == 1, "notify failure leaked");
        });
        test("healthy HAL returned and callback registered", [] {
            require(BF::openHal() == &device && notifies == 1 && closes == 0, "healthy HAL rejected");
            require(device.notify == BF::notify && vendorProperty == "goodix_fod", "callback or property incorrect");
            device.common.close(&device.common);
        });
        test("constructor fails before starting worker on HAL failure", [] {
            lookupStatus = -ENOENT;
            bool fatal = false;
            try { BF instance; } catch (const FatalError&) { fatal = true; }
            require(fatal && eventPair[0] < 0 && closes == 0, "failed HAL not rejected before worker");
        });
        test("worker open failure is safely joined before HAL close", [] {
            { BF instance; }
            require(closes == 1 && !closeWhileWorkerRunning && BF::sInstance == nullptr, "worker cleanup failed");
        });
        test("in-flight worker finishes before HAL is closed", [] {
            failFodOpen = false;
            sendFodEvent = true;
            auto* instance = new BF;
            {
                std::unique_lock<std::mutex> lock(gateMutex);
                require(gate.wait_for(lock, std::chrono::seconds(2), [] { return extEntered; }), "worker never entered HAL");
            }
            require(getTouchModes().empty(), "display polling changed touch mode");
            auto destruction = std::async(std::launch::async, [instance] { delete instance; });
            {
                std::unique_lock<std::mutex> lock(gateMutex);
                require(gate.wait_for(lock, std::chrono::seconds(2), [] { return stopWritten; }), "destructor never signaled stop");
            }
            require(destruction.wait_for(std::chrono::seconds(0)) == std::future_status::timeout, "destructor did not wait for worker");
            require(closes == 0, "HAL closed during worker operation");
            {
                std::lock_guard<std::mutex> lock(gateMutex);
                releaseExt = true;
            }
            gate.notify_all();
            require(destruction.wait_for(std::chrono::seconds(2)) == std::future_status::ready, "worker failed to stop");
            destruction.get();
            require(fodClosed && closes == 1 && !closeWhileWorkerRunning && BF::sInstance == nullptr, "worker/HAL teardown order invalid");
        });
        test("orphan and null callbacks do not construct a new HAL", [] {
            fingerprint_msg_t msg{};
            msg.type = FINGERPRINT_ERROR;
            msg.data.error = FINGERPRINT_ERROR_CANCELED;
            BF::notify(&msg);
            BF::notify(nullptr);
            require(lookups == 0 && BF::sInstance == nullptr, "orphan callback recreated singleton");
            {
                BF instance;
                BF::notify(&msg);
                BF::notify(nullptr);
                auto callback = std::make_shared<android::hardware::biometrics::fingerprint::V2_1::IBiometricsFingerprintClientCallback>();
                instance.setNotify(callback);
                BF::notify(&msg);
                require(callback->calls == 1, "registered callback not delivered");
            }
            BF::notify(&msg);
            require(lookups == 1 && closes == 1 && BF::sInstance == nullptr, "post-close callback recreated singleton");
        });
        test("HBM ready uses bit zero for flags 0 through 3", [] {
            failFodOpen = false;
            int fd = fakeOpen(FOD_UI_PATH, O_RDONLY);
            require(fd >= 0, "wrong FOD display path");
            for (int i = 0; i < 4; ++i) {
                hbmFlag = static_cast<char>('0' + i);
                require(readHbmReady(fd) == ((i & 1) != 0), "HBM flag decoded incorrectly");
            }
            hbmFlag = 'x';
            require(!readHbmReady(fd), "invalid HBM flag accepted");
            hostClose(fd);
        });
        test("authentication acquired events restore auth mode and terminal success clears it", [] {
            BF instance;
            attachClient(instance);
            instance.authenticate(1, 0);
            acquired(FINGERPRINT_ACQUIRED_GOOD);
            acquired(FINGERPRINT_ACQUIRED_VENDOR_BASE + 21);
            acquired(FINGERPRINT_ACQUIRED_VENDOR_BASE + 44);
            acquired(FINGERPRINT_ACQUIRED_VENDOR_BASE + 23);
            fingerprint_msg_t msg{};
            msg.type = FINGERPRINT_AUTHENTICATED;
            msg.data.authenticated.finger.fid = 7;
            BF::notify(&msg);
            acquired(FINGERPRINT_ACQUIRED_VENDOR_BASE + 21);
            require(getTouchModes() == std::vector<int>({1, 0, 1, 0, 1, 0, 0}), "auth touch mode sequence wrong");
        });
        test("enrollment waiting events preserve enroll mode until final sample", [] {
            BF instance;
            attachClient(instance);
            android::hardware::hidl_array<uint8_t, 69> token{};
            instance.enroll(token, 0, 30);
            acquired(FINGERPRINT_ACQUIRED_GOOD);
            acquired(FINGERPRINT_ACQUIRED_VENDOR_BASE + 22);
            fingerprint_msg_t msg{};
            msg.type = FINGERPRINT_TEMPLATE_ENROLLING;
            msg.data.enroll.samples_remaining = 1;
            BF::notify(&msg);
            require(getTouchModes() == std::vector<int>({2, 0, 2, 2}), "non-final sample did not restore enrollment");
            msg.data.enroll.samples_remaining = 0;
            BF::notify(&msg);
            acquired(FINGERPRINT_ACQUIRED_VENDOR_BASE + 21);
            require(getTouchModes() == std::vector<int>({2, 0, 2, 2, 0, 0}), "enroll touch mode sequence wrong");
        });
        test("failed authentication match keeps operation active", [] {
            BF instance;
            attachClient(instance);
            instance.authenticate(1, 0);
            acquired(FINGERPRINT_ACQUIRED_GOOD);
            fingerprint_msg_t msg{};
            msg.type = FINGERPRINT_AUTHENTICATED;
            BF::notify(&msg);
            acquired(FINGERPRINT_ACQUIRED_VENDOR_BASE + 21);
            require(getTouchModes() == std::vector<int>({1, 0, 1, 1}), "non-match cleared authentication mode");
        });
        test("terminal errors clear mode and stale waiting cannot restore it", [] {
            BF instance;
            attachClient(instance);
            instance.authenticate(1, 0);
            fingerprint_msg_t msg{};
            msg.type = FINGERPRINT_ERROR;
            msg.data.error = FINGERPRINT_ERROR_TIMEOUT;
            BF::notify(&msg);
            acquired(FINGERPRINT_ACQUIRED_VENDOR_BASE + 21);
            require(getTouchModes() == std::vector<int>({1, 0, 0}), "error did not clear touch mode");
        });
        test("cancel and postEnroll clear saved modes", [] {
            BF instance;
            attachClient(instance);
            instance.authenticate(1, 0);
            instance.cancel();
            acquired(FINGERPRINT_ACQUIRED_VENDOR_BASE + 21);
            android::hardware::hidl_array<uint8_t, 69> token{};
            instance.enroll(token, 0, 30);
            instance.postEnroll();
            acquired(FINGERPRINT_ACQUIRED_VENDOR_BASE + 22);
            require(getTouchModes() == std::vector<int>({1, 0, 0, 2, 0, 0}), "cancel/postEnroll retained stale mode");
        });
        test("failed enrollment and authentication roll back touch mode", [] {
            BF instance;
            attachClient(instance);
            authenticateStatus = -EIO;
            enrollStatus = -EIO;
            require(instance.authenticate(1, 0).value == RequestStatus::SYS_EIO, "authentication error lost");
            acquired(FINGERPRINT_ACQUIRED_VENDOR_BASE + 21);
            android::hardware::hidl_array<uint8_t, 69> token{};
            require(instance.enroll(token, 0, 30).value == RequestStatus::SYS_EIO, "enrollment error lost");
            acquired(FINGERPRINT_ACQUIRED_VENDOR_BASE + 22);
            require(getTouchModes() == std::vector<int>({1, 0, 0, 2, 0, 0}), "failure did not roll back touch mode");
        });
        std::cout << passed << " host regression cases passed\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "FAIL " << error.what() << '\n';
        return 1;
    }
}
