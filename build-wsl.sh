#!/usr/bin/env bash
# GKI Kernel 交互式本地构建脚本（WSL / Linux）
#
# 用法:
#   bash build-wsl.sh                 交互式菜单
#   bash build-wsl.sh --build         直接用上次预设构建，不显示菜单
#   bash build-wsl.sh --retry         上次构建失败/中断时直接重试
#   bash build-wsl.sh --no-auto       启动时不自动提示继续上次构建
#
# 说明:
#   - 所有选项最终都转成 .github/workflows/scripts/build.py 的命令行参数
#   - 使用 auto 构建：有 build/build.sh 时走旧版构建，否则走 Kleaf/Bazel
#   - 配置永久保存在 /root/kernel/config（build.conf / paths.conf / avb.pem）
#   - GKI_CONFIG_DIR 可改配置目录；非 root 且 /root 不可写时退回 $HOME/kernel/config
#   - 工作目录、ccache、AVB 路径会记住并直接读取，不再重复询问
#   - 缺少依赖时自动 apt-get 安装；设置 GKI_NO_APT=1 可跳过
#   - 复用工作目录时，build.py 会在同步前重置 common 内核树，避免补丁残留
#   - AVB 首次自动生成自签密钥并复用（GKI_NO_AVB=1 可跳过）

set -uo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || echo "${BASH_SOURCE[0]}")"
REPO_ROOT="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"
SCRIPTS_DIR="$REPO_ROOT/.github/workflows/scripts"
BUILD_PY="$SCRIPTS_DIR/build.py"
PYTHON="${PYTHON:-python3}"

# 永久配置目录（首次缺少时自动创建；非 root 且 /root 不可写时退回 $HOME）
CONFIG_DIR="${GKI_CONFIG_DIR:-/root/kernel/config}"
PRESET_FILE="${GKI_PRESET_FILE:-}"
PATHS_FILE="${GKI_PATHS_FILE:-}"
AVB_KEY_FILE="${GKI_AVB_KEY_FILE:-}"

SELECTED=""
SELECTED_INDEX=-1
MENU_DEFAULT_INDEX=-1
BUILD_ARGS=()
AUTO_ACTION=""

while [ $# -gt 0 ]; do
    case "$1" in
        --build|--last) AUTO_ACTION="build" ;;
        --retry)        AUTO_ACTION="retry" ;;
        --no-auto)      export GKI_AUTO_RESUME=0 ;;
        -h|--help)
            sed -n '2,/^$/p' "$SCRIPT_PATH"
            exit 0
            ;;
        *)
            echo "未知参数: $1（使用 --help 查看用法）" >&2
            exit 2
            ;;
    esac
    shift
done

# 上次预设 / 交互选项的默认值
PRESET_MODE=""
LAST_RESULT=""
ANDROID=""
KERNEL=""
SUB_LEVEL=""
OS_PATCH=""
PAGE_SIZE="4k"
REVISION=""
KSU_VERSION="Stable(标准)"
KSU_BRANCH=""
KSU_COMMIT=""
SUSFS_COMMIT=""
CUSTOM_VERSION=""
ZRAM="n"
KPM="y"
SUSFS="y"
BBG="n"
OP8E="n"
BBR="n"
WORKSPACE=""
BOOT_SIGN_KEY_PATH="${BOOT_SIGN_KEY_PATH:-}"
CCACHE_DIR="${CCACHE_DIR:-}"

# ---------------------------------------------------------------------------
# 基础交互
# ---------------------------------------------------------------------------

die() {
    echo "错误: $*" >&2
    exit 1
}

pause() {
    [ "${GKI_NO_PAUSE:-0}" = "1" ] && return 0
    echo
    read -rp "按回车返回主菜单..." _ || true
}

