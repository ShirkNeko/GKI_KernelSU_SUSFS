# Vendored SUSFS support files

These files are copied from [WildKernels/GKI_KernelSU_SUSFS](https://github.com/WildKernels/GKI_KernelSU_SUSFS)
(GPL-3.0-or-later) for the android17-6.18 port and the 6.12 patch-context/compile fixes.

- `susfs618/kernel_patches/`: local 6.12 -> 6.18 SUSFS port (upstream `gki-android17-6.18` branch does not exist).
- `scripts/susfs_618_*.py`: 6.18 task_mmu, compile and prototype fixes.
- `scripts/apply_susfs_compat.py`: adapts the 6.12 patch for sublevels before the `show_smaps_rollup` refactor (< 92).
- `scripts/susfs_restore_context.py`: restores `#include <trace/hooks/*>` context lines the patch expects.
- `scripts/susfs_guard_priority_helpers.py`: guards credential helpers that upstream leaves outside their `#ifdef`.
- `scripts/susfs_fdinfo_fanotify_arity.py`: fixes `fanotify_fdinfo()` arity after the patch widens `show_fdinfo()`.
- `scripts/bbg_618_compat.py`: Baseband-guard `blkdev_get_no_open()` compatibility for 6.18.

Source revision: WildKernels/GKI_KernelSU_SUSFS master as of 2026-11-14.
