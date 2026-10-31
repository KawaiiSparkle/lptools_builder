# patches/ — 让 AOSP partition_tools 能在 Windows 上跑的补丁

这些补丁**只在 Windows 构建时使用**（`scripts/build-windows.sh` 会在结束时自动 revert）。
不要把它们留在 Linux 构建的树里：`0002` 给 `liblp` 的 `open()` 加了 `O_BINARY`，
而 Linux 的 `<fcntl.h>` 没有这个宏，Linux 侧会直接编译失败。

## 来源

`0001`–`0003` 取自 [Rprop/aosp15_partition_tools](https://github.com/Rprop/aosp15_partition_tools)
（Apache-2.0，AOSP 派生物），针对 `android-15.0.0_r25` 生成。
`0001` 里额外新增的 `lpdumps` 目标就是把 `lpdump.cc` 重新声明成一个不含
`liblpdump` / `libbinder` 的纯静态 host 二进制——Linux 版的 `lpdump` 走的是

```
liblpdump (shared) -> libprotobuf-cpp-full, libbase, liblog, liblp
```

这条链在 Windows 上没有对应产物，所以只能另立一个目标名。

## 每个补丁干了什么

| 补丁 | AOSP 工程 | 内容 |
| :-- | :-- | :-- |
| `0001` | `platform/system/extras` | 给 `lpmake` / `lpadd` / `lpunpack` 打开 `target.windows`；把 `shared_libs` 换成 `static_libs` 并加 `stl: "c++_static"`；新增 `lpdumps` 目标；源码里补 `EX_*` 常量、`sysexits.h` 守卫、`Realpath` -> `_fullpath`、`O_BINARY` |
| `0002` | `platform/system/core` | `liblp` 读写 metadata 的 `open()` 加 `O_BINARY`；`WriteToImageFile` 在 Windows 上先 `close()` 再 `rename()`；`libsparse` 的 `simg2img` / `img2simg` / `append2simg` 打开 windows 目标，并把不存在的 `asprintf` 换成 `snprintf` |
| `0003` | `platform/external/e2fsprogs` | `ext2simg` 打开 windows 目标 + 全静态链接 + 补 `libbase`；用 `windows_io_manager` 替掉 `unix_io_manager` |

## 上游现在是什么状态

- `lpmake` **上游已经自带** `target: { windows: { enabled: true } }`，源码里也有
  `#ifdef WIN32 ... EX_*` 和 `#else ... O_BINARY = 0` 的写法，
  说明 Google 是支持用 Soong 交叉编译 Windows host 工具的，只是没给全套工具都打开。
- `lpdump` / `lpunpack` / `lpadd` / `simg2img` / `img2simg` 上游没有，
  这就是补丁存在的全部理由。

## 三个真实的坑（也是这些补丁的核心价值）

1. **`O_BINARY`**：Windows 的 `open()` 默认是文本模式，会把 `\n` 翻译成 `\r\n`。
   super 的 metadata 里只要有 `\n` 就被写坏，读出来就是「看起来正常但校验失败」。必须逐处加。
2. **`rename()`**：Windows 上目标文件被打开或已存在时 `rename` 会失败，
   所以 liblp 写 geometry 时要先 `close(fd)` 再重试。
3. **`sysexits.h` / `asprintf` / `unix_io_manager`**：mingw 都没有，得给替代实现。

## 刷新补丁（换 AOSP ref 时）

补丁是按 `android-15.0.0_r25` 的 blob 上下文生成的。换 ref 后如果 `git apply -3` 报冲突：

```bash
# 1) 在同步好的树里手工改到能编
# 2) 逐工程重新导出
git -C aosp/system/extras diff > patches/0001-system-extras-partition_tools-windows.patch
git -C aosp/system/core  diff > patches/0002-system-core-liblp-libsparse-windows.patch
git -C aosp/external/e2fsprogs diff > patches/0003-external-e2fsprogs-ext2simg-windows.patch
```

`scripts/build-windows.sh` 失败时会告诉你是哪个工程、哪个补丁，并保留现场（不 revert），
方便你直接从那里 `git diff` 导出新补丁。
