#!/usr/bin/env bash
# GKI Kernel 交互式本地构建脚本（WSL / Linux）
#
# 用法:
#   bash .github/workflows/scripts/build-wsl.sh
#
# 说明:
#   - 所有选项最终都转成 build.py 的命令行参数，不重复实现构建逻辑
#   - 工作目录默认 $HOME/gki-build（WSL 的 ext4 分区比 /mnt/c 快很多）
#   - 不设置 BOOT_SIGN_KEY_PATH 时，boot.img 的 AVB 签名会被跳过

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." 2>/dev/null && pwd || echo "$SCRIPT_DIR")"
if git -C "$SCRIPT_DIR" rev-parse --show-toplevel >/dev/null 2>&1; then
    REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
fi
BUILD_PY="$SCRIPT_DIR/build.py"
PYTHON="${PYTHON:-python3}"

SELECTED=""
SELECTED_INDEX=-1
BUILD_ARGS=()

# ---------------------------------------------------------------------------
# 基础交互
# ---------------------------------------------------------------------------

die() {
    echo "错误: $*" >&2
    exit 1
}

pause() {
    echo
    read -rp "按回车返回主菜单..." _ || true
}

menu() {
    local title="$1"
    shift
    local -a items=("$@")
    local i choice

    if [ "${#items[@]}" -eq 0 ]; then
        echo "没有可选项。"
        return 1
    fi

    echo
    echo "== ${title} =="
    for i in "${!items[@]}"; do
        printf '  %2d) %s\n' "$((i + 1))" "${items[$i]}"
    done

    while true; do
        read -rp "请选择 [1-${#items[@]}]: " choice || return 1
        if [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#items[@]} )); then
            SELECTED_INDEX=$((choice - 1))
            SELECTED="${items[$SELECTED_INDEX]}"
            return 0
        fi
        echo "无效输入，请重试。"
    done
}

read_value() {
    local prompt="$1" default="${2:-}" value
    if [ -n "$default" ]; then
        read -rp "${prompt} [${default}]: " value || return 1
        echo "${value:-$default}"
    else
        read -rp "${prompt}: " value || return 1
        echo "$value"
    fi
}

ask_yn() {
    local prompt="$1" default="${2:-n}" suffix answer
    if [ "$default" = "y" ]; then
        suffix="[Y/n]"
    else
        suffix="[y/N]"
    fi
    while true; do
        read -rp "${prompt} ${suffix} " answer || return 1
        answer="${answer:-$default}"
        case "${answer,,}" in
            y|yes) return 0 ;;
            n|no) return 1 ;;
            *) echo "请输入 y 或 n。" ;;
        esac
    done
}

# ---------------------------------------------------------------------------
# 从 Python 配置读取选项，避免在 shell 里重复维护版本表
# ---------------------------------------------------------------------------

list_combos() {
    "$PYTHON" - "$SCRIPT_DIR" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from config import ANDROID_KERNEL_MAP
for android, kernels in ANDROID_KERNEL_MAP.items():
    for kernel in kernels:
        print(f"{android.value}|{kernel.value}")
PY
}

list_matrix_rows() {
    local combo="$1"
    "$PYTHON" - "$SCRIPT_DIR" "$combo" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from matrix import load_matrix
key = sys.argv[2]
for cfg in load_matrix().get(key, []):
    page = cfg.get("page_size", "4k")
    revision = cfg.get("revision", "")
    print(f"{cfg['sub_level']}|{cfg['os_patch_level']}|{page}|{revision}")
PY
}

# ---------------------------------------------------------------------------
# 选项收集
# ---------------------------------------------------------------------------

