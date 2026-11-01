# 原版 AOSP Linux 构建基线

2026-11-01 在 GitHub Codespaces 实际拉取官方源码，使用原版 Soong 成功编译
Linux x86_64 工具。上游 C/C++、Android.bp 和其他受 Git 管理的源码未修改，
构建后对 41 个已同步项目的工作区、暂存区与未跟踪文件检查均通过。
新增的 `lptools_host` 产品配置仅选择 host 安装目标。

## 源码与环境

| 项目 | 实测值 |
|---|---|
| 请求 ref | `android-latest-release` |
| 实际项目分支 | `android17-release` |
| 官方 manifest 提交 | `ad156f32caaa06dae91c02d443f6a8fe210eaa54` |
| 实际同步项目 | 41，逐项目完整 SHA 见[固化清单](evidence/upstream-20261101/aosp-manifest.xml) |
| 固化清单 SHA-256 | `2700c1f9e1f184652aaf78220743672904b015b5bd7be67713cb4578a1cde931` |
| 构建宿主 | Linux x86_64，Ubuntu 24.04.5，4 核、约 16 GiB RAM |
| 构建产品 | `lptools_host-trunk_staging-eng` |
| 并行度 | `-j4` |
| 上游补丁 | 无 |

首次完整流程 26 分 01 秒：init 5 秒、sync 14 分 47 秒、构建 11 分 05 秒、
收集与验证各不足 1 秒、打包 4 秒。随后使用同一源码树执行 `--skip-sync`
复验，47 秒构建、4 秒打包，总计 51 秒。以上是单次运行记录，不是性能保证。

## 成功命令

```bash
export PATH="$HOME/.local/bin:$PATH" LPTOOLS_PRODUCT=lptools_host
bash scripts/build-linux.sh \
  --ref android-latest-release --jobs 4 \
  --project-list /workspaces/lptools_builder/config/projects-latest.txt \
  --work /tmp/lptools-aosp --out /workspaces/lp-debug/dist

bash scripts/build-linux.sh \
  --ref android-latest-release --jobs 4 --skip-sync \
  --work /tmp/lptools-aosp --out /workspaces/lp-debug/dist
```

AOSP 源码放在 Codespace 的 `/tmp` 大容量卷；构建产物与日志保存在
`/workspaces/lp-debug`。`/tmp` 内容可能在重建容器时丢失，源码并不属于持久备份。
成功产物、日志与来源记录已另行下载并核对 SHA-256。

## 产物与验证

产物含 `lpmake`、`lpdump`、`lpunpack`、`lpadd`、`lpflash`、`simg2img`、
`img2simg` 七个二进制，以及 Soong 安装的 `lib64` 运行库。复验后的压缩包
为 20,518,449 字节，SHA-256：

```text
b176c4e2101380f8cb3a079199f0b07c14fedeee33b886714510223d4857316a
```

两次构建中的七个二进制哈希一致。清单包含每个运行库及二进制的大小与哈希，
见[实际 bundle 清单](evidence/upstream-20261101/manifest.json)。打包后校验通过。

冒烟检查 7 项全部通过：三个主要 LP 工具可执行、创建 64 MiB super、
读取分区表、解包后逐字节比较 system/vendor，以及 sparse 编解码往返。
这证明该 Linux 宿主上的合成镜像流程可用；`lpadd`/`lpflash` 仅确认编译和打包，
没有执行刷写。未据此推断 Android 10–16 实际固件、OTA 或其他平台已通过。

本次修正的 builder 问题是 host 安装路径应为 `OUT_DIR/host/...`，即使
使用 `--soong-only` 也不在 `OUT_DIR/soong/host/...`；另修复 rolling manifest
分支名称不同于项目分支时的来源记录，实际读取项目已抓取 ref 并核对 HEAD。

构建使用的 builder HEAD 为 `da5ed55db5fbc1f0380b3f53b9219008ce960141`，
工作区包含上述安装路径和来源 helper 修复，不能仅靠 HEAD 重现。
[builder 差异](evidence/upstream-20261101/builder.diff)和
[实际输入哈希](evidence/upstream-20261101/builder-inputs.sha256)保留了这个区别；
固化版本仅另外整理注释、生成工作流并保存日志。

## 固化的工作流

[原版构建入口](../.github/workflows/lp-upstream.yml)由
[生成器](../scripts/make-upstream-workflow.py)生成，在 `build/upstream` 推送或
手动指定 ref 时运行。流程为官方同步、源码清洁检查、host 编译、镜像往返、
来源记录和 Artifact 上传。源码脚本逐字节嵌入并校验摘要，不需要 runner 私钥。
Artifact 保存 bundle、哈希、逐项目源码身份及同步/编译日志。

Codespaces 成功与 Actions 成功分别记录；工作流实际结果以对应运行页为准。
其他 target 的移植依据见[适配基线](target-porting.md)。
