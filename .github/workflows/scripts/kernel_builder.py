"""GKI Kernel 构建器。

每一步都显式指定 cwd，不再依赖进程级 os.chdir；下载和打补丁走统一助手，
补丁应用失败会显式报错，而不是静默产出坏镜像。
"""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import (
    ANYKERNEL_CONFIG,
    BBG_CONFIG,
    KERNEL_PATCHES_CONFIG,
    KPM_KPIMG_URL,
    KPM_KPTOOLS_URL,
    KPM_PATCH_SDK_VERSION,
    KSU_REPO_CONFIG,
    LEGACY_FIXES,
    OP8E_PATCH_URL,
    SUKISU_PATCH_REPO_CONFIG,
    SUSFS_618_BUNDLE,
    SUSFS_618_BRANCH,
    SUSFS_COMPAT_SCRIPTS,
    SUSFS_REPO_CONFIG,
    TOOLCHAIN_CONFIG,
    BuildConfig,
)

logger = logging.getLogger(__name__)


def q(value) -> str:
    """Shell 参数转义。"""
    return shlex.quote(str(value))


@dataclass
class BuildResult:
    success: bool
    config: BuildConfig
    message: str = ""
    artifacts: list = field(default_factory=list)
    build_time: Optional[float] = None


class ShellCommand:
    def __init__(self, cwd: Optional[str] = None, env: Optional[dict] = None):
        self.cwd = str(cwd) if cwd else None
        self.env = env or os.environ.copy()

    def run(self, cmd: str, cwd: Optional[str] = None, check: bool = True,
            capture_output: bool = False, timeout: Optional[int] = None) -> subprocess.CompletedProcess:
        logger.info("$ %s", cmd)
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                cwd=str(cwd) if cwd else self.cwd,
                env=self.env,
                capture_output=capture_output,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            logger.error("命令超时: %s", cmd)
            raise

        if check and result.returncode != 0:
            logger.error("命令失败 (exit %s): %s", result.returncode, cmd)
            if capture_output:
                if result.stdout:
                    logger.error("stdout: %s", result.stdout.strip()[-2000:])
                if result.stderr:
                    logger.error("stderr: %s", result.stderr.strip()[-2000:])
            raise subprocess.CalledProcessError(
                result.returncode, cmd, output=result.stdout, stderr=result.stderr
            )
        return result

    def output(self, cmd: str, cwd: Optional[str] = None) -> str:
        result = self.run(cmd, cwd=cwd, check=False, capture_output=True)
        return result.stdout.strip() if result.returncode == 0 else ""