collect_ksu_options() {
    KSU_VERSION="Stable(标准)"
    KSU_BRANCH=""
    KSU_COMMIT=""
    SUSFS_COMMIT=""
    CUSTOM_VERSION=""

    menu "SukiSU 版本" \
        "Stable(标准) -> builtin" \
        "Dev(开发) -> builtin-new" \
        "Custom(自定义) -> 手动输入分支" || return 1
    KSU_VERSION="${SELECTED%% ->*}"

    if [ "$KSU_VERSION" = "Custom(自定义)" ]; then
        KSU_BRANCH="$(read_value "SukiSU 分支 / tag / commit" "builtin-new")"
        if ! [[ "$KSU_BRANCH" =~ ^[A-Za-z0-9][A-Za-z0-9._/~^-]*$ ]]; then
            echo "分支名包含不安全字符，已取消。"
            return 1
        fi
    fi

    KSU_COMMIT="$(read_value "指定 SukiSU commit（留空忽略）" "")"
    SUSFS_COMMIT="$(read_value "指定 SUSFS commit / ref（留空忽略）" "")"
    CUSTOM_VERSION="$(read_value "自定义内核版本名（留空忽略）" "")"
}

collect_flags() {
    ZRAM="n"
    KPM="y"
    BBG="n"
    OP8E="n"
    BBR="n"

    if ask_yn "启用 ZRAM (LZ4KD)?" n; then ZRAM="y"; fi
    if ! ask_yn "启用 KPM 修补?" y; then KPM="n"; fi
    if ask_yn "启用 Baseband-guard (BBG)?" n; then BBG="y"; fi
    if ask_yn "启用 OnePlus 8E 支持?" n; then OP8E="y"; fi
    if ask_yn "设置 BBR 为默认拥塞算法?" n; then BBR="y"; fi
}

supports_16k() {
    case "$1" in
        6.1|6.6|6.12|6.18) return 0 ;;
        *) return 1 ;;
    esac
}

collect_page_size() {
    local kernel="$1" default="${2:-4k}" answer
    if ! supports_16k "$kernel"; then
        PAGE_SIZE="4k"
        echo "Kernel ${kernel} 不支持 16k page size，使用 4k。"
        return 0
    fi

    while true; do
        answer="$(read_value "Page Size (4k/16k)" "$default")" || return 1
        case "$answer" in
            4k|16k) PAGE_SIZE="$answer"; return 0 ;;
            *) echo "请输入 4k 或 16k。" ;;
        esac
    done
}

# ---------------------------------------------------------------------------
# 构建参数
# ---------------------------------------------------------------------------

build_common_args() {
    BUILD_ARGS=(
        --ksu-version "$KSU_VERSION"
        --workspace "$WORKSPACE"
    )
    if [ -n "$KSU_BRANCH" ]; then BUILD_ARGS+=(--ksu-branch "$KSU_BRANCH"); fi
    if [ -n "$KSU_COMMIT" ]; then BUILD_ARGS+=(--ksu-commit "$KSU_COMMIT"); fi
    if [ -n "$SUSFS_COMMIT" ]; then BUILD_ARGS+=(--susfs-commit "$SUSFS_COMMIT"); fi
    if [ -n "$CUSTOM_VERSION" ]; then BUILD_ARGS+=(--custom-version "$CUSTOM_VERSION"); fi
    if [ "$ZRAM" = "y" ]; then BUILD_ARGS+=(--zram); fi
    if [ "$KPM" = "n" ]; then BUILD_ARGS+=(--no-kpm); fi
    if [ "$BBG" = "y" ]; then BUILD_ARGS+=(--bbg); fi
    if [ "$OP8E" = "y" ]; then BUILD_ARGS+=(--op8e); fi
    if [ "$BBR" = "y" ]; then BUILD_ARGS+=(--bbr); fi
}

run_build() {
    local dry_run="${1:-n}"
    local -a args=("${BUILD_ARGS[@]}")
    local rc

    if [ "$dry_run" = "y" ]; then
        args+=(--dry-run)
    fi

    echo
    echo "执行:"
    printf '  %q' "$PYTHON"
    printf ' %q' "$BUILD_PY" "${args[@]}"
    echo
    echo
    "$PYTHON" "$BUILD_PY" "${args[@]}"
    rc=$?

    echo
    if [ "$rc" -eq 0 ]; then
        echo "构建完成 (exit 0)。产物位于: $WORKSPACE"
    elif [ "$rc" -eq 2 ]; then
        echo "部分配置成功 (exit 2)，请查看上面的失败列表。"
    else
        echo "构建失败 (exit $rc)。"
    fi

    find "$WORKSPACE" -maxdepth 3 -type f \( -name '*.zip' -o -name '*.img' \) -print 2>/dev/null \
        | tail -20 | sed 's/^/  /'
    pause
}