menu() {
    local title="$1"
    shift
    local -a items=("$@")
    local i choice default_index="${MENU_DEFAULT_INDEX:--1}"
    MENU_DEFAULT_INDEX=-1

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
        local hint=""
        if (( default_index >= 0 && default_index < ${#items[@]} )); then
            hint="（回车=第 $((default_index + 1)) 项）"
        fi
        read -rp "请选择 [1-${#items[@]}]${hint}: " choice || return 1
        if [ -z "$choice" ] && (( default_index >= 0 )); then
            choice=$((default_index + 1))
        fi
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

expand_tilde() {
    case "$1" in
        "~")   echo "$HOME" ;;
        "~/"*) echo "$HOME/${1#\~/}" ;;
        *)     echo "$1" ;;
    esac
}

# ---------------------------------------------------------------------------
# 永久配置（默认 /root/kernel/config）
# ---------------------------------------------------------------------------

init_config_paths() {
    if ! mkdir -p "$CONFIG_DIR" 2>/dev/null; then
        CONFIG_DIR="$HOME/kernel/config"
        mkdir -p "$CONFIG_DIR" 2>/dev/null || die "无法创建配置目录: $CONFIG_DIR"
    fi
    chmod 700 "$CONFIG_DIR" 2>/dev/null || true

    [ -n "$PRESET_FILE" ] || PRESET_FILE="$CONFIG_DIR/build.conf"
    [ -n "$PATHS_FILE" ] || PATHS_FILE="$CONFIG_DIR/paths.conf"
    [ -n "$AVB_KEY_FILE" ] || AVB_KEY_FILE="$CONFIG_DIR/avb.pem"

    # 兼容旧版本的 $HOME/.gki-build-preset
    if [ ! -f "$PRESET_FILE" ] && [ -f "$HOME/.gki-build-preset" ]; then
        cp "$HOME/.gki-build-preset" "$PRESET_FILE" 2>/dev/null || true
        chmod 600 "$PRESET_FILE" 2>/dev/null || true
    fi
}

load_paths() {
    [ -f "$PATHS_FILE" ] || return 1
    # shellcheck disable=SC1090
    . "$PATHS_FILE" || return 1
    return 0
}

save_paths() {
    [ -n "${PATHS_FILE:-}" ] || return 0
    local tmp="${PATHS_FILE}.tmp"
    {
        printf 'WORKSPACE=%q\n' "$WORKSPACE"
        printf 'BOOT_SIGN_KEY_PATH=%q\n' "${BOOT_SIGN_KEY_PATH:-}"
        printf 'CCACHE_DIR=%q\n' "${CCACHE_DIR:-}"
    } > "$tmp" 2>/dev/null && mv "$tmp" "$PATHS_FILE" 2>/dev/null
    chmod 600 "$PATHS_FILE" 2>/dev/null || true
}

ensure_avb_key() {
    local key="${1:-${BOOT_SIGN_KEY_PATH:-$AVB_KEY_FILE}}"

    if [ "${GKI_NO_AVB:-0}" = "1" ]; then
        unset BOOT_SIGN_KEY_PATH
        echo "GKI_NO_AVB=1，跳过 AVB 签名。"
        return 0
    fi

    key="$(expand_tilde "$key")"
    [ -n "$key" ] || key="$AVB_KEY_FILE"
    if [ -d "$(dirname "$key")" ]; then
        key="$(cd "$(dirname "$key")" && pwd)/$(basename "$key")"
    fi
    BOOT_SIGN_KEY_PATH="$key"

    if [ -f "$key" ]; then
        export BOOT_SIGN_KEY_PATH
        echo "AVB key: $key"
        return 0
    fi

    mkdir -p "$(dirname "$key")" 2>/dev/null || true
    if [ -d "$(dirname "$key")" ]; then
        key="$(cd "$(dirname "$key")" && pwd)/$(basename "$key")"
    fi
    BOOT_SIGN_KEY_PATH="$key"
    if command -v openssl >/dev/null 2>&1 && openssl genrsa -out "$key" 2048 >/dev/null 2>&1; then
        chmod 600 "$key" 2>/dev/null || true
        export BOOT_SIGN_KEY_PATH
        echo "已生成自签 AVB 密钥: $key"
        return 0
    fi

    echo "警告: 无法生成 AVB 私钥，本次跳过 boot.img 签名。"
    unset BOOT_SIGN_KEY_PATH
    return 0
}

# ---------------------------------------------------------------------------
# 上次预设
# ---------------------------------------------------------------------------

load_preset() {
    [ -f "$PRESET_FILE" ] || return 1
    # shellcheck disable=SC1090
    . "$PRESET_FILE" || return 1
    return 0
}

save_preset() {
    local tmp="${PRESET_FILE}.tmp"
    {
        printf 'PRESET_MODE=%q\n' "$PRESET_MODE"
        printf 'LAST_RESULT=%q\n' "${LAST_RESULT:-}"
        printf 'ANDROID=%q\n' "$ANDROID"
        printf 'KERNEL=%q\n' "$KERNEL"
        printf 'SUB_LEVEL=%q\n' "$SUB_LEVEL"
        printf 'OS_PATCH=%q\n' "$OS_PATCH"
        printf 'PAGE_SIZE=%q\n' "$PAGE_SIZE"
        printf 'REVISION=%q\n' "$REVISION"
        printf 'KSU_VERSION=%q\n' "$KSU_VERSION"
        printf 'KSU_BRANCH=%q\n' "$KSU_BRANCH"
        printf 'KSU_COMMIT=%q\n' "$KSU_COMMIT"
        printf 'SUSFS_COMMIT=%q\n' "$SUSFS_COMMIT"
        printf 'CUSTOM_VERSION=%q\n' "$CUSTOM_VERSION"
        printf 'ZRAM=%q\n' "$ZRAM"
        printf 'KPM=%q\n' "$KPM"
        printf 'SUSFS=%q\n' "$SUSFS"
        printf 'BBG=%q\n' "$BBG"
        printf 'OP8E=%q\n' "$OP8E"
        printf 'BBR=%q\n' "$BBR"
    } > "$tmp" 2>/dev/null && mv "$tmp" "$PRESET_FILE" 2>/dev/null
    chmod 600 "$PRESET_FILE" 2>/dev/null || true
}

show_preset() {
    echo "上次预设 ($PRESET_FILE):"
    case "$PRESET_MODE" in
        single) echo "  方式: 单配置构建" ;;
        dry)    echo "  方式: 单配置 dry-run" ;;
        matrix) echo "  方式: 按内核线构建全部 sublevel" ;;
        all)    echo "  方式: 构建全部矩阵" ;;
    esac
    if [ "$PRESET_MODE" = "matrix" ]; then
        echo "  目标: ${ANDROID} / ${KERNEL}（全部 sublevel）"
    elif [ "$PRESET_MODE" != "all" ]; then
        echo "  目标: ${ANDROID} / ${KERNEL}  ${SUB_LEVEL}  ${OS_PATCH}  [${PAGE_SIZE}]"
    fi
    local ksu_branch_note=""
    if [ "$KSU_VERSION" = "Custom(自定义)" ] && [ -n "$KSU_BRANCH" ]; then
        ksu_branch_note=" (${KSU_BRANCH})"
    fi
    echo "  SukiSU: ${KSU_VERSION}${ksu_branch_note}"
    echo "  选项: SUSFS=${SUSFS}  ZRAM=${ZRAM}  KPM=${KPM}  BBG=${BBG}  OP8E=${OP8E}  BBR=${BBR}"
    echo "  AVB 私钥: ${BOOT_SIGN_KEY_PATH:-未设置}"
    echo "  配置目录: ${CONFIG_DIR}"
    case "${LAST_RESULT:-}" in
        success) echo "  上次结果: 成功" ;;
        partial) echo "  上次结果: 部分成功" ;;
        failed)  echo "  上次结果: 失败" ;;
        running) echo "  上次结果: 中断/未完成" ;;
        *)       echo "  上次结果: 未知" ;;
    esac
    echo "  工作目录: ${WORKSPACE}"
}

