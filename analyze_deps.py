#!/usr/bin/env python3
"""
analyze_deps.py — 分析 Android.bp（Soong）构建文件，找出 LP 分区工具
（lpmake / lpdump / lpunpack / lpadd / lpflash）的完整依赖闭包与跨平台障碍点。

为什么要这个脚本：
  AOSP 的 partition_tools 依赖一串分散在多个 git 仓库里的 C++ 库
  （liblp、libsparse、libbase、liblog…）。AOSP 上游每次一改，人肉 grep
  既慢又容易漏。这个脚本直接解析 Android.bp，自动给出：

    1. 目标工具及其依赖模块的传递闭包（含每个模块的源文件清单）
    2. 被引用、但在当前扫描目录里找不到的模块（=> 需要 clone 另外的仓库）
    3. 每个源文件里疑似跨平台障碍的 #include（openssl / zlib / sys/* / linux/* …）
       —— 这些正是"直接用 zig cc 交叉编译"不平凡的原因。

用法：
  python3 analyze_deps.py <dir> [<dir> ...] \
        [--tools lpmake lpdump lpunpack] [--no-includes]

只依赖标准库，Python 3.8+ 即可。
"""

import argparse
import os
import re
import sys
from collections import deque

# 会承载 srcs / 库依赖的模块类型
CC_TYPES = {
    "cc_binary", "cc_binary_host", "cc_library", "cc_library_shared",
    "cc_library_static", "cc_library_headers", "cc_defaults",
    "cc_prebuilt_library", "cc_prebuilt_binary",
}

# 会引入另一个模块（用于闭包展开）的 key
DEP_KEYS = (
    "shared_libs", "static_libs", "whole_static_libs", "header_libs",
    "export_shared_lib_headers", "export_static_lib_headers", "defaults",
)

# 疑似跨平台/外部依赖的 include 特征
OBSTACLE_PATTERNS = [
    (r"#include <openssl/", "openssl"),
    (r"#include <zlib.h>", "zlib"),
    (r"#include <bzlib.h>", "bzip2"),
    (r"#include <lzma.h>", "liblzma"),
    (r"#include <sys/", "sys/* (POSIX)"),
    (r"#include <linux/", "linux/*"),
    (r"#include <unistd.h>", "unistd (POSIX)"),
    (r"#include <pthread.h>", "pthread"),
    (r"#include <dirent.h>", "dirent (POSIX)"),
    (r"#include <fcntl.h>", "fcntl (POSIX)"),
    (r"#include <endian.h>", "endian.h (glibc)"),
    (r"#include <arpa/", "arpa/* (POSIX)"),
    (r"#include <netinet/", "netinet/*"),
    (r"#include <syslog.h>", "syslog (POSIX)"),
    (r"#include <poll.h>", "poll (POSIX)"),
    (r"#include <cutils/", "cutils (libcutils)"),
    (r"#include <log/", "log (liblog)"),
    (r"#include <android/", "android/* (bionic)"),
    (r"#include <ext4_utils/", "ext4_utils"),
    (r"#include <liblp/", "liblp"),
    (r"#include <libsparse/", "libsparse"),
    (r"#include <android-base/", "libbase"),
]


# ---------------------------------------------------------------------------
# 极简 Android.bp 解析器（覆盖 cc_* 模块 / 顶层变量 / target 嵌套 / 列表拼接）
# ---------------------------------------------------------------------------

