#!/usr/bin/env python3
"""Telegram 构建通知。

用法:
    telegram_notify.py single --android android14 --kernel 6.1 ...
    telegram_notify.py release --tag v2.4.0 --url ... --success 5 --failed 1
"""
import argparse
import json
import os
import ssl
import sys
import urllib.request
import uuid


def as_bool(value) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


class TelegramNotifier:
    def __init__(self, bot_token: str = None, chat_id: str = None, thread_id: str = None):
        self.bot_token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")
        self.thread_id = thread_id or os.environ.get("TELEGRAM_MESSAGE_THREAD_ID", "")
        self.ssl_ctx = ssl.create_default_context()

    def _post(self, method: str, data: bytes, content_type: str) -> bool:
        url = f"https://api.telegram.org/bot{self.bot_token}/{method}"
        request = urllib.request.Request(
            url, data=data, headers={"Content-Type": content_type}
        )
        try:
            with urllib.request.urlopen(request, context=self.ssl_ctx, timeout=30) as response:
                result = json.loads(response.read())
        except Exception as e:
            print(f"请求失败: {e}")
            return False
        if result.get("ok"):
            print(f"发送成功 (message_id: {result['result'].get('message_id')})")
            return True
        print(f"发送失败: {result.get('description')}")
        return False

    def send_message(self, message: str, parse_mode: str = "HTML",
                     disable_web_page_preview: bool = True) -> bool:
        if not self.bot_token or not self.chat_id:
            print("错误: 缺少 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID")
            return False

        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_web_page_preview,
        }
        if self.thread_id:
            payload["message_thread_id"] = self.thread_id
        return self._post(
            "sendMessage",
            json.dumps(payload).encode("utf-8"),
            "application/json",
        )

    def send_document(self, file_path: str, caption: str = None) -> bool:
        if not self.bot_token or not self.chat_id:
            print("错误: 缺少 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID")
            return False
        if not os.path.exists(file_path):
            print(f"文件不存在: {file_path}")
            return False

        with open(file_path, "rb") as f:
            file_data = f.read()

        fields = {"chat_id": self.chat_id}
        if self.thread_id:
            fields["message_thread_id"] = self.thread_id
        if caption:
            fields["caption"] = caption
            fields["parse_mode"] = "HTML"

        body, content_type = _encode_multipart(
            fields, "document", os.path.basename(file_path), file_data
        )
        return self._post("sendDocument", body, content_type)


def _encode_multipart(fields: dict, file_field: str, file_name: str,
                      file_data: bytes) -> tuple:
    boundary = f"----gki-build-{uuid.uuid4().hex}"
    chunks = []
    for name, value in fields.items():
        chunks.append(
            (
                f'--{boundary}\r\n'
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f'{value}\r\n'
            ).encode("utf-8")
        )
    chunks.append(
        (
            f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="{file_field}"; filename="{file_name}"\r\n'
            f'Content-Type: application/octet-stream\r\n\r\n'
        ).encode("utf-8")
        + file_data
        + b"\r\n"
    )
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def parse_sha256sums(file_path: str) -> list:
    files = []
    if not file_path or not os.path.exists(file_path):
        return files
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            parts = line.strip().split(None, 1)
            if len(parts) == 2:
                files.append({"hash": parts[0], "filename": parts[1]})
    return files


KSU_REF_BY_VERSION = {
    "Stable(标准)": "builtin",
    "Dev(开发)": "builtin-new",
}


def build_single_notify_message(android_version: str, kernel_version: str,
                                sub_level: str, os_patch_level: str,
                                kernelsu_version: str, kernelsu_ref: str,
                                use_zram: bool, use_kpm: bool,
                                page_size: str = "4k",
                                hashes_file: str = None) -> str:
    message = f"""✅ <b>内核构建成功</b>

<b>📱 Android:</b> {android_version}
<b>🔧 Kernel:</b> {kernel_version}
<b>📌 Sub Level:</b> {sub_level}
<b>📅 OS Patch:</b> {os_patch_level}

<b>💾 SukiSU 版本:</b> {kernelsu_version}"""
    if kernelsu_ref:
        message += f"\n<b>🌿 SukiSU 分支:</b> {kernelsu_ref}"
    if page_size == "16k":
        message += "\n<b>📐 Page Size:</b> 16K"
    message += f"""
<b>🔧 ZRAM:</b> {"启用" if use_zram else "禁用"}
<b>📦 KPM:</b> {"启用" if use_kpm else "禁用"}"""

    files = parse_sha256sums(hashes_file)
    if files:
        message += "\n\n<b>📋 文件校验 (SHA256):</b>"
        for file_info in files:
            message += (
                f"\n<code>{os.path.basename(file_info['filename'])}</code>"
                f"\n<code>{file_info['hash']}</code>"
            )
    return message