run_preset() {
    local auto_confirm="${1:-n}"

    if [ -z "$PRESET_MODE" ]; then
        echo "还没有保存过预设。"
        pause
        return
    fi

    show_preset
    if [ "$auto_confirm" != "y" ] && ! ask_yn "按上次预设开始构建?" y; then
        return
    fi

    mkdir -p "$WORKSPACE" || { echo "无法创建工作目录: $WORKSPACE"; pause; return; }
    export GKI_WORKSPACE="$WORKSPACE"

    BUILD_ARGS=()
    build_common_args
    case "$PRESET_MODE" in
        single|dry)
            BUILD_ARGS+=(
                --android "$ANDROID"
                --kernel "$KERNEL"
                --sub-level "$SUB_LEVEL"
                --os-patch "$OS_PATCH"
                --page-size "$PAGE_SIZE"
            )
            if [ -n "$REVISION" ]; then BUILD_ARGS+=(--revision "$REVISION"); fi
            if [ "$PRESET_MODE" = "dry" ]; then run_build y; else run_build n; fi
            ;;
        matrix)
            BUILD_ARGS+=(--matrix "$ANDROID-$KERNEL")
            run_build n
            ;;
        all)
            BUILD_ARGS+=(--all)
            run_build n
            ;;
        *)
            echo "预设内容无效: $PRESET_MODE"
            pause
            ;;
    esac
}

