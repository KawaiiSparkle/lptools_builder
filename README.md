# lptools_builder — 与 imgpatchtools-rs 配套的 LP 工具链构建

> 一句话：这是 `imgpatchtools-rs` 的**配套件**——把 AOSP `partition_tools`
> （lpmake / lpdump / lpunpack / lpadd / lpflash）和 `libsparse` 的
> simg2img / img2simg 的构建、冒烟检查、**清单与哈希**打包及发布流程，
> 供下游（imgpatchtools-rs、脚本、CI）取用。

**已验证范围**：以 `android-15.0.0_r1` 构建的 Linux x86_64 和 Windows x86_64
已通过[真实云端构建 #13](https://github.com/KawaiiSparkle/lptools_builder/actions/runs/41252581019)。
Linux 完成镜像往返冒烟测试；Windows 下载产物也完成原生 raw / sparse 镜像及
`imgpatchtools-rs` 配套往返字节比对。其他架构与源码 ref 仍需分别验证；当前没有
冷树 / 热树性能基准，也不保证重复构建逐字节一致。

## 1. 为什么需要这个仓库

`imgpatchtools-rs` 处理的是**传统块 OTA**：`transfer.list` + `new.dat` +
`patch.dat` → 分区镜像。动态分区（dynamic partitions）设备的镜像装在
`super.img` 里，要 `lpunpack` 才能取出单个分区，改完还要 `lpmake` 拼回去。

LP（Logical Partition）元数据是 AOSP 单方面演进的（v10.0 → v10.1 → v10.2 …），
所以这里**不重写**，而是直接追踪 AOSP 官方实现。代价是需要一个 AOSP 构建环境，
本仓库就是把这件事自动化、可验证、可交付。

## 2. 构建入口与资源准备

当前脚本针对以下配置问题设置了入口与检查：

| # | 问题 | 后果 | 现在 |
|---|---|---|---|
| 1 | 目录级构建入口与模块定义不匹配 | 可能遇到 `No Android.mk found` 等入口错误 | 显式调用 `m <模块名>`（Soong 模块入口） |
| 2 | 没有 `lunch` | Soong 直接拒绝：`TARGET_PRODUCT is not defined` | 构建前 `lunch`（带多候选回退） |
| 3 | 仓库名与检出路径混用 | `platform/build` 检出到 `build/make`；不存在 `platform/build/make` 仓库，直接同步会失败 | 校验器输出去重后的真实 manifest 项目名，构建脚本使用同一结果同步 |
| 4 | 依赖自定义 runner，且未预算磁盘 / 内存 | 同步、编译可能耗尽资源；可用量随 runner 镜像变化 | 托管 runner + prepare action 分别清理磁盘、准备 swap，并输出前后资源数据 |
| 5 | 只传 artifact，不发布、不验证、无哈希 | 90 天后 artifact 过期；没人知道产物对不对；下游无法固定引用 | 端到端冒烟测试 + `manifest.json` + `SHA256SUMS` + GitHub Release |

工具可用性依赖所选 AOSP ref。Linux / macOS 脚本要求 `lpmake`、`lpdump`、`lpunpack`、
`simg2img`、`img2simg`，并将 `lpadd` / `lpflash` 作为可选工具尝试；Windows 要求
`lpmake.exe`、`lpdump.exe`、`lpunpack.exe`，其余工具按实际生成情况收集。缺少必需
工具会失败，可选工具缺失只告警；完整清单以 bundle 的 `manifest.json` 为准。

## 3. 流水线与目标矩阵

```
repo init --depth=1 (manifest 元数据)
   └─ tools/check_projects.py      ← 校验 required-projects.txt 中的项目是否在 manifest 中
repo sync -c --optimized-fetch --prune --fail-fast <所需项目集>
   └─ source build/envsetup.sh && lunch <候选产品>
   └─ m --soong-only <host 安装路径>             ← 只请求所需 host 工具
   └─ scripts/smoke-test.sh        ← host 构建默认检查制镜像 / 解包往返及字节比对
   └─ repo manifest + forall --ignore-missing     ← 固化已同步项目的实际 HEAD
   └─ scripts/make-manifest.py     ← manifest.json + SHA256SUMS
   └─ tar.gz / zip → artifact → 满足门禁且请求发布时进入 Release
```

CI 与本地共用以下脚本；Windows / Android 的同步阶段委托给 `build-linux.sh --sync-only`：

| 入口 | 用途 |
|---|---|
| `scripts/build-linux.sh` | Linux 与 macOS 的 host 产物（CI 与本机共用同一支脚本） |
| `scripts/build-windows.sh` | 在干净 AOSP 工程上应用 `patches/`，构建 Windows host-cross exe；成功后逆向本次补丁，失败保留现场 |
| `scripts/build-android.sh` | 尝试收集所选产品的 Android device 二进制；源码树须先提供对应 device 模块 |

workflow 配置的目标矩阵（文件名是预期产物，尚不表示已构建成功）：

| 平台 | 产物 | 构建方式 | 是否门禁 |
|---|---|---|---|
| `linux-x86_64` | `lptools-linux-x86_64.tar.gz` | ubuntu runner 原生 `m` | 必需 |
| `linux-arm64` | `lptools-linux-arm64.tar.gz` | `ubuntu-24.04-arm` 上尝试 host 构建，需要适配 AOSP 的 x86_64 预编译工具链 | 实验（失败不阻塞发布） |
| `windows-x86_64` | `lptools-windows-x86_64.zip` | Linux 上 host_cross(windows) + `patches/` | 必需 |
| `darwin-arm64` | `lptools-darwin-arm64.tar.gz` | `macos-15` 上尝试 host 构建 | 实验（失败不阻塞发布） |
| `darwin-x86_64` | `lptools-darwin-x86_64.tar.gz` | `macos-15-intel` 上尝试 host 构建 | 实验 |
| `android-arm64` / `android-arm` | `lptools-android-<abi>.tar.gz` | 在 Linux 上尝试构建并收集产品的 bionic device 输出 | 实验 |

**门禁规则**：`release` 等待所有构建 job 结束，且只有运行未取消、`linux` 与
`windows` 都为 `success`、未启用 `check_patches`，并由 tag push 或手动
`publish: true` 请求发布时才进入发布阶段。发布前还会检查 `linux-x86_64`、
`windows-x86_64` 两份 bundle 的压缩包、同名 `.sha256` 和
`manifest.json`，校验压缩包哈希及清单的基本字段；固化 manifest 必须存在、项目数为
正、逐项目记录完整提交号，且实际文件哈希与记录一致。实验 job 失败不阻止满足上述条件的发布；Linux ARM64 / Darwin 使用
`continue-on-error`，Android 的失败结果则由显式 release 门禁排除。Release notes 只
汇总下载到的清单与资产，**没有 job 失败报告**，失败详情仍需查看 Actions。

**推送到 `main` 与默认手动运行只跑 Linux x86_64 + Windows x86_64**，普通分支推送不会发布 Release。Linux ARM64 / Darwin / Android
默认跳过，可通过 `extra_targets` 选择 `linux-arm64`、`darwin`、`android` 及其组合。Tag push 与每月定时
任务会尝试全矩阵，但不保证实验目标有产物；定时任务本身不发布。

**实验与架构限制**：stock AOSP 的 Linux 预编译工具链以 x86_64 为宿主，不能直接在
ARM64 runner 上执行；Linux ARM64 需要另行移植构建链，Darwin 的完整链路也尚未验证。对 stock `android-15.0.0_r1` 的模块检查已确认：`lpmake`、`lpunpack`、
`lpadd`、`simg2img`、`img2simg` 是 host-only，`lpdump` 的 device 变体依赖 Binder
服务。因此 Android 目标需要额外的 device 模块移植及依赖适配；仅换 `lunch` 或 ABI
不会得到独立设备工具，也不能保证在设备 / Termux 运行。

来源信息（哪个 AOSP 提交、哪个分支或 tag、哪次构建）由脚本现场采集：合并 manifest
并读取 manifest 仓库实际 fetch 的 ref；用 `repo forall --ignore-missing` 枚举已同步
项目，将其 HEAD 固化到 `aosp-manifest.xml`。来源清单无效时构建失败，不再生成
零项目的空记录。这些字段用于溯源，不保证二进制可复现。详见 4.5 节。

## 4. 快速开始

### 4.1 下载 Release 中已有的产物

以下列出预期文件名；请先检查实际 Release 资产，实验平台可能缺失。

| 平台 | 下载 | 解压后 |
|---|---|---|
| Linux x86_64 | `lptools-linux-x86_64.tar.gz` | `bin/` + 已安装的 `lib64/` / `lib/` 运行库 + `manifest.json` + `SHA256SUMS` |
| Linux arm64 | `lptools-linux-arm64.tar.gz` | 同上 |
| macOS arm64 | `lptools-darwin-arm64.tar.gz` | 同上 |
| macOS x86_64 | `lptools-darwin-x86_64.tar.gz` | 同上 |
| Windows x86_64 | `lptools-windows-x86_64.zip` | `bin\*.exe` + 同一套元数据 |
| Android（实验） | `lptools-android-<abi>.tar.gz` | bionic `bin/`；`lib/` 仅尽力收集直接依赖，运行时仍须设备验证 |

地址统一是
`https://github.com/KawaiiSparkle/lptools_builder/releases/latest/download/<文件名>`，
已发布压缩包应有同名 `.sha256`；校验后再使用。

Linux / macOS：

```bash
sha256sum -c lptools-linux-x86_64.tar.gz.sha256   # macOS 用 shasum -a 256 -c
tar xzf lptools-linux-x86_64.tar.gz               # -> lptools-linux-x86_64/{bin,manifest.json,SHA256SUMS}
export PATH="$PWD/lptools-linux-x86_64/bin:$PATH"
lpdump --version || lpdump -h
```

Windows 原生版（无需 WSL；运行效果仍需在 Windows 验证）：

```powershell
# 用 certutil 计算 SHA-256，与下载的 .sha256 内容核对
certutil -hashfile lptools-windows-x86_64.zip SHA256
# 解压后推荐放在 imgpatchtools-rs.exe 同目录，或设 IMGTOOLS_LPTOOLS_DIR 指过去
Expand-Archive lptools-windows-x86_64.zip -DestinationPath .
imgpatchtools-rs.exe super doctor
```

### 4.2 本地构建（Linux / macOS / WSL2 出 host 产物）

```bash
# 首次：装 repo
mkdir -p ~/.local/bin
curl -sSL https://storage.googleapis.com/git-repo-downloads/repo -o ~/.local/bin/repo
chmod +x ~/.local/bin/repo && export PATH=$HOME/.local/bin:$PATH

# 完整流程；磁盘与耗时取决于源码 ref、工具链、网络与并行度
scripts/build-linux.sh --ref android-15.0.0_r1

# 之后复用已有树，只重编
scripts/build-linux.sh --skip-sync

# 只同步不编译（例如先确认依赖清单）
scripts/build-linux.sh --ref android-16.0.0_r2 --sync-only
```

WSL2 建议：将 AOSP 树放在 `~/aosp` 这类 Linux 文件系统路径，用 `--work ~/aosp`
指定，产物可再用 `--out /mnt/c/...` 导出。跨文件系统的 I/O 开销需按实际环境测量；
这里没有固定倍数的性能结论。脚本的 30 GB 空闲磁盘告警只是预算提示，不能代表实测
需求，还应为输出、缓存和新增 swap 单独留空间。

macOS 实验入口使用同一支脚本，下面的命令不代表工具链已验证：

```bash
scripts/build-linux.sh --ref android-15.0.0_r1        # 在对应 macOS host 上尝试
scripts/build-linux.sh --arch x86_64 --skip-sync      # 仅适用于实际产出 x86_64 host 工具的树
```

`build-linux.sh --arch` 指定期望的 host 输出架构，用于校验实际二进制与产物命名；
它**不设置 Soong 的 host 架构，也不提供跨架构编译**。实际输出与请求不一致时拒绝
打包，不能在 x86_64 host 上仅传 `--arch arm64` 就得到 arm64 工具。Android 版本兼容性
仍需按源码 ref 和实际镜像验证。macOS 要用大小写敏感的卷，脚本会自检；不通过时按
提示用 `hdiutil create -type SPARSE -fs 'Case-sensitive APFS'` 建卷。


### 4.3 本地构建 Windows 原生产物

必须在 **Linux** 上尝试构建，当前方案依赖 AOSP 的 Linux x86 mingw 预编译工具链；
未验证其他 host 架构或真实 Windows AOSP 构建。

```bash
scripts/build-windows.sh --skip-sync          # 复用已经同步好的 aosp/ 树
scripts/build-windows.sh --arch x86           # 尝试所选树提供的 32 位 Windows 输出
scripts/build-windows.sh --check-patches      # 同步后检查补丁适用性，不编译、不应用补丁
scripts/build-windows.sh --revert-only        # 仅逆向精确匹配的 Windows 补丁
scripts/build-windows.sh --keep-patches       # 保留补丁供检查
```

更换源码 ref 时，先在已切换到目标 ref 的树上运行 `--check-patches`。检查仅判断当前
补丁是否适用，不证明它能编译或运行。未指定 `--skip-sync` 时仍会先调用
`build-linux.sh --sync-only`，首次检查需要同步 AOSP，不能承诺固定耗时。

构建前预检相关工程的 tree / index 是否干净；应用补丁使用不修改 index 的 `git apply`。
成功后仅逆向本次应用的补丁，失败时保留现场供检查，`--keep-patches` 则主动保留。
`--revert-only` 也只逆向精确匹配的补丁并拒绝漂移，不会清空整个工程的修改。
可顺序复用已同步的树，但不要让 Linux 与 Windows 构建并发操作同一棵树。
当前 host 构建使用 `--soong-only`，从 `OUT_DIR/host/<os>-<arch>/bin` 收集产物；
Windows 的目录名固定为 `windows-x86`，实际 32/64 位由补丁的 `compile_multilib` 选择，
打包时再检查 PE 架构。手工撤回 32 位构建的补丁时也要传 `--arch x86`。

### 4.4 CI

Actions → **Build lptools** → Run workflow：

`aosp_ref` 的可用值可以在 [AOSP 官方 manifest 分支 / 标签列表](https://android.googlesource.com/platform/manifest/+refs)
中查看，Run workflow 的输入提示也附有这个地址。页面的 **Branches** 和 **Tags** 分别列出
分支与标签；填写短名称，例如 `android-latest-release` 或 `android-15.0.0_r36`。
`android-latest-release` 是滚动分支，后续运行可能切换到新的 Android 发布系列；
`android-15.0.0_r36` 是固定发布标签。实际使用的 manifest 提交与逐项目提交会记录在产物中。
引用存在不等于当前最小项目集已经验证支持该版本。

无需下载 AOSP，也可以用 Git 实时查询（仅列出远程引用，不创建本地源码树）：

```bash
# 所有分支
git ls-remote --heads https://android.googlesource.com/platform/manifest

# 所有标签，按版本名称倒序排列；--refs 排除 annotated tag 的 ^{} 重复行
git ls-remote --tags --refs --sort=-version:refname https://android.googlesource.com/platform/manifest

# 只列出 Android 15 的标签；替换通配模式即可筛选其他版本
git ls-remote --tags --refs --sort=-version:refname https://android.googlesource.com/platform/manifest "refs/tags/android-15.*"
```

输出每行为 `<SHA> <ref>`，将右侧的 `refs/heads/` 或 `refs/tags/` 前缀去掉后填入
`aosp_ref`。列表表示该引用存在，不代表当前构建配置已验证支持；当前 Linux / Windows
x86_64 已验证的基线仍是 `android-15.0.0_r1`。

| 输入 | 默认 | 说明 |
|---|---|---|
| `aosp_ref` | `android-15.0.0_r1` | `platform/manifest` 的 tag 或分支；[查看官方可用列表](https://android.googlesource.com/platform/manifest/+refs)，如 `android-15.0.0_r1`、`main` |
| `product` | `lptools_host` | Linux / Darwin / Windows 构建引导用的 `lunch` 目标；Android 实验脚本按 ABI 选择产品 |
| `android_abis` | `arm64,arm` | Android 实验目标请求的 ABI；还需 `extra_targets` 包含 `android`，留空则跳过该 job |
| `check_patches` | 否 | 只跑 Windows 补丁检查，跳过所有构建及发布；首次仍需准备环境与同步源码，不应用补丁 |
| `publish` | 否 | 勾选则创建 / 更新 GitHub Release |
| `extra_targets` | 空 | 额外尝试 `linux-arm64`、`darwin`、`android` 及其逗号分隔组合；空 = 只跑 Linux / Windows x86_64 |

默认手动运行并行执行 `linux`（x86_64）与 `windows`；实验目标的选择、发布的
非取消门禁及两份必需资产检查见第 3 节。`release` 下载已上传的 artifact，检查通过后用
`scripts/make-release-notes.py` 生成说明，再调用 `gh release create/upload`。实验
目标的失败不会自动变成 Release notes 中的失败列表。

同一 workflow / Git ref 上的普通手动运行（`publish: false`）共用 concurrency group，
新运行会自动取消先前未完成的普通运行。Tag push 和 `publish: true` 运行使用包含
`run_id` 的独立 group，不会被另一次运行自动取代；用户仍可手动取消。此隔离也不等于
串行发布：同一 AOSP ref、同一 UTC 日期的手动发布可能使用相同 Release tag，并发
上传 / 更新时仍可能争用，必要时应主动错开。

启用 `check_patches` 时只执行 Windows 检查，Linux / Darwin / Android 与发布均跳过。
检查阶段不应用补丁、不编译；准备环境及首次 `repo sync` 仍然执行，已有目标树才适合
在本地配合 `--skip-sync --check-patches` 复查。

#### 速度与日志

当前没有冷树 / 热树性能基准，以下是减少默认工作量与改善观察性的实现选择，不能据此
声称特定加速倍数或比第三方 action 更快。

- **同步与默认工作量**：`repo init --depth=1` 不启用 `--partial-clone`；
  `repo sync` 使用 `--optimized-fetch --prune --fail-fast`，只同步项目清单中的仓库。
  Blobless checkout 可能引入按需抓取，实际取舍取决于网络、缓存与源码树。默认手动
  运行省去 Linux ARM64 / Darwin / Android；已压缩 bundle 上传时使用 `compression-level: 0`。
- **Linux 至少 12 GiB 总活跃 swap**：prepare action 调用 `scripts/prepare-swap.sh`，
  `swap-size-gb` 默认 `12`，单位为 GiB。脚本汇总已有活跃 swap，仅为不足部分分配
  新 swap 文件（为页头和 MiB 对齐略作上取整）；已有容量达标则不分配，`0` 表示不
  更改已有 swap。分配先用 `fallocate`，失败再用 `dd`，检查磁盘并至少保留 1 GiB
  空间；构建所需空间仍须另行预算。Swap 是**内存兜底，会消耗磁盘**，不能释放磁盘
  或保证无 OOM，频繁换页也可能拖慢构建。该输入属于 composite action，并非
  Run workflow 的手动输入。
- **阶段分组 + 心跳**：构建脚本共用 `scripts/log.sh`，在 Actions 中按阶段折叠
  `::group::`；`run_beat` 默认每 `15` 秒报告已运行时长，可用 `LP_BEAT_SECONDS`
  调整。构建心跳覆盖 `envsetup + lunch + m`，底层 stdout / stderr 直接透传；
  `lunch` 在同一 shell 内逐次执行并实时输出，每次失败均标出目标和退出码；产品名
  优先尝试 Android 15 的 `<product>-trunk_staging-eng`，再回退旧版双段式参数。
  显式传入三段式目标时仅尝试该目标，不替换用户指定的 release。
  Linux / Darwin / Windows host 构建默认使用 builder 自带的 `lptools_host`，继承官方
  `module_arm64`，并通过 `PRODUCT_SOURCE_ROOT_DIRS` 排除 `bionic/apex/` 中的设备 APEX
  模块；其余 Bionic 头文件和库定义仍可见。产品文件临时安装到源码树的 `device/` 下，
  退出时只删除本次创建的目录；显式选择其他产品时不安装此配置。在 `lunch` 前设置
  `TARGET_BUILD_UNBUNDLED=true`、`ALLOW_MISSING_DEPENDENCIES=true` 与
  `WITH_DEXPREOPT=false`（host 工具不需要设备端 ART / DEX 预优化），然后通过
  `m --soong-only` 指定 host 安装路径目标，避免模块名目标连带构建 Android 变体，
  也避免完整 GSI 产品对 VNDK 快照和镜像的要求。
  部分源码树允许无关模块缺失；所请求工具真正依赖的模块缺失仍会使构建失败。
- **时间戳与颜色**：logger 自己的状态、告警及心跳行默认带 UTC 时间（`LP_TS=0`
  可关闭），底层原始输出不保证每行有时间戳。交互终端或 GitHub Actions 可使用
  ANSI 颜色，设置非空 `NO_COLOR` 或 `TERM=dumb` 时关闭 logger 的颜色；这不会
  重写底层工具自身的颜色输出。
- **阶段结果与退出码**：结束时向 GitHub Step Summary 写阶段耗时及结果，本地写
  stdout；失败阶段记录失败退出码，脚本保留原失败码，避免 summary 或清理掩盖错误。

### 4.5 Release 页面上的来源信息

Release 说明里的三张表都是机器从产物里读出来的，没有手写字段：

| 表 | 内容 | 数据来源 |
|---|---|---|
| AOSP source | 请求的 ref、实际 fetch 的分支或 tag、manifest 提交、固化项目数、`aosp-manifest.xml` 的 SHA-256 | `repo manifest` 合并清单、`.repo/manifests` 的 git、已同步项目的实际 HEAD |
| Builder | lptools_builder 的仓库、提交、分支、run 链接 | `GITHUB_SHA` / `GITHUB_REF_NAME` / `GITHUB_RUN_ID` |
| Bundles | 每个平台的文件名、工具清单、SHA-256 | 各 bundle 的 `manifest.json` |

同样的数据也在产物本地：解压后 `manifest.json` 的 `aosp_branch` / `aosp_tag` /
`aosp_manifest_revision` / `aosp_pinned_manifest` 就是这几项。`aosp-manifest.xml`
必须包含本次实际同步项目的完整提交号；导出失败、空清单或哈希不符会阻止打包或发布。
按 tag 构建时允许分支字段为空，不把 repo 的本地 `default` 分支当作上游分支。
这些来源记录不包含完整环境，也不能证明重建会得到逐字节相同的二进制。

### 4.6 原版 AOSP 云端编译

独立工作流 **LP upstream build (unpatched)** 在 `build/upstream` 分支推送时运行，
也支持手动指定 `aosp_ref`（默认 `android-latest-release`）。它从 Google 官方 manifest
同步源码，用原版 Soong 编译 Linux x86_64 的 LP 和 sparse 工具；不加载 `patches/`，
也不运行历史 writer 构建和跨版本矩阵。新增的 `lptools_host` 产品配置只选择 host
安装目标，没有修改上游 C/C++ 或 Android.bp。

流程为同步、确认各上游项目干净、编译、打包目录内的工具往返验证、保存来源记录。
Artifact **lp-upstream-linux-x86_64** 包含 bundle、SHA-256、同步/构建日志和来源记录；失败时也尝试
保存实际拉取的 branch、manifest 提交、各项目提交和源码状态。只有云端实际通过
才代表本次 ref 编译成功。2026-11-01 已在 4 核 Codespaces 实测原版
`android-latest-release`（实际 `android17-release`）成功构建 7 个工具，完整流程
26 分 01 秒，热树复验 51 秒；镜像与 sparse 往返通过。源码身份、命令和验证边界
见[原版构建基线](docs/upstream-baseline.md)。其他 target 以此基线另行适配。

[scripts/make-upstream-workflow.py](scripts/make-upstream-workflow.py) 生成此入口，
复用脚本嵌入机制；修改嵌入的脚本后须重新生成。其他平台的上游能力与分阶段
适配点见 [target-porting.md](docs/target-porting.md)。

### 4.7 latest-release 跨版本兼容性验证（独立研究入口）

独立工作流 **LP latest-release compatibility** 在 `compat/latest-release` 分支推送时运行，
也支持手动指定 `latest_ref`（默认 `android-latest-release`）。它先编译当前源码的 Linux
x86_64 host 工具，再从官方 `android-10.0.0_r1` 至 `android-16.0.0_r1` 的精确提交编译
历史 `liblp` 与 `lpmake.cc`。历史 LP 源码保持原样；使用本次 latest 构建的 Clang、
libc++ 和辅助库，具体来源与哈希单独记录，因此不等同于七套完整旧版 AOSP 工具链重建。

测试包含合成 super 的 raw / sparse、元数据 10.0、两个 metadata slots、分区 SHA-256
比对，以及从解包结果调用 latest `lpmake` 重打包。历史 writer 声明支持 Virtual A/B
时，另要求 metadata 10.2 的读取与重打包。必测用例缺失、跳过或失败都会阻止通过；
外层还核对七个确切 ref 和实际测试 writer 的哈希。每次 `--out` 必须使用空目录，
避免把旧解包文件或旧 desparse 结果算成本次证据。

这些合成测试不覆盖 metadata 10.1、多块设备 super、实际固件、刷机或 OTA / snapshot
状态机；只有实际运行报告中的通过项才代表已验证范围。本地 harness 单元测试通过
只说明测试程序自身的判断符合预期。

Linux 上已有 latest AOSP 源码树和 bundle 时可以直接运行：

```bash
python3 -u scripts/run-lp-compatibility.py \
  --aosp /srv/aosp \
  --latest-bin /srv/dist/lptools-linux-x86_64/bin \
  --out /srv/compat-run-01
```

工作流 artifact 保留总体报告、逐用例日志、历史源码提交与构建命令，以及 latest bundle
和固化 manifest。即使 latest 编译失败，也尝试保存已同步项目的实际提交与失败前的来源
记录。[scripts/make-compat-workflow.py](scripts/make-compat-workflow.py) 将所需脚本的
精确字节嵌入 workflow；修改这些文件后须重新运行生成器。

## 5. 产物契约（给 imgpatchtools-rs 用）

```
lptools-linux-x86_64/
├── bin/
│   ├── lpmake  lpdump  lpunpack  [lpadd]  [lpflash]
│   └── simg2img  img2simg
├── [lib64/]          # Soong 已安装的主机共享运行库，保留相对加载路径
├── [lib/]            # 存在时一并收集，链接解引用以脱离构建目录
├── manifest.json     # 本次构建记录的版本与文件哈希
├── SHA256SUMS
└── aosp-manifest.xml  # 必需：已同步项目逐一固化到实际提交
```

Windows 版使用相同的元数据布局，`platform` 为 `windows-x86_64`，二进制带 `.exe`，
使用 `.zip` 便于 `Expand-Archive` 解压。`lpadd.exe`、稀疏镜像工具和
`ext2simg.exe` / `append2simg.exe` 均取决于所选源码树的可选构建结果：

```
lptools-windows-x86_64.zip
└── lptools-windows-x86_64/
    ├── bin/
    │   ├── lpmake.exe  lpdump.exe  lpunpack.exe  [lpadd.exe]
    │   └── [simg2img.exe]  [img2simg.exe]  [ext2simg.exe]  [append2simg.exe]
    ├── manifest.json
    ├── SHA256SUMS
    └── aosp-manifest.xml
```

`manifest.json`（`schema: 2`）：

| 字段 | 含义 |
|---|---|
| `bundle` / `bundle_version` / `platform` | 产物标识，如 `lptools` / `android-15.0.0_r1` / `linux-x86_64` |
| `kind` / `host_arch` / `target_abi` | 产物种类（`host` / `windows` / `android`）与架构 |
| `aosp_ref` | 请求的 ref（tag 或分支），CI 的 `aosp_ref` 输入 |
| `aosp_branch` / `aosp_tag` | 从合并清单和 manifest 仓库的实际 fetch ref 解析；tag 构建不虚构分支名 |
| `aosp_manifest_revision` / `aosp_manifest_url` | manifest 仓库的提交与来源，用于溯源；不能单独固化所有项目或构建环境 |
| `aosp_pinned_manifest` | `{file, projects, sha256}`：已同步项目固化清单的文件名、正项目数与 SHA-256；缺失或无效时构建失败 |
| `build_product` | 本次 `lunch` 目标；host / Windows 用于构建引导，Android 对应尝试构建的设备产品 |
| `builder.commit` / `builder.branch` / `builder.run` | 构建脚本自身的版本与 CI run |
| `tools.<name>` | 每个二进制的 SHA-256 |
| `files.<path>` | 每个文件的 SHA-256 + size |

下游校验建议：`sha256(tools[name]) == manifest.tools[name]`，并把 `aosp_branch` +
`aosp_manifest_revision` 写进自己的日志，出问题时能直接对上是哪个 AOSP 分支的哪个提交。

### 5.1 让 imgpatchtools-rs 找到它

解压后把 bundle 交给 `imgpatchtools-rs` 有四种方式，任选其一：

```bash
# 1) 环境变量指向 bundle 根目录（指向其中的 bin/ 也可以）
export IMGTOOLS_LPTOOLS_DIR=$HOME/lptools-linux-x86_64

# 2) 放在 imgpatchtools-rs 可执行文件旁边
tar -xzf lptools-linux-x86_64.tar.gz -C "$(dirname "$(command -v imgpatchtools-rs)")"

# 3) 加进 PATH
export PATH=$HOME/lptools-linux-x86_64/bin:$PATH

# 4) Windows + WSL2：bundle 放进 WSL，Windows 侧设发行版名（wsl -l -q 里的那个）
set IMGTOOLS_LPTOOLS_WSL=Ubuntu
```

确认接线（退出码 0 = 找到，1 = 没找到，可直接写进 CI）：

```bash
imgpatchtools-rs super doctor          # 文本报告：模式、bundle 目录、每个二进制的路径
imgpatchtools-rs super doctor --json   # 机器可读
```

标准往返（super.img → 分区镜像 → 改 → 回包）：

```bash
imgpatchtools-rs super info super.img                     # 元数据摘要
imgpatchtools-rs super info super.img --json > super.json # 也可直接产出清单
imgpatchtools-rs super unpack super.img -o out            # 解出 *_a.img 并写 out/super.json

#   ...用 blockimg / applypatch 等子命令处理 out/system_a.img...

imgpatchtools-rs super pack --layout out -o super_new.img \
    --image system_a=out/system_a.patched.img             # 只替换其中一个分区
imgpatchtools-rs super info super_new.img                 # 与原始对照
```

## 6. Windows 怎么办

Windows 可选择原生 exe 或通过 WSL2 运行 Linux bundle；先确认实际 Release 中有对应
产物，并在自己的镜像上验证：

| 方案 | 产物 | 机器上要装什么 | 适用 |
|---|---|---|---|
| **6.1 原生 .exe** | `lptools-windows-x86_64.zip` | 无需 WSL；运行依赖以实际产物检查为准 | 最终用户、CI、不方便开 WSL 的机器 |
| **6.2 WSL2** | Linux 版 `.tar.gz` | WSL2 + 一个发行版 | 已有 WSL，希望直接使用 Linux bundle |

### 6.1 原生 .exe（推荐）

本方案使用 AOSP 的 Windows host-cross 路径，依赖源码树提供相应工具链和 Soong
配置，例如 `prebuilts/gcc/linux-x86/host/x86_64-w64-mingw32-4.8` 与
`build/soong/cc/config/x86_windows_host.go`。脚本从
`out/host_cross/windows-<arch>/bin`（或 `OUT_DIR` 下对应路径）收集输出，并检查
PE 架构；目标是否存在及依赖能否构建须按所选 ref 验证。

`patches/` 中的三个补丁针对 `android-15.0.0_r25` 上下文生成，为相关模块增加
Windows 目标及可移植性修改，并新增静态 `lpdumps` 目标以避开原 `liblpdump` 共享库
依赖链；收集时将它命名为 `lpdump.exe`。这些改动尚未经过真实 AOSP 构建 / Windows
运行验证。`scripts/build-windows.sh` 执行补丁预检、构建、收集与打包，成功时逆向
本次补丁，失败时保留：

```bash
bash scripts/build-windows.sh --skip-sync       # 复用已经同步好的 aosp/ 树
bash scripts/build-windows.sh --arch x86        # 尝试所选树提供的 32 位 Windows 目标
bash scripts/build-windows.sh --revert-only     # 仅逆向精确匹配的补丁，漂移时停止
```

默认请求的产物是 `dist/lptools-windows-x86_64.zip`；CI 的 `windows` job 成功且通过
资产检查是发布条件之一。脚本在 Linux 上构建 exe，不运行 Windows 冒烟测试；下面的
命令是下载后在 Windows 上进行验证的用法，不是已完成验证的报告：

```powershell
# 解压到 imgpatchtools-rs.exe 旁边，或设 IMGTOOLS_LPTOOLS_DIR 指向它
imgpatchtools-rs super doctor        # 查看报告中的工具路径及状态，按当前下游版本解释退出码
imgpatchtools-rs super unpack super.img -o out
imgpatchtools-rs super pack --layout out -o super_new.img --image system_a=out/system_a.patched.img
```

补丁事务要求相关工程的 tree / index 干净，并拒绝未跟踪文件；`git apply` 不修改
index。成功时仅逆向本次补丁，失败或 `--keep-patches` 时保留现场，供检查和导出
`git diff`；精确逆向不能通过时停止，不能用整工程 checkout 丢弃修改。留存的 Windows
补丁可能使 Linux 构建报 `O_BINARY` 等错误，复用树前应先检查并安全还原。

补丁处理的三类可移植性问题（细节见 `patches/README.md`；实际效果仍须构建与运行验证）：

1. `O_BINARY`——避免 Windows 文本模式在二进制 I/O 中进行换行转换；
2. `rename()`——Windows 不能覆盖已打开/已存在的文件，liblp 写 geometry 要先 `close(fd)`；
3. mingw 没有 `sysexits.h` / `asprintf` / `unix_io_manager`，得给替代实现。

### 6.2 WSL2（备选）

在 Windows 上安装 WSL2，并把匹配 WSL 架构的 Linux bundle 放进发行版内。
`imgpatchtools-rs` 提供 WSL 调用及路径转换入口；实际发现结果应通过
`super doctor` 检查。

```powershell
wsl --install -d Ubuntu
# 在 WSL 里解包 lptools-linux-x86_64.tar.gz 到 ~/lptools
setx IMGTOOLS_LPTOOLS_WSL Ubuntu     # 值是 wsl -l -q 里显示的名字
```

### 6.3 为什么不维护一份 mingw 移植 fork

本仓库在同步的 AOSP 树上应用 `patches/`，保留与上游的对应关系。补丁目标是增加
Windows 构建目标并适配 I/O 模式、路径和缺失的 POSIX 接口，仍需随源码 ref 检查漂移
及验证行为；不能仅凭补丁的设计目的断言 LP 语义与上游逐字节等价。成功构建默认逆向
本次补丁，失败现场与 `--keep-patches` 的例外见 4.3 节。


## 7. 排错

| 现象 | 原因 / 处理 |
|---|---|
| `TARGET_PRODUCT is not defined` | 没 `lunch`；确认脚本里的候选产品存在，或用 `--product` 指定 |
| `lunch: invalid lunch combo` | 该 AOSP 版本没有这个产品；看 `build/make/target/product/AndroidProducts.mk` |
| `No Android.mk found` | 还在用 `mmm`；改用 `m <模块>` |
| `module "xxx" not found` | 依赖清单缺仓库：把定义该模块的 `platform/...` 加进 `required-projects.txt`，先跑 `tools/check_projects.py` 确认拼写 |
| `repo sync` 中途磁盘满 | 检查 prepare action 的资源报告、源码 / 输出 / 缓存占用与 swap 文件；按实际 runner 镜像清理或换更大磁盘，swap 不释放磁盘 |
| clang 报 `prebuilts/clang/... not found` | 清单里的 `prebuilts/clang/host/linux-x86` 没同步成功，重跑该 project |
| 构建 OOM / 被 kill | 检查内存 / swap 数据、退出码与内核日志；降低 `--jobs`，确认活跃 swap 容量。12 GiB 目标不保证避免 OOM |
| Windows 构建：`could not apply 000X-...` | 补丁按 `android-15.0.0_r25` 生成，换 ref 后上下文可能漂移；脚本会用 `patch -p1 --fuzz` 兜底，仍失败就按 `patches/README.md` 用 `git diff` 重新导出 |
| Windows 构建：`no host_cross output` | 检查 Soong 目标、`OUT_DIR`、补丁状态与前面的失败日志；缺少输出并不能单独证明补丁被覆盖。修正原因后再重跑 |
| Windows 构建：`missing required tool: zip` | 装 zip（`apt-get install zip` / `pacman -S zip`），脚本产出 zip 而不是 tar.gz |
| 之后 Linux 构建突然编不过（`O_BINARY was not declared`） | 检查是否留有 Windows 补丁；`scripts/build-windows.sh --revert-only` 只逆向精确匹配部分，漂移时保留现场并手动检查 |

## 8. 相关工具

- `analyze_deps.py`：解析 `Android.bp`，打印某个模块的传递依赖闭包、找不到归属仓库的
  模块、以及源码里的跨平台障碍 `#include`。用于**排查该补哪个 project**，不改构建。

```bash
python3 analyze_deps.py aosp/system/extras/partition_tools aosp/system/core \
        --tools lpmake lpunpack lpdump --no-includes
```

## 9. 版本策略

- 产物跟随 `aosp_ref`，一个 ref 一套产物，**不做跨版本混合**；
- 手动发布的 Release tag：`lptools-<ref_slug>-<YYYYMMDD>`（UTC 日期）；简单 ref 原样保留，含 `/` 等字符的 ref 会规范化并附哈希。Tag push 使用触发的 `lptools-*` tag；
- 新建 Release 时传 `--latest`；更新已有 Release 只替换资产与 notes，不保证重新排序 `latest`。各 bundle 内的 `manifest.json` 用于溯源。

## 10. 许可证

构建脚本：随本仓库。产物为 AOSP 二进制，遵循 AOSP 的 Apache-2.0（以及各依赖库自身
许可证，如 liblp/libsparse 为 Apache-2.0）。

`patches/` 下的三个补丁是 AOSP 源码的修改，同样是 Apache-2.0；来源与归属见
`patches/README.md`。