# ---------------------------------------------------------------------------
# 目标选择
# ---------------------------------------------------------------------------

pick_combo() {
    local -a combos=() labels=()
    local item android kernel

    mapfile -t combos < <(list_combos)
    if [ "${#combos[@]}" -eq 0 ]; then
        echo "无法读取 Android/Kernel 组合。"
        return 1
    fi
    for item in "${combos[@]}"; do
        IFS='|' read -r android kernel <<< "$item"
        labels+=("${android} / ${kernel}")
    done

    menu "选择 Android / Kernel" "${labels[@]}" || return 1
    IFS='|' read -r ANDROID KERNEL <<< "${combos[$SELECTED_INDEX]}"
}

do_single_build() {
    local dry_run="$1"
    local combo sub_level os_patch page_size revision
    local -a rows=() labels=()

    pick_combo || return 0
    combo="${ANDROID}-${KERNEL}"
    collect_ksu_options || return 0
    collect_flags

    mapfile -t rows < <(list_matrix_rows "$combo")
    for row in "${rows[@]}"; do
        IFS='|' read -r sub_level os_patch page_size revision <<< "$row"
        labels+=("${sub_level}  ${os_patch}  [${page_size}]${revision:+  rev=${revision}}")
    done
    labels+=("手动输入 sublevel / os_patch")

    menu "选择 Sublevel / OS Patch" "${labels[@]}" || return 0

    if [ "$SELECTED_INDEX" -eq "${#rows[@]}" ]; then
        sub_level="$(read_value "Sublevel（数字或 X）" "")"
        os_patch="$(read_value "OS Patch Level（如 2026-09，或 lts）" "")"
        revision=""
        if [ "$ANDROID" = "android12" ]; then
            revision="$(read_value "Android 12 Revision（如 r1，留空忽略）" "")"
        fi
        collect_page_size "$KERNEL" "4k"
    else
        IFS='|' read -r sub_level os_patch page_size revision <<< "${rows[$SELECTED_INDEX]}"
        collect_page_size "$KERNEL" "$page_size"
    fi

    if [ -z "$sub_level" ] || [ -z "$os_patch" ]; then
        echo "sublevel / os_patch 不能为空。"
        return 0
    fi
    if [ "$PAGE_SIZE" = "16k" ] && [ "$KPM" = "y" ]; then
        echo "提示: 16k 构建会跳过 KPM 修补。"
    fi

    build_common_args
    BUILD_ARGS+=(
        --android "$ANDROID"
        --kernel "$KERNEL"
        --sub-level "$sub_level"
        --os-patch "$os_patch"
        --page-size "$PAGE_SIZE"
    )
    if [ -n "$revision" ]; then BUILD_ARGS+=(--revision "$revision"); fi

    run_build "$dry_run"
}

do_matrix_build() {
    local combo item android kernel
    local -a combos=() labels=()

    mapfile -t combos < <(list_combos)
    for item in "${combos[@]}"; do
        IFS='|' read -r android kernel <<< "$item"
        labels+=("${android} / ${kernel}")
    done
    menu "选择要构建的内核线" "${labels[@]}" || return 0
    combo="${combos[$SELECTED_INDEX]}"

    collect_ksu_options || return 0
    collect_flags
    build_common_args
    BUILD_ARGS+=(--matrix "$combo")

    echo
    echo "将构建 ${combo} 的全部 sublevel（矩阵中定义了 page_size 的会按 16k 构建）。"
    if ask_yn "继续?" y; then
        run_build n
    fi
}

do_all_build() {
    collect_ksu_options || return 0
    collect_flags
    build_common_args
    BUILD_ARGS+=(--all)

    echo
    echo "将构建 matrix.json 里的全部配置，耗时很长。"
    if ask_yn "确定构建全部矩阵?" n; then
        run_build n
    fi
}

# ---------------------------------------------------------------------------
# 依赖与初始化
# ---------------------------------------------------------------------------

