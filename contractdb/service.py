from __future__ import annotations

import hashlib
import io
import json
import shutil
import sqlite3
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import BinaryIO

from PIL import Image
from pypdf import PdfReader

from .config import ALLOWED_EXTENSIONS, MIME_TYPES, MAX_UPLOAD_BYTES
from .db import audit, connect, now, transaction
from .extract import extract_document


class UploadError(ValueError):
    pass


class DuplicateError(UploadError):
    def __init__(self, document_id: str):
        self.document_id = document_id
        super().__init__("该文件已在合同库中")


def clean_filename(filename: str) -> str:
    return filename.replace("\\", "/").split("/")[-1].strip()[:240]


def check_file(path: Path, suffix: str) -> None:
    if suffix == ".pdf":
        with path.open('rb') as stream:
            header = stream.read(5)
        if header != b"%PDF-":
            raise UploadError("文件内容不是有效的 PDF")
        try:
            reader = PdfReader(path, strict=False)
            if reader.is_encrypted:
                raise UploadError("PDF 已加密，请先使用原件密码解密后上传")
            if not len(reader.pages):
                raise UploadError("PDF 没有可读取的页面")
        except UploadError:
            raise
        except Exception as exc:
            raise UploadError("PDF 结构损坏或无法读取") from exc
    elif suffix == ".docx":
        if not zipfile.is_zipfile(path):
            raise UploadError("文件内容不是有效的 Word 文档")
        with zipfile.ZipFile(path) as archive:
            if len(archive.infolist()) > 10000 or sum(item.file_size for item in archive.infolist()) > 200 * 1024 * 1024:
                raise UploadError("Word 文档解压后过大，请缩小附件或图片后上传")
            if "word/document.xml" not in archive.namelist():
                raise UploadError("Word 文档缺少正文")
    else:
        try:
            with Image.open(path) as image:
                image.verify()
        except Exception as exc:
            raise UploadError("图片文件无法读取") from exc


def ingest(
    root: Path,
    stream: BinaryIO,
    original_filename: str,
    *,
    user_id: int | None = None,
    source_note: str | None = None,
) -> str:
    filename = clean_filename(original_filename)
    suffix = Path(filename).suffix.lower()
    if not filename or suffix not in ALLOWED_EXTENSIONS:
        raise UploadError("只支持 PDF、DOCX、PNG、JPG 和 TIFF 文件")
    identifier = uuid.uuid4().hex
    temporary = root / "tmp" / f"{identifier}.upload"
    digest = hashlib.sha256()
    size = 0
    target = root / "originals" / f"{identifier}{suffix}"
    committed = False
    try:
        with temporary.open("wb") as output:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise UploadError("文件超过 100 MB 上限")
                digest.update(chunk)
                output.write(chunk)
        if not size:
            raise UploadError("文件为空")
        check_file(temporary, suffix)
        sha256 = digest.hexdigest()
        stored_filename = f"{identifier}{suffix}"
        with transaction(root) as connection:
            duplicate = connection.execute(
                "SELECT id FROM documents WHERE sha256 = ? AND archived_at IS NULL", (sha256,)
            ).fetchone()
            if duplicate:
                raise DuplicateError(duplicate["id"])
            temporary.replace(target)
            stamp = now()
            default_type = connection.execute("SELECT id FROM import_types WHERE name = '合同'").fetchone()
            connection.execute(
                """INSERT INTO documents
                   (id, original_filename, stored_filename, sha256, mime_type, file_size,
                    type_id, source_note, created_by, updated_by, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (identifier, filename, stored_filename, sha256, MIME_TYPES[suffix], size,
                 default_type["id"] if default_type else None, source_note, user_id, user_id, stamp, stamp),
            )
            audit(connection, "import", document_id=identifier, user_id=user_id,
                  details={"original_filename": filename, "sha256": sha256, "source_note": source_note})
        committed = True
        return identifier
    except sqlite3.IntegrityError as exc:
        if target.exists():
            target.unlink()
        with connect(root) as connection:
            duplicate = connection.execute(
                "SELECT id FROM documents WHERE sha256 = ? AND archived_at IS NULL", (digest.hexdigest(),)
            ).fetchone()
        if duplicate:
            raise DuplicateError(duplicate["id"]) from exc
        raise
    finally:
        temporary.unlink(missing_ok=True)
        if not committed:
            target.unlink(missing_ok=True)


def process_extraction(root: Path, document_id: str) -> None:
    with transaction(root) as connection:
        row = connection.execute(
            "SELECT stored_filename FROM documents WHERE id = ?", (document_id,)
        ).fetchone()
        if not row:
            return
        connection.execute(
            "UPDATE documents SET extraction_status = 'processing', extraction_error = NULL WHERE id = ?",
            (document_id,),
        )
    try:
        text, candidates = extract_document(root / "originals" / row["stored_filename"])
        with transaction(root) as connection:
            connection.execute(
                """UPDATE documents SET extraction_status = 'ready', extraction_error = NULL,
                   extracted_text = ?, candidates_json = ? WHERE id = ?""",
                (text, json.dumps(candidates, ensure_ascii=False), document_id),
            )
            audit(connection, "extract", document_id=document_id,
                  details={"candidate_count": sum(map(len, candidates.values()))})
    except Exception as exc:
        with transaction(root) as connection:
            connection.execute(
                "UPDATE documents SET extraction_status = 'error', extraction_error = ? WHERE id = ?",
                (str(exc)[:400], document_id),
            )
            audit(connection, "extract_error", document_id=document_id, details={"error": str(exc)[:400]})


class ExtractionQueue:
    def __init__(self, root: Path, *, synchronous: bool = False):
        self.root = root
        self.synchronous = synchronous
        self.executor = None if synchronous else ThreadPoolExecutor(max_workers=1, thread_name_prefix="extract")

    def submit(self, document_id: str) -> None:
        if self.synchronous:
            process_extraction(self.root, document_id)
        else:
            assert self.executor is not None
            self.executor.submit(process_extraction, self.root, document_id)

    def resume(self) -> None:
        with connect(self.root) as connection:
            rows = connection.execute(
                "SELECT id FROM documents WHERE extraction_status IN ('queued', 'processing')"
            ).fetchall()
        for row in rows:
            self.submit(row["id"])


def import_existing(root: Path, source_dir: Path, queue: ExtractionQueue) -> list[tuple[str, str]]:
    results = []
    for path in sorted(source_dir.glob("*.pdf")):
        try:
            with path.open("rb") as stream:
                document_id = ingest(root, stream, path.name, source_note=str(path.resolve()))
            queue.submit(document_id)
            results.append((path.name, "已导入，待核对"))
        except DuplicateError:
            results.append((path.name, "已存在，未重复导入"))
    return results
