#!/usr/bin/env python3
"""P1Pro 控制台 · 飞牛版 启动入口。

    python3 app.py                      # 默认配置 config.json
    python3 app.py --port 9787
    python3 app.py --host 192.168.0.15  # 临时指定插座地址（不写回配置）

只用 Python 3 标准库，装完 Python 就能跑，不需要 pip install 任何东西。
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from p1pro import __version__                      # noqa: E402
from p1pro.config import Config                    # noqa: E402
from p1pro.protocol import PlugClient, PlugError   # noqa: E402
from p1pro.sampler import Sampler                  # noqa: E402
from p1pro.server import App, serve                # noqa: E402
from p1pro.store import Store                      # noqa: E402

BANNER = r"""
  ____  _ ____                  _   _    _   _
 |  _ \| |  _ \ _ __ ___       | \ | |  / \  / ___|
 | |_) | | |_) | '__/ _ \ _____|  \| | / _ \ \___ \
 |  __/| |  __/| | | (_) |_____| |\  |/ ___ \ ___) |
 |_|   |_|_|   |_|  \___/      |_| \_/_/   \_\____/
"""


def local_ips():
    found = set()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.2)
        sock.connect(("223.5.5.5", 80))
        found.add(sock.getsockname()[0])
        sock.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except OSError:
        pass
    return sorted(ip for ip in found if not ip.startswith("127."))


def main(argv=None):
    default_config = os.environ.get("P1PRO_CONFIG") or os.path.join(BASE_DIR, "config.json")
    parser = argparse.ArgumentParser(
        description="向日葵 P1Pro 智能插线板控制台（飞牛 fnOS 版）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-c", "--config", default=default_config,
                        help="配置文件路径（默认 config.json）")
    parser.add_argument("--host", help="插座 IP，覆盖配置文件")
    parser.add_argument("--port", type=int, help="Web 端口，覆盖配置文件")
    parser.add_argument("--bind", help="监听地址，覆盖配置文件")
    parser.add_argument("--interval", type=int, help="采样间隔秒，覆盖配置文件")
    parser.add_argument("--no-sampler", action="store_true", help="只开 Web，不采样（排障用）")
    parser.add_argument("--check", action="store_true", help="连通性自检后退出")
    parser.add_argument("--version", action="version", version="p1pro-nas %s" % __version__)
    args = parser.parse_args(argv)

    first_run = not os.path.exists(args.config)
    config = Config(args.config)

    # 容器 / 飞牛应用场景：用环境变量给首次启动播种，之后的改动以配置文件为准
    if first_run:
        env_host = (os.environ.get("P1PRO_HOST") or "").strip()
        env_token = (os.environ.get("P1PRO_TOKEN") or "").strip()
        env_db = (os.environ.get("P1PRO_DB") or "").strip()
        if env_host:
            config.set("device.host", env_host)
        if env_token:
            config.set("web.access_token", env_token)
        if env_db:
            config.set("storage.db_path", env_db)

    # P1PRO_PORT 给容器用，TRIM_SERVICE_PORT 是飞牛分配的服务端口
    env_port = (os.environ.get("P1PRO_PORT")
                or os.environ.get("TRIM_SERVICE_PORT") or "").strip()
    if env_port.isdigit() and not args.port:
        args.port = int(env_port)

    if args.host:
        config.set("device.host", args.host)
    if args.port:
        config.set("web.port", int(args.port))
    if args.bind:
        config.set("web.host", args.bind)
    if args.interval:
        config.set("sampler.interval_seconds", max(1, min(300, args.interval)))

    client = PlugClient(config.host, config.port, config.timeout)

    # ---- 自检模式 ----
    if args.check:
        print("目标 %s:%d" % (config.host, config.port))
        try:
            sn = client.call("get_plug_sn")
            print("  get_plug_sn   OK   sn=%s type=%s" % (sn.get("sn"), sn.get("type")))
            info = client.get_info()
            print("  get_plug_info OK   name=%s fw=%s mac=%s" % (
                info["name"], info["version"], info["mac"]))
            status = client.get_status()
            print("  status        OK   插口=%s led=%s def_st=%s" % (
                "".join(str(v) for v in status["sockets"]), status["led"], status["def_st"]))
            electric = client.get_electric()
            print("  electric      OK   %.2f W / %.1f V / %.0f mA" % (
                electric["power_mw"] / 1000.0,
                electric["vol_mv"] / 1000.0,
                electric["curr_ua"] / 1000.0))
            print("  ------------------------------")
            print("  往返耗时 %.0f ms，自检通过" % client.stats.get("last_ms", 0))
            return 0
        except PlugError as exc:
            print("  失败：%s" % exc)
            return 2

    store = Store(
        config.db_path,
        interval=config.interval,
        raw_retention_days=int(config.get("storage.raw_retention_days", 7)),
        minute_retention_days=int(config.get("storage.minute_retention_days", 400)),
    )
    sampler = Sampler(config, client, store)
    app = App(config, store, client, sampler)

    if not args.no_sampler:
        sampler.start()

    bind = str(config.get("web.host", "0.0.0.0"))
    port = int(config.get("web.port", 9787))
    try:
        httpd = serve(app, bind, port)
    except OSError as exc:
        print("端口 %d 绑定失败：%s" % (port, exc), file=sys.stderr)
        print("换个端口：python3 app.py --port 8788", file=sys.stderr)
        return 3

    print(BANNER)
    print("  P1Pro 控制台 · 飞牛版  v%s" % __version__)
    print("  " + "-" * 46)
    print("  插座     %s:%d" % (config.host, config.port))
    print("  采样     每 %d 秒（%s）" % (
        config.interval, "已启用" if not args.no_sampler else "已禁用"))
    print("  数据     %s" % config.db_path)
    show = ["127.0.0.1"] if bind in ("0.0.0.0", "::") else [bind]
    if bind in ("0.0.0.0", "::"):
        show = local_ips() or ["127.0.0.1"]
    for ip in show:
        print("  访问     http://%s:%d" % (ip, port))
    print("  " + "-" * 46)
    print("  Ctrl+C 停止")
    print("", flush=True)

    stopping = {"flag": False}

    def _shutdown(signum, frame):
        if stopping["flag"]:
            return
        stopping["flag"] = True
        print("\n正在停止…", flush=True)
        try:
            httpd.shutdown()
        except Exception:
            pass

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        sampler.stop()
        store.close()
        print("已停止。", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
