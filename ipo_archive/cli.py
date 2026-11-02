from collections import Counter
from pathlib import Path
from logging.handlers import RotatingFileHandler
import argparse
import json
import logging
import os
import plistlib
import shutil
import subprocess
import sqlite3
import sys
import time

from .models import ArchiveError, CATEGORIES, EXCHANGES
from .transport import Transport
from .exchanges import ExchangeAPI
from .store import Store, atomic_json, instance_lock
from .engine import Engine

BASE = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = {
    "output_dir": "./资料库", "state_dir": "./运行状态", "exchanges": ["sse", "szse", "bse"],
    "page_size": 50, "request_interval_seconds": 2.5, "jitter_seconds": 0.75,
    "connect_timeout_seconds": 10, "request_timeout_seconds": 180, "max_attempts": 3,
    "max_file_size_mb": 500, "recheck_days": 7, "recent_overlap_days": 2, "watch_interval_hours": 24,
}


def load_config(filename):
    path = Path(filename).expanduser().resolve()
    config = DEFAULT_CONFIG.copy()
    if path.exists():
        incoming = json.loads(path.read_text(encoding="utf-8"))
        unknown = set(incoming) - set(config)
        if unknown:
            raise ArchiveError("配置含未知字段：" + ", ".join(unknown))
        config.update(incoming)
    else:
        raise ArchiveError("配置不存在，请先执行 init")
    for name in ("output_dir", "state_dir"):
        value = Path(config[name]).expanduser()
        config[name] = str((path.parent / value).resolve())
    if config["output_dir"] == config["state_dir"]:
        raise ArchiveError("资料目录和运行状态目录不能相同")
    limits = {"page_size": (1, 100), "request_interval_seconds": (1, 3600), "jitter_seconds": (0, 60),
              "connect_timeout_seconds": (1, 120), "request_timeout_seconds": (10, 3600), "max_attempts": (1, 5),
              "max_file_size_mb": (1, 2048), "recheck_days": (1, 365), "recent_overlap_days": (0, 30), "watch_interval_hours": (1, 8760)}
    for key, (low, high) in limits.items():
        value = config[key]
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not low <= value <= high:
            raise ArchiveError(f"配置 {key} 应在 {low}～{high} 之间")
    for key in ("page_size", "max_attempts", "max_file_size_mb"):
        if not isinstance(config[key], int):
            raise ArchiveError(key + " 必须为整数")
    if (not isinstance(config["exchanges"], list) or not config["exchanges"] or
            any(x not in EXCHANGES for x in config["exchanges"]) or len(set(config["exchanges"])) != len(config["exchanges"])):
        raise ArchiveError("exchanges 必须是 sse、szse、bse 中不重复的交易所列表")
    return config


def logger_for(state_dir):
    logger = logging.getLogger("ipo_archive")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    for handler in (logging.StreamHandler(), RotatingFileHandler(Path(state_dir) / "运行日志.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")):
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger


