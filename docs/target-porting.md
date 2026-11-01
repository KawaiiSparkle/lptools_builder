# 其他 target 的适配基线

本轮先构建官方 `android-latest-release` 的 Linux x86_64 host 工具。原版编译入口
不加载已有 Windows 补丁；新增 host 产品配置只选择安装目标。以下结论来自
`android17-release` 的源码审阅，尚不代表这些 target 已在云端编译或运行成功。

## 上游现状与下一步

| Target | 上游已有能力 | 本项目接下来的实际适配点 |
|---|---|---|
| Linux x86_64 | Soong 的 glibc host 工具链；LP host 模块 | 先完成云端编译、运行库打包与制镜像/解包字节比对，保存实际源码 SHA |
| Windows x86_64 | `x86_64-pc-windows-gnu` 与 MinGW sysroot；partition_tools 中只有 `lpmake` 显式启用 Windows | 先独立编译上游已开放的 `lpmake`，明确选择 64 位；再逐个开放其他工具和缺少的依赖 variant，并依据编译/运行错误修正 I/O |
| Linux ARM64 | `linux_musl` 的 `aarch64-linux-musl`；原生 ARM64 host 也会选 musl | 保留 AOSP musl 头文件、CRT 与 `libc_musl` 图，按需加入 `platform/external/musl`；优先核实 x86_64 host 上的交叉选择，不能假定所有预编译构建工具能直接在 ARM64 runner 执行 |
| macOS ARM64 / x86_64 | Darwin 工具链；ARM64 triple 为 `aarch64-apple-darwin` | 在 macOS runner 上使用受支持的 Xcode SDK；源码树需大小写敏感卷。不能只换 triple 就从 Linux 得到完整 Darwin 安装产物 |
| Android ARM64 / ARM | 两种 device 工具链；现有 `lpdump`/`lpflash` device 模块 | 为 `lpmake`/`lpadd`/`lpunpack` 建立 device 模块；稀疏工具同样核对模块类型；明确采用独立镜像 CLI 还是系统 Binder 客户端，ARM32 单独选 secondary variant |

Windows 的 OS variant 默认关闭，而且 Windows 偏好 32 位、二进制默认
`MultilibFirst`。因此模块标了 `enabled: true` 也不能证明得到 x86_64。

Linux glibc host 的架构集合是 x86/x86_64；ARM64 的普通 Linux host ABI 在这套
Soong 中使用 musl。AOSP 的 musl 配置刻意用 `--sysroot /dev/null` 和专门的 CRT
模块，不能拿 Ubuntu 的 ARM64 glibc sysroot 直接替代。

Darwin 的 SDK/工具变量只在 macOS 构建宿主初始化，依赖 `/usr/bin/xcrun` 寻找
SDK、链接器、ar、lipo、strip。Soong 还会跳过 Darwin HostCross 的安装步骤；
若以后专门做 Linux→Darwin，需要同时处理 SDK、工具发现及产物收集。

## Android 的 lpdump 必须明确区分

上游 `lpdump` 的 host 入口使用 `lpdump_host.cc`，直接调用 `LpdumpMain`。
Android 入口使用 `lpdump_target.cc`，启动/连接 `lpdumpd` 并通过 Binder 请求服务。
本项目用于普通镜像文件的设备工具应单独提供直接入口，并逐一处理 Android
版本的 `liblpdump` 依赖；不能把现有 Binder 客户端当成可独立分发的 host CLI。

`cc_binary_host` 的模块类型决定它没有 device variant；只增加
`device_supported: true` 不能把这个类型变成 Android 二进制。需要转换成能同时
支持 host/device 的定义，或新增明确命名的 device 模块。

## 每个 target 的验收

1. 来源使用实际固化的 manifest 和项目 SHA，记录新增构建配置及所有源码改动。
2. 检查 ELF / PE / Mach-O 的实际架构与动态加载器、运行库；Windows 强制核对 x64。
3. 在对应系统实际运行 `lpmake → lpdump → lpunpack`，逐字节比较分区，并验证
   `img2simg → simg2img`。交叉链接成功与目标机实际可运行分别记录。
4. 只有通过的 target 才进入正式产物矩阵；失败日志保留首个真实缺项，避免继续
   依赖旧补丁是否碰巧能应用。

## 官方依据

- [partition_tools 模块与 Windows opt-in](https://android.googlesource.com/platform/system/extras/+/refs/heads/android17-release/partition_tools/Android.bp)
- [Soong OS/架构与 host 选择](https://android.googlesource.com/platform/build/soong/+/refs/heads/android17-release/android/arch.go)
- [Windows 工具链与 MinGW sysroot](https://android.googlesource.com/platform/build/soong/+/refs/heads/android17-release/cc/config/x86_windows_host.go)
- [ARM Linux host 工具链](https://android.googlesource.com/platform/build/soong/+/refs/heads/android17-release/cc/config/arm_linux_host.go)
- [Linux musl 默认 CRT / sysroot](https://android.googlesource.com/platform/build/soong/+/refs/heads/android17-release/cc/config/x86_linux_host.go)
- [Darwin SDK 与工具发现](https://android.googlesource.com/platform/build/soong/+/refs/heads/android17-release/cc/config/darwin_host.go)
- [二进制模块类型与 multilib](https://android.googlesource.com/platform/build/soong/+/refs/heads/android17-release/cc/binary.go)
- [lpdump host 入口](https://android.googlesource.com/platform/system/extras/+/refs/heads/android17-release/partition_tools/lpdump_host.cc)
- [lpdump Android Binder 入口](https://android.googlesource.com/platform/system/extras/+/refs/heads/android17-release/partition_tools/lpdump_target.cc)
