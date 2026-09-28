from __future__ import annotations

import argparse
import getpass
import json
import socket
from pathlib import Path

from .backup import create_backup, prune_backups, restore_backup, sha256_file
from .config import PROJECT_ROOT, data_dir, ensure_data_dirs
from .db import audit, connect, init_db, now, transaction
from .migrations import Migration, rollback
from .service import ExtractionQueue, import_existing
from .folder_export import export_dev_fee_folders


def main():
    parser = argparse.ArgumentParser(description="局域网合同数据库")
    commands = parser.add_subparsers(dest="command", required=True)
    server = commands.add_parser("serve", help="启动服务")
    server.add_argument("--host", default="0.0.0.0")
    server.add_argument("--port", type=int, default=8000)
    importer = commands.add_parser("import-pdfs", help="导入目录中的 PDF，保留原件")
    importer.add_argument("source", nargs="?", type=Path, default=PROJECT_ROOT)
    backer = commands.add_parser("backup", help="备份数据库及全部原件")
    # 默认 0 = 不清理，桌面版行为不变；便携版每次停止都备份，需要设个上限
    backer.add_argument("--keep", type=int, default=0, help="只保留最近 N 个自动备份（0 表示不清理）")
    folder_export = commands.add_parser("export-dev-fee", help="按签订年份和公司复制已核对的开发费合同")
    folder_export.add_argument("--output", type=Path, default=PROJECT_ROOT / "开发费合同归档")
    restorer = commands.add_parser("restore", help="恢复到新的空目录")
    restorer.add_argument("archive", type=Path)
    restorer.add_argument("--target", type=Path, required=True)
    commands.add_parser("verify", help="检查数据库和原件完整性")
    reset = commands.add_parser("reset-password", help="在服务主机重设账号密码")
    reset.add_argument("username")
    migrate = commands.add_parser("migrate", help="建新数据模型并把旧数据幂等平移过去")
    migrate.add_argument("--dry-run", action="store_true", help="只检查，不写入任何改动")
    rollback = commands.add_parser("rollback-model", help="删除新数据模型，回到迁移前状态")
    rollback.add_argument("--expect-version", type=int, default=None, help="仅当当前模型版本一致时才回滚")
    rollback.add_argument("--yes", action="store_true", help="跳过确认")
    args = parser.parse_args()
    root = data_dir()
    if args.command == "restore":
        restore_backup(args.archive.resolve(), args.target)
        print(f"已校验并恢复到：{args.target.resolve()}")
        return
    ensure_data_dirs(root)
    init_db(root)
    if args.command == "migrate":
        report = Migration(root).run(dry_run=args.dry_run)
        mode = "检查（未写入）" if report["dry_run"] else "已执行"
        print(f"[{mode}] 运行编号 {report['run_id']}，模型版本 {report['version_before']} → {report['version_after']}")
        for name in report["applied"]:
            print(f"  新建步骤：{name}")
        for key, value in report["stats"].items():
            print(f"  {key}：{value}")
        counts = report["issue_counts"]
        print(f"  待处理事项：阻断 {counts['block']}，提醒 {counts['warn']}，说明 {counts['info']}")
        for item in report["issues"]:
            if item["severity"] in ("block", "warn"):
                print(f"  [{item['severity']}] {item['code']} {item['subject']}：{item['detail']}")
        if report["problems"]:
            print("一致性自检未通过：")
            for problem in report["problems"]:
                print(f"  - {problem}")
            raise SystemExit(1)
        print("一致性自检通过。")
        if report["dry_run"]:
            print("这是检查模式，数据库未发生任何改动。去掉 --dry-run 才会真正写入。")
        else:
            print("如需回到迁移前状态：python -m contractdb rollback-model --expect-version 1")
        return
    if args.command == "rollback-model":
        if not args.yes:
            raise SystemExit("回滚会删除新数据模型（旧合同、旧流水、原件不受影响）。确认请加 --yes")
        result = rollback(root, confirm_version=args.expect_version)
        print(f"已移除数据模型版本 {result['version_removed']}，删除表：")
        for table, count in result["dropped"].items():
            print(f"  {table}（{count} 行）")
        print("旧表 documents / finance_records / payment_confirmations 与原件目录未被改动。")
        return
    if args.command == "serve":
        from waitress import serve
        from .web import create_app
        app = create_app()
        app.config.update(SERVICE_PORT=args.port, SERVICE_HOST=args.host)
        print(f"本机地址：http://127.0.0.1:{args.port}", flush=True)
        addresses = sorted({item[4][0] for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET) if not item[4][0].startswith("127.")})
        if args.host == "0.0.0.0":
            for address in addresses:
                print(f"局域网候选地址：http://{address}:{args.port}", flush=True)
        print(f"数据库与原件目录：{root}\n首次访问请在本机创建管理员。服务运行期间请保持此进程开启。", flush=True)
        serve(app, host=args.host, port=args.port, threads=6, max_request_body_size=101 * 1024 * 1024)
    elif args.command == "import-pdfs":
        queue = ExtractionQueue(root, synchronous=True)
        for filename, status in import_existing(root, args.source.resolve(), queue):
            print(f"{status}：{filename}", flush=True)
    elif args.command == "backup":
        archive = create_backup(root, keep=args.keep)
        print(f"备份已生成：{archive}")
        for stale in prune_backups(root, args.keep):
            print(f"已清理旧备份：{stale.name}")
    elif args.command == "export-dev-fee":
        result = export_dev_fee_folders(root, args.output)
        print(f"导出目录：{args.output.resolve()}")
        print(f"已归类开发费合同 {result['exported']} 份；待核对合同 {result['pending']} 份，其中 {result['pending_clues']} 份含开发费线索。")
    elif args.command == "verify":
        with connect(root) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            rows = connection.execute("SELECT id, stored_filename, sha256 FROM documents").fetchall()
        failures = []
        for row in rows:
            path = root / "originals" / row["stored_filename"]
            if not path.is_file() or sha256_file(path) != row["sha256"]:
                failures.append(row["id"])
        print(json.dumps({"database": integrity, "documents": len(rows), "invalid_originals": failures}, ensure_ascii=False))
        if integrity != "ok" or failures:
            raise SystemExit(1)
    elif args.command == "reset-password":
        from .web import password_hasher
        with connect(root) as connection:
            user = connection.execute("SELECT id FROM users WHERE username = ?", (args.username,)).fetchone()
        if not user:
            raise SystemExit("账号不存在")
        password = getpass.getpass("新密码（至少 10 位）：")
        if len(password) < 10 or password != getpass.getpass("再次输入："):
            raise SystemExit("密码不足 10 位或两次输入不一致")
        with transaction(root) as connection:
            connection.execute("UPDATE users SET password_hash = ?, session_version = session_version + 1 WHERE id = ?", (password_hasher.hash(password), user["id"]))
            audit(connection, "user_update", details={"username": args.username, "local_password_reset": True})
        print("密码已重设，该账号需要重新登录。")


if __name__ == "__main__":
    main()
