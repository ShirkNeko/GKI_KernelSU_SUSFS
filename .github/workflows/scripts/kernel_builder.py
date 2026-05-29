import os
import subprocess
import logging
from pathlib import Path
from typing import Optional, Callable, Dict, Any
from dataclasses import dataclass, field

from config import (
    BuildConfig, 
    KSU_REPO_CONFIG, 
    SUSFS_REPO_CONFIG, 
    SUKISU_PATCH_REPO_CONFIG,
    ANYKERNEL_CONFIG,
    KERNEL_PATCHES_CONFIG,
    BBG_CONFIG,
    TOOLCHAIN_CONFIG,
    LEGACY_FIXES,
    OP8E_PATCH_URL,
    KPM_PATCH_URL,
    KernelVersion,
    AndroidVersion,
)


# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


@dataclass
class BuildResult:
    """构建结果"""
    success: bool
    config: BuildConfig
    message: str = ""
    artifacts: list = field(default_factory=list)
    build_time: Optional[float] = None


class ShellCommand:
    """Shell 命令执行器"""
    
    def __init__(self, cwd: Optional[str] = None, env: Optional[Dict] = None):
        self.cwd = cwd
        self.env = env or os.environ.copy()
    
    def run(self, cmd: str, check: bool = True, capture_output: bool = False,
            shell: bool = True, timeout: Optional[int] = None) -> subprocess.CompletedProcess:
        """执行 shell 命令"""
        logger.info(f"执行命令: {cmd}")
        try:
            result = subprocess.run(
                cmd,
                shell=shell,
                cwd=self.cwd,
                env=self.env,
                capture_output=capture_output,
                text=True,
                timeout=timeout,
                check=check
            )
            return result
        except subprocess.CalledProcessError as e:
            logger.error(f"命令执行失败: {cmd}")
            logger.error(f"错误信息: {e.stderr if e.stderr else str(e)}")
            raise
        except subprocess.TimeoutExpired as e:
            logger.error(f"命令执行超时: {cmd}")
            raise
    
    def run_with_callback(self, cmd: str, callback: Optional[Callable] = None) -> str:
        """执行命令并实时输出"""
        logger.info(f"执行命令: {cmd}")
        process = subprocess.Popen(
            cmd,
            shell=True,
            cwd=self.cwd,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )
        
        output_lines = []
        for line in process.stdout:
            line = line.rstrip()
            output_lines.append(line)
            if callback:
                callback(line)
        
        process.wait()
        if process.returncode != 0:
            raise RuntimeError(f"命令执行失败: {' '.join(output_lines[-10:])}")
        
        return "\n".join(output_lines)


