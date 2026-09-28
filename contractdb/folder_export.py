"""Create a readable copy of confirmed development-fee contracts."""

from __future__ import annotations

import csv
import os
import re
import shutil
import tempfile
from pathlib import Path

from .backup import sha256_file
from .db import connect


INVALID_WINDOWS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED_WINDOWS_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_name(value: str, *, limit: int = 100) -> str:
    name = INVALID_WINDOWS_CHARS.sub("_", value).strip(" .")[:limit].rstrip(" .")
    if not name or name.upper() in RESERVED_WINDOWS_NAMES:
        name = f"_{name or '未命名'}"
    return name


def csv_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return f"'{text}" if text.lstrip().startswith(("=", "+", "-", "@")) else text


def write_csv(path: Path, headers: list[str], rows: list[list[object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8-sig", newline="", dir=path.parent,
                                     suffix=".partial", delete=False) as handle:
        temporary = Path(handle.name)
        writer = csv.writer(handle)
        writer.writerow(headers)
        for row in rows:
            writer.writerow([csv_cell(value) for value in row])
    temporary.replace(path)


def export_dev_fee_folders(data_root: Path, output_root: Path) -> dict[str, int]:
    data_root = data_root.resolve()
    output_root = output_root.resolve()
    if output_root == data_root or data_root in output_root.parents:
        raise ValueError("导出目录不能放在数据库数据目录内。")
    with connect(data_root) as connection:
        rows = connection.execute("""
            SELECT d.id, d.original_filename, d.stored_filename, d.sha256, d.signed_date,
                   d.review_status, d.extraction_status, d.extracted_text, d.archived_at,
                   c.id AS company_id, c.name AS company_name, s.name AS subtype_name
            FROM documents d
            LEFT JOIN companies c ON c.id = d.company_id
            LEFT JOIN subtypes s ON s.id = d.subtype_id
            ORDER BY d.signed_date, c.name, d.original_filename, d.id
        """).fetchall()

    confirmed = [row for row in rows if row["review_status"] == "verified" and row["subtype_name"] == "开发费"]
    pending = [row for row in rows if row["review_status"] == "pending"]
    output_root.mkdir(parents=True, exist_ok=True)
    exported: list[list[object]] = []
    company_folders: dict[str, int] = {}
    for row in confirmed:
        source = data_root / "originals" / row["stored_filename"]
        if not source.is_file() or sha256_file(source) != row["sha256"]:
            raise ValueError(f"原件缺失或校验失败：{row['id']}")
        year = row["signed_date"][:4] if row["signed_date"] else "签订日期待补充"
        company = safe_name(row["company_name"] or "公司待补充")
        if row["company_name"]:
            previous_id = company_folders.setdefault(company, row["company_id"])
            if previous_id != row["company_id"]:
                company = f"{company}__{row['company_id']}"
        filename = f"{safe_name(Path(row['original_filename']).stem, limit=120)}__{row['id'][:8]}{Path(row['original_filename']).suffix.lower()}"
        relative = Path(year) / company / filename
        target = output_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if sha256_file(target) != row["sha256"]:
                raise ValueError(f"目标文件与数据库原件不一致，请先核对：{target}")
        else:
            with tempfile.NamedTemporaryFile("wb", dir=target.parent, suffix=".partial", delete=False) as handle:
                temporary = Path(handle.name)
                with source.open("rb") as input_file:
                    shutil.copyfileobj(input_file, handle)
            try:
                if sha256_file(temporary) != row["sha256"]:
                    raise ValueError(f"复制后校验失败：{row['id']}")
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        exported.append([row["id"], year, row["company_name"] or "公司待补充", row["signed_date"],
                         row["original_filename"], str(relative), row["sha256"], "是" if row["archived_at"] else "否"])

    pending_rows: list[list[object]] = []
    clue_count = 0
    for row in pending:
        filename_clue = "开发费" in row["original_filename"]
        text_clue = "开发费" in row["extracted_text"]
        if filename_clue or text_clue:
            clue_count += 1
        pending_rows.append([row["id"], row["original_filename"], row["signed_date"],
                             row["company_name"], row["subtype_name"], row["extraction_status"],
                             "是" if filename_clue else "否", "是" if text_clue else "否"])
    pending_rows.sort(key=lambda row: (row[6] != "是" and row[7] != "是", row[1]))
    write_csv(output_root / "已归类开发费清单.csv",
              ["记录编号", "签订年份", "合同相对方公司", "签订日期", "原文件名", "归档相对路径", "SHA256", "已归档记录"], exported)
    write_csv(output_root / "待核对合同清单.csv",
              ["记录编号", "原文件名", "签订日期", "合同相对方公司", "当前子类别", "提取状态", "文件名含开发费", "已提取正文含开发费"], pending_rows)
    return {"exported": len(exported), "pending": len(pending), "pending_clues": clue_count}