class KernelBuilder:
    KERNEL_CONFIG_TEMPLATE = """
# === KernelSU Config ===
CONFIG_KSU=y
CONFIG_KPM=y

# === TMPFS Config ===
CONFIG_TMPFS_XATTR=y
CONFIG_TMPFS_POSIX_ACL=y

# === Network Config ===
CONFIG_IP_NF_TARGET_TTL=y
CONFIG_IP6_NF_TARGET_HL=y
CONFIG_IP6_NF_MATCH_HL=y

# === BBR Config ===
CONFIG_TCP_CONG_ADVANCED=y
CONFIG_TCP_CONG_BBR=y
CONFIG_NET_SCH_FQ=y
CONFIG_TCP_CONG_BIC=n
CONFIG_TCP_CONG_WESTWOOD=n
CONFIG_TCP_CONG_HTCP=n

# === SUSFS Config ===
CONFIG_KSU_SUSFS=y
CONFIG_KSU_SUSFS_INLINE_HOOK=y
CONFIG_KSU_SUSFS_SUS_PATH=y
CONFIG_KSU_SUSFS_SUS_MOUNT=y
CONFIG_KSU_SUSFS_SUS_KSTAT=y
CONFIG_KSU_SUSFS_SPOOF_UNAME=y
CONFIG_KSU_SUSFS_ENABLE_LOG=y
CONFIG_KSU_SUSFS_HIDE_KSU_SUSFS_SYMBOLS=y
CONFIG_KSU_SUSFS_SPOOF_CMDLINE_OR_BOOTCONFIG=y
CONFIG_KSU_SUSFS_OPEN_REDIRECT=y
CONFIG_KSU_SUSFS_SUS_MAP=y
CONFIG_KSU_SUSFS_PRIORITY_UID_FILTER=y
"""

    ZRAM_CONFIG_5_10 = "CONFIG_ZSMALLOC=y\nCONFIG_ZRAM=y\nCONFIG_MODULE_SIG=n\nCONFIG_CRYPTO_LZO=y\nCONFIG_ZRAM_DEF_COMP_LZ4KD=y\n"
    ZRAM_CONFIG_COMMON = "CONFIG_CRYPTO_LZ4HC=y\nCONFIG_CRYPTO_LZ4K=y\nCONFIG_CRYPTO_LZ4KD=y\nCONFIG_CRYPTO_842=y\nCONFIG_CRYPTO_LZ4K_OPLUS=y\nCONFIG_ZRAM_WRITEBACK=y\n"

    def __init__(self, config: BuildConfig, workspace: str):
        self.config = config
        self.workspace = Path(workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.env = os.environ.copy()
        self.shell = ShellCommand(cwd=str(self.workspace), env=self.env)

        self.work_dir = self.workspace / config.config_name
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.susfs_dir = self.workspace / "susfs4ksu"
        self.sukisu_patch_dir = self.workspace / "SukiSU_patch"
        self.anykernel_dir = self.workspace / "AnyKernel3"
        self.kernel_patches_dir = self.workspace / "kernel_patches"
        self.toolchain_dir = self.workspace / "toolchain"
        self.mkbootimg_dir = self.workspace / "mkbootimg"
        self.repo_path: Optional[Path] = None

        self.ksu_ref_actual: Optional[str] = None
        self.ksu_commit_actual: Optional[str] = None

        self._setup_env()

    # ------------------------------------------------------------------
    # 基础助手
    # ------------------------------------------------------------------

    def _setup_env(self):
        self.env["CONFIG"] = self.config.config_name
        self.env["CCACHE_COMPILERCHECK"] = "%compiler% -dumpmachine; %compiler% -dumpversion"
        self.env["CCACHE_NOHASHDIR"] = "true"
        self.env["CCACHE_HARDLINK"] = "true"
        self.shell.env = self.env

    def _run(self, cmd: str, cwd: Optional[Path] = None, **kwargs) -> subprocess.CompletedProcess:
        return self.shell.run(cmd, cwd=cwd, **kwargs)

    def _git_output(self, args: list, cwd: Path) -> str:
        return self.shell.output("git " + " ".join(q(a) for a in args), cwd=cwd)

    def _read(self, path: Path) -> str:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    def _write(self, path: Path, content: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    def _append(self, path: Path, content: str) -> None:
        with open(path, "a", encoding="utf-8") as f:
            f.write(content)

    def _fetch(self, url: str, dest: Path, required: bool = True) -> bool:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        result = self._run(
            f"curl -fSL --retry 3 --connect-timeout 20 -o {q(dest)} {q(url)}",
            cwd=self.workspace,
            check=False,
            capture_output=True,
        )
        ok = result.returncode == 0 and dest.exists() and dest.stat().st_size > 0
        if not ok:
            message = f"下载失败: {url}"
            if required:
                raise RuntimeError(message)
            logger.warning("%s (已跳过)", message)
        return ok

    def _apply_patch(self, patch_file: Path, cwd: Path, required: bool = True, fuzz: int = 3) -> bool:
        patch_file = Path(patch_file)
        if not patch_file.exists():
            if required:
                raise FileNotFoundError(f"补丁不存在: {patch_file}")
            logger.warning("补丁不存在，跳过: %s", patch_file)
            return False

        probe = self._run(
            f"patch -p1 -F{fuzz} --dry-run -i {q(patch_file)}",
            cwd=cwd,
            check=False,
            capture_output=True,
        )
        if probe.returncode != 0:
            detail = ((probe.stdout or "") + (probe.stderr or "")).strip()
            if required:
                raise RuntimeError(f"补丁无法应用: {patch_file.name}\n{detail[-2000:]}")
            logger.warning("补丁无法应用，跳过: %s\n%s", patch_file.name, detail[-2000:])
            return False

        self._run(f"patch -p1 -F{fuzz} -i {q(patch_file)}", cwd=cwd)
        logger.info("补丁已应用: %s", patch_file.name)
        return True

    # ------------------------------------------------------------------
    # 仓库与工具链
    # ------------------------------------------------------------------

    def clone_repositories(self):
        logger.info("=== 开始克隆仓库 ===")
        if self.config.kernel_version == "6.18":
            self._materialise_susfs_618()
        else:
            url, branch = self._susfs_repo()
            self._clone_repo("SUSFS", self.susfs_dir, url, branch)

        repos = [
            ("SukiSU Patch", self.sukisu_patch_dir, SUKISU_PATCH_REPO_CONFIG["repo_url"], None),
            ("AnyKernel3", self.anykernel_dir, ANYKERNEL_CONFIG["repo_url"], ANYKERNEL_CONFIG["branch"]),
            ("Kernel Patches", self.kernel_patches_dir, KERNEL_PATCHES_CONFIG["repo_url"], None),
        ]
        for name, dest, url, branch in repos:
            self._clone_repo(name, dest, url, branch)

        self._apply_susfs_commit()
        logger.info("=== 仓库克隆完成 ===")

    def _susfs_repo(self) -> tuple:
        if self.config.kernel_version == "6.12":
            return SUSFS_REPO_CONFIG["android16_url"], self.config.kernel_branch
        return SUSFS_REPO_CONFIG["default_url"], self.config.kernel_branch

    def _clone_repo(self, name: str, dest: Path, url: str, branch: Optional[str]):
        if dest.exists():
            logger.info("%s 已存在，跳过", name)
            return
        logger.info("克隆 %s...", name)
        cmd = f"git clone {q(url)}"
        if branch:
            cmd += f" -b {q(branch)}"
        cmd += f" {q(dest)}"
        self._run(cmd, cwd=self.workspace)

    def _materialise_susfs_618(self):
        """6.18 没有上游 SUSFS 分支，使用随仓库内置的 6.12 -> 6.18 移植。"""
        logger.info("使用内置 SUSFS %s 移植（来源: %s）", SUSFS_618_BRANCH, SUSFS_618_BUNDLE)
        if not SUSFS_618_BUNDLE.is_dir():
            raise RuntimeError(f"缺少内置 6.18 SUSFS 移植目录: {SUSFS_618_BUNDLE}")

        dest = self.susfs_dir / "kernel_patches"
        shutil.copytree(SUSFS_618_BUNDLE, dest, dirs_exist_ok=True)
        required = (
            "50_add_susfs_in_gki-android17-6.18.patch",
            "fs/susfs.c",
            "include/linux/susfs.h",
            "include/linux/susfs_def.h",
        )
        missing = [rel for rel in required if not (dest / rel).exists()]
        if missing:
            raise RuntimeError(f"内置 6.18 SUSFS 移植不完整: {', '.join(missing)}")

    def _run_vendor_script(self, script: str, *args, cwd: Path) -> subprocess.CompletedProcess:
        path = SUSFS_COMPAT_SCRIPTS / script
        if not path.exists():
            raise RuntimeError(f"缺少兼容脚本: {path}")
        cmd = f"{q(sys.executable)} {q(path)}"
        if args:
            cmd += " " + " ".join(q(a) for a in args)
        return self._run(cmd, cwd=cwd)

    def _apply_susfs_commit(self):
        if not self.config.susfs_commit:
            return
        if not (self.susfs_dir / ".git").exists():
            logger.warning("SUSFS 不是 git 仓库（内置移植），忽略 susfs_commit")
            return

        ref = self.config.susfs_commit
        self._run("git fetch origin --tags --prune", cwd=self.susfs_dir, check=False)
        if ref.startswith("HEAD~"):
            self._run(f"git reset --hard {q(ref)}", cwd=self.susfs_dir)
        else:
            self._run(f"git checkout {q(ref)}", cwd=self.susfs_dir)
        commit = self._git_output(["rev-parse", "--short=12", "HEAD"], self.susfs_dir)
        logger.info("SUSFS 已切换到 %s (%s)", ref, commit)

    def clone_toolchain(self):
        logger.info("=== 克隆工具链 ===")
        if not self.toolchain_dir.exists():
            self._run(
                f"git clone {q(TOOLCHAIN_CONFIG['aosp_mirror'] + '/kernel/prebuilts/build-tools')} "
                f"-b {q(TOOLCHAIN_CONFIG['build_tools_branch'])} --depth 1 {q(self.toolchain_dir)}",
                cwd=self.workspace,
            )
        if not self.mkbootimg_dir.exists():
            self._run(
                f"git clone {q(TOOLCHAIN_CONFIG['aosp_mirror'] + '/platform/system/tools/mkbootimg')} "
                f"-b {q(TOOLCHAIN_CONFIG['mkbootimg_branch'])} --depth 1 {q(self.mkbootimg_dir)}",
                cwd=self.workspace,
            )

        self.env["AVBTOOL"] = str(self.toolchain_dir / "linux-x86/bin/avbtool")
        self.env["MKBOOTIMG"] = str(self.mkbootimg_dir / "mkbootimg.py")
        self.env["UNPACK_BOOTIMG"] = str(self.mkbootimg_dir / "unpack_bootimg.py")
        if "BOOT_SIGN_KEY_PATH" in os.environ:
            self.env["BOOT_SIGN_KEY_PATH"] = os.environ["BOOT_SIGN_KEY_PATH"]
        self.shell.env = self.env
        logger.info("=== 工具链准备完成 ===")

    def setup_repo_tool(self):
        logger.info("=== 安装 repo 工具 ===")
        repo_dir = self.workspace / "git-repo"
        repo_dir.mkdir(exist_ok=True)
        self.repo_path = repo_dir / "repo"
        if not self.repo_path.exists():
            self._fetch("https://storage.googleapis.com/git-repo-downloads/repo", self.repo_path)
            self._run(f"chmod a+rx {q(self.repo_path)}")
        self.env["REPO"] = str(self.repo_path)
        self.shell.env = self.env

    def init_and_sync_kernel(self):
        logger.info("=== 初始化和同步内核源代码 ===")
        if not self.repo_path:
            raise RuntimeError("repo 工具尚未安装")

        formatted_branch = self.config.formatted_branch
        self._run(
            f"{q(self.repo_path)} init --depth=1 -u https://android.googlesource.com/kernel/manifest "
            f"-b common-{formatted_branch} --repo-rev=v2.16",
            cwd=self.work_dir,
        )

        remote = subprocess.run(
            ["git", "ls-remote", "https://android.googlesource.com/kernel/common", formatted_branch],
            capture_output=True,
            text=True,
        ).stdout.strip()

        if not remote:
            logger.warning("远端不存在分支 %s，repo sync 可能失败", formatted_branch)

        if "deprecated" in remote:
            manifest_path = self.work_dir / ".repo/manifests/default.xml"
            content = self._read(manifest_path)
            content = content.replace(f'"{formatted_branch}"', f'"deprecated/{formatted_branch}"')
            self._write(manifest_path, content)

        logger.info("同步内核源代码...")
        self._run(
            f"{q(self.repo_path)} --trace sync -c -j$(nproc --all) --no-tags --fail-fast",
            cwd=self.work_dir,
        )

        common_dir = self.work_dir / "common"
        if not common_dir.exists():
            raise RuntimeError("repo sync 失败，common 目录不存在")

        self._apply_legacy_fixes(remote)
        logger.info("=== 内核源代码同步完成 ===")

    def _apply_legacy_fixes(self, remote_branch: str = ""):
        av, kv = self.config.android_version, self.config.kernel_version
        sub = self.config.get_sub_level_int()
        is_deprecated = "deprecated" in remote_branch
        common_dir = self.work_dir / "common"

        if is_deprecated and av == "android13" and kv == "5.15" and sub and sub < 123:
            fix = common_dir / "fix-5.15.patch"
            if self._fetch(LEGACY_FIXES["android13-5.15-below-123"]["url"], fix, required=False):
                self._apply_patch(fix, common_dir, required=False)

        if av == "android12" and kv == "5.10" and sub and sub < 136:
            fix = common_dir / "fix-fdinfo.patch"
            if self._fetch(LEGACY_FIXES["android12-5.10-below-136"]["url"], fix, required=False):
                self._apply_patch(fix, common_dir, required=False)

    def apply_toolchain_fixes(self):
        """6.12/6.18 在新 runner 的 glibc >= 2.38 下需要补 resolve_btfids 的 CFLAGS。"""
        if self.config.kernel_version not in ("6.12", "6.18"):
            return
        makefile = self.work_dir / "common/tools/bpf/resolve_btfids/Makefile"
        if not makefile.exists():
            return
        content = self._read(makefile)
        old = "$(Q)$(MAKE) -C $(SUBCMD_SRC) OUTPUT=$(abspath $(dir $@))/ $(abspath $@)"
        new = (
            '$(Q)$(MAKE) -C $(SUBCMD_SRC) EXTRA_CFLAGS="$(CFLAGS)" '
            "OUTPUT=$(abspath $(dir $@))/ $(abspath $@)"
        )
        if old in content and "EXTRA_CFLAGS" not in content:
            logger.info("应用 resolve_btfids EXTRA_CFLAGS 修复 (glibc >= 2.38)")
            self._write(makefile, content.replace(old, new))

    # ------------------------------------------------------------------
    # KernelSU / SUSFS / 补丁
    # ------------------------------------------------------------------

    def add_kernel_supatch(self):
        if not self.config.support_op8e:
            return
        logger.info("=== 添加 OnePlus 8E 支持补丁 ===")
        drivers_dir = self.work_dir / "common/drivers"
        if not drivers_dir.exists():
            return
        if not self._fetch(OP8E_PATCH_URL, drivers_dir / "hmbird_patch.c", required=False):
            return
        makefile = drivers_dir / "Makefile"
        if makefile.exists() and "hmbird_patch.o" not in self._read(makefile):
            self._append(makefile, "obj-y += hmbird_patch.o\n")

    def add_kernelsu(self):
        ref = self.config.kernelsu_ref
        logger.info("=== 添加 KernelSU (ref: %s) ===", ref)

        setup_path = self.workspace / "ksu-setup.sh"
        self._fetch(KSU_REPO_CONFIG["setup_script"], setup_path)

        cmd = f"bash {q(setup_path)}"
        if ref:
            cmd += f" {q(ref)}"
        self._run(cmd, cwd=self.work_dir)

        ksu_dir = self.work_dir / "KernelSU"
        if not (ksu_dir / "kernel").exists():
            raise RuntimeError(f"KernelSU 安装失败: {ksu_dir}/kernel 不存在")

        head = self._git_output(["rev-parse", "HEAD"], ksu_dir)
        target = self._git_output(["rev-parse", "--verify", f"{ref}^{{commit}}"], ksu_dir)
        if not target or head != target:
            raise RuntimeError(
                f"KernelSU 未能切换到 {ref}（当前 HEAD: {head[:12] or 'unknown'}）"
            )

        self.ksu_commit_actual = head[:12]
        branch = self._git_output(["rev-parse", "--abbrev-ref", "HEAD"], ksu_dir)
        if branch in ("", "HEAD"):
            branch = self._git_output(["describe", "--tags", "--always"], ksu_dir)
        self.ksu_ref_actual = branch or ref
        logger.info("KernelSU 实际版本: %s (%s)", self.ksu_ref_actual, self.ksu_commit_actual)

        self._fix_kernelsu_newer_kernels(ksu_dir)

    def _fix_kernelsu_newer_kernels(self, ksu_dir: Path):
        """6.12+ 的 SUSFS 补丁 extern ksu_selinux_hide_enabled，KSU 侧需去掉 static。"""
        if self.config.kernel_version not in ("6.12", "6.18"):
            return
        path = ksu_dir / "kernel/feature/selinux_hide.c"
        if not path.exists():
            return
        content = self._read(path)
        old = "static bool ksu_selinux_hide_enabled __read_mostly = true;"
        new = "bool ksu_selinux_hide_enabled __read_mostly = true;"
        if old in content:
            self._write(path, content.replace(old, new))
            logger.info("KernelSU selinux_hide: 导出 ksu_selinux_hide_enabled")

    def add_bbg(self):
        if not self.config.use_bbg:
            return
        logger.info("=== 添加 Baseband-guard ===")
        common_dir = self.work_dir / "common"
        if not common_dir.exists():
            return

        setup_path = common_dir / "bbg-setup.sh"
        if self._fetch(BBG_CONFIG["setup_script"], setup_path, required=False):
            self._run(f"bash {q(setup_path)}", cwd=common_dir)

        if self.config.kernel_version == "6.18":
            bbg_dir = common_dir / "security/baseband-guard"
            if bbg_dir.is_dir():
                self._run_vendor_script("bbg_618_compat.py", str(bbg_dir), cwd=common_dir)

        config_file = common_dir / "arch/arm64/configs/gki_defconfig"
        if config_file.exists() and "CONFIG_BBG=y" not in self._read(config_file):
            self._append(config_file, "CONFIG_BBG=y\n")

        kconfig_file = common_dir / "security/Kconfig"
        if kconfig_file.exists():
            content = self._read(kconfig_file)
            content = re.sub(
                r"(config LSM.*?)(default .*)(\n.*?help)",
                lambda m: m.group(1) + (
                    "lockdown,baseband_guard"
                    if "lockdown" in m.group(2) and "baseband_guard" not in m.group(2)
                    else m.group(2)
                ) + m.group(3),
                content,
                flags=re.DOTALL,
            )
            self._write(kconfig_file, content)

    def apply_susfs_patches(self):
        logger.info("=== 应用 SUSFS 补丁 ===")
        common_dir = self.work_dir / "common"
        patch_name = self.config.get_susfs_patch_filename()
        susfs_patch = self.susfs_dir / "kernel_patches" / patch_name
        if not susfs_patch.exists():
            raise RuntimeError(f"找不到 SUSFS 补丁: {patch_name}")

        self._run(f"cp {q(susfs_patch)} {q(common_dir)}/", cwd=self.work_dir)
        for src, dst in [
            (self.susfs_dir / "kernel_patches/fs", common_dir / "fs"),
            (self.susfs_dir / "kernel_patches/include/linux", common_dir / "include/linux"),
        ]:
            if src.exists():
                self._run(f"cp -r {q(src)}/* {q(dst)}/", cwd=self.work_dir)

        patch_file = common_dir / patch_name
        version = f"{self.config.android_version}-{self.config.kernel_version}"
        sub_level = self.config.get_sub_level_int() or 999

        # 上游补丁的通用兼容修复
        self._run_vendor_script("susfs_guard_priority_helpers.py", "fs/susfs.c", cwd=common_dir)
        self._run_vendor_script(
            "susfs_restore_context.py", patch_name, str(common_dir), cwd=common_dir
        )

        if version == "android16-6.12" and sub_level < 92:
            # 6.12 早期 sublevel 的 show_smaps_rollup / exec.c 形态不同
            self._run_vendor_script(
                "apply_susfs_compat.py", str(common_dir), str(patch_file), cwd=common_dir
            )
        else:
            self._apply_patch(patch_file, common_dir, required=True)

        if version == "android17-6.18":
            # 6.18 的 mount id、task_mmu、SELinux cred 与 6.12 差异无法写进通用补丁
            self._run_vendor_script("susfs_618_task_mmu.py", str(common_dir), cwd=common_dir)
            self._run_vendor_script("susfs_618_compile_fixes.py", str(common_dir), cwd=common_dir)
            self._run_vendor_script("susfs_618_prototypes.py", str(common_dir), cwd=common_dir)

        self._run_vendor_script(
            "susfs_fdinfo_fanotify_arity.py", str(common_dir), cwd=common_dir
        )

    def apply_sukisu_patches(self):
        logger.info("=== 应用 SukiSU 补丁 ===")
        common_dir = self.work_dir / "common"
        hooks_patch = self.sukisu_patch_dir / "69_hide_stuff.patch"
        if not hooks_patch.exists():
            logger.warning("未找到 69_hide_stuff.patch，跳过")
            return
        self._run(f"cp {q(hooks_patch)} {q(common_dir)}/", cwd=self.work_dir)
        self._apply_patch(common_dir / hooks_patch.name, common_dir, required=False)

    def apply_zram_patches(self):
        if not self.config.use_zram:
            return
        logger.info("=== 应用 ZRAM (LZ4KD) 补丁 ===")
        common_dir = self.work_dir / "common"
        for src, dst in [
            (self.sukisu_patch_dir / "other/zram/lz4k/include/linux", common_dir / "include/linux"),
            (self.sukisu_patch_dir / "other/zram/lz4k/lib", common_dir / "lib"),
            (self.sukisu_patch_dir / "other/zram/lz4k/crypto", common_dir / "crypto"),
            (self.sukisu_patch_dir / "other/zram/lz4k_oplus", common_dir / "lib"),
        ]:
            if src.exists():
                self._run(f"cp -r {q(src)}/* {q(dst)}/", cwd=self.work_dir)

        zram_patch_dir = self.sukisu_patch_dir / f"other/zram/zram_patch/{self.config.kernel_version}"
        for patch in ("lz4kd.patch", "lz4k_oplus.patch"):
            patch_path = zram_patch_dir / patch
            if patch_path.exists():
                self._apply_patch(patch_path, common_dir, required=False)

    def apply_task_mmu_fixes(self):
        logger.info("=== 应用 task_mmu.c 修复 ===")
        common_dir = self.work_dir / "common"
        task_mmu = common_dir / "fs/proc/task_mmu.c"
        if not task_mmu.exists():
            return

        content = self._read(task_mmu)

        # ===== 修复 VMA_PAD_START 未定义 =====
        if "VMA_PAD_START" in content and "#define VMA_PAD_START" not in content:
            logger.info("检测到 VMA_PAD_START 未定义，正在添加宏定义...")
            lines = content.split("\n")
            new_lines = []
            inserted = False
            for line in lines:
                new_lines.append(line)
                if not inserted and line.strip().startswith("#include <linux/pkeys.h>"):
                    new_lines.extend([
                        "",
                        "// VMA_PAD_START fix for SUSFS",
                        "#ifndef VMA_PAD_START",
                        "#define VMA_PAD_START(vma) ((vma)->vm_end)",
                        "#endif",
                    ])
                    inserted = True
            if inserted:
                content = "\n".join(new_lines)
                logger.info("已添加 VMA_PAD_START 宏定义")

        # ===== 修复 dentry 未初始化 =====
        if "struct dentry *dentry;" in content:
            content = content.replace("struct dentry *dentry;", "struct dentry *dentry = NULL;")
            logger.info("已修复 dentry 未初始化问题")

        self._write(task_mmu, content)

        fb = f"{self.config.android_version}-{self.config.kernel_version}"
        if fb == "android15-6.6" and "unsigned int nr_subpages" not in content:
            self._fix_base_c_header()
        elif fb == "android14-6.1" and "if (!vma_pages(vma))" not in content:
            self._fix_base_c_header()
            content = self._read(task_mmu)
            if "goto show_pad;" in content:
                self._write(task_mmu, content.replace("goto show_pad;", "return 0;"))
        elif fb in ("android12-5.10", "android13-5.10", "android13-5.15") and "if (!vma_pages(vma))" not in content:
            content = self._read(task_mmu)
            if "goto show_pad;" in content:
                self._write(task_mmu, content.replace("goto show_pad;", "return 0;"))

    def _fix_base_c_header(self):
        base_c = self.work_dir / "common/fs/proc/base.c"
        if not base_c.exists():
            return
        content = self._read(base_c)
        if "#include <linux/dma-buf.h>" not in content:
            content = content.replace(
                "#include <linux/cpufreq_times.h>",
                "#include <linux/cpufreq_times.h>\n#include <linux/dma-buf.h>",
            )
            self._write(base_c, content)

    # ------------------------------------------------------------------
    # 内核配置
    # ------------------------------------------------------------------

    def configure_kernel(self):
        logger.info("=== 配置内核 ===")
        config_file = self.work_dir / "common/arch/arm64/configs/gki_defconfig"
        if not config_file.exists():
            logger.warning("配置文件不存在: %s", config_file)
            return

        self._append(config_file, self.KERNEL_CONFIG_TEMPLATE)
        sus_path = "y" if self.config.kernel_version != "6.6" else "n"
        self._append(config_file, f"CONFIG_KSU_SUSFS_SUS_PATH={sus_path}\n")

        if self.config.use_zram:
            self._configure_zram()
            self._configure_bazel()

        if self.config.set_default_bbr:
            self._append(config_file, "CONFIG_DEFAULT_BBR=y\n")

        build_config = self.work_dir / "common/build.config.gki"
        if build_config.exists():
            content = self._read(build_config)
            self._write(build_config, content.replace("check_defconfig", ""))

    def _configure_zram(self):
        config_file = self.work_dir / "common/arch/arm64/configs/gki_defconfig"
        if self.config.kernel_version == "5.10":
            self._append(config_file, self.ZRAM_CONFIG_5_10)
        else:
            content = self._read(config_file)
            self._write(config_file, content.replace("CONFIG_ZRAM=m", "CONFIG_ZRAM=y"))
            self._append(config_file, "CONFIG_ZSMALLOC=y\n")
        self._append(config_file, self.ZRAM_CONFIG_COMMON)

    def _configure_bazel(self):
        modules_bzl = self.work_dir / "common/modules.bzl"
        if modules_bzl.exists():
            content = self._read(modules_bzl)
            modified = False
            for old in ('"drivers/block/zram/zram.ko",\n', '"drivers/block/zram/zram.ko",',
                        '"mm/zsmalloc.ko",\n', '"mm/zsmalloc.ko",'):
                if old in content:
                    content = content.replace(old, "")
                    modified = True
            if modified:
                self._write(modules_bzl, content)
        self._append(
            self.work_dir / "common/arch/arm64/configs/gki_defconfig",
            "CONFIG_MODULE_SIG_FORCE=n\n",
        )

    def configure_kernel_name(self):
        logger.info("=== 配置内核名称 ===")
        max_custom_len = 48
        safe_custom_version = ""
        if self.config.custom_version:
            safe_custom_version = self.config.custom_version.rstrip("-")[:max_custom_len]

        setlocalversion = self.work_dir / "common/scripts/setlocalversion"
        if setlocalversion.exists():
            content = self._read(setlocalversion)
            if safe_custom_version:
                if self.config.kernel_version in ("6.12", "6.18"):
                    # 6.12/6.18 的 setlocalversion 末尾直接输出完整版本串
                    kernel_string = (
                        f"{self.config.kernel_version}.{self.config.sub_level}-"
                        f"{self.config.android_version}-{safe_custom_version}"
                    )
                    lines = content.rstrip("\n").split("\n")
                    if lines:
                        lines[-1] = f'echo "{kernel_string}"'
                    content = "\n".join(lines) + "\n"
                else:
                    lines = content.split("\n")
                    for i, line in enumerate(lines):
                        if 'echo "$res"' in line and not line.strip().startswith("#"):
                            lines[i] = f'\techo "{safe_custom_version}$res"'
                            break
                    content = "\n".join(lines)
            content = content.replace("-dirty", "")
            self._write(setlocalversion, content)

        current_time = datetime.now(timezone.utc).strftime("%a %b %d %H:%M:%S UTC %Y")
        mkcompile_h = self.work_dir / "common/scripts/mkcompile_h"
        if mkcompile_h.exists():
            content = self._read(mkcompile_h)
            content = content.replace(
                'UTS_VERSION="$(echo $UTS_VERSION $CONFIG_FLAGS $TIMESTAMP | cut -b -$UTS_LEN)"',
                f'UTS_VERSION="#1 SMP PREEMPT {current_time}"',
            )
            self._write(mkcompile_h, content)

        if self.config.kernel_version in ("6.1", "6.6", "6.12", "6.18"):
            init_makefile = self.work_dir / "common/init/Makefile"
            if init_makefile.exists():
                content = self._read(init_makefile)
                content = content.replace(
                    '$(preempt-flag-y) "$(build-timestamp)"',
                    f'$(preempt-flag-y) "{current_time}"',
                )
                self._write(init_makefile, content)

        if not (self.work_dir / "build/build.sh").exists():
            self._configure_kleaf_name(safe_custom_version)

    def _configure_kleaf_name(self, safe_custom_version: str):
        bazel_build = self.work_dir / "common/BUILD.bazel"
        if bazel_build.exists():
            content = self._read(bazel_build)
            if 'check_defconfig = "disabled"' not in content:
                lines = []
                inserted = False
                for line in content.split("\n"):
                    lines.append(line)
                    if not inserted and line.strip() == 'name = "kernel_aarch64",':
                        lines.append('    check_defconfig = "disabled",')
                        inserted = True
                content = "\n".join(lines)
            content = "\n".join(
                line for line in content.split("\n")
                if '"protected_exports_list"' not in line
                and "protected_module_names_list" not in line
            )
            self._write(bazel_build, content)

        modules_bzl = self.work_dir / "common/modules.bzl"
        if modules_bzl.exists():
            content = self._read(modules_bzl)
            content = re.sub(
                r"^protected_modules = \[.*\]$",
                "protected_modules = []",
                content,
                flags=re.MULTILINE,
            )
            self._write(modules_bzl, content)

        abi_path = self.work_dir / "common/android/abi_gki_protected_exports_aarch64"
        if abi_path.exists():
            try:
                if abi_path.is_dir():
                    shutil.rmtree(abi_path)
                else:
                    abi_path.unlink()
            except OSError:
                pass

        stamp_bzl = self.work_dir / "build/kernel/kleaf/impl/stamp.bzl"
        if stamp_bzl.exists():
            content = self._read(stamp_bzl)
            self._write(stamp_bzl, content.replace("-maybe-dirty", ""))

        if safe_custom_version:
            config_file = self.work_dir / "common/arch/arm64/configs/gki_defconfig"
            if config_file.exists():
                content = self._read(config_file)
                content = re.sub(
                    r'^CONFIG_LOCALVERSION=".*"$',
                    f'CONFIG_LOCALVERSION="{safe_custom_version}"',
                    content,
                    flags=re.MULTILINE,
                )
                self._write(config_file, content)
            else:
                logger.warning("配置文件不存在，跳过 custom_version 设置: %s", config_file)

    def show_kernel_config(self):
        logger.info("=== 显示内核配置列表 ===")
        config_file = self.work_dir / "common/arch/arm64/configs/gki_defconfig"
        if not config_file.exists():
            logger.warning("配置文件不存在: %s", config_file)
            return

        config_lines = [
            line.strip() for line in self._read(config_file).splitlines()
            if line.strip().startswith("CONFIG_")
        ]

        key_configs = {
            "CONFIG_KSU": "KernelSU",
            "CONFIG_KPM": "KPM",
            "CONFIG_KSU_SUSFS": "SUSFS",
            "CONFIG_BBG": "Baseband-guard",
            "CONFIG_BBR": "BBR",
            "CONFIG_ZRAM": "ZRAM",
        }
        logger.info("关键配置状态:")
        for prefix, name in key_configs.items():
            found = [c for c in config_lines if c.startswith(prefix)]
            logger.info("  [%s] %s", "已启用" if found else "未配置", name)
            for item in sorted(found):
                logger.info("      -> %s", item)

        if self.config.use_zram:
            zram_configs = [
                c for c in config_lines
                if any(x in c for x in ("ZRAM", "ZSMALLOC", "LZ4", "LZ4KD", "CRYPTO_LZ4", "MODULE_SIG"))
            ]
            if zram_configs:
                logger.info("ZRAM 相关配置:")
                for item in sorted(zram_configs):
                    logger.info("  -> %s", item)
        logger.info("-" * 60)

    # ------------------------------------------------------------------
    # 编译与产物
    # ------------------------------------------------------------------

    def build_kernel(self) -> bool:
        logger.info("=== 开始编译内核 ===")
        build_config = self.work_dir / "common/build.config.gki.aarch64"
        if build_config.exists():
            content = self._read(build_config)
            content = content.replace("BUILD_SYSTEM_DLKM=1", "BUILD_SYSTEM_DLKM=0")
            lines = [
                line for line in content.split("\n")
                if "MODULES_ORDER=android/gki_aarch64_modules" not in line
                and "KMI_SYMBOL_LIST_STRICT_MODE" not in line
            ]
            self._write(build_config, "\n".join(lines))

        try:
            if (self.work_dir / "build/build.sh").exists():
                if self.config.page_size == "16k":
                    raise RuntimeError("16k page size 仅支持 Kleaf/Bazel 构建")
                logger.info("使用旧版构建方式...")
                result = self._run(
                    'LTO=thin BUILD_CONFIG=common/build.config.gki.aarch64 '
                    'build/build.sh CC="/usr/bin/ccache clang"',
                    cwd=self.work_dir,
                    check=False,
                )
            else:
                logger.info("使用 Bazel 构建方式...")
                disk_cache = Path.home() / ".cache/bazel"
                kernel_version = self.config.kernel_version
                page_flag = " --page_size=16k" if self.config.page_size == "16k" else ""
                if kernel_version == "6.18":
                    target = "//common:kernel_aarch64"
                    lto_flag = ""
                elif kernel_version == "6.12":
                    target = "//common:kernel_aarch64/Image"
                    lto_flag = ""
                elif self.config.page_size == "16k":
                    # 6.1/6.6 的 16k 构建按参考流程用 Image target
                    target = "//common:kernel_aarch64/Image"
                    lto_flag = ""
                else:
                    target = "//common:kernel_aarch64_dist"
                    lto_flag = " --lto=thin"
                result = self._run(
                    f"tools/bazel build --disk_cache={q(disk_cache)} --config=fast"
                    f"{lto_flag}{page_flag} {target}",
                    cwd=self.work_dir,
                    check=False,
                )
        except Exception as e:
            logger.error("编译过程出错: %s", e)
            return False

        if result.returncode == 0:
            logger.info("=== 内核编译成功 ===")
            return True
        logger.error("内核编译失败 (exit %s)", result.returncode)
        return False

    def verify_kernel_config(self):
        """确认 Kleaf 没有把 KSU/SUSFS 配置从最终 .config 里丢掉。"""
        candidates = [
            self.work_dir / "bazel-bin/common/kernel_aarch64_config/out_dir/.config",
            self.work_dir / f"out/{self.config.android_version}-{self.config.kernel_version}/dist/.config",
        ]
        config_file = next((p for p in candidates if p.exists()), None)
        if config_file is None:
            config_file = next(
                self.work_dir.glob("bazel-bin/common/**/out_dir/.config"), None
            )
        if config_file is None:
            logger.warning("未找到生成的 .config，跳过配置校验")
            return

        content = self._read(config_file)
        missing = [
            name for name in ("CONFIG_KSU=y", "CONFIG_KSU_SUSFS=y")
            if name not in content
        ]
        if missing:
            raise RuntimeError(
                f"最终 .config 缺少 {', '.join(missing)}: {config_file}"
            )
        if self.config.page_size == "16k" and "CONFIG_ARM64_16K_PAGES=y" not in content:
            raise RuntimeError(f"16k page size 未生效: {config_file}")
        logger.info("配置校验通过: CONFIG_KSU / CONFIG_KSU_SUSFS 均在 %s", config_file)

    def patch_kpm_image(self):
        if (
            not self.config.use_kpm
            or self.config.kernel_version in ("6.6", "6.12", "6.18")
            or self.config.page_size == "16k"
        ):
            return
        logger.info("=== 修补 Image 文件 (KPM) ===")

        if self.config.android_version in ("android12", "android13"):
            image_dir = self.work_dir / f"out/{self.config.android_version}-{self.config.kernel_version}/dist"
        else:
            image_dir = self.work_dir / "bazel-bin/common/kernel_aarch64"

        if not (image_dir / "Image").exists():
            logger.warning("未找到 Image，跳过 KPM 修补")
            return

        tools_dir = self.workspace / "kpm-tools"
        tools_dir.mkdir(exist_ok=True)
        kpimg = tools_dir / "kpimg"
        kptools = tools_dir / "kptools-linux"
        self._fetch(KPM_KPIMG_URL, kpimg)
        self._fetch(KPM_KPTOOLS_URL, kptools)
        self._run(f"chmod +x {q(kptools)}")

        result = self._run(
            f"{q(kptools)} -p -s {KPM_PATCH_SDK_VERSION} -i Image -k {q(kpimg)} -o oImage",
            cwd=image_dir,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("KPM 修补失败：kptools 返回非零")

        oimage = image_dir / "oImage"
        if not oimage.exists():
            raise RuntimeError("KPM 修补失败：未生成 oImage")
        self._run(f"mv {q(oimage)} {q(image_dir / 'Image')}", cwd=image_dir)

    def prepare_boot_images(self) -> list:
        logger.info("=== 准备启动镜像 ===")
        bootimgs_dir = self.work_dir / "bootimgs"
        bootimgs_dir.mkdir(exist_ok=True)
        artifacts: list = []

        if self.config.android_version in ("android12", "android13"):
            image_source = self.work_dir / f"out/{self.config.android_version}-{self.config.kernel_version}/dist"
        else:
            image_source = self.work_dir / "bazel-bin/common/kernel_aarch64"

        for image_name in ("Image", "Image.lz4"):
            src = image_source / image_name
            if src.exists():
                self._run(f"cp {q(src)} {q(bootimgs_dir)}/ && cp {q(src)} {q(self.work_dir)}/", cwd=self.work_dir)

        if (self.work_dir / "Image").exists():
            self._run("gzip -n -k -f -9 Image", cwd=self.work_dir, check=False)

        if self.config.android_version == "android12":
            self._prepare_android12_boot_images(bootimgs_dir, artifacts)
        else:
            self._create_boot_image_variants(bootimgs_dir, artifacts, has_ramdisk=False)
        return artifacts

    def _prepare_android12_boot_images(self, bootimgs_dir: Path, artifacts: list):
        gki_url = (
            "https://dl.google.com/android/gki/gki-certified-boot-android12-5.10-"
            f"{self.config.os_patch_level}_{self.config.revision}.zip"
        )
        fallback_url = "https://dl.google.com/android/gki/gki-certified-boot-android12-5.10-2023-01_r1.zip"
        probe = subprocess.run(
            f"curl -sL -w '%{{http_code}}' {q(gki_url)} -o /dev/null",
            shell=True,
            capture_output=True,
            text=True,
        )
        url = gki_url if "200" in probe.stdout else fallback_url

        self._run(f"curl -fLo gki-kernel.zip {q(url)}", cwd=bootimgs_dir)
        self._run("unzip -o gki-kernel.zip && rm gki-kernel.zip", cwd=bootimgs_dir)

        boot_img_path = bootimgs_dir / "boot-5.10.img"
        if boot_img_path.exists():
            self._run(f"$UNPACK_BOOTIMG --boot_img={q(boot_img_path)}", cwd=bootimgs_dir)
        self._create_boot_image_variants(bootimgs_dir, artifacts, has_ramdisk=True)

    def _create_boot_image_variants(self, bootimgs_dir: Path, artifacts: list, has_ramdisk: bool = False):
        if (bootimgs_dir / "Image").exists():
            self._run("gzip -n -k -f -9 Image", cwd=bootimgs_dir, check=False)

        for kernel_file, output_file in (("Image", "boot.img"), ("Image.gz", "boot-gz.img"), ("Image.lz4", "boot-lz4.img")):
            kernel_path = bootimgs_dir / kernel_file
            if not kernel_path.exists():
                continue
            cmd = f"$MKBOOTIMG --header_version 4 --kernel {kernel_file} --output {output_file}"
            if has_ramdisk:
                cmd += f" --ramdisk out/ramdisk --os_version 12.0.0 --os_patch_level {self.config.os_patch_level}"
            self._run(cmd, cwd=bootimgs_dir, check=False)
            self._run(
                f"$AVBTOOL add_hash_footer --partition_name boot "
                f"--partition_size $((64 * 1024 * 1024)) --image {output_file} "
                f"--algorithm SHA256_RSA2048 --key $BOOT_SIGN_KEY_PATH",
                cwd=bootimgs_dir,
                check=False,
            )
            dest = self.work_dir / f"{self.config.artifact_prefix}-{output_file}"
            self._run(f"cp {output_file} {q(dest)}", cwd=bootimgs_dir, check=False)
            if dest.exists():
                artifacts.append(str(dest))

    def create_anykernel_zips(self) -> list:
        logger.info("=== 创建 AnyKernel3 ZIP 文件 ===")
        artifacts = []
        ak3_dir = self.anykernel_dir

        for suffix in ("", "-lz4", "-gz"):
            image_file = f"Image{suffix}"
            image_path = self.work_dir / image_file
            if not image_path.exists():
                continue
            zip_name = f"{self.config.artifact_prefix}-AnyKernel3{suffix}.zip"
            zip_path = self.work_dir / zip_name
            self._run(f"cp {q(image_path)} {q(ak3_dir)}/", cwd=self.work_dir)
            self._run(f"zip -r {q(zip_path)} ./*", cwd=ak3_dir)
            self._run(f"rm -f {q(ak3_dir / image_file)}", cwd=self.work_dir, check=False)
            if zip_path.exists():
                artifacts.append(str(zip_path))
        return artifacts

    def _susfs_version(self) -> str:
        header = self.susfs_dir / "kernel_patches/include/linux/susfs.h"
        if header.exists():
            match = re.search(
                r'#define\s+SUSFS_VERSION\s+"([^"]+)"', self._read(header)
            )
            if match:
                return match.group(1)
        return "unknown"

    def _write_build_info(self, artifacts: list) -> Path:
        kv = self.config.kernel_version
        if kv == "6.18":
            susfs_source = "bundled-gki-android17-6.18-port"
            susfs_commit = "local-6.12-port"
        elif kv == "6.12":
            susfs_source = SUSFS_REPO_CONFIG["android16_url"]
            susfs_commit = self._git_output(["rev-parse", "--short=12", "HEAD"], self.susfs_dir)
        else:
            susfs_source = SUSFS_REPO_CONFIG["default_url"]
            susfs_commit = self._git_output(["rev-parse", "--short=12", "HEAD"], self.susfs_dir)

        info = {
            "config": self.config.to_dict(),
            "ksu": {
                "requested_ref": self.config.kernelsu_ref,
                "resolved_ref": self.ksu_ref_actual,
                "commit": self.ksu_commit_actual,
            },
            "susfs": {
                "source": susfs_source,
                "version": self._susfs_version(),
                "requested_ref": self.config.susfs_commit,
                "commit": susfs_commit,
            },
            "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "artifacts": [Path(a).name for a in artifacts],
        }
        path = self.work_dir / f"build-info-{self.config.config_name}.json"
        self._write(path, json.dumps(info, indent=2, ensure_ascii=False) + "\n")
        return path

    def build(self) -> BuildResult:
        start_time = time.time()
        logger.info("=" * 50)
        logger.info("开始 GKI Kernel 构建 - %s", self.config.config_name)
        logger.info("=" * 50)

        try:
            self.clone_repositories()
            self.clone_toolchain()
            self.setup_repo_tool()
            self.init_and_sync_kernel()
            self.apply_toolchain_fixes()
            self.add_kernel_supatch()
            self.add_kernelsu()
            self.add_bbg()
            self.apply_susfs_patches()
            self.apply_sukisu_patches()
            self.apply_zram_patches()
            self.apply_task_mmu_fixes()
            self.configure_kernel()
            self.configure_kernel_name()
            self.show_kernel_config()

            if not self.build_kernel():
                return BuildResult(
                    success=False,
                    config=self.config,
                    message="内核编译失败",
                    build_time=time.time() - start_time,
                )
            self.verify_kernel_config()

            self.patch_kpm_image()
            artifacts = []
            artifacts.extend(self.prepare_boot_images())
            artifacts.extend(self.create_anykernel_zips())
            artifacts.append(str(self._write_build_info(artifacts)))

            build_time = time.time() - start_time
            logger.info("构建成功! 耗时: %.2f 秒, 生成 %d 个产物", build_time, len(artifacts))
            return BuildResult(
                success=True,
                config=self.config,
                message="构建成功",
                artifacts=artifacts,
                build_time=build_time,
            )
        except Exception as e:
            logger.exception("构建过程出错")
            return BuildResult(
                success=False,
                config=self.config,
                message=str(e),
                build_time=time.time() - start_time,
            )