class BpParser:
    def __init__(self, text, path=""):
        self.s = text
        self.n = len(text)
        self.i = 0
        self.path = path
        self.vars = {}

    def error(self, msg):
        line = self.s.count("\n", 0, self.i) + 1
        raise ValueError(f"{self.path}:{line}: {msg}")

    def skip_ws(self):
        while self.i < self.n:
            c = self.s[self.i]
            if c in " \t\r\n":
                self.i += 1
            elif c == "/" and self.i + 1 < self.n and self.s[self.i + 1] == "/":
                while self.i < self.n and self.s[self.i] != "\n":
                    self.i += 1
            elif c == "/" and self.i + 1 < self.n and self.s[self.i + 1] == "*":
                self.i += 2
                while self.i + 1 < self.n and not (
                    self.s[self.i] == "*" and self.s[self.i + 1] == "/"
                ):
                    self.i += 1
                self.i += 2
            else:
                break

    def peek(self):
        self.skip_ws()
        return self.s[self.i] if self.i < self.n else ""

    def parse_ident(self):
        self.skip_ws()
        start = self.i
        while self.i < self.n and (self.s[self.i].isalnum() or self.s[self.i] in "_-."):
            self.i += 1
        if self.i == start:
            self.error(f"expected identifier, got {self.s[self.i:self.i+10]!r}")
        return self.s[start:self.i]

    def parse_string(self):
        self.skip_ws()
        self.i += 1  # skip opening quote
        out = []
        while self.i < self.n:
            c = self.s[self.i]
            if c == "\\":
                out.append(self.s[self.i + 1])
                self.i += 2
            elif c == '"':
                self.i += 1
                return "".join(out)
            else:
                out.append(c)
                self.i += 1
        self.error("unterminated string")

    def parse_number(self):
        self.skip_ws()
        start = self.i
        while self.i < self.n and (self.s[self.i].isalnum() or self.s[self.i] in "+-."):
            self.i += 1
        tok = self.s[start:self.i]
        try:
            return int(tok, 0)
        except ValueError:
            return tok

    def parse_atom(self):
        c = self.peek()
        if c == '"':
            return self.parse_string()
        if c == "[":
            return self.parse_list()
        if c == "{":
            return self.parse_object()
        if c == "":
            self.error("unexpected EOF")
        if c.isdigit() or c == "-":
            return self.parse_number()
        if c.isalpha() or c == "_":
            name = self.parse_ident()
            if name == "true":
                return True
            if name == "false":
                return False
            if name in self.vars:
                return self.vars[name]
            return ("var", name)
        self.error(f"unexpected char {c!r}")

    def parse_expr(self):
        v = self.parse_atom()
        while True:
            self.skip_ws()
            if self.i < self.n and self.s[self.i] == "+":
                self.i += 1
                v = self.concat(v, self.parse_atom())
            else:
                return v

    @staticmethod
    def concat(a, b):
        if isinstance(a, list) and isinstance(b, list):
            return a + b
        if isinstance(a, str) and isinstance(b, str):
            return a + b
        if isinstance(a, list):
            return a + [b]
        if isinstance(b, list):
            return [a] + b
        return [a, b]

    def parse_list(self):
        self.skip_ws()
        self.i += 1  # skip '['
        items = []
        while True:
            self.skip_ws()
            if self.i < self.n and self.s[self.i] == "]":
                self.i += 1
                return items
            items.append(self.parse_expr())
            self.skip_ws()
            if self.i < self.n and self.s[self.i] == ",":
                self.i += 1
                continue
            if self.i < self.n and self.s[self.i] == "]":
                self.i += 1
                return items
            self.error("expected ',' or ']' in list")

    def parse_object(self):
        self.skip_ws()
        self.i += 1  # skip '{'
        obj = {}
        while True:
            self.skip_ws()
            if self.i < self.n and self.s[self.i] == "}":
                self.i += 1
                return obj
            key = self.parse_ident()
            self.skip_ws()
            if self.i >= self.n or self.s[self.i] != ":":
                self.error(f"expected ':' after key {key!r}")
            self.i += 1
            obj[key] = self.parse_expr()
            self.skip_ws()
            if self.i < self.n and self.s[self.i] == ",":
                self.i += 1

    def parse_file(self):
        """返回 [(module_type, body_dict), ...]，顶层变量存入 self.vars"""
        stmts = []
        while True:
            self.skip_ws()
            if self.i >= self.n:
                break
            name = self.parse_ident()
            self.skip_ws()
            if self.i < self.n and self.s[self.i] == "=":
                self.i += 1
                self.vars[name] = self.parse_expr()
            elif self.i < self.n and self.s[self.i] == "{":
                stmts.append((name, self.parse_object()))
            else:
                self.error(f"expected '=' or '{{' after {name!r}")
        return stmts


# ---------------------------------------------------------------------------
# 收集模块 / 计算闭包 / 扫描 include
# ---------------------------------------------------------------------------

def _as_strs(v):
    """把任意解析值压平成字符串列表"""
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    if isinstance(v, (list, tuple)):
        out = []
        for x in v:
            out += _as_strs(x)
        return out
    if isinstance(v, tuple) and len(v) == 2 and v[0] == "var":
        return [v[1]]
    return [str(v)]


def _all_srcs(body):
    """递归收集 body 里所有 srcs（含 target.{host,windows,linux,...} 嵌套）"""
    out = []
    def walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "srcs":
                    out.extend(_as_strs(v))
                else:
                    walk(v)
        elif isinstance(obj, list):
            for x in obj:
                walk(x)
    walk(body)
    return out


def collect_modules(dirs):
    """扫描每个目录下的 Android.bp，返回 {module_name: {type,file,body}}"""
    registry = {}
    collisions = []
    for d in dirs:
        for root, _, files in os.walk(d):
            if os.sep + ".git" in root + os.sep:
                continue
            for fn in files:
                if fn != "Android.bp":
                    continue
                path = os.path.join(root, fn)
                try:
                    with open(path, encoding="utf-8", errors="replace") as f:
                        text = f.read()
                    stmts = BpParser(text, path).parse_file()
                except Exception:
                    continue
                for mtype, body in stmts:
                    name = body.get("name")
                    if not isinstance(name, str):
                        continue
                    if name in registry:
                        collisions.append((name, registry[name]["file"], path))
                        continue
                    registry[name] = {"type": mtype, "file": path, "body": body}
    return registry, collisions


