"""Run with python -m gsm_wechat_agent.ilink_cli (also inside the Agent image)."""

from __future__ import annotations

import argparse
import getpass
import io
import logging
from pathlib import Path
import signal
import sqlite3
import sys
from threading import Event, Thread
import time
from typing import Callable

import httpx

from gsm_wechat_agent.ilink import (
    DEFAULT_API_URL,
    IlinkAccount,
    IlinkAPI,
    IlinkError,
    IlinkStore,
    api_base_url,
    state_directory,
)
from gsm_wechat_agent.ilink_receiver import IlinkReceiver


def display_qrcode(value: str) -> None:
    import qrcode

    qr = qrcode.QRCode(border=4)
    qr.add_data(value)
    rendered = io.StringIO()
    qr.print_ascii(out=rendered, invert=True)
    try:
        rendered.getvalue().encode(sys.stdout.encoding or "utf-8")
    except UnicodeError:
        print("当前终端无法显示二维码，请打开备用链接扫码。")
    else:
        print(rendered.getvalue(), end="")
    print("请用个人微信扫码并确认授权。备用二维码链接：")
    print(value)


def login(
    store: IlinkStore,
    api: IlinkAPI,
    *,
    display: Callable[[str], None] = display_qrcode,
    read_code: Callable[[str], str] = getpass.getpass,
    timeout: float = 480,
    wait: Callable[[float], None] = time.sleep,
) -> None:
    deadline = time.monotonic() + timeout
    refreshes = 0
    verify_code = None
    base_url = DEFAULT_API_URL
    qrcode = ""
    while time.monotonic() < deadline:
        if not qrcode:
            if refreshes >= 3:
                raise IlinkError("qr_refresh_limit")
            result = api.get_qrcode([account.token for account in store.accounts()])
            qrcode = result.get("qrcode")
            content = result.get("qrcode_img_content")
            if (
                not isinstance(qrcode, str)
                or not qrcode
                or len(qrcode) > 4096
                or not isinstance(content, str)
                or not content
                or len(content) > 4096
            ):
                raise IlinkError("invalid_qrcode")
            display(content)
            refreshes += 1
            base_url = DEFAULT_API_URL
            verify_code = None
        try:
            result = api.qr_status(qrcode, base_url=base_url, verify_code=verify_code)
        except httpx.HTTPError:
            wait(1)
            continue
        status = result.get("status")
        if status == "confirmed":
            account = IlinkAccount(
                account_id=result.get("ilink_bot_id"),
                user_id=result.get("ilink_user_id"),
                token=result.get("bot_token"),
                base_url=result.get("baseurl") or base_url,
            )
            store.save_account(account)
            print(
                "微信连接已保存。接收器会自动发现该账号；请在微信机器人会话发送 /pair 面板配对码。"
            )
            return
        if status == "binded_redirect":
            # That response does not supply credentials: never claim a login without them.
            if not store.accounts():
                raise IlinkError("already_bound_without_local_credentials")
            print("微信已提示绑定过此实例，本地已有凭据。请用 status 检查接收器状态。")
            return
        if status in ("expired", "verify_code_blocked"):
            qrcode = ""
        elif status == "scaned_but_redirect":
            host = result.get("redirect_host")
            if not isinstance(host, str):
                raise IlinkError("invalid_redirect")
            base_url = api_base_url("https://" + host)
        elif status == "need_verifycode":
            verify_code = read_code("输入手机微信显示的验证码：").strip()
            if (
                not verify_code.isascii()
                or not verify_code.isdigit()
                or len(verify_code) > 12
            ):
                raise IlinkError("invalid_verify_code")
            continue
        elif status == "scaned":
            verify_code = None
        elif status != "wait":
            raise IlinkError("unknown_qr_status")
        wait(1)
    raise IlinkError("qr_login_timeout")


def run(store: IlinkStore) -> None:
    stop = Event()
    workers: dict[str, tuple[Thread, Event]] = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    try:
        while not stop.is_set():
            accounts = store.accounts()
            if len(accounts) > 8:
                raise IlinkError("account_limit_exceeded")
            live_ids = {account.account_id for account in accounts}
            for account_id, (thread, worker_stop) in list(workers.items()):
                if account_id not in live_ids:
                    worker_stop.set()
                if not thread.is_alive():
                    del workers[account_id]
            for account in accounts:
                if account.account_id not in workers:
                    receiver = IlinkReceiver(store)
                    worker_stop = Event()
                    thread = Thread(
                        target=receiver.run_account,
                        args=(account.account_id, worker_stop),
                        daemon=True,
                    )
                    workers[account.account_id] = (thread, worker_stop)
                    thread.start()
            stop.wait(1)
    finally:
        for _, worker_stop in workers.values():
            worker_stop.set()
        deadline = time.monotonic() + 60
        for thread, _ in workers.values():
            thread.join(max(0, deadline - time.monotonic()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="个人微信 iLink 私聊连接器")
    parser.add_argument("--state-dir", type=Path, default=state_directory())
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("login", help="微信扫码授权，凭据只保存在本地")
    commands.add_parser("status", help="检查本地连接和接收器状态，不显示密钥")
    commands.add_parser("run", help="持续接收私聊消息")
    logout = commands.add_parser("logout", help="删除本地连接凭据，停止该账号收发消息")
    logout.add_argument("--account", required=True, help="status 显示的连接账号 ID")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        store = IlinkStore(args.state_dir)
        if args.command == "login":
            api = IlinkAPI()
            try:
                login(store, api)
            finally:
                api.close()
        elif args.command == "status":
            health = store.health()
            print("连接状态：" + health["status"])
            for account in store.accounts():
                print("连接账号：" + account.account_id)
            for warning in health["warnings"]:
                print("提示：" + warning)
        elif args.command == "logout":
            store.remove_account(args.account)
            print("本地连接已删除。管理账号的微信绑定可在面板解除。")
        else:
            run(store)
        return 0
    except (IlinkError, httpx.HTTPError, OSError, sqlite3.Error) as exc:
        # Exception strings from HTTP clients can include QR/credential material.
        print(
            "微信连接失败："
            + (str(exc) if isinstance(exc, IlinkError) else type(exc).__name__)
        )
        return 1
    except KeyboardInterrupt:
        print("微信连接流程已停止。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