class KernelBuilder:
    """内核构建器"""
    
    # 内核配置模板
    KERNEL_CONFIG_TEMPLATE = """
# === KernelSU Config ===
CONFIG_KSU=y
CONFIG_KPM=y
CONFIG_KSU_SUSFS_SUS_SU=n

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
CONFIG_KSU_SUSFS_SUS_MAP=y
CONFIG_KSU_SUSFS_SUS_MOUNT=y
CONFIG_KSU_SUSFS_AUTO_ADD_SUS_KSU_DEFAULT_MOUNT=y
CONFIG_KSU_SUSFS_AUTO_ADD_SUS_BIND_MOUNT=y
CONFIG_KSU_SUSFS_SUS_KSTAT=y
CONFIG_KSU_SUSFS_TRY_UMOUNT=y
CONFIG_KSU_SUSFS_AUTO_ADD_TRY_UMOUNT_FOR_BIND_MOUNT=y
CONFIG_KSU_SUSFS_SPOOF_UNAME=y
CONFIG_KSU_SUSFS_ENABLE_LOG=y
CONFIG_KSU_SUSFS_HIDE_KSU_SUSFS_SYMBOLS=y
CONFIG_KSU_SUSFS_SPOOF_CMDLINE_OR_BOOTCONFIG=y
CONFIG_KSU_SUSFS_OPEN_REDIRECT=y
"""

    ZRAM_CONFIG_5_10 = """
CONFIG_ZSMALLOC=y
CONFIG_ZRAM=y
CONFIG_MODULE_SIG=n
CONFIG_CRYPTO_LZO=y
CONFIG_ZRAM_DEF_COMP_LZ4KD=y
"""

    ZRAM_CONFIG_COMMON = """
CONFIG_CRYPTO_LZ4HC=y
CONFIG_CRYPTO_LZ4K=y
CONFIG_CRYPTO_LZ4KD=y
CONFIG_CRYPTO_842=y
CONFIG_CRYPTO_LZ4K_OPLUS=y
CONFIG_ZRAM_WRITEBACK=y
"""

    def __init__(self, config: BuildConfig, workspace: str):
        self.config = config
        self.workspace = Path(workspace)
        self.shell = ShellCommand(cwd=workspace)
        self.env = os.environ.copy()
        
        # 创建工作目录
        self.work_dir = self.workspace / config.config_name
        self.work_dir.mkdir(parents=True, exist_ok=True)
        
        # 仓库目录
        self.kernel_dir = self.work_dir / "kernel"
        self.susfs_dir = self.workspace / "susfs4ksu"
        self.sukisu_patch_dir = self.workspace / "SukiSU_patch"
        self.anykernel_dir = self.workspace / "AnyKernel3"
        self.kernel_patches_dir = self.workspace / "kernel_patches"
        self.toolchain_dir = self.workspace / "toolchain"
        self.mkbootimg_dir = self.workspace / "mkbootimg"
        
        # 设置环境变量
        self._setup_env()
    
    def _setup_env(self):
        """设置环境变量"""
        self.env["CONFIG"] = self.config.config_name
        self.env["CCACHE_COMPILERCHECK"] = "%compiler% -dumpmachine; %compiler% -dumpversion"
        self.env["CCACHE_NOHASHDIR"] = "true"
        self.env["CCACHE_HARDLINK"] = "true"
        self.shell.env = self.env
    
    def _run_cmd(self, cmd: str, **kwargs) -> subprocess.CompletedProcess:
        """运行命令"""
        return self.shell.run(cmd, **kwargs)
    
    def _chdir(self, path: Path) -> None:
        """切换目录并更新 shell cwd"""
        os.chdir(path)
        self.shell.cwd = str(path)
    
    def _apply_susfs_commit(self) -> None:
        """应用 SUSFS commit (支持 hash 或相对偏移如 HEAD~1)"""
        if not self.config.susfs_commit:
            return
        
        if not self.susfs_dir.exists():
            logger.warning("SUSFS 目录不存在，跳过 commit 应用")
            return
        
        logger.info(f"应用 SUSFS commit: {self.config.susfs_commit}")
        
        self._chdir(self.susfs_dir)
        
        # 检查是否为相对偏移 (如 HEAD~1, HEAD~3)
        if self.config.susfs_commit.startswith("HEAD~"):
            # 相对偏移
            logger.info(f"使用相对偏移: {self.config.susfs_commit}")
            self._run_cmd(f"git fetch origin", check=False)
            self._run_cmd(f"git reset --hard {self.config.susfs_commit}", check=False)
            
            # 获取实际 commit hash
            result = self._run_cmd("git rev-parse HEAD", check=False)
            if result.returncode == 0:
                actual_commit = result.stdout.strip()
                logger.info(f"当前 SUSFS commit: {actual_commit}")
        else:
            # 直接使用 commit hash
            logger.info(f"切换到指定 commit: {self.config.susfs_commit}")
            self._run_cmd(f"git fetch origin", check=False)
            self._run_cmd(f"git checkout {self.config.susfs_commit}", check=False)
        
        self._chdir(self.workspace)
    
    def clone_repositories(self) -> None:
        """克隆所有必需的仓库"""
        logger.info("=== 开始克隆仓库 ===")
        
        # 克隆 SUSFS
        if not self.susfs_dir.exists():
            logger.info(f"克隆 SUSFS 仓库...")
            self._run_cmd(
                f"git clone {SUSFS_REPO_CONFIG['repo_url']} -b {self.config.kernel_branch}",
                check=False
            )
        else:
            logger.info("SUSFS 仓库已存在，跳过克隆")
        
        # 应用 SUSFS commit (如果指定了)
        self._apply_susfs_commit()
        
        # 克隆 SukiSU Patch
        if not self.sukisu_patch_dir.exists():
            logger.info("克隆 SukiSU Patch 仓库...")
            self._run_cmd(
                f"git clone {SUKISU_PATCH_REPO_CONFIG['repo_url']}",
                check=False
            )
        else:
            logger.info("SukiSU Patch 仓库已存在，跳过克隆")
        
        # 克隆 AnyKernel3
        if not self.anykernel_dir.exists():
            logger.info("克隆 AnyKernel3 仓库...")
            self._run_cmd(
                f"git clone {ANYKERNEL_CONFIG['repo_url']} -b {ANYKERNEL_CONFIG['branch']}",
                check=False
            )
        else:
            logger.info("AnyKernel3 仓库已存在，跳过克隆")
        
        # 克隆 Kernel Patches
        if not self.kernel_patches_dir.exists():
            logger.info("克隆 Kernel Patches 仓库...")
            self._run_cmd(
                f"git clone {KERNEL_PATCHES_CONFIG['repo_url']}",
                check=False
            )
        else:
            logger.info("Kernel Patches 仓库已存在，跳过克隆")
        
        logger.info("=== 仓库克隆完成 ===")
    
    def clone_toolchain(self) -> None:
        """克隆工具链"""
        logger.info("=== 克隆工具链 ===")
        
        if not self.toolchain_dir.exists():
            logger.info("克隆 build-tools...")
            self._run_cmd(
                f"git clone {TOOLCHAIN_CONFIG['aosp_mirror']}/kernel/prebuilts/build-tools "
                f"-b {TOOLCHAIN_CONFIG['build_tools_branch']} --depth 1 {self.toolchain_dir}",
                check=False
            )
        
        if not self.mkbootimg_dir.exists():
            logger.info("克隆 mkbootimg...")
            self._run_cmd(
                f"git clone {TOOLCHAIN_CONFIG['aosp_mirror']}/platform/system/tools/mkbootimg "
                f"-b {TOOLCHAIN_CONFIG['mkbootimg_branch']} --depth 1 {self.mkbootimg_dir}",
                check=False
            )
        
        # 设置工具链环境变量
        self.env["AVBTOOL"] = str(self.toolchain_dir / "linux-x86/bin/avbtool")
        self.env["MKBOOTIMG"] = str(self.mkbootimg_dir / "mkbootimg.py")
        self.env["UNPACK_BOOTIMG"] = str(self.mkbootimg_dir / "unpack_bootimg.py")

        # 设置签名密钥路径
        if "BOOT_SIGN_KEY_PATH" in os.environ:
            self.env["BOOT_SIGN_KEY_PATH"] = os.environ["BOOT_SIGN_KEY_PATH"]

        self.shell.env = self.env

        logger.info("=== 工具链准备完成 ===")
    
    def setup_repo_tool(self) -> None:
        """安装 repo 工具"""
        logger.info("=== 安装 repo 工具 ===")
        
        repo_dir = self.workspace / "git-repo"
        repo_dir.mkdir(exist_ok=True)
        
        repo_path = repo_dir / "repo"
        if not repo_path.exists():
            logger.info("下载 repo 工具...")
            self._run_cmd(
                f"curl https://storage.googleapis.com/git-repo-downloads/repo > {repo_path}",
                check=False
            )
            self._run_cmd(f"chmod a+rx {repo_path}", check=False)
        
        self.env["REPO"] = str(repo_path)
        self.shell.env = self.env
        
        logger.info("=== repo 工具安装完成 ===")
    
    def init_and_sync_kernel(self) -> None:
        """初始化和同步内核源代码"""
        logger.info("=== 初始化和同步内核源代码 ===")
        
        # 设置工作目录
        self._chdir(self.work_dir)
        self.shell.cwd = str(self.work_dir)
        
        formatted_branch = self.config.formatted_branch
        
        # 初始化 repo
        logger.info(f"初始化 repo: {formatted_branch}")
        self._run_cmd(
            f"$REPO init --depth=1 "
            f"--u https://android.googlesource.com/kernel/manifest "
            f"-b common-{formatted_branch} --repo-rev=v2.16",
            check=False
        )
        
        # 检查是否为 deprecated 分支
        remote_branch_result = subprocess.run(
            f"git ls-remote https://android.googlesource.com/kernel/common {formatted_branch}",
            shell=True, capture_output=True, text=True
        )
        remote_branch = remote_branch_result.stdout.strip()
        
        if "deprecated" in remote_branch:
            logger.info("检测到 deprecated 分支，修改 manifest...")
            manifest_path = self.work_dir / ".repo/manifests/default.xml"
            with open(manifest_path, "r") as f:
                content = f.read()
            content = content.replace(
                f'"{formatted_branch}"',
                f'"deprecated/{formatted_branch}"'
            )
            with open(manifest_path, "w") as f:
                f.write(content)
        
        # 保存 REMOTE_BRANCH 到环境变量
        self.env["REMOTE_BRANCH"] = remote_branch
        
        # 同步
        logger.info("同步内核源代码...")
        self._run_cmd("$REPO --trace sync -c -j$(nproc --all) --no-tags --fail-fast", check=False)
        
        # 检查 common 目录是否存在
        common_dir = self.work_dir / "common"
        if not common_dir.exists():
            logger.error("repo sync 失败，common 目录不存在")
            raise RuntimeError(
                f"repo sync 失败！\n"
                "可能原因: 网络问题或仓库不可用\n"
                "请重试构建"
            )
        
        # 旧版内核兼容修复
        self._apply_legacy_fixes(remote_branch)
        
        logger.info("=== 内核源代码同步完成 ===")
    
    def _apply_legacy_fixes(self, remote_branch: str = "") -> None:
        """应用旧版内核兼容补丁"""
        av = self.config.android_version
        kv = self.config.kernel_version
        sub_level = self.config.get_sub_level_int()
        
        # 检查是否为 deprecated 分支
        is_deprecated = "deprecated" in remote_branch
        
        # android13-5.15 below 123 需要特殊修复
        if is_deprecated and av == "android13" and kv == "5.15" and sub_level and sub_level < 123:
            logger.info("修复 5.15 仅支持旧版 C 库的 BUG")
            common_dir = self.work_dir / "common"
            self._chdir(common_dir)
            
            patch_url = LEGACY_FIXES['android13-5.15-below-123']['url']
            self._run_cmd(
                f"curl -LSs {patch_url} -o fix_5.15.legacy.patch && patch -p1 < fix_5.15.legacy.patch",
                check=False
            )
            
            self._chdir(self.work_dir)
        
        # android12-5.10 below 136 需要 fdinfo 修复
        if av == "android12" and kv == "5.10" and sub_level and sub_level < 136:
            logger.info("应用 android12-5.10 fdinfo 修复补丁...")
            common_dir = self.work_dir / "common"
            self._chdir(common_dir)
            
            patch_url = LEGACY_FIXES['android12-5.10-below-136']['url']
            self._run_cmd(
                f"curl -LSs {patch_url} | patch -p1",
                check=False
            )
            
            self._chdir(self.work_dir)
    
    def add_kernel_supatch(self) -> None:
        """添加 OnePlus 8E 支持补丁"""
        if not self.config.support_op8e:
            return
        
        logger.info("=== 添加 OnePlus 8E 支持补丁 ===")
        
        drivers_dir = self.work_dir / "common/drivers"
        if not drivers_dir.exists():
            logger.warning("驱动目录不存在，跳过 OnePlus 8E 支持补丁")
            return
        
        self._chdir(drivers_dir)
        
        logger.info("下载 hmbird_patch.c...")
        self._run_cmd(f"curl -LSs {OP8E_PATCH_URL} -o hmbird_patch.c", check=False)
        
        if (drivers_dir / "hmbird_patch.c").exists():
            logger.info("将补丁添加到 Makefile...")
            with open(drivers_dir / "Makefile", "a") as f:
                f.write("obj-y += hmbird_patch.o\n")
        
        logger.info("=== OnePlus 8E 支持补丁添加完成 ===")
    
    def add_kernelsu(self) -> None:
        """添加 KernelSU"""
        logger.info("=== 添加 KernelSU ===")
        
        self._chdir(self.work_dir)
        
        # 如果指定了 commit hash，先克隆指定版本
        if self.config.kernelsu_commit:
            logger.info(f"使用指定的 KernelSU commit: {self.config.kernelsu_commit}")
            # 临时修改 setup 脚本来使用指定 commit
            setup_url = f"https://raw.githubusercontent.com/SukiSU-Ultra/SukiSU-Ultra/{self.config.kernelsu_commit}/kernel/setup.sh"
        else:
            setup_url = KSU_REPO_CONFIG["setup_script"]
        
        logger.info("执行 KernelSU 安装脚本...")
        self._run_cmd(
            f"curl -LSs {setup_url} | bash {self.config.ksu_branch_arg}",
            check=False
        )
        
        # 如果使用了指定 commit，切换到该 commit
        if self.config.kernelsu_commit:
            ksu_dir = self.work_dir / "KernelSU"
            if ksu_dir.exists():
                self._chdir(ksu_dir)
                self._run_cmd(f"git checkout {self.config.kernelsu_commit}", check=False)
                self._chdir(self.work_dir)
        
        logger.info("=== KernelSU 添加完成 ===")
    
    def add_bbg(self) -> None:
        """添加 Baseband-guard 支持"""
        if not self.config.use_bbg:
            return
        
        logger.info("=== 添加 Baseband-guard 支持 ===")
        
        common_dir = self.work_dir / "common"
        if not common_dir.exists():
            return
        
        self._chdir(common_dir)
        
        # 下载并运行 setup 脚本
        logger.info("下载并运行 Baseband-guard setup...")
        self._run_cmd(f"wget -O- {BBG_CONFIG['setup_script']} | bash", check=False)
        
        # 添加内核配置
        config_file = common_dir / "arch/arm64/configs/gki_defconfig"
        if config_file.exists():
            with open(config_file, "a") as f:
                f.write("CONFIG_BBG=y\n")
        
        # 修改 LSM Kconfig
        kconfig_file = common_dir / "security/Kconfig"
        if kconfig_file.exists():
            with open(kconfig_file, "r") as f:
                content = f.read()

            # 在 config LSM 块中找到 default 行，添加 baseband_guard
            import re
            # 匹配 config LSM 块
            pattern = r'(config LSM.*?)(default .*)(\n.*?help)'
            def replace_default(match):
                prefix = match.group(1)
                default_line = match.group(2)
                suffix = match.group(3)
                # 如果已经有 baseband_guard，跳过
                if 'baseband_guard' in default_line:
                    return match.group(0)
                # 如果有 lockdown，替换它
                if 'lockdown' in default_line:
                    default_line = default_line.replace('lockdown', 'lockdown,baseband_guard')
                return prefix + default_line + suffix

            content = re.sub(pattern, replace_default, content, flags=re.DOTALL)
            with open(kconfig_file, "w") as f:
                f.write(content)

        logger.info("=== Baseband-guard 添加完成 ===")
    
    def apply_susfs_patches(self) -> None:
        """应用 SUSFS 补丁"""
        logger.info("=== 应用 SUSFS 补丁 ===")

        self._chdir(self.work_dir)
        common_dir = self.work_dir / "common"

        # 复制 SUSFS 补丁
        susfs_patch = self.susfs_dir / "kernel_patches" / self.config.get_susfs_patch_filename()
        if susfs_patch.exists():
            self._run_cmd(f"cp {susfs_patch} {common_dir}/", check=False)

        # 复制 SUSFS 源文件
        fs_dir = self.susfs_dir / "kernel_patches/fs"
        if fs_dir.exists():
            self._run_cmd(f"cp -r {fs_dir}/* {common_dir}/fs/", check=False)

        include_dir = self.susfs_dir / "kernel_patches/include/linux"
        if include_dir.exists():
            self._run_cmd(f"cp -r {include_dir}/* {common_dir}/include/linux/", check=False)

        # 应用主补丁
        if susfs_patch.exists():
            patch_file = common_dir / self.config.get_susfs_patch_filename()
            if patch_file.exists():
                self._chdir(common_dir)
                self._run_cmd(f"patch -p1 --fuzz=3 < {patch_file}", check=False)
                self._chdir(self.work_dir)

        logger.info("=== SUSFS 补丁应用完成 ===")
    
    def apply_sukisu_patches(self) -> None:
        """应用 SukiSU 特定补丁"""
        logger.info("=== 应用 SukiSU 补丁 ===")
        
        self._chdir(self.work_dir / "common")
        
        # 复制 hooks 补丁
        hooks_patch = self.sukisu_patch_dir / "69_hide_stuff.patch"
        if hooks_patch.exists():
            self._run_cmd(f"cp {hooks_patch} .", check=False)
            self._run_cmd("patch -p1 -F 3 < 69_hide_stuff.patch", check=False)
        
        logger.info("=== SukiSU 补丁应用完成 ===")
    
    def apply_zram_patches(self) -> None:
        """应用 ZRAM (LZ4KD) 补丁"""
        if not self.config.use_zram:
            return
        
        logger.info("=== 应用 ZRAM (LZ4KD) 补丁 ===")
        
        self._chdir(self.work_dir / "common")
        
        # 复制 LZ4K 源文件
        lz4k_inc = self.sukisu_patch_dir / "other/zram/lz4k/include/linux"
        lz4k_lib = self.sukisu_patch_dir / "other/zram/lz4k/lib"
        lz4k_crypto = self.sukisu_patch_dir / "other/zram/lz4k/crypto"
        lz4k_oplus = self.sukisu_patch_dir / "other/zram/lz4k_oplus"
        
        if lz4k_inc.exists():
            self._run_cmd(f"cp -r {lz4k_inc}/* include/linux/", check=False)
        if lz4k_lib.exists():
            self._run_cmd(f"cp -r {lz4k_lib}/* lib/", check=False)
        if lz4k_crypto.exists():
            self._run_cmd(f"cp -r {lz4k_crypto}/* crypto/", check=False)
        if lz4k_oplus.exists():
            self._run_cmd(f"cp -r {lz4k_oplus} lib/", check=False)
        
        # 应用补丁
        zram_patch_dir = self.sukisu_patch_dir / f"other/zram/zram_patch/{self.config.kernel_version}"
        
        lz4kd_patch = zram_patch_dir / "lz4kd.patch"
        if lz4kd_patch.exists():
            logger.info("应用 lz4kd.patch...")
            self._run_cmd(f"patch -p1 -F 3 < {lz4kd_patch}", check=False)
        
        lz4k_oplus_patch = zram_patch_dir / "lz4k_oplus.patch"
        if lz4k_oplus_patch.exists():
            logger.info("应用 lz4k_oplus.patch...")
            self._run_cmd(f"patch -p1 -F 3 < {lz4k_oplus_patch}", check=False)
        
        logger.info("=== ZRAM 补丁应用完成 ===")
    
    def apply_task_mmu_fixes(self) -> None:
        """应用 task_mmu.c 修复"""
        logger.info("=== 应用 task_mmu.c 修复 ===")

        self._chdir(self.work_dir / "common")

        task_mmu_file = Path("fs/proc/task_mmu.c")
        if not task_mmu_file.exists():
            logger.warning("task_mmu.c 文件不存在，跳过修复")
            return

        av = self.config.android_version
        kv = self.config.kernel_version
        formatted_branch = f"{av}-{kv}"

        # android15-6.6 修复
        if formatted_branch == "android15-6.6":
            with open(task_mmu_file, "r") as f:
                content = f.read()

            # 检查是否需要添加 nr_subpages
            if "unsigned int nr_subpages = __PAGE_SIZE / PAGE_SIZE;" not in content:
                logger.info("未找到 nr_subpages，正在进行补丁修复")

                lines = content.split('\n')
                new_lines = []
                inserted = False
                for line in lines:
                    new_lines.append(line)
                    # 在 "int ret = 0, copied = 0;" 之后添加两行
                    if not inserted and "int ret = 0, copied = 0;" in line:
                        new_lines.append('\tunsigned int nr_subpages = __PAGE_SIZE / PAGE_SIZE;')
                        new_lines.append('\tpagemap_entry_t *res = NULL;')
                        inserted = True

                if inserted:
                    with open(task_mmu_file, "w") as f:
                        f.write('\n'.join(new_lines))

                    # 立即撤销修改（fake patch）
                    logger.info("撤销 task_mmu.c 的修改（fake patch）")
                    with open(task_mmu_file, "r") as f:
                        content = f.read()
                    lines = content.split('\n')
                    new_lines = []
                    for line in lines:
                        if '\tunsigned int nr_subpages = __PAGE_SIZE / PAGE_SIZE;' not in line and \
                           '\tpagemap_entry_t *res = NULL;' not in line:
                            new_lines.append(line)
                    with open(task_mmu_file, "w") as f:
                        f.write('\n'.join(new_lines))

            # 修复 base.c 头文件
            self._fix_base_c_header()

        # android14-6.1 修复
        elif formatted_branch == "android14-6.1":
            with open(task_mmu_file, "r") as f:
                content = f.read()

            # 检查 vma_pages 是否存在
            if "if (!vma_pages(vma))" not in content:
                logger.info("未找到 vma_pages，正在进行补丁修复")

                # 修复 base.c 头文件
                self._fix_base_c_header()

                # 执行修复
                if "goto show_pad;" in content:
                    logger.info("执行 task_mmu.c 修复")
                    content = content.replace("goto show_pad;", "return 0;")
                    with open(task_mmu_file, "w") as f:
                        f.write(content)

        # android12-5.10, android13-5.10, android13-5.15 修复
        elif formatted_branch in ["android12-5.10", "android13-5.10", "android13-5.15"]:
            with open(task_mmu_file, "r") as f:
                content = f.read()

            # 检查 vma_pages 是否存在
            if "if (!vma_pages(vma))" not in content:
                logger.info("未找到 vma_pages，正在进行补丁修复")

                # 执行修复
                if "goto show_pad;" in content:
                    logger.info("执行 task_mmu.c 修复")
                    content = content.replace("goto show_pad;", "return 0;")
                    with open(task_mmu_file, "w") as f:
                        f.write(content)

        logger.info("=== task_mmu.c 修复完成 ===")

    def _fix_base_c_header(self) -> None:
        """修复 base.c 头文件"""
        base_c = self.work_dir / "common/fs/proc/base.c"

        if not base_c.exists():
            return

        with open(base_c, "r") as f:
            content = f.read()

        # 检查是否需要添加头文件
        if "#include <linux/dma-buf.h>" not in content:
            logger.info("未找到 #include <linux/dma-buf.h>，添加缺失的头文件")
            content = content.replace(
                "#include <linux/cpufreq_times.h>",
                "#include <linux/cpufreq_times.h>\n#include <linux/dma-buf.h>"
            )
            with open(base_c, "w") as f:
                f.write(content)
    
    def configure_kernel(self) -> None:
        """配置内核"""
        logger.info("=== 配置内核 ===")
        
        self._chdir(self.work_dir)
        config_file = self.work_dir / "common/arch/arm64/configs/gki_defconfig"
        
        if not config_file.exists():
            logger.error(f"配置文件不存在: {config_file}")
            return
        
        # 写入基础配置
        with open(config_file, "a") as f:
            f.write(self.KERNEL_CONFIG_TEMPLATE)
        
        # 写入 SUSFS SUS_PATH 配置
        if self.config.kernel_version != "6.6":
            with open(config_file, "a") as f:
                f.write("CONFIG_KSU_SUSFS_SUS_PATH=y\n")
        else:
            with open(config_file, "a") as f:
                f.write("CONFIG_KSU_SUSFS_SUS_PATH=n\n")
        
        # ZRAM 配置
        if self.config.use_zram:
            self._configure_zram()
            self._configure_bazel()
        
        # 默认 BBR
        if self.config.set_default_bbr:
            with open(config_file, "a") as f:
                f.write("CONFIG_DEFAULT_BBR=y\n")
        
        # 移除 check_defconfig
        build_config = self.work_dir / "common/build.config.gki"
        if build_config.exists():
            with open(build_config, "r") as f:
                content = f.read()
            content = content.replace("check_defconfig", "")
            with open(build_config, "w") as f:
                f.write(content)

        logger.info("=== 内核配置完成 ===")
    
    def _configure_zram(self) -> None:
        """配置 ZRAM"""
        config_file = self.work_dir / "common/arch/arm64/configs/gki_defconfig"
        kv = self.config.kernel_version

        # 读取配置文件
        with open(config_file, "r") as f:
            content = f.read()

        if kv == "5.10":
            with open(config_file, "a") as f:
                f.write(self.ZRAM_CONFIG_5_10)
        elif kv == "6.6":
            content = content.replace("CONFIG_ZRAM=m", "CONFIG_ZRAM=y")
            with open(config_file, "w") as f:
                f.write(content)
            with open(config_file, "a") as f:
                f.write("CONFIG_ZSMALLOC=y\n")
        else:  # 5.15, 6.1
            content = content.replace("CONFIG_ZRAM=m", "CONFIG_ZRAM=y")
            with open(config_file, "w") as f:
                f.write(content)
            with open(config_file, "a") as f:
                f.write("CONFIG_ZSMALLOC=y\n")

        with open(config_file, "a") as f:
            f.write(self.ZRAM_CONFIG_COMMON)
    
    def _configure_bazel(self) -> None:
        """配置 Bazel 构建"""
        modules_bzl = self.work_dir / "common/modules.bzl"

        if modules_bzl.exists():
            logger.info("修改 modules.bzl 移除 zram 和 zsmalloc")
            with open(modules_bzl, "r") as f:
                content = f.read()

            # 移除 zram 和 zsmalloc 模块
            modified = False
            if '"drivers/block/zram/zram.ko",' in content:
                content = content.replace('"drivers/block/zram/zram.ko",\n', '')
                content = content.replace('"drivers/block/zram/zram.ko",', '')
                modified = True
            if '"mm/zsmalloc.ko",' in content:
                content = content.replace('"mm/zsmalloc.ko",\n', '')
                content = content.replace('"mm/zsmalloc.ko",', '')
                modified = True

            if modified:
                with open(modules_bzl, "w") as f:
                    f.write(content)
                logger.info("已移除 zram 和 zsmalloc 模块引用")

        config_file = self.work_dir / "common/arch/arm64/configs/gki_defconfig"
        with open(config_file, "a") as f:
            f.write("CONFIG_MODULE_SIG_FORCE=n\n")

        logger.info("Bazel 配置完成")
    
    def configure_kernel_name(self) -> None:
        """配置内核名称"""
        logger.info("=== 配置内核名称 ===")

        # 先进入配置目录
        self._chdir(self.work_dir)

        # 长度安全保护：UTS_RELEASE 硬限制为 64 字符(含\0)，预留 15 字符给 git hash
        MAX_CUSTOM_LEN = 48
        safe_custom_version = ""
        if self.config.custom_version:
            safe_custom_version = self.config.custom_version.rstrip('-')[:MAX_CUSTOM_LEN]
            if len(self.config.custom_version) > MAX_CUSTOM_LEN:
                logger.warning(
                    f"自定义版本过长({len(self.config.custom_version)}字符)，"
                    f"已截断至{MAX_CUSTOM_LEN}字符以防止UTS_RELEASE溢出"
                )

        # 配置版本号
        setlocalversion = self.work_dir / "common/scripts/setlocalversion"
        if setlocalversion.exists():
            with open(setlocalversion, "r") as f:
                content = f.read()

            if safe_custom_version:
                # 替换最后一行的 echo "$res" 为自定义版本
                lines = content.split('\n')
                modified = False
                for i, line in enumerate(lines):
                    if 'echo "$res"' in line and not line.strip().startswith('#'):
                        lines[i] = f'\techo "{safe_custom_version}$res"'
                        modified = True
                        logger.info(f"已设置自定义版本: {safe_custom_version} + githash")
                        break

                if not modified:
                    logger.warning("未在 setlocalversion 中找到 'echo \"$res\"' 行，自定义版本未生效")
                else:
                    with open(setlocalversion, "w") as f:
                        f.write('\n'.join(lines))
            else:
                logger.info("未设置自定义版本，使用默认版本号 + githash")

            if (self.work_dir / "build/build.sh").exists():
                if "-dirty" in content:
                    content = content.replace("-dirty", "")
                    with open(setlocalversion, "w") as f:
                        f.write(content)

        # 配置构建时间
        import datetime
        current_time = datetime.datetime.utcnow().strftime("%a %b %d %H:%M:%S UTC %Y")
        logger.info(f"构建时间: {current_time}")

        mkcompile_h = self.work_dir / "common/scripts/mkcompile_h"
        if mkcompile_h.exists():
            with open(mkcompile_h, "r") as f:
                content = f.read()
            content = content.replace(
                'UTS_VERSION="$(echo $UTS_VERSION $CONFIG_FLAGS $TIMESTAMP | cut -b -$UTS_LEN)"',
                f'UTS_VERSION="#1 SMP PREEMPT {current_time}"'
            )
            with open(mkcompile_h, "w") as f:
                f.write(content)

        # 6.1 和 6.6 内核额外配置
        if self.config.kernel_version in ["6.1", "6.6"]:
            init_makefile = self.work_dir / "common/init/Makefile"
            if init_makefile.exists():
                with open(init_makefile, "r") as f:
                    content = f.read()
                content = content.replace(
                    '$(preempt-flag-y) "$(build-timestamp)"',
                    f'$(preempt-flag-y) "{current_time}"'
                )
                with open(init_makefile, "w") as f:
                    f.write(content)

        # Bazel 配置
        if not (self.work_dir / "build/build.sh").exists():
            # Bazel 构建
            bazel_build = self.work_dir / "common/BUILD.bazel"
            if bazel_build.exists():
                with open(bazel_build, "r") as f:
                    content = f.read()
                # 删除 protected_exports_list 行
                lines = [l for l in content.split('\n')
                        if '"protected_exports_list"' not in l or 'android/abi_gki_protected_exports_aarch64' not in l]
                with open(bazel_build, "w") as f:
                    f.write('\n'.join(lines))

            # 删除 abi 文件
            abi_path = self.work_dir / "common/android/abi_gki_protected_exports_aarch64"
            if abi_path.exists():
                import shutil
                import os
                try:
                    if abi_path.is_dir():
                        shutil.rmtree(abi_path)
                    else:
                        abi_path.unlink()
                except Exception as e:
                    logger.warning(f"删除 abi 路径时出错 (忽略): {e}")

            # 修改 stamp.bzl
            stamp_bzl = self.work_dir / "build/kernel/kleaf/impl/stamp.bzl"
            if stamp_bzl.exists():
                with open(stamp_bzl, "r") as f:
                    content = f.read()
                content = content.replace("-maybe-dirty", "")
                with open(stamp_bzl, "w") as f:
                    f.write(content)

            # 设置 LOCALVERSION
            if self.config.custom_version:
                config_file = self.work_dir / "common/arch/arm64/configs/gki_defconfig"
                with open(config_file, "r") as f:
                    content = f.read()
                import re
                content = re.sub(
                    r'^CONFIG_LOCALVERSION=".*"$',
                    f'CONFIG_LOCALVERSION="{self.config.custom_version}"',
                    content,
                    flags=re.MULTILINE
                )
                with open(config_file, "w") as f:
                    f.write(content)

        # 移除 -dirty
        if (self.work_dir / "build/build.sh").exists():
            with open(setlocalversion, "r") as f:
                content = f.read()
            if "-dirty" in content:
                content = content.replace("-dirty", "")
                with open(setlocalversion, "w") as f:
                    f.write(content)

        logger.info("=== 内核名称配置完成 ===")
    
    def build_kernel(self) -> bool:
        """编译内核"""
        logger.info("=== 开始编译内核 ===")
        
        self._chdir(self.work_dir)
        
        # 修改 build.config
        build_config_aarch64 = self.work_dir / "common/build.config.gki.aarch64"
        if build_config_aarch64.exists():
            with open(build_config_aarch64, "r") as f:
                content = f.read()

            content = content.replace("BUILD_SYSTEM_DLKM=1", "BUILD_SYSTEM_DLKM=0")

            # 删除包含 MODULES_ORDER 的行
            lines = [l for l in content.split('\n') if 'MODULES_ORDER=android/gki_aarch64_modules' not in l]
            # 删除包含 KMI_SYMBOL_LIST_STRICT_MODE 的行
            lines = [l for l in lines if 'KMI_SYMBOL_LIST_STRICT_MODE' not in l]

            with open(build_config_aarch64, "w") as f:
                f.write('\n'.join(lines))
        
        # 执行构建
        try:
            if (self.work_dir / "build/build.sh").exists():
                # 旧版构建方式
                logger.info("使用旧版构建方式 (build.sh)...")
                result = self._run_cmd(
                    "LTO=thin BUILD_CONFIG=common/build.config.gki.aarch64 "
                    "build/build.sh CC=\"/usr/bin/ccache clang\"",
                    check=False
                )
            else:
                # Bazel 构建方式
                logger.info("使用 Bazel 构建方式...")
                result = self._run_cmd(
                    "tools/bazel build --disk_cache=/home/runner/.cache/bazel "
                    "--config=fast --lto=thin //common:kernel_aarch64_dist",
                    check=False
                )
            
            if result.returncode == 0:
                logger.info("=== 内核编译成功 ===")
                return True
            else:
                logger.error(f"内核编译失败: {result.stderr if result.stderr else 'Unknown error'}")
                return False
                
        except Exception as e:
            logger.error(f"编译过程出错: {e}")
            return False
    
    def patch_kpm_image(self) -> None:
        """修补 Image 文件 (KPM)"""
        if not self.config.use_kpm:
            return
        
        if self.config.kernel_version == "6.6":
            logger.info("6.6 内核跳过 KPM Image 修补")
            return
        
        logger.info("=== 修补 Image 文件 (KPM) ===")
        
        self._chdir(self.work_dir)
        
        # 根据 Android 版本确定路径
        if self.config.android_version in ["android12", "android13"]:
            image_dir = self.work_dir / f"out/{self.config.android_version}-{self.config.kernel_version}/dist"
        else:  # android14, android15
            image_dir = self.work_dir / "bazel-bin/common/kernel_aarch64"
        
        if not image_dir.exists():
            logger.warning(f"Image 目录不存在: {image_dir}")
            return
        
        self._chdir(image_dir)
        
        # 下载并应用补丁
        logger.info("下载 KPM patch 脚本...")
        self._run_cmd(f"curl -LSs {KPM_PATCH_URL} -o patch", check=False)
        self._run_cmd("chmod 777 patch", check=False)
        self._run_cmd("./patch", check=False)
        
        # 重命名 Image
        if (image_dir / "oImage").exists():
            self._run_cmd("mv oImage Image", check=False)
        
        logger.info("=== Image 修补完成 ===")
    
    def prepare_boot_images(self) -> list:
        """准备启动镜像"""
        logger.info("=== 准备启动镜像 ===")
        
        self._chdir(self.work_dir)
        
        bootimgs_dir = self.work_dir / "bootimgs"
        bootimgs_dir.mkdir(exist_ok=True)
        
        artifacts = []
        
        # 确定 Image 路径
        if self.config.android_version in ["android12", "android13"]:
            image_source = self.work_dir / f"out/{self.config.android_version}-{self.config.kernel_version}/dist"
        else:
            image_source = self.work_dir / "bazel-bin/common/kernel_aarch64"
        
        # 复制 Image 文件
        for image_name in ["Image", "Image.lz4"]:
            src = image_source / image_name
            if src.exists():
                self._run_cmd(f"cp {src} {bootimgs_dir}/", check=False)
                self._run_cmd(f"cp {src} {self.work_dir}/", check=False)
        
        # 创建 gzip 压缩版本
        if (self.work_dir / "Image").exists():
            self._run_cmd("gzip -n -k -f -9 Image", check=False)
        
        # Android 12 特殊处理
        if self.config.android_version == "android12":
            self._prepare_android12_boot_images(bootimgs_dir, artifacts)
        else:
            self._prepare_boot_images_generic(bootimgs_dir, artifacts)
        
        logger.info("=== 启动镜像准备完成 ===")
        return artifacts
    
    def _prepare_android12_boot_images(self, bootimgs_dir: Path, artifacts: list) -> None:
        """准备 Android 12 启动镜像"""
        self._chdir(bootimgs_dir)
        
        # 下载 GKI 内核
        gki_url = (
            f"https://dl.google.com/android/gki/gki-certified-boot-android12-5.10-"
            f"{self.config.os_patch_level}_{self.config.revision}.zip"
        )
        fallback_url = "https://dl.google.com/android/gki/gki-certified-boot-android12-5.10-2023-01_r1.zip"
        
        # 检查 URL 是否可用
        result = subprocess.run(
            f"curl -sL -w '%{{http_code}}' {gki_url} -o /dev/null",
            shell=True, capture_output=True, text=True
        )
        
        if "200" in result.stdout:
            logger.info("从主 URL 下载 GKI 内核...")
            self._run_cmd(f"curl -Lo gki-kernel.zip {gki_url}", check=False)
        else:
            logger.info("使用备用 URL 下载 GKI 内核...")
            self._run_cmd(f"curl -Lo gki-kernel.zip {fallback_url}", check=False)
        
        # 解压
        self._run_cmd("unzip -o gki-kernel.zip", check=False)
        self._run_cmd("rm gki-kernel.zip", check=False)
        
        # 解包 boot.img
        boot_img_path = bootimgs_dir / "boot-5.10.img"
        if boot_img_path.exists():
            self._run_cmd(f"$UNPACK_BOOTIMG --boot_img={boot_img_path}", check=False)
        
        # 创建各种格式的 boot.img
        self._create_boot_image_variants(bootimgs_dir, artifacts, has_ramdisk=True)
    
    def _prepare_boot_images_generic(self, bootimgs_dir: Path, artifacts: list) -> None:
        """准备通用启动镜像"""
        self._chdir(bootimgs_dir)
        self._create_boot_image_variants(bootimgs_dir, artifacts, has_ramdisk=False)
    
    def _create_boot_image_variants(self, bootimgs_dir: Path, artifacts: list, has_ramdisk: bool = False) -> None:
        """创建各种格式的 boot.img"""
        self._chdir(bootimgs_dir)
        
        # 创建 Image.gz
        if (bootimgs_dir / "Image").exists():
            self._run_cmd("gzip -n -k -f -9 Image", check=False)
        
        boot_img_vars = [
            ("Image", "boot.img", []),
            ("Image.gz", "boot-gz.img", []),
            ("Image.lz4", "boot-lz4.img", []),
        ]
        
        for kernel_file, output_file, extra_args in boot_img_vars:
            kernel_path = bootimgs_dir / kernel_file
            if not kernel_path.exists():
                continue
            
            # 创建 boot.img
            cmd = f"$MKBOOTIMG --header_version 4 --kernel {kernel_file} --output {output_file}"
            
            if has_ramdisk:
                cmd += " --ramdisk out/ramdisk --os_version 12.0.0"
                cmd += f" --os_patch_level {self.config.os_patch_level}"
            
            self._run_cmd(cmd, check=False)
            
            # 添加签名
            avb_cmd = (
                f"$AVBTOOL add_hash_footer --partition_name boot "
                f"--partition_size $((64 * 1024 * 1024)) "
                f"--image {output_file} "
                f"--algorithm SHA256_RSA2048 --key $BOOT_SIGN_KEY_PATH"
            )
            self._run_cmd(avb_cmd, check=False)
            
            # 复制到输出目录
            dest = self.work_dir / f"{self.config.android_version}-{self.config.kernel_version}.{self.config.sub_level}-{self.config.os_patch_level}-{output_file}"
            self._run_cmd(f"cp {output_file} {dest}", check=False)
            artifacts.append(str(dest))
    
    def create_anykernel_zips(self) -> list:
        """创建 AnyKernel3 ZIP 文件"""
        logger.info("=== 创建 AnyKernel3 ZIP 文件 ===")
        
        self._chdir(self.work_dir)
        
        artifacts = []
        ak3_dir = self.anykernel_dir
        
        for format_name, suffix in [("", ""), ("-lz4", ".lz4"), ("-gz", ".gz")]:
            image_file = f"Image{suffix}"
            image_path = self.work_dir / image_file
            
            if not image_path.exists():
                continue
            
            zip_name = (
                f"{self.config.android_version}-{self.config.kernel_version}."
                f"{self.config.sub_level}-{self.config.os_patch_level}-"
                f"AnyKernel3{format_name}.zip"
            )
            
            # 复制 Image 到 AnyKernel3 目录
            self._run_cmd(f"cp {image_path} {ak3_dir}/", check=False)
            
            # 创建 ZIP
            self._chdir(ak3_dir)
            self._run_cmd(f"zip -r ../{zip_name} ./*", check=False)
            
            # 清理
            self._run_cmd(f"rm {ak3_dir}/{image_file}", check=False)
            
            artifacts.append(str(self.work_dir / zip_name))
            self._chdir(self.work_dir)
        
        logger.info("=== AnyKernel3 ZIP 创建完成 ===")
        return artifacts
    
    def build(self) -> BuildResult:
        """执行完整构建流程"""
        import time
        start_time = time.time()
        
        logger.info("=" * 50)
        logger.info("开始 GKI Kernel 构建")
        logger.info(f"配置: {self.config.config_name}")
        logger.info(f"KSU Commit: {self.config.kernelsu_commit or 'latest'}")
        logger.info("=" * 50)
        
        try:
            # 1. 准备工具和仓库
            self.clone_repositories()
            self.clone_toolchain()
            self.setup_repo_tool()
            
            # 2. 同步内核源码
            self.init_and_sync_kernel()
            
            # 3. 应用补丁
            self.add_kernel_supatch()
            self.add_kernelsu()
            self.add_bbg()
            self.apply_susfs_patches()
            self.apply_sukisu_patches()
            self.apply_zram_patches()
            self.apply_task_mmu_fixes()
            
            # 4. 配置内核
            self.configure_kernel()
            self.configure_kernel_name()
            
            # 5. 编译
            build_success = self.build_kernel()
            if not build_success:
                return BuildResult(
                    success=False,
                    config=self.config,
                    message="内核编译失败",
                    build_time=time.time() - start_time
                )
            
            # 6. 修补 Image
            self.patch_kpm_image()
            
            # 7. 准备镜像
            artifacts = []
            artifacts.extend(self.prepare_boot_images())
            artifacts.extend(self.create_anykernel_zips())
            
            build_time = time.time() - start_time
            
            logger.info("=" * 50)
            logger.info(f"构建成功! 耗时: {build_time:.2f} 秒")
            logger.info(f"生成 {len(artifacts)} 个产物文件")
            logger.info("=" * 50)
            
            return BuildResult(
                success=True,
                config=self.config,
                message="构建成功",
                artifacts=artifacts,
                build_time=build_time
            )
            
        except Exception as e:
            logger.error(f"构建过程出错: {e}")
            return BuildResult(
                success=False,
                config=self.config,
                message=str(e),
                build_time=time.time() - start_time
            )