def positive(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("必须为正整数")
    return number


def parser():
    cli = argparse.ArgumentParser(description="三所 IPO 文件归档器：默认不下载；只有 sync/watch --download 会下载正式文件。")
    cli.add_argument("--config", default=str(BASE / "config.json"), help="配置文件路径")
    commands = cli.add_subparsers(dest="command")
    commands.add_parser("init", help="创建初始配置，不联网")
    doctor = commands.add_parser("doctor", help="检查公开接口，只取列表和文件元数据，不下载附件")
    doctor.add_argument("--url", help="只检查一个官方 IPO 详情页")
    doctor.add_argument("--exchange", choices=EXCHANGES, action="append")
    for name in ("scan", "sync", "watch"):
        cmd = commands.add_parser(name, help={"scan": "只更新元数据和待下载清单", "sync": "扫描并下载新增/未完成文件", "watch": "按间隔自动增量更新（前台常驻）"}[name])
        cmd.add_argument("--exchange", choices=EXCHANGES, action="append")
        cmd.add_argument("--url", help="仅处理一个公司项目详情页")
        cmd.add_argument("--force-check", action="store_true", help="强制复查详情，不重下已经成功的版本")
        cmd.add_argument("--max-projects", type=positive, help="每个交易所本次最多检查几个项目")
        cmd.add_argument("--max-downloads", type=positive, help="本次最多尝试下载几个文件")
        if name in ("sync", "watch"):
            cmd.add_argument("--download", action="store_true", required=True, help="显式启用附件下载")
        if name == "watch":
            cmd.add_argument("--interval-hours", type=positive, help="检查间隔，默认 24 小时")
    commands.add_parser("status", help="本地统计，不联网")
    commands.add_parser("verify", help="校验本地文件 SHA256，损坏项回到待下载，不联网")
    retry = commands.add_parser("retry-failed", help="重置失败项的等待时间，不联网、不下载")
    retry.add_argument("--exchange", choices=EXCHANGES, action="append")
    schedule = commands.add_parser("schedule", help="管理 macOS 每日更新；template 仅生成配置，不启用")
    schedule.add_argument("action", choices=("template", "install", "remove", "status"))
    schedule.add_argument("--download", action="store_true", help="install 时必须显式启用下载")
    schedule.add_argument("--hour", type=int, default=19)
    schedule.add_argument("--minute", type=int, default=30)
    return cli


def schedule(args, config):
    label = "cn.ipo-archive.daily"
    destination = Path.home() / "Library/LaunchAgents" / (label + ".plist")
    service = f"gui/{os.getuid()}/{label}"
    if args.action == "status":
        print("已安装" if destination.exists() else "未安装（不会自动下载）")
        if sys.platform == "darwin" and destination.exists():
            subprocess.run(["launchctl", "print", service], check=False)
        return
    if args.action == "remove":
        if sys.platform != "darwin":
            raise ArchiveError("该命令适用于 macOS；其他系统请使用 watch")
        subprocess.run(["launchctl", "bootout", service], capture_output=True)
        destination.unlink(missing_ok=True)
        print("每日自动更新已移除")
        return
    if not 0 <= args.hour <= 23 or not 0 <= args.minute <= 59:
        raise ArchiveError("时间必须为 00:00～23:59")
    payload = {"Label": label, "ProgramArguments": [sys.executable, str(BASE / "ipo_downloader.py"), "--config",
        str(Path(args.config).expanduser().resolve()), "sync", "--download"], "WorkingDirectory": str(BASE),
        "StartCalendarInterval": {"Hour": args.hour, "Minute": args.minute}, "RunAtLoad": False,
        "ProcessType": "Background", "LowPriorityIO": True,
        "StandardOutPath": str(Path(config["state_dir"]) / "定时任务输出.log"),
        "StandardErrorPath": str(Path(config["state_dir"]) / "定时任务错误.log"),
        "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"}}
    target = Path(config["state_dir"]) / (label + ".plist")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(plistlib.dumps(payload))
    if args.action == "template":
        print("仅生成模板，未注册定时任务：" + str(target))
        return
    if not args.download:
        raise ArchiveError("安装会启用未来的自动下载，需显式指定 schedule install --download")
    if sys.platform != "darwin":
        raise ArchiveError("自动安装适用于 macOS；其他系统可运行 watch --download")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ArchiveError("每日任务已存在；请先检查 schedule status，再 remove 后重装")
    shutil.copyfile(target, destination)
    result = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(destination)], capture_output=True, text=True)
    if result.returncode:
        destination.unlink(missing_ok=True)
        raise ArchiveError("定时任务安装失败：" + result.stderr.strip())
    print(f"已启用每日 {args.hour:02}:{args.minute:02} 增量更新（按本机时区）；未立即下载。")