missing_commands() {
    local -a need=(python3 git curl patch zip unzip tar gzip make ccache)
    local cmd missing=()
    for cmd in "${need[@]}"; do
        command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
    done
    echo "${missing[*]:-}"
}

install_dependencies() {
    if ! command -v sudo >/dev/null 2>&1; then
        echo "没有 sudo，请手动安装缺少的依赖。"
        return 1
    fi
    echo "即将执行 apt-get 安装常用构建依赖..."
    sudo apt-get update
    sudo apt-get install -y \
        git curl python3 zip unzip patch ccache build-essential openssl \
        bc bison flex libssl-dev libelf-dev libncurses-dev rsync cpio zstd lz4
}

init_environment() {
    local missing

    if [ ! -f "$BUILD_PY" ]; then
        die "找不到 $BUILD_PY"
    fi

    if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
        die "需要 Python 3.9 或更新版本（当前: $("$PYTHON" --version 2>&1)）"
    fi

    missing="$(missing_commands)"
    if [ -n "$missing" ]; then
        echo "缺少命令: $missing"
        if ask_yn "现在用 apt-get 安装依赖?" n; then
            install_dependencies || true
        fi
    fi

    WORKSPACE="${GKI_WORKSPACE:-$HOME/gki-build}"
    WORKSPACE="$(read_value "构建工作目录" "$WORKSPACE")"
    mkdir -p "$WORKSPACE" || die "无法创建工作目录: $WORKSPACE"
    export GKI_WORKSPACE="$WORKSPACE"

    case "$WORKSPACE" in
        /mnt/*)
            echo "提示: 工作目录在 Windows 挂载盘上，WSL 下编译会明显变慢，建议放到 \$HOME。"
            ;;
    esac

    if [ -n "${CCACHE_DIR:-}" ]; then
        export CCACHE_DIR
    else
        export CCACHE_DIR="$HOME/.ccache"
    fi
    mkdir -p "$CCACHE_DIR" 2>/dev/null || true

    local key="${BOOT_SIGN_KEY_PATH:-}"
    key="$(read_value "AVB 签名私钥路径（留空则跳过 boot.img 签名）" "$key")"
    if [ -n "$key" ]; then
        if [ -f "$key" ]; then
            BOOT_SIGN_KEY_PATH="$(cd "$(dirname "$key")" && pwd)/$(basename "$key")"
            export BOOT_SIGN_KEY_PATH
            echo "AVB key: $BOOT_SIGN_KEY_PATH"
        else
            echo "警告: 找不到 $key，本次跳过 AVB 签名。"
            unset BOOT_SIGN_KEY_PATH
        fi
    else
        unset BOOT_SIGN_KEY_PATH
    fi
}

show_matrix() {
    "$PYTHON" "$BUILD_PY" --list-configs
    echo
    "$PYTHON" "$BUILD_PY" --list-matrix
    pause
}

# ---------------------------------------------------------------------------
# 主菜单
# ---------------------------------------------------------------------------

main() {
    trap 'echo; echo "已取消。"; exit 130' INT

    echo "=============================================="
    echo " GKI Kernel 本地交互构建"
    echo " 仓库: $REPO_ROOT"
    echo "=============================================="

    init_environment

    while true; do
        if ! menu "主菜单" \
            "单配置构建" \
            "单配置 dry-run（只校验）" \
            "按内核线构建全部 sublevel" \
            "构建全部矩阵" \
            "查看构建矩阵" \
            "检查/安装依赖" \
            "退出"; then
            break
        fi

        case "$SELECTED" in
            "单配置构建")            do_single_build n ;;
            "单配置 dry-run（只校验）") do_single_build y ;;
            "按内核线构建全部 sublevel") do_matrix_build ;;
            "构建全部矩阵")          do_all_build ;;
            "查看构建矩阵")          show_matrix ;;
            "检查/安装依赖")
                echo "缺少命令: $(missing_commands)"
                if ask_yn "是否安装构建依赖?" n; then install_dependencies || true; fi
                pause
                ;;
            "退出") break ;;
        esac
    done
    echo "再见。"
}

main "$@"
