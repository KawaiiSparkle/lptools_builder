# lptools — Android 逻辑分区（super.img）工具：追踪 AOSP 的自动构建

> 目的：把 `imgpatchtools-rs` 里的 `super` / `lpmake` / `lpdump` / `lpunpack`
> 从主仓库剥离，不再用 Rust 重写，改为**直接追踪 AOSP 的 `partition_tools`**，
> 用 GitHub Actions 手动触发构建出与 AOSP 逐字节一致的二进制。

## 为什么不再自己写

AOSP 的 `system/extras/partition_tools`（lpmake / lpdump / lpunpack / lpadd / lpflash）：

- 是 LP（Logical Partition / super.img）规范的**唯一权威实现**，LP 元数据版本一改（v10.0 → v10.1 → v10.2…），这里第一个更新；
- 依赖 `system/core` 的 `liblp` + `libsparse` + `libbase`，三者同源演进。

自己用 Rust 重写一遍，代价是每次 AOSP 改 LP 格式都要人肉对拍，否则会产出与设备不兼容的 super.img。
结论：**与其重复造轮子，不如追踪轮子本身。**

## 构建方式：最小 checkout + 官方 mmm

lpmake/lpunpack 无法脱离 liblp/libsparse 单独编译，且 AOSP 是多仓库（`repo`）管理。
本方案在"轻量"和"与 AOSP 一致"之间取平衡：

1. `repo init` 完整 manifest —— 只拉几 MB 的 manifest 元数据，不占源码空间；
2. `repo sync <required-projects.txt 里列出的 project>` —— 只 checkout 编译所需的 ~30 个仓库（约 10GB），
   而不是完整树的 60GB；
3. `mmm system/extras/partition_tools` —— 走官方 Soong 构建，产物与所选 AOSP 版本**逐字节一致**，
   liblp 的任何 API/格式变更都自动吸收，不会像 vendored 构建那样漂移。

### 为什么不是"只 clone 一个文件夹"？

`partition_tools` 不是自包含的。用 `Android.bp` 逐层解码，依赖链是洋葱式的：

```
lpmake / lpunpack / lpadd / lpdump
  └─ liblp        (system/core/fs_mgr/liblp)
       ├─ libbase  (system/libbase) ── fmtlib (external) + liblog (system/logging)
       ├─ liblog
       ├─ libcrypto_utils (system/core) ── OpenSSL/boringssl
       ├─ libsparse (system/core)
       └─ ext4_utils (system/extras) ── zlib
  └─ lpdump 额外要 protobuf + jsonpb
```

这些库分散在多个 git 仓库里，且用 Soong（`Android.bp`）构建，普通工具链没法直接吃。
所以"只拉一个文件夹"编译不过；最小可行单位就是上面这份 project 清单。

## 磁盘 & 依赖

| 项目 | 大小 / 说明 |
|---|---|
| 最小 checkout 树 | ~20–25GB（实测 prebuilts/clang + prebuilts/build-tools 就占 ~12GB，其余源码 ~2GB + .git blob 对象） |
| 构建产物 | 数十 MB（静态链接的 host 工具） |
| runner 要求 | 需 ~25GB 空闲磁盘；用 self-hosted runner 或大规格 runner（如 `ubuntu-latest-16-cores`），免费的 `ubuntu-latest`（~14GB）装不下 |

## 使用步骤

1. 把 `build-lptools.yml` 放进仓库 `.github/workflows/`，`required-projects.txt` 放仓库根目录。
2. 确认 runner：默认 `runs-on: [self-hosted, linux, x64]`；没有自托管就改成 `ubuntu-latest-8-cores`（付费）。
3. 到 **Actions → Build LP partition tools → Run workflow**，填 AOSP 版本（如 `android-15.0.0_r25` 或 `main`）。
4. 跑完从 Artifacts 下载 `lp-tools-<版本>.tar.gz`。

## 缺 project 怎么办（迭代补依赖）

Soong 的 bootstrap 依赖网很复杂，`required-projects.txt` 是"尽力而为"的清单，第一次跑通
可能需要补 1–2 次。补法很简单——看 `mmm` 的报错：

| 报错提示 | 要补的 project |
|---|---|
| `module "libX" not found` / 缺 `include <...>` | 找到定义该 module/头文件的仓库，把对应 `platform/...` 加进 `required-projects.txt` |
| 缺 `prebuilts/...` 路径 | 补对应 `platform/prebuilts/...` |
| Soong/go 依赖报缺 | 补 `platform/external/...` 下的 go module 仓库 |

补完重跑 workflow 即可。一旦定稿，这份清单就稳定了。

## 产出

```
dist/linux-x86_64/{lpmake, lpdump, lpunpack, lpadd, lpflash, simg2img, img2simg}
```

## 已知限制

- **macOS 宿主二进制**：AOSP 已不支持 darwin 宿主构建，也无法从 Linux 交叉编译 mac 版；
  Linux 版 + WSL 已覆盖绝大多数场景。
- **Windows 版**：需 `HOST_CROSS_OS=windows` 交叉编译，且需要额外的 mingw prebuilts。
  本 workflow 当前只出 Linux 版；需要 Windows 再单独加（如有需求可扩展）。
- **sparse 互转**：`simg2img` / `img2simg` 属于 `system/core/libsparse`，本 workflow 一并编译了。
