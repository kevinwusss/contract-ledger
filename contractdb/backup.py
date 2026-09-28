from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from .db import connect, now

# create_backup 自动生成的文件名：contracts-20260928T015911Z.zip
_AUTO_BACKUP = re.compile(r"^contracts-\d{8}T\d{6}Z\.zip$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prune_backups(root: Path, keep: int) -> list[Path]:
    """只保留最近 keep 个自动备份，返回被删掉的。

    备份里含有全部原件，一份就是上百 MB。便携版每次安全停止都会做一份，U 盘很容易
    被塞满，所以给它一个上限。默认不改桌面版的行为：调用方不传 keep 就不清理。

    只动 create_backup 自动生成的那种文件名（contracts-<时间戳>.zip），手工改名或
    另存的备份不碰——那些通常是有人特意留的。
    """
    if keep <= 0:
        return []
    directory = root / "backups"
    if not directory.is_dir():
        return []
    candidates = sorted(
        (p for p in directory.glob("contracts-*.zip") if _AUTO_BACKUP.match(p.name)),
        key=lambda p: p.name,
    )
    removed = []
    for path in candidates[:-keep]:
        path.unlink(missing_ok=True)
        removed.append(path)
    return removed


def create_backup(root: Path, output: Path | None = None, keep: int = 0) -> Path:
    stamp = now().replace(":", "").replace("+0000", "Z")
    output = output or root / "backups" / f"contracts-{stamp}.zip"
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root / "tmp") as temporary:
        snapshot = Path(temporary) / "contracts.sqlite3"
        source = connect(root)
        target = sqlite3.connect(snapshot)
        try:
            source.backup(target)
        finally:
            source.close()
            target.close()
        connection = sqlite3.connect(snapshot)
        try:
            rows = connection.execute("SELECT stored_filename, sha256 FROM documents").fetchall()
        finally:
            connection.close()
        manifest = {"format": 1, "created_at": now(), "files": {"contracts.sqlite3": sha256_file(snapshot)}}
        temporary_output = output.with_suffix(".partial")
        try:
            with zipfile.ZipFile(temporary_output, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.write(snapshot, "contracts.sqlite3")
                for filename, expected_hash in rows:
                    path = root / "originals" / filename
                    if not path.is_file() or sha256_file(path) != expected_hash:
                        raise ValueError(f"原件缺失或校验失败：{filename}")
                    name = f"originals/{filename}"
                    archive.write(path, name)
                    manifest["files"][name] = expected_hash
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            temporary_output.replace(output)
        except Exception:
            temporary_output.unlink(missing_ok=True)
            raise
    return output


def restore_backup(archive_path: Path, target: Path) -> None:
    target = target.resolve()
    if target.exists() and any(target.iterdir()):
        raise ValueError("恢复目录必须不存在或为空，避免覆盖现有数据库")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as temporary:
        staging = Path(temporary)
        with zipfile.ZipFile(archive_path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            if manifest.get("format") != 1 or "contracts.sqlite3" not in manifest.get("files", {}):
                raise ValueError("备份格式不正确")
            expected_names = set(manifest["files"]) | {"manifest.json"}
            if len(archive.namelist()) != len(expected_names) or set(archive.namelist()) != expected_names:
                raise ValueError("备份文件清单不一致")
            for name, expected_hash in manifest["files"].items():
                relative = PurePosixPath(name)
                if relative.is_absolute() or ".." in relative.parts or "\\" in name or ":" in name:
                    raise ValueError("备份中包含不安全的路径")
                if name != "contracts.sqlite3" and not (len(relative.parts) == 2 and relative.parts[0] == "originals"):
                    raise ValueError("备份包含不支持的文件")
                path = staging.joinpath(*relative.parts)
                path.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(name) as source, path.open("wb") as output:
                    import shutil
                    shutil.copyfileobj(source, output)
                if sha256_file(path) != expected_hash:
                    raise ValueError(f"备份校验失败：{name}")
        connection = sqlite3.connect(staging / "contracts.sqlite3")
        try:
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("数据库完整性检查未通过")
            rows = connection.execute("SELECT stored_filename, sha256 FROM documents").fetchall()
            for filename, digest in rows:
                if manifest["files"].get(f"originals/{filename}") != digest:
                    raise ValueError("数据库与原件清单不匹配")
        finally:
            connection.close()
        target.mkdir(exist_ok=True)
        for child in staging.iterdir():
            child.replace(target / child.name)
        for name in ("tmp", "backups", "originals"):
            (target / name).mkdir(exist_ok=True)
