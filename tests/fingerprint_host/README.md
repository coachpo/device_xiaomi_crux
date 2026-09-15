# Fingerprint host regression verification

Normal `python3 -m unittest discover -s tests` discovery includes this suite. Run it independently from the device repository:

```sh
python3 -m unittest discover -s tests -p test_fingerprint.py -v
# Or run the C++ harness directly:
python3 tests/fingerprint_host/run.py
```

An optional first argument selects another fingerprint source directory. The runner requires Python 3 and a C++17 host compiler (`c++`, or the command in `CXX`). It creates generated headers and the executable in a temporary directory and compiles the **actual, current** `BiometricsFingerprint.cpp`, `BiometricsFingerprint.h`, and local `fingerprint.h`, with `-std=gnu++17 -pthread -Wall -Wextra -Werror`. It does not copy or rewrite production functions.

## Coverage

The 30 runtime cases cover:

- HAL lookup/open failures; null module, method table, open method, returned device, or close method.
- Rejected ABI version and each missing required fingerprint method; supported rejected devices close exactly once.
- Registration failures, synchronous registration callbacks, successful registration, and constructor failure before starting a worker.
- Worker open failure and deterministic teardown ordering: the fake vendor call blocks; the destructor signals its stop descriptor; the test proves the HAL remains open until the worker returns and closes its file descriptor.
- No singleton creation for callbacks before initialization, with a null message, or after destruction; delivery to an active client.
- Display HBM flags `0` through `3` and an invalid flag; the display worker does not change touch mode.
- Xiaomi touch `SET_CUR_VALUE` request `0`, mode `10`, zero-filled unused words, and driver result in the first word.
- Authentication/enrollment start, acquired/waiting events, nonterminal enrollment, failed match retry, successful completion, errors, cancel/post-enroll, and failed operation rollback.

## Scope

The host fakes replace Android HIDL/platform headers, HAL lookup, sysfs/device opens, ioctl, and the Linux eventfd/poll readiness boundary. A Unix socket pair supplies stop readiness on macOS. Fatal logging throws a test exception; production fatal logging terminates the process. Generated platform declarations are deliberately narrow and do not prove Android binary layout or HIDL transport compatibility.

This verifies wrapper control flow and resource ownership with real C++ threads. It does not validate the proprietary Goodix binary, actual hardware/sysfs/SELinux access, SurfaceFlinger integration, Soong linkage, or an Android target build. The production display write helper uses the real host filesystem and logs expected absent-sysfs failures through a quiet fake logger; successful kernel sysfs writes are not claimed.
