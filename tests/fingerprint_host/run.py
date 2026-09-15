#!/usr/bin/env python3
"""Compile the current fingerprint wrapper against narrow host platform fakes."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

HEADERS = {
'hardware/hardware.h': r'''
#pragma once
#include <stdint.h>
#define HARDWARE_MODULE_API_VERSION(major, minor) (((major) << 8) | (minor))
struct hw_module_t;
struct hw_device_t { uint32_t version; int (*close)(hw_device_t*); };
struct hw_module_methods_t { union { int (*open)(const hw_module_t*, const char*, hw_device_t**); int (*fakeOpen)(const hw_module_t*, const char*, hw_device_t**); }; };
struct hw_module_t { hw_module_methods_t* methods; };
int hw_get_module_by_class(const char*, const char*, const hw_module_t**);
''',
'hardware/hw_auth_token.h': '#pragma once\n#include <stdint.h>\nstruct hw_auth_token_t { uint8_t payload[69]; };\n',
'android-base/unique_fd.h': r'''
#pragma once
int hostClose(int);
namespace android::base {
class unique_fd {
    int fd_ = -1;
public:
    unique_fd() = default;
    explicit unique_fd(int fd) : fd_(fd) {}
    unique_fd(const unique_fd&) = delete;
    ~unique_fd() { reset(); }
    void reset(int fd = -1) { if (fd_ >= 0) hostClose(fd_); fd_ = fd; }
    int get() const { return fd_; }
};
}
''',
'android-base/strings.h': '#pragma once\n#include <string>\nnamespace android::base { inline bool StartsWith(const std::string& s, const char* p) { return s.find(p) == 0; } }\n',
'cutils/properties.h': '#pragma once\nint property_set(const char*, const char*);\n',
'sys/eventfd.h': '#pragma once\n#define EFD_CLOEXEC 1\nint eventfd(unsigned int, int);\n',
'android/log.h': '#pragma once\n',
'hidl/MQDescriptor.h': '#pragma once\n',
'hidl/Status.h': '#pragma once\n',
'log/log.h': r'''
#pragma once
#include <stdexcept>
struct FatalError : std::runtime_error { FatalError() : std::runtime_error("fatal HAL guard") {} };
#define ALOGE(...) do {} while (0)
#define ALOGD(...) do {} while (0)
#define ALOGI(...) do {} while (0)
#define ALOGV(...) do {} while (0)
inline void fatalForTest() { throw FatalError(); }
#define LOG_ALWAYS_FATAL_IF(condition, ...) do { if (condition) fatalForTest(); } while (0)
#ifndef TEMP_FAILURE_RETRY
#define TEMP_FAILURE_RETRY(exp) ({ decltype(exp) rc_; do { rc_ = (exp); } while (rc_ == -1 && errno == EINTR); rc_; })
#endif
''',
'android/hardware/biometrics/fingerprint/2.3/IBiometricsFingerprint.h': r'''
#pragma once
#include <array>
#include <memory>
#include <string>
#include <vector>
#include <cstdint>
namespace android {
using status_t = int;
template<class T> using sp = std::shared_ptr<T>;
namespace hardware {
template<class T> struct Return { T value; Return(T v) : value(v) {} bool isOk() const { return true; } operator T() const { return value; } };
template<> struct Return<void> { bool isOk() const { return true; } };
using Void = Return<void>;
using hidl_string = std::string;
template<class T> using hidl_vec = std::vector<T>;
template<class T, size_t N> using hidl_array = std::array<T, N>;
namespace biometrics::fingerprint::V2_1 {
enum class RequestStatus { SYS_OK, SYS_ENOENT, SYS_EINTR, SYS_EIO, SYS_EAGAIN, SYS_ENOMEM, SYS_EACCES, SYS_EFAULT, SYS_EBUSY, SYS_EINVAL, SYS_ENOSPC, SYS_ETIMEDOUT, SYS_UNKNOWN };
enum class FingerprintError { ERROR_HW_UNAVAILABLE, ERROR_UNABLE_TO_PROCESS, ERROR_TIMEOUT, ERROR_NO_SPACE, ERROR_CANCELED, ERROR_UNABLE_TO_REMOVE, ERROR_LOCKOUT, ERROR_VENDOR };
enum class FingerprintAcquiredInfo { ACQUIRED_GOOD, ACQUIRED_PARTIAL, ACQUIRED_INSUFFICIENT, ACQUIRED_IMAGER_DIRTY, ACQUIRED_TOO_SLOW, ACQUIRED_TOO_FAST, ACQUIRED_VENDOR };
struct IBiometricsFingerprintClientCallback {
    int calls = 0;
    template<class... T> Return<void> onError(T...) { ++calls; return {}; }
    template<class... T> Return<void> onAcquired(T...) { ++calls; return {}; }
    template<class... T> Return<void> onEnrollResult(T...) { ++calls; return {}; }
    template<class... T> Return<void> onRemoved(T...) { ++calls; return {}; }
    template<class... T> Return<void> onAuthenticated(T...) { ++calls; return {}; }
    template<class... T> Return<void> onEnumerate(T...) { ++calls; return {}; }
};
}
namespace biometrics::fingerprint::V2_3 {
using namespace V2_1;
struct IBiometricsFingerprint {
    virtual ~IBiometricsFingerprint() = default;
    virtual Return<uint64_t> setNotify(const sp<IBiometricsFingerprintClientCallback>&) = 0;
    virtual Return<uint64_t> preEnroll() = 0;
    virtual Return<RequestStatus> enroll(const hidl_array<uint8_t, 69>&, uint32_t, uint32_t) = 0;
    virtual Return<RequestStatus> postEnroll() = 0;
    virtual Return<uint64_t> getAuthenticatorId() = 0;
    virtual Return<RequestStatus> cancel() = 0;
    virtual Return<RequestStatus> enumerate() = 0;
    virtual Return<RequestStatus> remove(uint32_t, uint32_t) = 0;
    virtual Return<RequestStatus> setActiveGroup(uint32_t, const hidl_string&) = 0;
    virtual Return<RequestStatus> authenticate(uint64_t, uint32_t) = 0;
    virtual Return<bool> isUdfps(uint32_t) = 0;
    virtual Return<void> onFingerDown(uint32_t, uint32_t, float, float) = 0;
    virtual Return<void> onFingerUp() = 0;
};
}
}
}
''',
'vendor/xiaomi/hardware/fingerprintextension/1.0/IXiaomiFingerprint.h': r'''
#pragma once
namespace vendor::xiaomi::hardware::fingerprintextension::V1_0 {
struct IXiaomiFingerprint { virtual ~IXiaomiFingerprint() = default; virtual android::hardware::Return<int32_t> extCmd(int32_t, int32_t) = 0; };
}
''',
}


def main():
    root = Path(__file__).resolve().parent
    source = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else root.parents[1] / "fingerprint"
    compiler = shlex.split(os.environ.get("CXX", "c++"))
    with tempfile.TemporaryDirectory(prefix="crux-fingerprint-host-") as temporary:
        build = Path(temporary)
        stubs = build / "stubs"
        for relative, contents in HEADERS.items():
            path = stubs / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents)
        executable = build / "harness"
        command = compiler + [
            "-std=gnu++17", "-pthread", "-Wall", "-Wextra", "-Werror",
            "-I", str(stubs), "-I", str(source), str(root / "harness.cpp"),
            "-o", str(executable),
        ]
        subprocess.run(command, check=True, timeout=60)
        subprocess.run([str(executable)], check=True, timeout=20)


if __name__ == "__main__":
    main()