def build_release_notify_message(release_tag: str, release_url: str,
                                 release_notes: str = None, hashes_file: str = None,
                                 success_count: str = None,
                                 failed_count: str = None) -> str:
    message = f"""🎉 <b>构建完成!</b>

📦 <b>版本:</b> {release_tag}"""
    if release_url:
        message += f'\n🔗 <b>下载:</b> <a href="{release_url}">点击下载</a>'

    success = int(success_count) if str(success_count).isdigit() else 0
    failed = int(failed_count) if str(failed_count).isdigit() else 0
    total = success + failed
    if total > 0:
        emoji = "✅" if success == total else "⚠️"
        message += f"""

{emoji} <b>构建统计:</b>
✅ 成功: {success}/{total}
❌ 失败: {failed}/{total}"""

    if release_notes:
        message += f"""

📝 <b>更新内容:</b>
{release_notes}"""

    files = parse_sha256sums(hashes_file)
    if files:
        message += "\n\n<b>📋 文件校验 (SHA256):</b>"
        for file_info in files:
            message += (
                f"\n<code>{os.path.basename(file_info['filename'])}</code>"
                f"\n<code>{file_info['hash']}</code>"
            )
    return message


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Telegram 构建通知")
    sub = parser.add_subparsers(dest="action", required=True)

    single = sub.add_parser("single", help="单版本构建通知")
    single.add_argument("--android", required=True)
    single.add_argument("--kernel", required=True)
    single.add_argument("--sub-level", required=True)
    single.add_argument("--os-patch", required=True)
    single.add_argument("--ksu-version", required=True)
    single.add_argument("--ksu-ref", default="")
    single.add_argument("--zram", default="false")
    single.add_argument("--kpm", default="true")
    single.add_argument("--page-size", default="4k")
    single.add_argument("--hashes", default=None)

    release = sub.add_parser("release", help="Release 通知")
    release.add_argument("--tag", required=True)
    release.add_argument("--url", default="")
    release.add_argument("--notes-file", default=None)
    release.add_argument("--hashes", default=None)
    release.add_argument("--success", default="0")
    release.add_argument("--failed", default="0")

    return parser


def main() -> int:
    args = build_parser().parse_args()
    notifier = TelegramNotifier()

    if args.action == "single":
        ksu_ref = args.ksu_ref or KSU_REF_BY_VERSION.get(args.ksu_version, "")
        message = build_single_notify_message(
            android_version=args.android,
            kernel_version=args.kernel,
            sub_level=args.sub_level,
            os_patch_level=args.os_patch,
            kernelsu_version=args.ksu_version,
            kernelsu_ref=ksu_ref,
            use_zram=as_bool(args.zram),
            use_kpm=as_bool(args.kpm),
            page_size=args.page_size,
            hashes_file=args.hashes,
        )
        sent = notifier.send_message(message)
        if args.hashes:
            notifier.send_document(args.hashes, "SHA256SUMS")
        return 0 if sent else 1

    notes = ""
    if args.notes_file and os.path.exists(args.notes_file):
        with open(args.notes_file, "r", encoding="utf-8") as f:
            notes = f.read()

    message = build_release_notify_message(
        release_tag=args.tag,
        release_url=args.url,
        release_notes=notes,
        hashes_file=args.hashes,
        success_count=args.success,
        failed_count=args.failed,
    )
    sent = notifier.send_message(message)
    if args.hashes:
        notifier.send_document(args.hashes, "SHA256SUMS")
    return 0 if sent else 1


if __name__ == "__main__":
    sys.exit(main())