# ---------------------------------------------------------------------------
# 从 Python 配置读取选项，避免在 shell 里重复维护版本表
# ---------------------------------------------------------------------------

list_combos() {
    "$PYTHON" - "$SCRIPTS_DIR" <<'PY'
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
    "$PYTHON" - "$SCRIPTS_DIR" "$combo" <<'PY'
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
    local -a labels=(
        "Stable(标准) -> builtin"
        "Dev(开发) -> builtin-new"
        "Custom(自定义) -> 手动输入分支"
    )
    local ksu_default=0

    [ -n "${KSU_VERSION:-}" ] || KSU_VERSION="Stable(标准)"
    case "$KSU_VERSION" in
        "Dev(开发)")       ksu_default=1 ;;
        "Custom(自定义)")  ksu_default=2 ;;
    esac

    MENU_DEFAULT_INDEX=$ksu_default
    menu "SukiSU 版本（上次: ${KSU_VERSION}）" "${labels[@]}" || return 1
    KSU_VERSION="${SELECTED%% ->*}"

    if [ "$KSU_VERSION" = "Custom(自定义)" ]; then
        KSU_BRANCH="$(read_value "SukiSU 分支 / tag / commit" "${KSU_BRANCH:-builtin-new}")"
        if ! [[ "$KSU_BRANCH" =~ ^[A-Za-z0-9][A-Za-z0-9._/~^-]*$ ]]; then
            echo "分支名包含不安全字符，已取消。"
            return 1
        fi
    fi

    KSU_COMMIT="$(read_value "指定 SukiSU commit（留空忽略）" "${KSU_COMMIT:-}")"
    SUSFS_COMMIT="$(read_value "指定 SUSFS commit / ref（留空忽略）" "${SUSFS_COMMIT:-}")"
    CUSTOM_VERSION="$(read_value "自定义内核版本名（留空忽略）" "${CUSTOM_VERSION:-}")"
    save_preset
}

collect_flags() {
    [ -n "${ZRAM:-}" ] || ZRAM="n"
    [ -n "${KPM:-}" ] || KPM="y"
    [ -n "${SUSFS:-}" ] || SUSFS="y"
    [ -n "${BBG:-}" ] || BBG="n"
    [ -n "${OP8E:-}" ] || OP8E="n"
    [ -n "${BBR:-}" ] || BBR="n"

    if ask_yn "启用 SUSFS?" "$SUSFS"; then SUSFS="y"; else SUSFS="n"; fi
    if [ "$SUSFS" = "n" ] && [ "$KSU_VERSION" = "Stable(标准)" ]; then
        echo "未启用 SUSFS 时 builtin 没有 branch link 钩子，已改用 Dev(builtin-new)。"
        KSU_VERSION="Dev(开发)"
        KSU_BRANCH=""
    fi
    if ask_yn "启用 ZRAM (LZ4KD)?" "$ZRAM"; then ZRAM="y"; else ZRAM="n"; fi
    if ask_yn "启用 KPM 修补?" "$KPM"; then KPM="y"; else KPM="n"; fi
    if ask_yn "启用 Baseband-guard (BBG)?" "$BBG"; then BBG="y"; else BBG="n"; fi
    if ask_yn "启用 OnePlus 8E 支持?" "$OP8E"; then OP8E="y"; else OP8E="n"; fi
    if ask_yn "设置 BBR 为默认拥塞算法?" "$BBR"; then BBR="y"; else BBR="n"; fi
    save_preset
}

supports_16k() {
    case "$1" in
        6.1|6.6|6.12|6.18) return 0 ;;
        *) return 1 ;;
    esac
}

