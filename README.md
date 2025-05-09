### 这是一个自动构建GKI内核的仓库

> 本仓库仅支持GKI设备，[在此](https://source.android.com/docs/core/architecture/kernel/gki-release-builds?hl=zh-cn)查看您的设备是否支持

> 非GKI和一加设备可以尝试[SukiSU云盘](https://alist.shirkneko.top)的资源
### 下载
可以[在此](https://github.com/Shirkneko/GKI_KernelSU_SUSFS/releases)下载您的资源
1. 关于Anykernel3.zip，下载不带**gz**、**lz4**后缀的。
- 然后使用刷入软件，例如[HorizonKernelFlasher](https://github.com/libxzr/HorizonKernelFlasher/releases)进行刷写内核
2. 关于boot.img，下载与你内核格式相匹配的（无压缩、gz、lz4），[参考](https://kernelsu.org/zh_CN/guide/installation.html#install-by-kernelsu-boot-image) **找到合适的 boot.img** 一节
- 使用[FASTBOOT](https://magiskcn.com/)刷入，或者使用刷写软件刷写到ROOT所在插槽的boot分区(例如爱玩机、Kernelflasher)
3. 关于SukiSU管理器，你应当下载 [SukiSU Ultra](https://github.com/ShirkNeko/SukiSU-Ultra/releases)版本

### 支持
| 功能 | 说明 |
| --- | --- |
| [KernelSU](https://kernelsu.org/zh_CN/) | 包括**原版、MKSU、SUKISU、NEXT** |
| [SUSFS4](https://gitlab.com/simonpunk/susfs4ksu) | 在内核层面辅助KSU隐藏的功能补丁 |
| [BBR](https://blog.thinkin.top/archives/ke-pu-bbrdao-di-shi-shi-me) | TCP拥塞控制算法，使网络更快？ |
| [Wireguard](https://zh.wikipedia.org/wiki/WireGuard) | 参考左侧wiki链接 |
| [LZ4KD](https://github.com/ShirkNeko/SukiSU_patch/tree/main/other) | 听说是来自HUAWEI source的ZRAM算法，补丁由[云彩之枫](http://www.coolapk.com/u/24963680)移植 |

<details>

<summary>还支持这几种算法，可在scene的ZRAM切换</summary>

### LZ4K、LZ4HC、deflate、842、zstdn、lz4k_oplus

</details>

> [!TIP]
> 1.在MT管理器的终端输入 `uname -r`获取内核版本号，然后在Action面板编译时输入这个版本号，可以伪装内核
>
>
> 2.在[workflow文件夹](https://github.com/zzh20188/GKI_KernelSU_SUSFS/tree/dev/.github/workflows)中，修改例如**kernel-a12-5.10.yml**文件，可以减少你不需要的GKI版本，增加编译速度；或者添加指定的GKI版本，可参考[修改为指定GKI版本](https://www.coolapk.com/feed/62820671?shareKey=OGMxYmZmNTk0YzIxNjgxNzM1MzI~&shareUid=11253396&shareFrom=com.coolapk.market_15.2.2)