def doctor(api, args, config):
    failures, report = 0, []
    for exchange in (args.exchange or config["exchanges"]):
        try:
            if args.url:
                project, docs = api.by_url(args.url)
                total = None
            else:
                projects, total, pages = api.page(exchange, 0)
                if not projects:
                    print(EXCHANGES[exchange] + "：列表为空，需核查")
                    failures += 1
                    continue
                project = projects[0]
                docs = api.documents(project)
            counts = dict(Counter(d.category for d in docs))
            item = {"exchange": project.exchange, "company": project.name, "project_id": project.id,
                    "official_project_count": total, "document_count": len(docs), "categories": counts,
                    "attachment_downloads": 0}
            report.append(item)
            print(json.dumps(item, ensure_ascii=False, indent=2))
        except (ArchiveError, OSError, ValueError) as error:
            failures += 1
            print(EXCHANGES[exchange] + "：检查未完成，" + str(error), file=sys.stderr)
        if args.url:
            break
    atomic_json(Path(config["state_dir"]) / "接口检查.json", {"checked_at": time.time(), "results": report, "failures": failures, "attachment_downloads": 0})
    return 2 if failures else 0


def main(argv=None):
    cli = parser()
    args = cli.parse_args(argv)
    if not args.command:
        cli.print_help()
        return 0
    try:
        if args.command == "init":
            path = Path(args.config).expanduser().resolve()
            if path.exists():
                print("配置已存在，未覆盖：" + str(path))
            else:
                atomic_json(path, DEFAULT_CONFIG)
                print("配置已创建，尚未联网或下载：" + str(path))
            return 0
        config = load_config(args.config)
        if args.command == "schedule":
            schedule(args, config)
            return 0
        with instance_lock(config["state_dir"]):
            log = logger_for(config["state_dir"])
            store = Store(config["state_dir"])
            try:
                if args.command == "status":
                    print(json.dumps(store.counts(), ensure_ascii=False, indent=2))
                    print("定时任务：" + ("已安装" if (Path.home() / "Library/LaunchAgents/cn.ipo-archive.daily.plist").exists() else "未安装"))
                    return 0
                if args.command == "retry-failed":
                    for exchange in (args.exchange or config["exchanges"]):
                        with store.db:
                            store.db.execute("UPDATE documents SET retry_at=0 WHERE status='failed' AND project_key IN (SELECT key FROM projects WHERE exchange=?)", (exchange,))
                            store.db.execute("UPDATE projects SET retry_at=0 WHERE exchange=?", (exchange,))
                    # Keep host cooldowns intact: retry-failed never evades a rate limit.
                    print("失败项等待时间已重置；交易所限流冷却仍有效。没有联网或下载。")
                    return 0
                api = ExchangeAPI(Transport(config["state_dir"], config, log), config["page_size"])
                api.http.blocked.update(store.runtime("cooldowns") or {})
                if args.command == "doctor":
                    try:
                        return doctor(api, args, config)
                    finally:
                        store.runtime("cooldowns", api.http.blocked)
                engine = Engine(config, store, api, log)
                if args.command == "verify":
                    print("需重新下载的缺失/损坏文件：", engine.reconcile_local(full_hash=True))
                    return 0
                while True:
                    engine = Engine(config, store, api, log)
                    summary = engine.run(args.exchange or config["exchanges"], download=args.command != "scan",
                        url=args.url, force=args.force_check, max_projects=args.max_projects, max_downloads=args.max_downloads)
                    print(json.dumps(summary, ensure_ascii=False, indent=2))
                    errors = sum(value for key, value in summary["run"].items() if key.endswith("errors"))
                    if args.command != "watch":
                        return 2 if errors else 0
                    interval = args.interval_hours or config["watch_interval_hours"]
                    log.info("本轮结束，%s 小时后检查；可按 Ctrl+C 停止", interval)
                    # Short waits keep cancellation responsive. No task is launched by merely importing this module.
                    end = time.monotonic() + interval * 3600
                    while time.monotonic() < end:
                        time.sleep(min(10, end - time.monotonic()))
            finally:
                store.close()
    except KeyboardInterrupt:
        print("已停止；已确认的进度保留，下次可继续。", file=sys.stderr)
        return 130
    except (ArchiveError, OSError, ValueError, sqlite3.DatabaseError, subprocess.SubprocessError) as error:
        print("未完成：" + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