collect_page_size() {
    local kernel="$1" matrix_default="${2:-4k}" default answer
    default="${PAGE_SIZE:-$matrix_default}"
    if ! supports_16k "$kernel"; then
        PAGE_SIZE="4k"
        echo "Kernel ${kernel} 不支持 16k page size，使用 4k。"
        save_preset
        return 0
    fi

    while true; do
        answer="$(read_value "Page Size (4k/16k)" "$default")" || return 1
        case "$answer" in
            4k|16k) PAGE_SIZE="$answer"; save_preset; return 0 ;;
            *) echo "请输入 4k 或 16k。" ;;
        esac
    done
}

# ---------------------------------------------------------------------------
# 构建参数
# ---------------------------------------------------------------------------

build_common_args() {
    BUILD_ARGS=(--workspace "$WORKSPACE")
    if [ -n "$CUSTOM_VERSION" ]; then BUILD_ARGS+=(--custom-version "$CUSTOM_VERSION"); fi

    # 补丁参数全部传递
    BUILD_ARGS+=(--ksu-version "$KSU_VERSION")
    # Stable/Dev 的分支由 build.py 按版本映射，只有 Custom 才传手动分支
    if [ "$KSU_VERSION" = "Custom(自定义)" ] && [ -n "$KSU_BRANCH" ]; then
        BUILD_ARGS+=(--ksu-branch "$KSU_BRANCH")
    fi
    if [ -n "$KSU_COMMIT" ]; then BUILD_ARGS+=(--ksu-commit "$KSU_COMMIT"); fi
    if [ -n "$SUSFS_COMMIT" ]; then BUILD_ARGS+=(--susfs-commit "$SUSFS_COMMIT"); fi
    if [ "$ZRAM" = "y" ]; then BUILD_ARGS+=(--zram); fi
    if [ "$KPM" = "n" ]; then BUILD_ARGS+=(--no-kpm); fi
    if [ "$SUSFS" = "n" ]; then BUILD_ARGS+=(--no-susfs); fi
    if [ "$BBG" = "y" ]; then BUILD_ARGS+=(--bbg); fi
    if [ "$OP8E" = "y" ]; then BUILD_ARGS+=(--op8e); fi
    if [ "$BBR" = "y" ]; then BUILD_ARGS+=(--bbr); fi
}

run_build() {
    local dry_run="${1:-n}"
    local -a args=("${BUILD_ARGS[@]}")
    local rc attempt=1 is_real=1 retry_default=y

    ensure_avb_key
    save_paths

    if [ "$dry_run" = "y" ]; then
        is_real=0
        args+=(--dry-run)
    fi

    while true; do
        if [ "$is_real" = "1" ]; then
            LAST_RESULT="running"
        fi
        save_preset

        echo
        echo "执行（第 ${attempt} 次）:"
        printf '  %q' "$PYTHON"
        printf ' %q' "$BUILD_PY" "${args[@]}"
        echo
        echo
        "$PYTHON" "$BUILD_PY" "${args[@]}"
        rc=$?

        if [ "$is_real" = "1" ]; then
            case "$rc" in
                0) LAST_RESULT="success" ;;
                2) LAST_RESULT="partial" ;;
                *) LAST_RESULT="failed" ;;
            esac
        fi
        save_preset

        echo
        if [ "$rc" -eq 0 ]; then
            echo "构建完成 (exit 0)。产物位于: $WORKSPACE"
        elif [ "$rc" -eq 2 ]; then
            echo "部分配置成功 (exit 2)，请查看上面的失败列表。"
        else
            echo "构建失败 (exit $rc)。"
        fi

        if [ "$is_real" = "0" ] || [ "$rc" -eq 0 ]; then
            break
        fi

        retry_default=y
        [ "$rc" -eq 2 ] && retry_default=n
        if ! ask_yn "是否用相同配置重试构建?" "$retry_default"; then
            break
        fi
        attempt=$((attempt + 1))
    done

    find "$WORKSPACE" -maxdepth 4 -type f \
        \( -name '*.zip' -o -name '*.img' \) \
        -print 2>/dev/null | tail -20 | sed 's/^/  /'
    find "$WORKSPACE" -maxdepth 5 -type f -name 'build-*.log' -print 2>/dev/null \
        | tail -10 | sed 's/^/  日志: /'
    pause
}