def compute_closure(registry, roots):
    """从 roots 展开依赖闭包；返回 (seen: {name: module|None}, unresolved: set)"""
    seen = {}
    unresolved = set()
    q = deque(roots)
    while q:
        m = q.popleft()
        if m in seen:
            continue
        mod = registry.get(m)
        if mod is None:
            unresolved.add(m)
            seen[m] = None
            continue
        seen[m] = mod
        body = mod["body"]
        for key in DEP_KEYS:
            for d in _as_strs(body.get(key)):
                q.append(d)
    return seen, unresolved


def _repo_of(path, dirs):
    for d in dirs:
        dd = os.path.abspath(d)
        if os.path.abspath(path).startswith(dd + os.sep):
            return os.path.basename(dd.rstrip(os.sep)) or dd
    return os.path.basename(os.path.dirname(path)) or "?"


def scan_includes(dirs, seen):
    """对闭包里的源文件扫描 include，返回 {类别: [(文件, 行号, 文本)]}"""
    hits = {}
    # 源文件路径解析缓存
    for name, mod in seen.items():
        if mod is None:
            continue
        bpdir = os.path.dirname(mod["file"])
        for src in _all_srcs(mod["body"]):
            if src.startswith(":"):
                continue  # filegroup 引用，跳过
            cand = os.path.join(bpdir, src)
            if not os.path.isfile(cand):
                # 兜底：按 basename 全树找
                base = os.path.basename(src)
                found = None
                for d in dirs:
                    for root, _, files in os.walk(d):
                        if base in files:
                            found = os.path.join(root, base)
                            break
                    if found:
                        break
                cand = found or cand
            if not os.path.isfile(cand):
                continue
            try:
                with open(cand, encoding="utf-8", errors="replace") as f:
                    for lineno, line in enumerate(f, 1):
                        for pat, cat in OBSTACLE_PATTERNS:
                            if re.search(pat, line):
                                hits.setdefault(cat, []).append((cand, lineno, line.strip()))
                                break
            except Exception:
                continue
    return hits


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

def render(roots, seen, unresolved, dirs, hits, show_includes):
    print("=" * 78)
    print("依赖闭包（按模块）")
    print("=" * 78)
    for name in sorted(seen):
        mod = seen[name]
        if mod is None:
            print(f"\n[{name}]  ⚠️ 未找到（可能在别的仓库）")
            continue
        mtype = mod["type"]
        repo = _repo_of(mod["file"], dirs)
        srcs = _all_srcs(mod["body"])
        deps = []
        for key in DEP_KEYS:
            for d in _as_strs(mod["body"].get(key)):
                deps.append(d)
        print(f"\n[{name}]  {mtype}  (repo: {repo})")
        print(f"    file: {mod['file']}")
        if srcs:
            print(f"    srcs ({len(srcs)}): {', '.join(srcs[:12])}"
                  + (" ..." if len(srcs) > 12 else ""))
        if deps:
            print(f"    deps ({len(deps)}): {', '.join(sorted(set(deps)))}")

    print("\n" + "=" * 78)
    print(f"未解析的外部模块（共 {len(unresolved)} 个）—— 这些指向额外仓库或系统库")
    print("=" * 78)
    for u in sorted(unresolved):
        print(f"  - {u}")

    if show_includes:
        print("\n" + "=" * 78)
        print("跨平台障碍扫描（#include）")
        print("=" * 78)
        if not hits:
            print("  （无命中）")
        for cat in sorted(hits):
            rows = hits[cat]
            print(f"\n[{cat}]  {len(rows)} 处")
            for f, ln, txt in rows[:8]:
                print(f"    {f}:{ln}: {txt}")
            if len(rows) > 8:
                print(f"    … 其余 {len(rows)-8} 处省略")


def main():
    ap = argparse.ArgumentParser(description="分析 AOSP partition_tools 的依赖闭包")
    ap.add_argument("dirs", nargs="+", help="包含 Android.bp 的目录（可多个）")
    ap.add_argument("--tools", nargs="+",
                    default=["lpmake", "lpdump", "lpunpack", "lpadd", "lpflash"],
                    help="目标模块名（默认 5 个 LP 工具）")
    ap.add_argument("--no-includes", action="store_true",
                    help="跳过 include 障碍扫描")
    args = ap.parse_args()

    registry, collisions = collect_modules(args.dirs)
    if collisions:
        print(f"[warn] {len(collisions)} 个模块名冲突（保留首个）", file=sys.stderr)

    seen, unresolved = compute_closure(registry, args.tools)

    # 只统计 cc_* 相关模块里的依赖，未解析的也一并展示
    hits = {} if args.no_includes else scan_includes(args.dirs, seen)

    print(f"扫描目录: {', '.join(args.dirs)}")
    print(f"解析到模块总数: {len(registry)}")
    print(f"目标工具: {', '.join(args.tools)}")
    print(f"闭包内模块数: {sum(1 for m in seen.values() if m is not None)}")
    print()
    render(args.tools, seen, unresolved, args.dirs, hits, not args.no_includes)


if __name__ == "__main__":
    main()
