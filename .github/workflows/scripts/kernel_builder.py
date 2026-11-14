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
import signal
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
    KPM_KPIMG_URL,
    KPM_KPTOOLS_URL,
    KPM_PATCH_SDK_VERSION,
    KPM_SUPPORTED_KERNELS,
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
    _active: Optional[subprocess.Popen] = None
    _signal_handlers_ready = False

    def __init__(self, cwd: Optional[str] = None, env: Optional[dict] = None):
        self.cwd = str(cwd) if cwd else None
        self.env = env or os.environ.copy()

    @staticmethod
    def _kill_group(proc: subprocess.Popen) -> None:
        """杀掉命令所在的整个进程组。

        命令都通过 shell=True 启动，只 kill 掉 /bin/sh 的话 repo sync、git、
        make 这些真正下载/编译的子进程会变成孤儿继续跑。
        """
        pgid = None
        try:
            pgid = os.getpgid(proc.pid)
        except OSError:
            pass
        # 只杀独立的进程组；pgid <= 1 或与自身同组时退回单进程 kill，
        # 避免误杀构建脚本自己。
        if pgid is not None and pgid > 1 and pgid != os.getpgid(0):
            try:
                os.killpg(pgid, signal.SIGKILL)
                return
            except OSError:
                pass
        try:
            proc.kill()
        except ProcessLookupError:
            pass

    @classmethod
    def _install_signal_handlers(cls) -> None:
        """收到 TERM/HUP 时先清掉正在跑的命令组，避免 repo 在后台继续下载。"""
        if cls._signal_handlers_ready:
            return

        def _cleanup(signum, _frame):
            proc = cls._active
            if proc is not None and proc.poll() is None:
                cls._kill_group(proc)
            raise SystemExit(128 + signum)

        for sig in (signal.SIGTERM, signal.SIGHUP):
            try:
                signal.signal(sig, _cleanup)
            except (ValueError, OSError):
                pass
        cls._signal_handlers_ready = True

    @classmethod
    def _start(cls, *args, **kwargs) -> subprocess.Popen:
        """每个命令单独一个会话/进程组，退出时才能整组清理。"""
        cls._install_signal_handlers()
        proc = subprocess.Popen(*args, start_new_session=True, **kwargs)
        cls._active = proc
        return proc

    @classmethod
    def _release(cls, proc: subprocess.Popen) -> None:
        if cls._active is proc:
            cls._active = None

    def run(self, cmd: str, cwd: Optional[str] = None, check: bool = True,
            capture_output: bool = False, timeout: Optional[int] = None,
            log_file: Optional[Path] = None) -> subprocess.CompletedProcess:
        logger.info("$ %s", cmd)
        if log_file is not None and not capture_output:
            return self._run_logged(cmd, cwd, check, timeout, Path(log_file))

        proc = self._start(
            cmd,
            shell=True,
            cwd=str(cwd) if cwd else self.cwd,
            env=self.env,
            stdout=subprocess.PIPE if capture_output else None,
            stderr=subprocess.PIPE if capture_output else None,
            text=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._kill_group(proc)
            proc.wait()
            logger.error("命令超时: %s", cmd)
            raise
        except BaseException:
            self._kill_group(proc)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            raise
        finally:
            self._release(proc)

        result = subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)

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

    def _run_logged(self, cmd: str, cwd: Optional[str], check: bool,
                    timeout: Optional[int], log_file: Path) -> subprocess.CompletedProcess:
        """运行命令并把 stdout/stderr 同时写到控制台和日志文件。"""
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(log_file, "a", encoding="utf-8", errors="replace") as log:
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            log.write(f"\n===== {stamp} =====\n$ {cmd}\n")
            log.flush()

            proc = self._start(
                cmd,
                shell=True,
                cwd=str(cwd) if cwd else self.cwd,
                env=self.env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            try:
                if proc.stdout is not None:
                    for line in proc.stdout:
                        sys.stdout.write(line)
                        sys.stdout.flush()
                        log.write(line)
                        log.flush()
                returncode = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._kill_group(proc)
                proc.wait()
                log.write(f"[timeout] {cmd}\n")
                log.flush()
                logger.error("命令超时: %s", cmd)
                raise
            except BaseException:
                self._kill_group(proc)
                proc.wait()
                raise
            finally:
                self._release(proc)

        result = subprocess.CompletedProcess(cmd, returncode)
        if check and returncode != 0:
            logger.error("命令失败 (exit %s): %s", returncode, cmd)
            raise subprocess.CalledProcessError(returncode, cmd)
        return result

    def output(self, cmd: str, cwd: Optional[str] = None) -> str:
        result = self.run(cmd, cwd=cwd, check=False, capture_output=True)
        return result.stdout.strip() if result.returncode == 0 else ""


class KernelBuilder:
    # 基础配置按功能拆开，只把开启的选项写进 gki_defconfig
    KSU_CONFIG = """
# === KernelSU Config ===
CONFIG_KSU=y
"""

    KPM_CONFIG = """CONFIG_KPM=y
"""

    TMPFS_CONFIG = """
# === TMPFS Config ===
CONFIG_TMPFS_XATTR=y
CONFIG_TMPFS_POSIX_ACL=y
"""

    NETWORK_CONFIG = """
# === Network Config ===
CONFIG_IP_NF_TARGET_TTL=y
CONFIG_IP6_NF_TARGET_HL=y
CONFIG_IP6_NF_MATCH_HL=y
"""

    BBR_CONFIG = """
# === BBR Config ===
CONFIG_TCP_CONG_ADVANCED=y
CONFIG_TCP_CONG_BBR=y
CONFIG_NET_SCH_FQ=y
CONFIG_TCP_CONG_BIC=n
CONFIG_TCP_CONG_WESTWOOD=n
CONFIG_TCP_CONG_HTCP=n
"""

    SUSFS_CONFIG = """
# === SUSFS Config ===
CONFIG_KSU_SUSFS=y
CONFIG_KSU_SUSFS_INLINE_HOOK=y
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

    ZRAM_CONFIG_5_10 = "CONFIG_ZSMALLOC=y\nCONFIG_ZRAM=y\nCONFIG_CRYPTO_LZO=y\nCONFIG_ZRAM_DEF_COMP_LZ4KD=y\n"
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
        # 命令在独立会话里跑，不能让 git 等终端输入/分页器
        self.env["GIT_TERMINAL_PROMPT"] = "0"
        self.env["GIT_PAGER"] = "cat"
        self.env["PAGER"] = "cat"
        self.shell.env = self.env

    def _run(self, cmd: str, cwd: Optional[Path] = None,
             log_file: Optional[Path] = None, **kwargs) -> subprocess.CompletedProcess:
        return self.shell.run(cmd, cwd=cwd, log_file=log_file, **kwargs)

    def _build_log_path(self) -> Path:
        """构建日志放在内核源码根目录（common），common 尚未同步时退回工作目录。"""
        common_dir = self.work_dir / "common"
        base = common_dir if common_dir.is_dir() else self.work_dir
        return base / f"build-{self.config.config_name}.log"

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
            f"patch -p1 -F{fuzz} --batch --dry-run -i {q(patch_file)}",
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

        self._run(f"patch -p1 -F{fuzz} --batch -i {q(patch_file)}", cwd=cwd)
        logger.info("补丁已应用: %s", patch_file.name)
        return True

    # ------------------------------------------------------------------
    # 仓库与工具链
    # ------------------------------------------------------------------

    def clone_patch_repositories(self):
        """只拉取打补丁需要的仓库（SUSFS / SukiSU_patch）。"""
        logger.info("=== 开始克隆补丁仓库 ===")
        if not self.config.use_susfs:
            logger.info("未启用 SUSFS，跳过 SUSFS 仓库")
        elif self.config.kernel_version == "6.18":
            self._materialise_susfs_618()
        else:
            url, branch = self._susfs_repo()
            self._clone_repo("SUSFS", self.susfs_dir, url, branch)

        repos = [
            ("SukiSU Patch", self.sukisu_patch_dir, SUKISU_PATCH_REPO_CONFIG["repo_url"], None),
        ]
        for name, dest, url, branch in repos:
            self._clone_repo(name, dest, url, branch)

        if self.config.use_susfs:
            self._apply_susfs_commit()
        logger.info("=== 补丁仓库克隆完成 ===")

    def clone_repositories(self):
        logger.info("=== 开始克隆仓库 ===")
        self.clone_patch_repositories()
        self.clone_anykernel()
        logger.info("=== 仓库克隆完成 ===")

    def clone_anykernel(self):
        self._clone_repo(
            "AnyKernel3", self.anykernel_dir,
            ANYKERNEL_CONFIG["repo_url"], ANYKERNEL_CONFIG["branch"],
        )

    def _susfs_repo(self) -> tuple:
        if self.config.kernel_version == "6.12":
            return SUSFS_REPO_CONFIG["android16_url"], self.config.kernel_branch
        return SUSFS_REPO_CONFIG["default_url"], self.config.kernel_branch

    def _clone_repo(self, name: str, dest: Path, url: str, branch: Optional[str]):
        if dest.exists():
            self._sync_repo(name, dest, url, branch)
            return
        logger.info("克隆 %s...", name)
        cmd = f"git clone {q(url)}"
        if branch:
            cmd += f" -b {q(branch)}"
        cmd += f" {q(dest)}"
        self._run(cmd, cwd=self.workspace)

    def _sync_repo(self, name: str, dest: Path, url: str, branch: Optional[str],
                   clean: bool = True):
        """复用已有克隆时强制同步远端/分支。

        工作目录是长期复用的，旧缓存可能来自别的内核线、别的远端，或者被
        本地修改弄脏。这里直接 fetch + `checkout -f -B` + `clean -fdx`，
        效果等同删除后重新 clone，但不用整仓重新下载。
        """
        if not (dest / ".git").exists():
            logger.info("%s 已存在但不是 git 仓库，跳过同步", name)
            return

        origin = self._git_output(["remote", "get-url", "origin"], dest)
        if url and origin != url:
            logger.info("%s origin 切换: %s -> %s", name, origin or "未知", url)
            self._run(f"git remote set-url origin {q(url)}", cwd=dest)

        fetched = self._run(
            "git fetch origin --prune", cwd=dest, check=False,
            capture_output=True, timeout=600,
        )

        if not branch:
            upstream = self._git_output(
                ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], dest
            )
            if not upstream:
                logger.warning("%s 没有跟踪分支，保留当前状态", name)
                return
            self._reset_repo_tree(dest, upstream, clean)
            logger.info("%s 已同步: %s", name, upstream)
            return

        target = f"origin/{branch}"
        if not self._git_output(["rev-parse", "--verify", target], dest):
            detail = ((fetched.stdout or "") + (fetched.stderr or "")).strip()
            raise RuntimeError(
                f"{name} 远端没有分支 {branch}（origin: {url}）"
                + (f": {detail[-500:]}" if detail else "")
            )

        current = self._git_output(["rev-parse", "--abbrev-ref", "HEAD"], dest)
        if current != branch:
            logger.info("%s 切换分支: %s -> %s", name, current or "未知", branch)
        # checkout -f -B 本身就会把工作树重置到目标；不要再补一次
        # reset --hard（在有些 WSL/磁盘上会卡住，且完全是多余操作）。
        switched = self._run(
            f"git checkout -f -B {q(branch)} {q(target)}",
            cwd=dest, check=False, timeout=600,
        )
        if switched.returncode != 0:
            raise RuntimeError(f"{name} 切换到分支 {branch} 失败: {dest}")
        if clean:
            self._run("git clean -fdx", cwd=dest, check=False, timeout=600)
        logger.info("%s 已同步到 %s", name, target)

    def _reset_repo_tree(self, dest: Path, target: str, clean: bool) -> None:
        self._run(f"git reset --hard {q(target)}", cwd=dest, check=False, timeout=600)
        if clean:
            self._run("git clean -fdx", cwd=dest, check=False, timeout=600)

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
        self._run(
            "git fetch origin --tags --prune",
            cwd=self.susfs_dir, check=False, timeout=600,
        )
        if ref.startswith("HEAD~"):
            self._run(f"git reset --hard {q(ref)}", cwd=self.susfs_dir, timeout=600)
        else:
            self._run(f"git checkout {q(ref)}", cwd=self.susfs_dir, timeout=600)
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

    def _reset_kernel_tree(self):
        """复用工作目录时清掉上次构建的补丁/配置残留，避免反向打补丁。"""
        common_dir = self.work_dir / "common"
        if not (common_dir / ".git").exists():
            return

        status = self._run(
            "git status --porcelain",
            cwd=common_dir,
            check=False,
            capture_output=True,
        )
        dirty = [
            line for line in (status.stdout or "").splitlines()
            if line.strip()
            and not (line.startswith("??") and line.rstrip().endswith(".log"))
        ]
        if status.returncode == 0 and not dirty:
            logger.info("内核树干净，跳过重置")
            return

        logger.info("=== 清理上次构建残留（git reset --hard + git clean -fdx） ===")
        self._run("git reset --hard HEAD", cwd=common_dir)
        # 保留内核源码根目录下的构建日志
        self._run("git clean -fdx -e '*.log'", cwd=common_dir)

    def _clean_previous_outputs(self):
        """构建前清掉上一次的 out 目录和打包产物。

        复用工作目录时，旧 Image/boot.img/AnyKernel3.zip 会被误当成本次
        结果；out 目录也可能残留上一轮的构建树。
        """
        for name in ("out", "bootimgs"):
            path = self.work_dir / name
            if path.exists():
                logger.info("清理上次构建输出: %s", path)
                shutil.rmtree(path, ignore_errors=True)

        patterns = (
            "Image", "Image.gz", "Image.lz4",
            "*-boot.img", "*-boot-gz.img", "*-boot-lz4.img",
            "*-AnyKernel3*.zip",
            "build-info-*.json",
        )
        removed = []
        for pattern in patterns:
            for path in self.work_dir.glob(pattern):
                if not path.is_file():
                    continue
                try:
                    path.unlink()
                    removed.append(path.name)
                except OSError as exc:
                    logger.warning("无法删除旧产物 %s: %s", path, exc)
        if removed:
            logger.info("已清理旧打包产物: %s", ", ".join(sorted(removed)))

    def _kill_stale_downloads(self):
        """结束上次中断遗留、仍在后台下载的 repo/git 进程。

        repo sync 带 -j 并发下载，主进程被杀后 git 子进程可能留在后台继续
        下载；缓存仓库里的孤儿 git 还会占着锁。只清理构建目录内的进程，
        避免误杀其他任务。
        """
        proc_root = Path("/proc")
        if not proc_root.is_dir():
            return

        roots = [
            str(self.work_dir),
            str(self.susfs_dir),
            str(self.sukisu_patch_dir),
            str(self.anykernel_dir),
            str(self.workspace / "kpm-tools"),
        ]

        def inside(path: str) -> bool:
            return any(path == root or path.startswith(root + os.sep) for root in roots)

        killed = []
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            if pid == os.getpid():
                continue
            try:
                cwd = os.readlink(entry / "cwd")
            except OSError:
                continue
            if not inside(cwd):
                continue
            try:
                cmdline = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode(
                    "utf-8", "replace"
                )
            except OSError:
                continue
            if "repo" not in cmdline and "git" not in cmdline:
                continue
            try:
                pgid = os.getpgid(pid)
            except OSError:
                continue
            try:
                if pgid > 1 and pgid != os.getpgid(0):
                    os.killpg(pgid, signal.SIGKILL)
                else:
                    os.kill(pid, signal.SIGKILL)
            except OSError:
                continue
            killed.append(pid)

        if killed:
            logger.info("已清理上次残留的下载进程: %s", ", ".join(str(p) for p in killed))

    def init_and_sync_kernel(self):
        logger.info("=== 初始化和同步内核源代码 ===")
        if not self.repo_path:
            raise RuntimeError("repo 工具尚未安装")

        self._reset_kernel_tree()

        formatted_branch = self.config.formatted_branch
        self._run(
            f"{q(self.repo_path)} init --depth=1 -u https://android.googlesource.com/kernel/manifest "
            f"-b common-{formatted_branch} --repo-rev=v2.16",
            cwd=self.work_dir,
        )

        common_revision = self._pin_archived_common_branch(formatted_branch)

        sync_cmd = (
            f"{q(self.repo_path)} --trace sync -c -j$(nproc --all) "
            "--no-tags --fail-fast"
        )
        logger.info("同步内核源代码...")
        attempts = 3
        for attempt in range(1, attempts + 1):
            try:
                self._run(sync_cmd, cwd=self.work_dir)
                break
            except subprocess.CalledProcessError:
                if attempt >= attempts:
                    raise
                logger.warning(
                    "repo sync 失败（第 %d/%d 次），清理残留进程后重试",
                    attempt,
                    attempts,
                )
                self._kill_stale_downloads()

        common_dir = self.work_dir / "common"
        if not common_dir.exists():
            raise RuntimeError("repo sync 失败，common 目录不存在")

        self._apply_legacy_fixes(common_revision)
        logger.info("=== 内核源代码同步完成 ===")

    COMMON_REPO_URL = "https://android.googlesource.com/kernel/common"

    def _remote_refs(self, pattern: str, heads: bool) -> Optional[str]:
        kind = "--heads" if heads else "--tags"
        result = self.shell.run(
            f"git ls-remote {kind} {q(self.COMMON_REPO_URL)} {q(pattern)}",
            check=False,
            capture_output=True,
        )
        if result.returncode != 0:
            return None
        return (result.stdout or "").strip()

    def _resolve_common_revision(self, branch: str) -> Optional[str]:
        """解析 kernel/common 上仍然可用的 revision。

        Google 会把过期的月份分支改名成 deprecated/<branch>，个别分支
        只留下 <branch>_rN 标签。直接按 manifest 里的分支名 fetch 会失败：
        “error: Cannot fetch kernel/common from ...”。
        """
        direct = self._remote_refs(f"refs/heads/{branch}", True)
        if direct is None:
            logger.warning("无法查询 kernel/common（网络异常），先按 manifest 分支继续")
            return branch
        if direct:
            return branch
        if self._remote_refs(f"refs/heads/deprecated/{branch}", True):
            return f"deprecated/{branch}"

        refs = self._remote_refs(f"refs/tags/{branch}_r*", False) or ""
        names = []
        for line in refs.splitlines():
            _, _, ref = line.partition("\t")
            if ref.startswith("refs/tags/") and not ref.endswith("^{}"):
                names.append(ref[len("refs/tags/"):])
        if not names:
            return None
        # r1 是该月份分支的首个发布，和 manifest 分支指向的版本最接近
        first = f"{branch}_r1"
        if first in names:
            return f"refs/tags/{first}"

        def release_no(name: str) -> int:
            match = re.search(r"_r(\d+)$", name)
            return int(match.group(1)) if match else 1 << 30

        return f"refs/tags/{min(names, key=release_no)}"

    def _pin_archived_common_branch(self, branch: str) -> str:
        """必要时用 local manifest 覆盖 kernel/common 的 revision。"""
        revision = self._resolve_common_revision(branch)
        if revision is None:
            raise RuntimeError(
                f"kernel/common 上找不到 {branch}（deprecated/ 与 {branch}_r* 标签均不存在），"
                "该内核版本已被上游移除"
            )

        override = self.work_dir / ".repo/local_manifests/common-revision.xml"
        if revision == branch:
            if override.exists():
                override.unlink()
            return revision

        override.parent.mkdir(parents=True, exist_ok=True)
        self._write(
            override,
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<manifest>\n"
            '  <remove-project name="kernel/common" />\n'
            f'  <project path="common" name="kernel/common" revision="{revision}">\n'
            '    <linkfile src=".bazelignore" dest=".bazelignore" />\n'
            "  </project>\n"
            "</manifest>\n",
        )
        logger.info("kernel/common 使用 %s（manifest 分支 %s 已归档）", revision, branch)
        return revision

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
        """构建前的源码兼容修复（kallsyms / resolve_btfids）。"""
        self._fix_kallsyms_on_each_symbol()
        self._fix_resolve_btfids_fatal_warnings()

        # 6.12/6.18 在新 runner 的 glibc >= 2.38 下需要补 resolve_btfids 的 CFLAGS。
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

    def _fix_resolve_btfids_fatal_warnings(self):
        """去掉 link-vmlinux.sh 传给 resolve_btfids 的 --fatal_warnings。

        6.12+ 在 CONFIG_WERROR=y 时会把 resolve_btfids 的警告升级为错误。
        KernelSU 的 unity build 会让 BTF 里出现同名类型的多份定义，
        resolve_btfids 打印 “WARN: multiple IDs found ... - using X” 后直接
        失败（vmlinux.unstripped Error 255）。上游默认只是警告，这里恢复
        默认行为：保留警告，由 resolve_btfids 选择第一个规范化类型 ID。
        """
        if self.config.kernel_version not in ("6.12", "6.18"):
            return
        script = self.work_dir / "common/scripts/link-vmlinux.sh"
        if not script.exists():
            return

        content = self._read(script)
        old = (
            '\tRESOLVE_BTFIDS_ARGS=""\n'
            "\tif is_enabled CONFIG_WERROR; then\n"
            '\t\tRESOLVE_BTFIDS_ARGS=" --fatal_warnings "\n'
            "\tfi\n"
        )
        if old in content:
            self._write(script, content.replace(old, '\tRESOLVE_BTFIDS_ARGS=""\n'))
            logger.info("resolve_btfids: 移除 --fatal_warnings（重复 BTF 类型降级为警告）")
            return

        if " --fatal_warnings " in content:
            self._write(script, content.replace(" --fatal_warnings ", " "))
            logger.info("resolve_btfids: 移除 --fatal_warnings（重复 BTF 类型降级为警告）")

    def _fix_kallsyms_on_each_symbol(self):
        """5.15 旧分支把 kallsyms_on_each_symbol 放在 CONFIG_LIVEPATCH 里。

        SukiSU builtin-new 的 kallsyms_common.h 会直接调用它；GKI defconfig
        没有开启 LIVEPATCH 时，vmlinux 链接会报 undefined symbol。
        这里移除该函数的 CONFIG_LIVEPATCH 包裹（等价于上游后续修复），
        对 6.1+ 已经无条件编译的内核是空操作。
        """
        path = self.work_dir / "common/kernel/kallsyms.c"
        if not path.exists():
            return

        lines = self._read(path).split("\n")
        def_idx = next(
            (i for i, line in enumerate(lines)
             if line.startswith("int kallsyms_on_each_symbol(")),
            None,
        )
        if def_idx is None:
            return

        guard_idx = None
        for i in range(def_idx - 1, max(def_idx - 12, -1), -1):
            stripped = lines[i].strip()
            if stripped.startswith("#endif") or stripped.startswith("#else"):
                break
            if "".join(stripped.split()) == "#ifdefCONFIG_LIVEPATCH":
                guard_idx = i
                break
        if guard_idx is None:
            return

        end_idx = next(
            (
                i for i in range(def_idx, len(lines))
                if lines[i].strip().startswith("#endif")
                and "CONFIG_LIVEPATCH" in lines[i]
            ),
            None,
        )
        if end_idx is None:
            logger.warning("kallsyms_on_each_symbol 找到 #ifdef 但没有匹配的 #endif，跳过")
            return

        del lines[end_idx]
        del lines[guard_idx]
        self._write(path, "\n".join(lines))
        logger.info("kallsyms_on_each_symbol: 已移除 CONFIG_LIVEPATCH 限制（5.15 兼容）")

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

    def _fix_kernelsu_newer_kernels(self, ksu_dir: Optional[Path] = None):
        """6.12+ 的 KernelSU 兼容修复。

        - feature/selinux_hide.c 里的定义去掉 static；
        - downstream/slow_avc_audit_defs.h 里的 static 声明改成 extern，
          否则同一个编译单元里会出现 static 与 non-static 冲突；
        - kernel/Makefile 去掉 `-g0`（改成 `-g`）：6.12 Kleaf 用
          gendwarfksyms 生成 MODVERSIONS，需要 ksu.o 的 DWARF，缺了会报
          “dwarf_get_units failed: no debugging information?”。
        KernelSU 源仓库和 common/drivers/kernelsu 两边都处理（可能是副本）。
        """
        if self.config.kernel_version not in ("6.12", "6.18"):
            return

        roots = [self.work_dir / "KernelSU/kernel", self.work_dir / "common/drivers/kernelsu"]
        if ksu_dir is not None:
            roots.insert(0, ksu_dir / "kernel")

        replacements = (
            (
                "feature/selinux_hide.c",
                "static bool ksu_selinux_hide_enabled __read_mostly = true;",
                "bool ksu_selinux_hide_enabled __read_mostly = true;",
                "导出 ksu_selinux_hide_enabled",
            ),
            (
                "downstream/slow_avc_audit_defs.h",
                "static bool ksu_selinux_hide_enabled;",
                "extern bool ksu_selinux_hide_enabled;",
                "slow_avc_audit_defs.h 改为 extern 声明",
            ),
            (
                "Makefile",
                "CFLAGS_ksu.o += -g0 -fno-unwind-tables",
                "CFLAGS_ksu.o += -g -fno-unwind-tables",
                "去掉 -g0，gendwarfksyms 需要 DWARF 调试信息",
            ),
        )

        seen = set()
        for root in roots:
            if not root.exists():
                continue
            for rel, old, new, desc in replacements:
                path = root / rel
                if not path.exists():
                    continue
                try:
                    key = path.resolve()
                except OSError:
                    key = path
                if key in seen:
                    continue
                seen.add(key)
                content = self._read(path)
                if old not in content:
                    continue
                self._write(path, content.replace(old, new))
                logger.info("KernelSU 兼容: %s (%s)", desc, path)

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
        if not self.config.use_susfs:
            logger.info("=== 未启用 SUSFS，跳过补丁 ===")
            return
        logger.info("=== 应用 SUSFS 补丁 ===")
        common_dir = self.work_dir / "common"
        patch_name = self.config.get_susfs_patch_filename()
        susfs_patch = self.susfs_dir / "kernel_patches" / patch_name
        if not susfs_patch.exists():
            branch = "内置"
            if (self.susfs_dir / ".git").exists():
                branch = self._git_output(
                    ["rev-parse", "--abbrev-ref", "HEAD"], self.susfs_dir
                ) or "未知"
            raise RuntimeError(
                f"找不到 SUSFS 补丁: {patch_name}"
                f"（{self.susfs_dir} 当前分支: {branch}）"
            )

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

    def _ksu_kconfigs(self) -> list:
        return [
            self.work_dir / "common/drivers/kernelsu/Kconfig",
            self.work_dir / "KernelSU/kernel/Kconfig",
        ]

    def _ksu_supports_inline_hook(self) -> bool:
        """builtin-new 用 KSU_SUSFS_INLINE_HOOK 选择 SUSFS 内联钩子；builtin 没有这个符号。"""
        return any(
            path.exists() and "config KSU_SUSFS_INLINE_HOOK" in self._read(path)
            for path in self._ksu_kconfigs()
        )

    def _ksu_supports_branch_link_hook(self) -> bool:
        """无 SUSFS 时用 KSU_HACK_ARM64_BRANCH_LINK 作为钩子；builtin 分支没有该符号。"""
        return any(
            path.exists() and "config KSU_HACK_ARM64_BRANCH_LINK" in self._read(path)
            for path in self._ksu_kconfigs()
        )

    def _kernel_config_template(self) -> str:
        """按本次构建开启的选项拼 gki_defconfig，未开启的功能不写入。"""
        parts = [self.KSU_CONFIG]
        if self.config.use_kpm:
            parts.append(self.KPM_CONFIG)
        parts.append(self.TMPFS_CONFIG)
        parts.append(self.NETWORK_CONFIG)
        if self.config.set_default_bbr:
            parts.append(self.BBR_CONFIG)
        if self.config.use_susfs:
            susfs = self.SUSFS_CONFIG
            if not self._ksu_supports_inline_hook():
                susfs = susfs.replace("CONFIG_KSU_SUSFS_INLINE_HOOK=y\n", "")
            parts.append(susfs)
        return "".join(parts)

    def configure_kernel(self):
        logger.info("=== 配置内核 ===")
        config_file = self.work_dir / "common/arch/arm64/configs/gki_defconfig"
        if not config_file.exists():
            logger.warning("配置文件不存在: %s", config_file)
            return

        self._append(config_file, self._kernel_config_template())
        if self.config.use_susfs:
            # 6.6 的 SUSFS 补丁对 SUS_PATH 支持不完整，保持关闭
            sus_path = "n" if self.config.kernel_version == "6.6" else "y"
            self._append(config_file, f"CONFIG_KSU_SUSFS_SUS_PATH={sus_path}\n")
        else:
            if not self._ksu_supports_branch_link_hook():
                raise RuntimeError(
                    "当前 SukiSU 分支没有 CONFIG_KSU_HACK_ARM64_BRANCH_LINK，"
                    "无法在不打 SUSFS 补丁时获得 root；请使用 Dev(builtin-new) "
                    "或 Custom 的 builtin-new 分支"
                )
            logger.info("无 SUSFS 构建：使用 CONFIG_KSU_HACK_ARM64_BRANCH_LINK 钩子")
            self._append(config_file, "CONFIG_KSU_SUSFS=n\n")
            self._append(config_file, "CONFIG_KSU_HACK_ARM64_BRANCH_LINK=y\n")

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

    def _disable_kleaf_defconfig_check(self, target: str = "kernel_aarch64"):
        """只给指定 kernel_build 块加 check_defconfig="disabled"。

        不能只看整个文件里是否存在该字符串：6.12/6.18 的 kernel_aarch64_tv
        已经带了这个属性，旧逻辑会误判成 kernel_aarch64 已处理，最终仍会在
        savedefconfig 校验时失败。
        """
        bazel_build = self.work_dir / "common/BUILD.bazel"
        if not bazel_build.exists():
            return

        lines = self._read(bazel_build).split("\n")
        name_re = re.compile(
            rf'^\s*name\s*=\s*"{re.escape(target)}",?\s*$'
        )
        start = next(
            (i for i, line in enumerate(lines) if name_re.match(line)),
            None,
        )
        if start is None:
            logger.warning("BUILD.bazel 未找到 target %s，跳过 check_defconfig", target)
            return
        end = next(
            (
                i for i in range(start + 1, len(lines))
                if lines[i].strip() == ")" and not lines[i][:1].isspace()
            ),
            len(lines),
        )
        if any('check_defconfig = "disabled"' in line for line in lines[start:end]):
            return

        lines.insert(start + 1, '    check_defconfig = "disabled",')
        self._write(bazel_build, "\n".join(lines))
        logger.info("已禁用 %s 的 check_defconfig 校验", target)

    def _configure_kleaf_name(self, safe_custom_version: str):
        self._disable_kleaf_defconfig_check()

        bazel_build = self.work_dir / "common/BUILD.bazel"
        if bazel_build.exists():
            content = self._read(bazel_build)
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

        config_lines = list(dict.fromkeys(
            line.strip() for line in self._read(config_file).splitlines()
            if line.strip().startswith("CONFIG_")
        ))

        def is_ksu(line: str) -> bool:
            # KernelSU 区不显示 SUSFS 选项，避免和下面的 SUSFS 区重复
            return line.startswith("CONFIG_KSU") and not line.startswith("CONFIG_KSU_SUSFS")

        key_configs = (
            ("KernelSU", is_ksu),
            ("KPM", lambda c: c.startswith("CONFIG_KPM")),
            ("SUSFS", lambda c: c.startswith("CONFIG_KSU_SUSFS")),
            ("Baseband-guard", lambda c: c.startswith("CONFIG_BBG")),
            ("BBR", lambda c: c.startswith("CONFIG_BBR")),
            ("ZRAM", lambda c: c.startswith("CONFIG_ZRAM")),
        )
        logger.info("关键配置状态:")
        for name, matches in key_configs:
            found = [c for c in config_lines if matches(c)]
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
        # 补丁应用完后再确认一次，避免补丁回退或重复同步把兼容修改覆盖掉
        self._fix_kallsyms_on_each_symbol()
        self._fix_kernelsu_newer_kernels()
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

        build_log = self._build_log_path()
        logger.info("构建日志: %s", build_log)
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
                    log_file=build_log,
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
                # 追加的 gki_defconfig 片段不是 savedefconfig 规范格式，关闭校验
                self._disable_kleaf_defconfig_check()
                result = self._run(
                    f"tools/bazel build --disk_cache={q(disk_cache)} --config=fast"
                    f"{lto_flag}{page_flag} {target}",
                    cwd=self.work_dir,
                    check=False,
                    log_file=build_log,
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
        """确认最终 .config 里的关键配置没有丢失。"""
        candidates = []
        candidates.extend([
            self.work_dir / "bazel-bin/common/kernel_aarch64_config/out_dir/.config",
            self.work_dir / f"out/{self.config.android_version}-{self.config.kernel_version}/dist/.config",
            self.work_dir / f"out/{self.config.android_version}-{self.config.kernel_version}/.config",
        ])
        config_file = next((p for p in candidates if p.exists()), None)
        if config_file is None:
            config_file = next(
                self.work_dir.glob("bazel-bin/common/**/out_dir/.config"), None
            )
        if config_file is None:
            config_file = next(self.work_dir.glob("out/**/.config"), None)
        if config_file is None:
            logger.warning("未找到生成的 .config，跳过配置校验")
            return

        content = self._read(config_file)
        if self.config.use_susfs:
            required = ["CONFIG_KSU=y", "CONFIG_KSU_SUSFS=y"]
            if self._ksu_supports_inline_hook():
                required.append("CONFIG_KSU_SUSFS_INLINE_HOOK=y")
        else:
            required = ["CONFIG_KSU=y", "CONFIG_KSU_HACK_ARM64_BRANCH_LINK=y"]
        missing = [name for name in required if name not in content]
        if missing:
            raise RuntimeError(
                f"最终 .config 缺少 {', '.join(missing)}: {config_file}"
            )

        if self.config.page_size == "4k" and "CONFIG_ARM64_4K_PAGES=y" not in content:
            raise RuntimeError(f"4k page size 未生效: {config_file}")

        if self.config.page_size == "16k" and "CONFIG_ARM64_16K_PAGES=y" not in content:
            raise RuntimeError(f"16k page size 未生效: {config_file}")
        logger.info("配置校验通过: %s", config_file)

    def patch_kpm_image(self):
        if (
            not self.config.use_kpm
            or self.config.kernel_version not in KPM_SUPPORTED_KERNELS
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
        # 每次重新拉取，避免旧版本/半截文件残留（下载本来也是每次都做）
        if tools_dir.exists():
            shutil.rmtree(tools_dir, ignore_errors=True)
        tools_dir.mkdir(parents=True, exist_ok=True)
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

        for image_name in ("Image", "Image.lz4", "Image.gz"):
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
        probe = self._run(
            f"curl -sL -w '%{{http_code}}' {q(gki_url)} -o /dev/null",
            check=False,
            capture_output=True,
        )
        url = gki_url if "200" in (probe.stdout or "") else fallback_url

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
        if not self.config.use_susfs:
            susfs_source = "disabled"
            susfs_commit = ""
        elif kv == "6.18":
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
            # 先清掉上次中断遗留的 repo/git 进程，避免缓存仓库被占锁
            self._kill_stale_downloads()
            self._clean_previous_outputs()
            self.clone_repositories()
            self.clone_toolchain()
            self.setup_repo_tool()
            self.init_and_sync_kernel()
            self.apply_toolchain_fixes()
            self.add_kernel_supatch()
            self.add_kernelsu()
            self.add_bbg()
            self.apply_susfs_patches()
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