# ---------------------------------------------------------------------------
# 目标选择
# ---------------------------------------------------------------------------

pick_combo() {
    local -a combos=() labels=()
    local item android kernel i default_index=-1 last=""

    mapfile -t combos < <(list_combos)
    if [ "${#combos[@]}" -eq 0 ]; then
        echo "无法读取 Android/Kernel 组合。"
        return 1
    fi
    for i in "${!combos[@]}"; do
        IFS='|' read -r android kernel <<< "${combos[$i]}"
        labels+=("${android} / ${kernel}")
        if [ "$android" = "$ANDROID" ] && [ "$kernel" = "$KERNEL" ]; then
            default_index=$i
        fi
    done
    [ "$default_index" -ge 0 ] && last="（上次: ${ANDROID} / ${KERNEL}）"

    MENU_DEFAULT_INDEX=$default_index
    menu "选择 Android / Kernel${last}" "${labels[@]}" || return 1
    IFS='|' read -r ANDROID KERNEL <<< "${combos[$SELECTED_INDEX]}"
    save_preset
}

do_single_build() {
    local dry_run="$1"
    local combo page_size sub os_patch rev i default_index=-1 last=""
    local -a rows=() labels=()

    PRESET_MODE="single"

    pick_combo || return 0
    combo="${ANDROID}-${KERNEL}"
    collect_ksu_options || return 0
    collect_flags

    mapfile -t rows < <(list_matrix_rows "$combo")
    for i in "${!rows[@]}"; do
        IFS='|' read -r sub os_patch page_size rev <<< "${rows[$i]}"
        labels+=("${sub}  ${os_patch}  [${page_size}]${rev:+  rev=${rev}}")
        if [ "$sub" = "$SUB_LEVEL" ] && [ "$os_patch" = "$OS_PATCH" ]; then
            default_index=$i
        fi
    done
    labels+=("手动输入 sublevel / os_patch")
    [ "$default_index" -ge 0 ] && last="（上次: ${SUB_LEVEL} / ${OS_PATCH}）"

    MENU_DEFAULT_INDEX=$default_index
    menu "选择 Sublevel / OS Patch${last}" "${labels[@]}" || return 0

    if [ "$SELECTED_INDEX" -eq "${#rows[@]}" ]; then
        SUB_LEVEL="$(read_value "Sublevel（数字或 X）" "${SUB_LEVEL:-}")"
        OS_PATCH="$(read_value "OS Patch Level（如 2026-09，或 lts）" "${OS_PATCH:-}")"
        REVISION=""
        if [ "$ANDROID" = "android12" ]; then
            REVISION="$(read_value "Android 12 Revision（如 r1，留空忽略）" "${REVISION:-}")"
        fi
        collect_page_size "$KERNEL" "4k"
    else
        IFS='|' read -r SUB_LEVEL OS_PATCH page_size REVISION <<< "${rows[$SELECTED_INDEX]}"
        collect_page_size "$KERNEL" "$page_size"
    fi
    save_preset

    if [ -z "$SUB_LEVEL" ] || [ -z "$OS_PATCH" ]; then
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
        --sub-level "$SUB_LEVEL"
        --os-patch "$OS_PATCH"
        --page-size "$PAGE_SIZE"
    )
    if [ -n "$REVISION" ]; then BUILD_ARGS+=(--revision "$REVISION"); fi

    run_build "$dry_run"
}

do_matrix_build() {
    local combo item android kernel i default_index=-1 last=""
    local -a combos=() labels=()

    PRESET_MODE="matrix"
    mapfile -t combos < <(list_combos)
    for i in "${!combos[@]}"; do
        IFS='|' read -r android kernel <<< "${combos[$i]}"
        labels+=("${android} / ${kernel}")
        if [ "$android" = "$ANDROID" ] && [ "$kernel" = "$KERNEL" ]; then
            default_index=$i
        fi
    done
    [ "$default_index" -ge 0 ] && last="（上次: ${ANDROID} / ${KERNEL}）"

    MENU_DEFAULT_INDEX=$default_index
    menu "选择要构建的内核线${last}" "${labels[@]}" || return 0
    IFS='|' read -r ANDROID KERNEL <<< "${combos[$SELECTED_INDEX]}"
    combo="${ANDROID}-${KERNEL}"
    save_preset

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
    PRESET_MODE="all"
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
    local -a need=(python3 git curl patch zip unzip tar gzip make ccache
                   lz4 bison flex bc perl rsync cpio openssl pahole)
    local cmd missing=()
    for cmd in "${need[@]}"; do
        command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
    done
    echo "${missing[*]:-}"
}

install_dependencies() {
    local -a apt_cmd

    if [ "$(id -u)" -eq 0 ]; then
        apt_cmd=(apt-get)
    elif command -v sudo >/dev/null 2>&1; then
        apt_cmd=(sudo apt-get)
    else
        echo "没有 sudo，请手动安装缺少的依赖。"
        return 1
    fi

    if ! command -v apt-get >/dev/null 2>&1; then
        echo "未检测到 apt-get，请手动安装缺少的依赖。"
        return 1
    fi

    echo "正在通过 apt-get 安装构建依赖..."
    "${apt_cmd[@]}" update
    "${apt_cmd[@]}" install -y \
        git curl python3 zip unzip patch ccache build-essential openssl perl \
        bc bison flex libssl-dev libelf-dev libncurses-dev rsync cpio zstd lz4 dwarves \
        device-tree-compiler
}

init_environment() {
    local missing

    init_config_paths

    if load_paths; then
        echo "已加载路径配置: $PATHS_FILE"
    fi
    if load_preset; then
        echo "已加载上次预设: $PRESET_FILE"
    fi

    if [ ! -f "$BUILD_PY" ]; then
        die "找不到 $BUILD_PY"
    fi

    if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
        die "需要 Python 3.9 或更新版本（当前: $("$PYTHON" --version 2>&1)）"
    fi

    missing="$(missing_commands)"
    if [ -n "$missing" ]; then
        echo "缺少命令: $missing"
        if [ "${GKI_NO_APT:-0}" = "1" ]; then
            echo "GKI_NO_APT=1，跳过自动安装。"
        elif command -v apt-get >/dev/null 2>&1; then
            install_dependencies || true
            missing="$(missing_commands)"
            if [ -n "$missing" ]; then
                echo "警告: 仍有缺少的命令: $missing"
            fi
        else
            echo "未检测到 apt-get，请手动安装上述命令。"
        fi
    fi

    if [ -n "${GKI_WORKSPACE:-}" ]; then
        WORKSPACE="$GKI_WORKSPACE"
    fi
    WORKSPACE="$(expand_tilde "${WORKSPACE:-}")"
    if [ -z "$WORKSPACE" ]; then
        WORKSPACE="$HOME/gki-build"
        WORKSPACE="$(expand_tilde "$(read_value "构建工作目录" "$WORKSPACE")")"
    else
        echo "使用记住的工作目录: $WORKSPACE"
    fi
    mkdir -p "$WORKSPACE" || die "无法创建工作目录: $WORKSPACE"
    export GKI_WORKSPACE="$WORKSPACE"

    case "$WORKSPACE" in
        /mnt/*)
            echo "提示: 工作目录在 Windows 挂载盘上，WSL 下编译会明显变慢，建议放到 \$HOME。"
            ;;
    esac

    if [ -n "${CCACHE_DIR:-}" ]; then
        CCACHE_DIR="$(expand_tilde "$CCACHE_DIR")"
    else
        CCACHE_DIR="$HOME/.ccache"
    fi
    export CCACHE_DIR
    mkdir -p "$CCACHE_DIR" 2>/dev/null || true
    save_paths
}

configure_paths() {
    echo "配置目录: $CONFIG_DIR"

    WORKSPACE="$(read_value "构建工作目录" "${WORKSPACE:-$HOME/gki-build}")"
    WORKSPACE="$(expand_tilde "$WORKSPACE")"
    mkdir -p "$WORKSPACE" || { echo "无法创建工作目录: $WORKSPACE"; pause; return; }
    export GKI_WORKSPACE="$WORKSPACE"

    local key
    key="$(read_value "AVB 私钥路径（留空使用自签 $AVB_KEY_FILE）" "${BOOT_SIGN_KEY_PATH:-}")"
    key="$(expand_tilde "$key")"
    [ -n "$key" ] || key="$AVB_KEY_FILE"
    BOOT_SIGN_KEY_PATH="$key"
    ensure_avb_key "$key"

    CCACHE_DIR="$(read_value "ccache 目录" "${CCACHE_DIR:-$HOME/.ccache}")"
    CCACHE_DIR="$(expand_tilde "$CCACHE_DIR")"
    export CCACHE_DIR
    mkdir -p "$CCACHE_DIR" 2>/dev/null || true

    save_paths
    echo "已保存路径配置: $PATHS_FILE"
    pause
}

show_matrix() {
    "$PYTHON" "$BUILD_PY" --list-configs
    echo
    "$PYTHON" "$BUILD_PY" --list-matrix
    pause
}

has_kernel_sources() {
    [ -n "${WORKSPACE:-}" ] || return 1
    [ -d "$WORKSPACE" ] || return 1
    find "$WORKSPACE" -maxdepth 4 -type d -name common -print -quit 2>/dev/null | grep -q .
}

maybe_resume_build() {
    [ "${GKI_AUTO_RESUME:-1}" = "1" ] || return 0
    [ -n "${PRESET_MODE:-}" ] || return 0
    case "$PRESET_MODE" in
        single|matrix|all) ;;
        *) return 0 ;;
    esac
    has_kernel_sources || return 0

    local state=""
    case "${LAST_RESULT:-}" in
        failed)  state="失败" ;;
        running) state="中断/未完成" ;;
    esac

    if [ -n "$state" ]; then
        echo
        echo "检测到上次构建${state}，且内核源码已存在。"
        if ask_yn "是否按上次预设继续/重试构建?" n; then
            run_preset y
        fi
    elif [ "$PRESET_MODE" = "single" ] && ask_yn "检测到已有内核源码，是否直接按上次配置构建?" n; then
        run_preset y
    fi
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

    if [ "$AUTO_ACTION" = "build" ]; then
        if [ -z "$PRESET_MODE" ]; then
            echo "没有可用的上次预设，无法 --build。"
            exit 1
        fi
        export GKI_NO_PAUSE=1
        run_preset y
        exit 0
    fi

    if [ "$AUTO_ACTION" = "retry" ]; then
        if [ -z "$PRESET_MODE" ]; then
            echo "没有可用的上次预设，无法 --retry。"
            exit 1
        fi
        case "${LAST_RESULT:-}" in
            failed|running) ;;
            *)
                echo "上次构建结果不是失败/中断（${LAST_RESULT:-未知}），不执行 --retry。"
                exit 1
                ;;
        esac
        if ! has_kernel_sources; then
            echo "没有找到已有内核源码，无法 --retry；请使用 --build 或交互模式。"
            exit 1
        fi
        export GKI_NO_PAUSE=1
        run_preset y
        exit 0
    fi

    maybe_resume_build

    while true; do
        local -a items=(
            "单配置构建"
            "单配置 dry-run（只校验）"
            "按内核线构建全部 sublevel"
            "构建全部矩阵"
            "修改路径/配置"
            "查看构建矩阵"
            "检查/安装依赖"
            "退出"
        )
        if [ -n "${PRESET_MODE:-}" ] && [ -f "$PRESET_FILE" ]; then
            items=("按上次预设构建" "${items[@]}")
        fi

        if ! menu "主菜单" "${items[@]}"; then
            break
        fi

        case "$SELECTED" in
            "按上次预设构建")          run_preset ;;
            "单配置构建")            do_single_build n ;;
            "单配置 dry-run（只校验）") do_single_build y ;;
            "按内核线构建全部 sublevel") do_matrix_build ;;
            "构建全部矩阵")          do_all_build ;;
            "修改路径/配置")          configure_paths ;;
            "查看构建矩阵")          show_matrix ;;
            "检查/安装依赖")
                if [ "${GKI_NO_APT:-0}" = "1" ]; then
                    echo "GKI_NO_APT=1，跳过自动安装。"
                else
                    install_dependencies || true
                fi
                pause
                ;;
            "退出") break ;;
        esac
    done
    echo "再见。"
}

main "$@"
