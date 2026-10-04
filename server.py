from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import xml.etree.ElementTree as ET
import zipfile
from contextlib import closing, contextmanager
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import quote, unquote, urlparse
from uuid import uuid4

SOURCE_ROOT = Path(__file__).resolve().parent
RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", SOURCE_ROOT))
LOCAL_DATA = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "PlasmidLibrary"
DB_PATH = LOCAL_DATA / "library.sqlite3"
LOG_PATH = LOCAL_DATA / "logs" / "Plasmora.log"
APP_VERSION = "0.8.0"
PROJECT_URL = "https://github.com/Threon1007-SU/Plasmora"
SETTINGS_DIR = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "PlasmidLibrary"
SETTINGS_PATH = SETTINGS_DIR / "settings.json"
DEFAULT_STORAGE = Path.home() / "Documents" / "Plasmora" / "原件"
STORAGE_ROOT = DEFAULT_STORAGE
LEGACY_ROOT = None
LOCK = threading.RLock()
LOGGER = logging.getLogger("plasmora")
SORT_ORDERS = {"name_asc", "name_desc", "import_new", "import_old", "size_large", "size_small"}
CLOSE_BEHAVIORS = {"ask", "tray", "quit"}


def init_logging():
    if LOGGER.handlers:
        return
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(LOG_PATH, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)


def report_progress(callback, phase, current, total, message=""):
    if callback:
        callback({"phase": phase, "current": current, "total": total, "message": message})


class OperationCancelled(Exception):
    pass


def check_cancel(cancel):
    if cancel and cancel():
        raise OperationCancelled("操作已取消；已完成的步骤会保留")


def get_close_behavior():
    with db() as c:
        row = c.execute("SELECT value FROM app_settings WHERE key='close_behavior'").fetchone()
    value = row["value"] if row else "ask"
    return value if value in CLOSE_BEHAVIORS else "ask"


def set_close_behavior(value):
    if value not in CLOSE_BEHAVIORS:
        raise ValueError("未知的关闭行为")
    with db() as c:
        c.execute("INSERT INTO app_settings(key,value) VALUES('close_behavior',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (value,))
    return value


def load_legacy_settings():
    global LEGACY_ROOT
    try:
        settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        old_path = settings.get("repository")
        if old_path:
            candidate = Path(old_path).expanduser().resolve()
            if candidate.is_dir():
                LEGACY_ROOT = candidate
    except (OSError, ValueError, TypeError):
        pass


@contextmanager
def db():
    LOCAL_DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def trash_db():
    LOCAL_DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(LOCAL_DATA / "trash.sqlite3", timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_trash():
    (LOCAL_DATA / "Trash").mkdir(parents=True, exist_ok=True)
    with trash_db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS trash_items (
            id INTEGER PRIMARY KEY,
            deleted_at TEXT NOT NULL,
            reason TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            trash_path TEXT NOT NULL
        )""")


def _archive_plasmid(c, item_id, reason):
    row = c.execute("SELECT * FROM library_plasmids WHERE id=?", (item_id,)).fetchone()
    if not row:
        raise FileNotFoundError("仓库中已没有这条质粒记录")
    source = Path(row["storage_path"])
    if not source.is_file():
        raise FileNotFoundError("原件文件不存在，无法保留可恢复副本")
    metadata = dict(row)
    metadata["tags"] = [dict(tag) for tag in c.execute(
        "SELECT tag,tag_kind FROM plasmid_tags WHERE plasmid_id=?", (item_id,))]
    metadata["groups"] = [group[0] for group in c.execute(
        "SELECT g.name FROM groups g JOIN plasmid_groups pg ON pg.group_id=g.id WHERE pg.plasmid_id=?", (item_id,))]
    folder = LOCAL_DATA / "Trash"
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / f"{uuid4().hex}.dna"
    try:
        with source.open("rb") as original, destination.open("xb") as output:
            digest, size = _copy_with_hash(original, output)
        if digest != row["sha256"] or size != row["file_size"]:
            raise ValueError("原件已在仓库外修改，请先同步后重试")
        with trash_db() as trash:
            entry_id = trash.execute(
                "INSERT INTO trash_items(deleted_at,reason,metadata_json,trash_path) VALUES(?,?,?,?)",
                (datetime.now().isoformat(timespec="seconds"), reason,
                 json.dumps(metadata, ensure_ascii=False), str(destination)),
            ).lastrowid
        return entry_id, destination
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def get_trash():
    init_trash()
    with trash_db() as c:
        rows = c.execute("SELECT id,deleted_at,reason,metadata_json FROM trash_items ORDER BY id DESC").fetchall()
    return [{"id": row["id"], "deletedAt": row["deleted_at"], "reason": row["reason"],
             "name": json.loads(row["metadata_json"])["file_name"]} for row in rows]


def restore_trash(entry_id):
    with LOCK:
        with trash_db() as trash:
            entry = trash.execute("SELECT * FROM trash_items WHERE id=?", (entry_id,)).fetchone()
        if not entry:
            raise FileNotFoundError("回收站中没有这条记录")
        metadata = json.loads(entry["metadata_json"])
        source = Path(entry["trash_path"])
        if not source.is_file() or (LOCAL_DATA / "Trash").resolve() not in source.resolve().parents:
            raise FileNotFoundError("回收站中的质粒文件不存在")
        STORAGE_ROOT.mkdir(parents=True, exist_ok=True)
        with db() as c:
            name = metadata["file_name"]
            if c.execute("SELECT 1 FROM library_plasmids WHERE file_name=? COLLATE NOCASE", (name,)).fetchone():
                name = _numbered_import_name(c, name)
            stored_name = safe_storage_name(name, STORAGE_ROOT)
            target = STORAGE_ROOT / stored_name
            try:
                with source.open("rb") as original, target.open("xb") as output:
                    digest, size = _copy_with_hash(original, output)
                if digest != metadata["sha256"] or size != metadata["file_size"]:
                    raise ValueError("回收站文件校验失败")
                old_id = metadata["id"]
                reuse_id = not c.execute("SELECT 1 FROM library_plasmids WHERE id=?", (old_id,)).fetchone()
                columns = "file_name,stored_name,storage_path,sha256,file_size,imported_at,note,favorite,last_viewed_at,file_mtime_ns"
                values = (name, stored_name, str(target), digest, size, metadata["imported_at"],
                          metadata.get("note", ""), metadata.get("favorite", 0),
                          metadata.get("last_viewed_at"), target.stat().st_mtime_ns)
                if reuse_id:
                    item_id = c.execute(f"INSERT INTO library_plasmids(id,{columns}) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                        (old_id, *values)).lastrowid
                else:
                    item_id = c.execute(f"INSERT INTO library_plasmids({columns}) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                        values).lastrowid
                c.executemany("INSERT INTO plasmid_tags(plasmid_id,tag,tag_kind) VALUES(?,?,?)",
                              [(item_id, tag["tag"], tag["tag_kind"]) for tag in metadata["tags"]])
                save_primer_index(c, item_id, parse_dna(target))
                for group_name in metadata["groups"]:
                    group = c.execute("SELECT id FROM groups WHERE name=?", (group_name,)).fetchone()
                    group_id = group["id"] if group else c.execute(
                        "INSERT INTO groups(name,created_at) VALUES(?,?)",
                        (group_name, datetime.now().isoformat(timespec="seconds"))).lastrowid
                    c.execute("INSERT OR IGNORE INTO plasmid_groups(plasmid_id,group_id) VALUES(?,?)", (item_id, group_id))
            except Exception:
                target.unlink(missing_ok=True)
                raise
        with trash_db() as trash:
            trash.execute("DELETE FROM trash_items WHERE id=?", (entry_id,))
        source.unlink(missing_ok=True)
        return {"id": item_id, "name": name}


def purge_trash(entry_id):
    with LOCK, trash_db() as trash:
        entry = trash.execute("SELECT trash_path FROM trash_items WHERE id=?", (entry_id,)).fetchone()
        if not entry:
            raise FileNotFoundError("回收站中没有这条记录")
        path = Path(entry["trash_path"])
        if (LOCAL_DATA / "Trash").resolve() not in path.resolve().parents:
            raise ValueError("回收站路径无效")
        if path.exists():
            path.unlink()
        trash.execute("DELETE FROM trash_items WHERE id=?", (entry_id,))


def parse_dna(path: Path):
    before = path.stat()
    raw = path.read_bytes()
    after = path.stat()
    if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
        raise ValueError("文件仍在写入，请稍后重试")
    pos = 0
    feature_xml = None
    primer_xml = None
    sequence = ""
    circular = False
    if len(raw) < 19 or raw[5:13] != b"SnapGene":
        raise ValueError("不是可识别的 SnapGene .dna 文件")
    while pos + 5 <= len(raw):
        tag = raw[pos]
        size = int.from_bytes(raw[pos + 1:pos + 5], "big")
        start, end = pos + 5, pos + 5 + size
        if end > len(raw):
            raise ValueError("文件数据不完整")
        data = raw[start:end]
        if tag == 0x00 and data:
            circular = bool(data[0] & 0x01)
            sequence = data[1:].decode("ascii", errors="ignore").upper()
        elif tag == 0x0A:
            feature_xml = data
        elif tag == 0x05:
            primer_xml = data
        pos = end
    if not sequence:
        raise ValueError("文件中没有 DNA 序列")
    features = []
    if feature_xml:
        root = ET.fromstring(feature_xml)
        for node in root.findall(".//Feature"):
            qualifiers = {}
            for q in node.findall("Q"):
                v = q.find("V")
                if v is None:
                    continue
                value = v.get("text") or v.get("int") or v.get("predef") or ""
                value = re.sub(r"<[^>]*>", " ", html.unescape(value))
                value = re.sub(r"\s+", " ", value).strip()
                if value:
                    qualifiers.setdefault(q.get("name", ""), []).append(value)
            segments = []
            for seg in node.findall("Segment"):
                match = re.match(r"(\d+)-(\d+)", seg.get("range", ""))
                if match:
                    segments.append([int(match.group(1)), int(match.group(2))])
            features.append({
                "name": node.get("name", "Unnamed feature"),
                "type": node.get("type", "misc_feature"),
                "direction": int(node.get("directionality", "0") or 0),
                "segments": segments,
                "qualifiers": qualifiers,
            })
    digest = hashlib.sha256(raw).hexdigest()
    tags = extract_tags(features)
    primers = parse_primers(primer_xml, len(sequence)) if primer_xml else []
    return {"sequence": sequence, "circular": circular, "features": features, "primers": primers,
            "sha256": digest, "tags": tags, "file_size": len(raw), "file_mtime_ns": after.st_mtime_ns}


def parse_primers(xml, sequence_length):
    """Read saved primers; display binding coordinates as 1-based inclusive ranges."""
    root = ET.fromstring(xml)

    def text(value):
        return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", html.unescape(value or ""))).strip()

    def number(value):
        try:
            result = float(value)
            return result if float('-inf') < result < float('inf') else None
        except (TypeError, ValueError):
            return None

    params = root.find(".//HybridizationParams")
    minimum_length = number(params.get("minContinuousMatchLen")) if params is not None else None
    minimum_tm = number(params.get("minMeltingTemperature")) if params is not None else None
    primers = []
    for node in root.findall(".//Primer"):
        sequence = re.sub(r"\s+", "", node.get("sequence", ""))
        sites, seen = [], set()
        for site in node.findall(".//BindingSite"):
            match = re.fullmatch(r"(\d+)-(\d+)", site.get("location", ""))
            if not match:
                continue
            # Unlike Feature ranges, saved primer ranges use zero-based inclusive positions.
            start, end = (int(value) + 1 for value in match.groups())
            if not (1 <= start <= sequence_length and 1 <= end <= sequence_length):
                continue
            annealed = re.sub(r"\s+", "", site.get("annealedBases", ""))
            tm = number(site.get("meltingTemperature"))
            if minimum_length is not None and annealed and len(annealed) < minimum_length:
                continue
            if minimum_tm is not None and tm is not None and tm < minimum_tm:
                continue
            strand = -1 if site.get("boundStrand") == "1" else 1
            key = (start, end, strand)
            if key in seen:
                continue
            seen.add(key)
            sites.append({"start": start, "end": end, "strand": strand,
                          "annealedSequence": annealed, "meltingTemperature": tm})
        upper = sequence.upper()
        gc = round(100 * (upper.count("G") + upper.count("C")) / len(upper), 1) if upper and set(upper) <= set("ACGT") else None
        primers.append({"name": text(node.get("name")), "sequence": sequence, "length": len(sequence),
                        "gcPercent": gc, "description": text(node.get("description")), "bindingSites": sites})
    return primers


def init_primer_index(c):
    columns = {row[1] for row in c.execute("PRAGMA table_info(library_plasmids)")}
    if "primer_indexed" not in columns:
        c.execute("ALTER TABLE library_plasmids ADD COLUMN primer_indexed INTEGER NOT NULL DEFAULT 0")
    c.execute("""CREATE TABLE IF NOT EXISTS plasmid_primer_names (
        plasmid_id INTEGER NOT NULL REFERENCES library_plasmids(id) ON DELETE CASCADE,
        name TEXT NOT NULL COLLATE NOCASE,
        PRIMARY KEY (plasmid_id, name)
    )""")


def save_primer_index(c, item_id, parsed):
    c.execute("DELETE FROM plasmid_primer_names WHERE plasmid_id=?", (item_id,))
    c.executemany("INSERT OR IGNORE INTO plasmid_primer_names(plasmid_id,name) VALUES(?,?)",
                  [(item_id, primer["name"]) for primer in parsed["primers"] if primer["name"]])
    c.execute("UPDATE library_plasmids SET primer_indexed=1 WHERE id=?", (item_id,))


def extract_tags(features):
    tags = {}
    for feature in features:
        name = feature.get("name", "").strip()
        if name:
            tags.setdefault(name.casefold(), {"tag": name, "kind": "feature"})
        for key in ("gene", "label", "product", "locus_tag", "standard_name"):
            for value in feature.get("qualifiers", {}).get(key, []):
                value = re.sub(r"\s+", " ", str(value)).strip()
                if value:
                    tags.setdefault(value.casefold(), {"tag": value, "kind": key})
    return list(tags.values())


def init_db():
    global STORAGE_ROOT
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS library_plasmids (
            id INTEGER PRIMARY KEY,
            file_name TEXT NOT NULL,
            stored_name TEXT NOT NULL,
            storage_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            imported_at TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            favorite INTEGER NOT NULL DEFAULT 0,
            last_viewed_at TEXT,
            file_mtime_ns INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS plasmid_tags (
            plasmid_id INTEGER NOT NULL REFERENCES library_plasmids(id) ON DELETE CASCADE,
            tag TEXT NOT NULL COLLATE NOCASE,
            tag_kind TEXT NOT NULL,
            PRIMARY KEY (plasmid_id, tag, tag_kind)
        );
        CREATE INDEX IF NOT EXISTS idx_plasmid_tags_tag ON plasmid_tags(tag COLLATE NOCASE);
        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS plasmid_groups (
            plasmid_id INTEGER NOT NULL REFERENCES library_plasmids(id) ON DELETE CASCADE,
            group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
            PRIMARY KEY (plasmid_id, group_id)
        );
        CREATE TABLE IF NOT EXISTS synonym_clusters (
            id INTEGER PRIMARY KEY
        );
        CREATE TABLE IF NOT EXISTS synonym_terms (
            id INTEGER PRIMARY KEY,
            cluster_id INTEGER NOT NULL REFERENCES synonym_clusters(id) ON DELETE CASCADE,
            term TEXT NOT NULL,
            term_key TEXT NOT NULL UNIQUE
        );
        CREATE INDEX IF NOT EXISTS idx_synonym_terms_cluster ON synonym_terms(cluster_id);
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """)
        plasmid_columns = {row["name"] for row in c.execute("PRAGMA table_info(library_plasmids)")}
        if "note" not in plasmid_columns:
            c.execute("ALTER TABLE library_plasmids ADD COLUMN note TEXT NOT NULL DEFAULT ''")
        if "favorite" not in plasmid_columns:
            c.execute("ALTER TABLE library_plasmids ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0")
        if "last_viewed_at" not in plasmid_columns:
            c.execute("ALTER TABLE library_plasmids ADD COLUMN last_viewed_at TEXT")
        if "file_mtime_ns" not in plasmid_columns:
            c.execute("ALTER TABLE library_plasmids ADD COLUMN file_mtime_ns INTEGER NOT NULL DEFAULT 0")
        migrate_plasmid_uniqueness(c)
        init_primer_index(c)
        if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='aliases'").fetchone():
            for row in c.execute("SELECT canonical,alias FROM aliases").fetchall():
                if str(row["canonical"]).strip().casefold() != str(row["alias"]).strip().casefold():
                    save_synonym_cluster(c, [row["canonical"], row["alias"]])
            c.execute("DROP TABLE aliases")
            c.execute("INSERT OR IGNORE INTO app_settings(key,value) VALUES('synonym_seeded','1')")
        if not c.execute("SELECT 1 FROM app_settings WHERE key='synonym_seeded'").fetchone():
            save_synonym_cluster(c, ["ITPR1", "IP3R1"])
            c.execute("INSERT INTO app_settings(key,value) VALUES('synonym_seeded','1')")
        row = c.execute("SELECT value FROM app_settings WHERE key='storage_dir'").fetchone()
        if row:
            STORAGE_ROOT = Path(row["value"]).expanduser()
        else:
            STORAGE_ROOT = DEFAULT_STORAGE
            c.execute("INSERT INTO app_settings(key,value) VALUES('storage_dir',?)", (str(STORAGE_ROOT),))
    STORAGE_ROOT.mkdir(parents=True, exist_ok=True)
    init_trash()
    recover_storage_migration()
    migrate_legacy_database()


def clean_synonym_terms(raw_terms):
    if not isinstance(raw_terms, list):
        raise ValueError("请提供同义词列表")
    terms = {}
    for value in raw_terms:
        if not isinstance(value, str):
            raise ValueError("同义词必须是文字")
        term = value.strip()
        if not term or len(term) > 100 or any(char in term for char in "\r\n"):
            raise ValueError("每个同义词应为 1–100 个字符")
        key = re.sub(r"[\s_-]+", "", term).casefold()
        if not key:
            raise ValueError("同义词需要包含字母或数字")
        terms.setdefault(key, term)
    if not 2 <= len(terms) <= 100:
        raise ValueError("每个集群需要 2–100 个不同的词")
    return terms


def save_synonym_cluster(c, raw_terms, cluster_id=None):
    terms = clean_synonym_terms(raw_terms)
    if cluster_id is not None and not c.execute("SELECT 1 FROM synonym_clusters WHERE id=?", (cluster_id,)).fetchone():
        raise FileNotFoundError("同义词集群不存在")
    keys = list(terms)
    placeholders = ",".join("?" for _ in keys)
    matching = c.execute(f"SELECT DISTINCT cluster_id FROM synonym_terms WHERE term_key IN ({placeholders})", keys).fetchall()
    matched_ids = {row["cluster_id"] for row in matching}
    if cluster_id is None:
        cluster_id = min(matched_ids) if matched_ids else c.execute("INSERT INTO synonym_clusters DEFAULT VALUES").lastrowid
    else:
        c.execute(f"DELETE FROM synonym_terms WHERE cluster_id=? AND term_key NOT IN ({placeholders})", [cluster_id, *keys])
    for other_id in sorted(matched_ids - {cluster_id}):
        c.execute("UPDATE synonym_terms SET cluster_id=? WHERE cluster_id=?", (cluster_id, other_id))
        c.execute("DELETE FROM synonym_clusters WHERE id=?", (other_id,))
    for key, term in terms.items():
        c.execute("INSERT INTO synonym_terms(cluster_id,term,term_key) VALUES(?,?,?) ON CONFLICT(term_key) DO UPDATE SET term=excluded.term", (cluster_id, term, key))
    return cluster_id


def get_synonym_clusters():
    with db() as c:
        rows = c.execute("SELECT c.id,t.term FROM synonym_clusters c JOIN synonym_terms t ON t.cluster_id=c.id ORDER BY c.id,t.term COLLATE NOCASE").fetchall()
    clusters = {}
    for row in rows:
        clusters.setdefault(row["id"], {"id": row["id"], "terms": []})["terms"].append(row["term"])
    return list(clusters.values())


def migrate_plasmid_uniqueness(c):
    unique_indexes = c.execute("PRAGMA index_list(library_plasmids)").fetchall()
    old_hash_constraint = any(
        [column[2] for column in c.execute(f"PRAGMA index_info({index[1]!r})")] == ["sha256"]
        for index in unique_indexes if index[2]
    )
    if old_hash_constraint:
        c.commit()
        c.execute("PRAGMA foreign_keys=OFF")
        try:
            c.execute("BEGIN IMMEDIATE")
            c.execute("""CREATE TABLE library_plasmids_new (
                id INTEGER PRIMARY KEY,
                file_name TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                storage_path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                file_size INTEGER NOT NULL,
                imported_at TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                favorite INTEGER NOT NULL DEFAULT 0,
                last_viewed_at TEXT,
                file_mtime_ns INTEGER NOT NULL DEFAULT 0
            )""")
            columns = "id,file_name,stored_name,storage_path,sha256,file_size,imported_at,note,favorite,last_viewed_at,file_mtime_ns"
            c.execute(f"INSERT INTO library_plasmids_new({columns}) SELECT {columns} FROM library_plasmids")
            c.execute("DROP TABLE library_plasmids")
            c.execute("ALTER TABLE library_plasmids_new RENAME TO library_plasmids")
            if c.execute("PRAGMA foreign_key_check").fetchone():
                raise sqlite3.IntegrityError("质粒数据库迁移后的分组或标签关联无效")
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.execute("PRAGMA foreign_keys=ON")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_library_plasmids_name_hash ON library_plasmids(file_name COLLATE NOCASE,sha256)")


def safe_storage_name(file_name: str, folder: Path):
    base = Path(file_name).name
    stem, suffix = Path(base).stem, Path(base).suffix or ".dna"
    candidate = base
    serial = 2
    while (folder / candidate).exists():
        candidate = f"{stem} ({serial}){suffix}"
        serial += 1
    return candidate


def inspect_import_conflict(source_path):
    source = Path(source_path).expanduser().resolve()
    if source.suffix.lower() != ".dna" or not source.is_file():
        raise ValueError("请选择有效的 .dna 文件")
    parsed = parse_dna(source)
    with db() as c:
        rows = c.execute("SELECT id,file_name,sha256,file_size,imported_at FROM library_plasmids WHERE file_name=? COLLATE NOCASE ORDER BY id", (source.name,)).fetchall()
    if not rows or any(row["sha256"] == parsed["sha256"] for row in rows):
        return None
    return {"incoming": {"name": source.name, "size": parsed["file_size"], "sha256": parsed["sha256"]},
            "existing": [{"id": row["id"], "name": row["file_name"], "size": row["file_size"],
                          "sha256": row["sha256"], "importedAt": row["imported_at"]} for row in rows]}


def _numbered_import_name(c, name):
    stem, suffix = Path(name).stem, Path(name).suffix
    number = 1
    while True:
        candidate = f"{stem} ({number}){suffix}"
        if not c.execute("SELECT 1 FROM library_plasmids WHERE file_name=? COLLATE NOCASE", (candidate,)).fetchone():
            return candidate
        number += 1


def _replace_imported_plasmid(source, parsed, existing, group_id):
    target = Path(existing["storage_path"])
    if STORAGE_ROOT.resolve() not in target.resolve().parents or not target.is_file():
        raise FileNotFoundError("仓库中的原有质粒文件不存在，无法替换")
    fd, temporary_name = tempfile.mkstemp(prefix=".plasmora-import-", suffix=".tmp", dir=STORAGE_ROOT)
    temporary = Path(temporary_name)
    staged = target.with_name(f".plasmora-original-{existing['id']}-{uuid4().hex}.tmp")
    old_staged = False
    trash_id = None
    try:
        with os.fdopen(fd, "wb") as output, source.open("rb") as original:
            digest, size = _copy_with_hash(original, output)
        if digest != parsed["sha256"] or size != parsed["file_size"]:
            raise ValueError("导入文件在读取过程中发生变化，请重试")
        with db() as c:
            trash_id, _ = _archive_plasmid(c, existing["id"], "replaced")
        os.replace(target, staged)
        old_staged = True
        os.replace(temporary, target)
        with db() as c:
            c.execute("UPDATE library_plasmids SET sha256=?,file_size=?,imported_at=?,file_mtime_ns=? WHERE id=?",
                      (digest, size, datetime.now().isoformat(timespec="seconds"), target.stat().st_mtime_ns, existing["id"]))
            c.execute("DELETE FROM plasmid_tags WHERE plasmid_id=?", (existing["id"],))
            c.executemany("INSERT OR IGNORE INTO plasmid_tags(plasmid_id,tag,tag_kind) VALUES(?,?,?)",
                          [(existing["id"], tag["tag"], tag["kind"]) for tag in parsed["tags"]])
            save_primer_index(c, existing["id"], parsed)
            grouped = c.execute("INSERT OR IGNORE INTO plasmid_groups(plasmid_id,group_id) VALUES(?,?)",
                                (existing["id"], group_id)).rowcount if group_id is not None else 0
        try:
            staged.unlink(missing_ok=True)
        except OSError:
            pass
        return {"id": existing["id"], "name": existing["file_name"], "replaced": True, "grouped": grouped,
                "trashId": trash_id}
    except Exception:
        if old_staged:
            target.unlink(missing_ok=True)
            os.replace(staged, target)
        raise
    finally:
        temporary.unlink(missing_ok=True)


def import_one(source_path, group_id=None, on_conflict="copy", existing_id=None):
    source = Path(source_path).expanduser().resolve()
    if source.suffix.lower() != ".dna" or not source.is_file():
        raise ValueError("请选择有效的 .dna 文件")
    parsed = parse_dna(source)
    with LOCK:
        with db() as c:
            if group_id is not None and not c.execute("SELECT 1 FROM groups WHERE id=?", (group_id,)).fetchone():
                raise FileNotFoundError("目标分组不存在")
            duplicate = c.execute("SELECT id,file_name FROM library_plasmids WHERE file_name=? COLLATE NOCASE AND sha256=?", (source.name, parsed["sha256"])).fetchone()
            if duplicate:
                grouped = c.execute("INSERT OR IGNORE INTO plasmid_groups(plasmid_id,group_id) VALUES(?,?)", (duplicate["id"], group_id)).rowcount if group_id is not None else 0
                return {"id": duplicate["id"], "name": duplicate["file_name"], "duplicate": True, "grouped": grouped}
            same_name = c.execute("SELECT id,file_name,storage_path FROM library_plasmids WHERE file_name=? COLLATE NOCASE ORDER BY id", (source.name,)).fetchall()
            if same_name:
                if on_conflict not in {"copy", "keep_existing", "replace"}:
                    raise ValueError("请选择同名文件的处理方式")
                if on_conflict in {"keep_existing", "replace"}:
                    existing = next((row for row in same_name if row["id"] == existing_id), None)
                    if existing is None:
                        raise ValueError("请选择一个仓库中已有的同名质粒")
                    if on_conflict == "keep_existing":
                        grouped = c.execute("INSERT OR IGNORE INTO plasmid_groups(plasmid_id,group_id) VALUES(?,?)", (existing["id"], group_id)).rowcount if group_id is not None else 0
                        return {"id": existing["id"], "name": existing["file_name"], "skipped": True, "grouped": grouped}
                    return _replace_imported_plasmid(source, parsed, existing, group_id)
                name = _numbered_import_name(c, source.name)
            else:
                name = source.name
        STORAGE_ROOT.mkdir(parents=True, exist_ok=True)
        stored_name = safe_storage_name(name, STORAGE_ROOT)
        target = STORAGE_ROOT / stored_name
        created = False
        try:
            with source.open("rb") as original, target.open("xb") as managed:
                created = True
                digest, size = _copy_with_hash(original, managed)
            if digest != parsed["sha256"] or size != parsed["file_size"]:
                raise ValueError("导入文件在读取过程中发生变化，请重试")
            with db() as c:
                duplicate = c.execute("SELECT id,file_name FROM library_plasmids WHERE file_name=? COLLATE NOCASE AND sha256=?", (name, parsed["sha256"])).fetchone()
                if duplicate:
                    grouped = c.execute("INSERT OR IGNORE INTO plasmid_groups(plasmid_id,group_id) VALUES(?,?)", (duplicate["id"], group_id)).rowcount if group_id is not None else 0
                    if created:
                        target.unlink(missing_ok=True)
                    return {"id": duplicate["id"], "name": duplicate["file_name"], "duplicate": True, "grouped": grouped}
                cur = c.execute("INSERT INTO library_plasmids(file_name,stored_name,storage_path,sha256,file_size,imported_at,file_mtime_ns) VALUES(?,?,?,?,?,?,?)",
                                (name, stored_name, str(target), parsed["sha256"], parsed["file_size"],
                                 datetime.now().isoformat(timespec="seconds"), target.stat().st_mtime_ns))
                item_id = cur.lastrowid
                c.executemany("INSERT OR IGNORE INTO plasmid_tags(plasmid_id,tag,tag_kind) VALUES(?,?,?)",
                              [(item_id, t["tag"], t["kind"]) for t in parsed["tags"]])
                save_primer_index(c, item_id, parsed)
                grouped = c.execute("INSERT INTO plasmid_groups(plasmid_id,group_id) VALUES(?,?)", (item_id, group_id)).rowcount if group_id is not None else 0
            return {"id": item_id, "name": name, "duplicate": False, "grouped": grouped}
        except Exception:
            if created:
                target.unlink(missing_ok=True)
            raise


def import_files(paths, group_id=None):
    if group_id is not None and (type(group_id) is not int or group_id <= 0):
        raise ValueError("目标分组无效")
    imported, duplicates, skipped, replaced, errors = [], [], [], [], []
    for path in paths:
        try:
            result = import_one(path, group_id)
            (duplicates if result.get("duplicate") else skipped if result.get("skipped") else replaced if result.get("replaced") else imported).append(result)
        except Exception as exc:
            errors.append({"file": Path(path).name, "error": str(exc)})
    return {"imported": imported, "duplicates": duplicates, "skipped": skipped, "replaced": replaced, "errors": errors,
            "grouped": sum(item["grouped"] for item in imported + duplicates + skipped + replaced),
            "groupId": group_id, "plasmids": get_plasmids()}


def get_plasmids():
    with db() as c:
        rows = c.execute("SELECT id,file_name,stored_name,sha256,file_size,imported_at,note,favorite,last_viewed_at,primer_indexed FROM library_plasmids ORDER BY file_name COLLATE NOCASE").fetchall()
        primer_names = {}
        for r in c.execute("SELECT plasmid_id,name FROM plasmid_primer_names ORDER BY name COLLATE NOCASE"):
            primer_names.setdefault(r["plasmid_id"], []).append(r["name"])
        tags = {}
        for r in c.execute("SELECT plasmid_id,tag FROM plasmid_tags ORDER BY tag COLLATE NOCASE"):
            tags.setdefault(r["plasmid_id"], []).append(r["tag"])
        groups = {}
        for r in c.execute("SELECT pg.plasmid_id,g.id,g.name FROM plasmid_groups pg JOIN groups g ON g.id=pg.group_id ORDER BY g.name COLLATE NOCASE"):
            groups.setdefault(r["plasmid_id"], []).append((r["id"], r["name"]))
        return [{
            "id": r["id"], "name": r["file_name"], "storedName": r["stored_name"],
            "size": r["file_size"], "sha256": r["sha256"], "importedAt": r["imported_at"],
            "note": r["note"],
            "favorite": bool(r["favorite"]), "lastViewedAt": r["last_viewed_at"],
            "tags": tags.get(r["id"], []),
            "primerNames": primer_names.get(r["id"], []), "primerIndexed": bool(r["primer_indexed"]),
            "groups": [g[1] for g in groups.get(r["id"], [])],
            "groupIds": [g[0] for g in groups.get(r["id"], [])],
        } for r in rows]


def sync_plasmid(item_id, parsed=None):
    with LOCK:
        with db() as c:
            row = c.execute("SELECT id,file_name,storage_path,sha256,file_size,file_mtime_ns,primer_indexed FROM library_plasmids WHERE id=?",
                            (item_id,)).fetchone()
        if not row:
            raise FileNotFoundError("仓库中已没有这条质粒记录")
        path = Path(row["storage_path"])
        if not path.is_file():
            raise FileNotFoundError("导入的原件文件不存在，请检查仓库目录")
        parsed = parsed or parse_dna(path)
        stat = path.stat()
        if parsed["file_size"] != stat.st_size or parsed.get("file_mtime_ns", stat.st_mtime_ns) != stat.st_mtime_ns:
            raise ValueError("文件仍在写入，请稍后重试")
        changed = parsed["sha256"] != row["sha256"] or parsed["file_size"] != row["file_size"]
        with db() as c:
            if changed:
                duplicate = c.execute(
                    "SELECT 1 FROM library_plasmids WHERE file_name=? COLLATE NOCASE AND sha256=? AND id<>?",
                    (row["file_name"], parsed["sha256"], item_id),
                ).fetchone()
                if duplicate:
                    raise ValueError("修改后的文件与仓库中的同名质粒完全相同，请先重命名其中一份")
                c.execute("UPDATE library_plasmids SET sha256=?,file_size=?,file_mtime_ns=? WHERE id=?",
                          (parsed["sha256"], parsed["file_size"], stat.st_mtime_ns, item_id))
                c.execute("DELETE FROM plasmid_tags WHERE plasmid_id=?", (item_id,))
                c.executemany("INSERT OR IGNORE INTO plasmid_tags(plasmid_id,tag,tag_kind) VALUES(?,?,?)",
                              [(item_id, tag["tag"], tag["kind"]) for tag in parsed["tags"]])
            else:
                c.execute("UPDATE library_plasmids SET file_mtime_ns=? WHERE id=?",
                          (stat.st_mtime_ns, item_id))
            if changed or not row["primer_indexed"]:
                save_primer_index(c, item_id, parsed)
        return parsed, changed or not row["primer_indexed"]


def sync_plasmid_if_changed(item_id):
    with db() as c:
        row = c.execute("SELECT id,storage_path,sha256,file_size,file_mtime_ns,primer_indexed FROM library_plasmids WHERE id=?",
                        (item_id,)).fetchone()
    if not row:
        raise FileNotFoundError("仓库中已没有这条质粒记录")
    path = Path(row["storage_path"])
    stat = path.stat()
    if not row["primer_indexed"]:
        _, changed = sync_plasmid(item_id)
        return changed
    if stat.st_mtime_ns == row["file_mtime_ns"] and stat.st_size == row["file_size"]:
        return False
    if stat.st_size == row["file_size"]:
        with path.open("rb") as source:
            digest, _ = _copy_with_hash(source)
        if digest == row["sha256"]:
            with db() as c:
                c.execute("UPDATE library_plasmids SET file_mtime_ns=? WHERE id=?", (stat.st_mtime_ns, item_id))
            return False
    _, changed = sync_plasmid(item_id)
    return changed


def sync_changed_plasmids(progress=None, cancel=None):
    with LOCK, db() as c:
        rows = c.execute("SELECT id,file_name,storage_path,sha256,file_size,file_mtime_ns FROM library_plasmids ORDER BY id").fetchall()
    updated, errors = [], []
    for index, row in enumerate(rows, 1):
        check_cancel(cancel)
        report_progress(progress, "sync", index - 1, len(rows), row["file_name"])
        try:
            if sync_plasmid_if_changed(row["id"]):
                updated.append(row["id"])
        except Exception as exc:
            LOGGER.exception("同步质粒失败：%s", row["file_name"])
            errors.append({"id": row["id"], "name": row["file_name"], "error": str(exc)})
    report_progress(progress, "sync", len(rows), len(rows), "同步完成")
    return {"updated": updated, "errors": errors, "checked": len(rows)}


def managed_plasmid_path(item_id):
    with db() as c:
        row = c.execute("SELECT storage_path FROM library_plasmids WHERE id=?", (item_id,)).fetchone()
    if not row:
        raise FileNotFoundError("仓库中已没有这条质粒记录")
    path = Path(row["storage_path"])
    if not path.is_file():
        raise FileNotFoundError("导入的原件文件不存在，请检查仓库目录")
    return path


def rename_plasmid(item_id, name):
    name = str(name).strip()
    if not name or name in {".", ".."} or any(c in name for c in '<>:"/\\|?*') or name.endswith((" ", ".")):
        raise ValueError("请输入有效的质粒名称，不能包含文件路径或特殊字符")
    if not name.lower().endswith(".dna"):
        name += ".dna"
    if len(name) > 200:
        raise ValueError("质粒名称不能超过 200 个字符")
    with LOCK, db() as c:
        current = c.execute("SELECT sha256 FROM library_plasmids WHERE id=?", (item_id,)).fetchone()
        if not current:
            raise FileNotFoundError("仓库中已没有这条质粒记录")
        if c.execute("SELECT 1 FROM library_plasmids WHERE file_name=? COLLATE NOCASE AND sha256=? AND id<>?", (name, current["sha256"], item_id)).fetchone():
            raise ValueError("已有同名且内容相同的质粒")
        c.execute("UPDATE library_plasmids SET file_name=? WHERE id=?", (name, item_id))
    return name


def update_plasmid_note(item_id, note):
    if not isinstance(note, str) or len(note) > 10000:
        raise ValueError("备注不能超过 10000 个字符")
    with LOCK, db() as c:
        cur = c.execute("UPDATE library_plasmids SET note=? WHERE id=?", (note, item_id))
        if not cur.rowcount:
            raise FileNotFoundError("仓库中已没有这条质粒记录")
    return note


def set_plasmid_favorite(item_id, favorite):
    if not isinstance(favorite, bool):
        raise ValueError("收藏状态必须为是或否")
    with LOCK, db() as c:
        cur = c.execute("UPDATE library_plasmids SET favorite=? WHERE id=?", (int(favorite), item_id))
        if not cur.rowcount:
            raise FileNotFoundError("仓库中已没有这条质粒记录")
    return favorite


def mark_plasmid_viewed(item_id):
    viewed_at = datetime.now().isoformat(timespec="microseconds")
    with LOCK, db() as c:
        cur = c.execute("UPDATE library_plasmids SET last_viewed_at=? WHERE id=?", (viewed_at, item_id))
        if not cur.rowcount:
            raise FileNotFoundError("仓库中已没有这条质粒记录")
    return viewed_at


def delete_plasmid(item_id):
    with LOCK:
        with db() as c:
            row = c.execute("SELECT storage_path FROM library_plasmids WHERE id=?", (item_id,)).fetchone()
        if not row:
            raise FileNotFoundError("仓库中已没有这条质粒记录")
        path = Path(row["storage_path"])
        # Only an imported copy inside the configured repository may be removed.
        if STORAGE_ROOT.resolve() not in path.resolve().parents:
            raise ValueError("原件路径不在当前仓库中，无法安全删除")
        with db() as c:
            trash_id, _ = _archive_plasmid(c, item_id, "deleted")
            c.execute("DELETE FROM library_plasmids WHERE id=?", (item_id,))
        try:
            path.unlink(missing_ok=True)
        except OSError:
            LOGGER.exception("删除后清理原件失败：%s", path.name)
        return {"trashId": trash_id}


def preview_plasmid(item_id):
    with db() as c:
        row = c.execute("SELECT file_name,storage_path FROM library_plasmids WHERE id=?", (item_id,)).fetchone()
    if not row:
        raise FileNotFoundError("仓库中已没有这条质粒记录")
    path = Path(row["storage_path"])
    if not path.is_file():
        raise FileNotFoundError("导入的原件文件不存在，请检查仓库目录")
    parsed = parse_dna(path)
    _, changed = sync_plasmid(item_id, parsed)
    return {"id": item_id, "name": row["file_name"], "length": len(parsed["sequence"]),
            "circular": parsed["circular"], "features": parsed["features"], "primers": parsed["primers"], "metadataChanged": changed}


def _copy_with_hash(source, destination=None):
    digest = hashlib.sha256()
    size = 0
    while chunk := source.read(1024 * 1024):
        digest.update(chunk)
        size += len(chunk)
        if destination is not None:
            destination.write(chunk)
    return digest.hexdigest(), size


def _temporary_archive_path(destination):
    destination = Path(destination).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".plasmora-archive-", suffix=".tmp", dir=destination.parent)
    os.close(fd)
    return destination, Path(name)


def _snapshot_database(target):
    with closing(sqlite3.connect(DB_PATH, timeout=30)) as source, closing(sqlite3.connect(target)) as snapshot:
        source.backup(snapshot)


def backup_library(destination, progress=None, cancel=None):
    destination = Path(destination).with_suffix(".plasmora")
    check_cancel(cancel)
    synchronized = sync_changed_plasmids(progress, cancel)
    if synchronized["errors"]:
        raise ValueError(f"有 {len(synchronized['errors'])} 个质粒无法同步，请先检查原件后再备份")
    with LOCK, tempfile.TemporaryDirectory(prefix="plasmora-snapshot-") as work:
        snapshot_path = Path(work) / "library.sqlite3"
        _snapshot_database(snapshot_path)
        with closing(sqlite3.connect(snapshot_path)) as snapshot:
            snapshot.row_factory = sqlite3.Row
            records = snapshot.execute("SELECT id,storage_path,sha256,file_size FROM library_plasmids ORDER BY id").fetchall()
        destination, temporary = _temporary_archive_path(destination)
        try:
            manifest = {"format": "PlasmoraBackup", "version": 1,
                        "createdAt": datetime.now().isoformat(timespec="seconds"), "files": []}
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                archive.write(snapshot_path, "library.sqlite3")
                manifest["dbSha256"] = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
                for index, record in enumerate(records, 1):
                    check_cancel(cancel)
                    report_progress(progress, "backup", index - 1, len(records), "正在写入质粒原件")
                    path = Path(record["storage_path"])
                    if not path.is_file():
                        raise FileNotFoundError(f"备份失败，缺少质粒文件：{path}")
                    entry = f"files/{record['id']}.dna"
                    with path.open("rb") as source, archive.open(entry, "w") as output:
                        digest, size = _copy_with_hash(source, output)
                    if digest != record["sha256"] or size != record["file_size"]:
                        raise ValueError(f"备份失败，质粒文件与仓库记录不一致：{path.name}")
                    manifest["files"].append({"id": record["id"], "sha256": digest, "size": size})
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            check_cancel(cancel)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    report_progress(progress, "backup", len(records), len(records), "备份完成")
    return {"path": str(destination), "count": len(manifest["files"])}


def _verify_backup_archive(path, work, extract_files=False, progress=None, cancel=None):
    archive_path = Path(path).expanduser().resolve()
    if not archive_path.is_file():
        raise FileNotFoundError("找不到备份文件")
    with zipfile.ZipFile(archive_path) as archive:
        if "manifest.json" not in archive.namelist() or archive.getinfo("manifest.json").file_size > 10 * 1024 * 1024:
            raise ValueError("备份清单缺失或过大")
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("format") != "PlasmoraBackup" or manifest.get("version") != 1:
            raise ValueError("不是受支持的 Plasmora 备份")
        files = manifest.get("files")
        if not isinstance(files, list) or not all(isinstance(item, dict) for item in files):
            raise ValueError("备份文件清单无效")
        expected = {"manifest.json", "library.sqlite3"}
        for item in files:
            if type(item.get("id")) is not int or item["id"] <= 0 or not isinstance(item.get("sha256"), str) or type(item.get("size")) is not int or item["size"] < 0:
                raise ValueError("备份文件清单无效")
            expected.add(f"files/{item['id']}.dna")
        names = archive.namelist()
        if len(names) != len(expected) or set(names) != expected:
            raise ValueError("备份内容与清单不一致")
        snapshot_path = Path(work) / "library.sqlite3"
        with archive.open("library.sqlite3") as source, snapshot_path.open("wb") as target:
            digest, _ = _copy_with_hash(source, target)
        if digest != manifest.get("dbSha256"):
            raise ValueError("备份数据库校验失败")
        try:
            with closing(sqlite3.connect(snapshot_path)) as snapshot:
                snapshot.row_factory = sqlite3.Row
                if snapshot.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("备份数据库完整性检查失败")
                records = snapshot.execute("SELECT id,sha256,file_size,stored_name FROM library_plasmids ORDER BY id").fetchall()
                needed = {"library_plasmids", "plasmid_tags", "groups", "plasmid_groups", "synonym_clusters", "synonym_terms", "app_settings"}
                actual = {row[0] for row in snapshot.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not needed.issubset(actual):
                    raise ValueError("备份数据库缺少仓库表")
        except sqlite3.Error as exc:
            raise ValueError(f"备份数据库无法读取：{exc}") from exc
        by_id = {item["id"]: item for item in files}
        if len(by_id) != len(files) or set(by_id) != {row["id"] for row in records}:
            raise ValueError("备份质粒清单与数据库不一致")
        for index, record in enumerate(records, 1):
            check_cancel(cancel)
            report_progress(progress, "verify", index - 1, len(records), "正在校验备份")
            item = by_id[record["id"]]
            if item["sha256"] != record["sha256"] or item["size"] != record["file_size"]:
                raise ValueError("备份质粒信息与数据库不一致")
            entry = f"files/{record['id']}.dna"
            if archive.getinfo(entry).file_size != item["size"]:
                raise ValueError(f"备份文件大小不正确：{entry}")
            target_path = Path(work) / f"{record['id']}.dna" if extract_files else None
            with archive.open(entry) as source:
                if target_path is None:
                    digest, size = _copy_with_hash(source)
                else:
                    with target_path.open("wb") as target:
                        digest, size = _copy_with_hash(source, target)
            if digest != item["sha256"] or size != item["size"]:
                raise ValueError(f"备份文件校验失败：{entry}")
    return manifest, records, snapshot_path


def inspect_backup(path):
    with tempfile.TemporaryDirectory(prefix="plasmora-inspect-") as work:
        manifest, records, _ = _verify_backup_archive(path, work)
    return {"path": str(Path(path).resolve()), "count": len(records), "createdAt": manifest.get("createdAt", "")}


def list_rollback_backups():
    folder = LOCAL_DATA / "rollback"
    if not folder.is_dir():
        return []
    return [{"name": path.name, "size": path.stat().st_size}
            for path in sorted(folder.glob("Before-restore-*.plasmora"), reverse=True) if path.is_file()]


def restore_backup(path, progress=None, cancel=None):
    with LOCK, tempfile.TemporaryDirectory(prefix="plasmora-restore-", dir=LOCAL_DATA) as work:
        manifest, records, snapshot_path = _verify_backup_archive(path, work, extract_files=True,
                                                                  progress=progress, cancel=cancel)
        with db() as c:
            old_paths = [Path(row["storage_path"]) for row in c.execute("SELECT storage_path FROM library_plasmids")]
        rollback_path = None
        if old_paths:
            check_cancel(cancel)
            rollback_dir = LOCAL_DATA / "rollback"
            rollback_dir.mkdir(parents=True, exist_ok=True)
            rollback_path = rollback_dir / f"Before-restore-{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:6]}.plasmora"
            backup_library(rollback_path, progress, cancel)
        STORAGE_ROOT.mkdir(parents=True, exist_ok=True)
        created_paths = []
        nonce = uuid4().hex[:8]
        try:
            with closing(sqlite3.connect(snapshot_path)) as restored:
                columns = {row[1] for row in restored.execute("PRAGMA table_info(library_plasmids)")}
                if "file_mtime_ns" not in columns:
                    restored.execute("ALTER TABLE library_plasmids ADD COLUMN file_mtime_ns INTEGER NOT NULL DEFAULT 0")
                for index, record in enumerate(records, 1):
                    check_cancel(cancel)
                    report_progress(progress, "restore", index - 1, len(records), "正在恢复质粒原件")
                    stored_name = f"Plasmora-{record['id']}-{record['sha256'][:12]}-{nonce}.dna"
                    target = STORAGE_ROOT / stored_name
                    with (Path(work) / f"{record['id']}.dna").open("rb") as source, target.open("xb") as output:
                        created_paths.append(target)
                        shutil.copyfileobj(source, output)
                    restored.execute("UPDATE library_plasmids SET stored_name=?,storage_path=?,file_mtime_ns=? WHERE id=?",
                                     (stored_name, str(target), target.stat().st_mtime_ns, record["id"]))
                restored.execute("INSERT INTO app_settings(key,value) VALUES('storage_dir',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(STORAGE_ROOT),))
                restored.commit()
                migrate_plasmid_uniqueness(restored)
                init_primer_index(restored)
                restored.commit()
                if restored.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("恢复后的数据库完整性检查失败")
            check_cancel(cancel)
            os.replace(snapshot_path, DB_PATH)
        except Exception:
            for target in created_paths:
                target.unlink(missing_ok=True)
            raise
        root = STORAGE_ROOT.resolve()
        for path in old_paths:
            try:
                if root in path.resolve().parents and path not in created_paths:
                    path.unlink(missing_ok=True)
            except OSError:
                pass
    report_progress(progress, "restore", len(records), len(records), "恢复完成")
    return {"ok": True, "count": len(records), "createdAt": manifest.get("createdAt", ""),
            "rollbackPath": str(rollback_path) if rollback_path else None}


def export_one_plasmid(item_id, destination):
    destination = Path(destination).expanduser().resolve().with_suffix(".dna")
    destination.parent.mkdir(parents=True, exist_ok=True)
    sync_plasmid_if_changed(item_id)
    with LOCK, db() as c:
        row = c.execute("SELECT file_name,storage_path,sha256,file_size FROM library_plasmids WHERE id=?", (item_id,)).fetchone()
        if not row:
            raise FileNotFoundError("仓库中已没有这条质粒记录")
        managed_paths = {os.path.normcase(str(Path(item["storage_path"]).resolve()))
                         for item in c.execute("SELECT storage_path FROM library_plasmids")}
        if os.path.normcase(str(destination)) in managed_paths:
            raise ValueError("不能覆盖仓库中受管理的质粒文件")
        source = Path(row["storage_path"])
        if not source.is_file():
            raise FileNotFoundError("导入的原件文件不存在，请检查仓库目录")
        fd, temporary_name = tempfile.mkstemp(prefix=".plasmora-export-", suffix=".tmp", dir=destination.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as output, source.open("rb") as original:
                digest, size = _copy_with_hash(original, output)
            if digest != row["sha256"] or size != row["file_size"]:
                raise ValueError("导出失败，质粒文件与仓库记录不一致")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    return {"path": str(destination), "name": row["file_name"], "size": size}


def export_plasmids(item_ids, destination, progress=None, cancel=None):
    if not isinstance(item_ids, list) or not item_ids or any(type(item) is not int for item in item_ids):
        raise ValueError("请至少选择一个质粒")
    ids = sorted(set(item_ids))
    for index, item_id in enumerate(ids, 1):
        check_cancel(cancel)
        report_progress(progress, "export", index - 1, len(ids), "正在检查质粒原件")
        sync_plasmid_if_changed(item_id)
    destination = Path(destination).with_suffix(".zip")
    with LOCK, db() as c:
        placeholders = ",".join("?" for _ in ids)
        rows = c.execute(f"SELECT id,file_name,storage_path,sha256,file_size,imported_at,note FROM library_plasmids WHERE id IN ({placeholders}) ORDER BY file_name COLLATE NOCASE", ids).fetchall()
        if len(rows) != len(ids):
            raise ValueError("选择中包含不存在的质粒")
        tags = {}
        groups = {}
        for row in c.execute(f"SELECT plasmid_id,tag FROM plasmid_tags WHERE plasmid_id IN ({placeholders}) ORDER BY tag COLLATE NOCASE", ids):
            tags.setdefault(row["plasmid_id"], []).append(row["tag"])
        for row in c.execute(f"SELECT pg.plasmid_id,g.name FROM plasmid_groups pg JOIN groups g ON pg.group_id=g.id WHERE pg.plasmid_id IN ({placeholders}) ORDER BY g.name COLLATE NOCASE", ids):
            groups.setdefault(row["plasmid_id"], []).append(row["name"])
        destination, temporary = _temporary_archive_path(destination)
        try:
            used_names = set()
            csv_rows = []
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                for index, row in enumerate(rows, 1):
                    check_cancel(cancel)
                    report_progress(progress, "export", index - 1, len(rows), row["file_name"])
                    name = Path(row["file_name"]).name
                    stem, suffix = Path(name).stem, Path(name).suffix or ".dna"
                    export_name = name
                    serial = 2
                    while export_name.casefold() in used_names:
                        export_name = f"{stem} ({serial}){suffix}"
                        serial += 1
                    used_names.add(export_name.casefold())
                    path = Path(row["storage_path"])
                    if not path.is_file():
                        raise FileNotFoundError(f"导出失败，缺少质粒文件：{path}")
                    with path.open("rb") as source, archive.open(f"质粒/{export_name}", "w") as output:
                        digest, size = _copy_with_hash(source, output)
                    if digest != row["sha256"] or size != row["file_size"]:
                        raise ValueError(f"导出失败，质粒文件与仓库记录不一致：{name}")
                    csv_rows.append([str(row["id"]), export_name, row["file_name"], str(row["file_size"]), row["imported_at"],
                                     "; ".join(tags.get(row["id"], [])), "; ".join(groups.get(row["id"], [])), row["note"], row["sha256"]])
                import csv
                import io
                output = io.StringIO()
                writer = csv.writer(output)
                writer.writerow(["仓库编号", "导出文件名", "质粒名称", "文件大小（字节）", "导入时间", "Feature 标签", "所属分组", "备注", "SHA-256"])
                writer.writerows(csv_rows)
                archive.writestr("质粒清单.csv", output.getvalue().encode("utf-8-sig"))
            check_cancel(cancel)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    report_progress(progress, "export", len(rows), len(rows), "导出完成")
    return {"path": str(destination), "count": len(rows)}


def _write_migration_journal(data):
    journal = LOCAL_DATA / "storage-migration.json"
    temporary = journal.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, journal)


def recover_storage_migration():
    journal = LOCAL_DATA / "storage-migration.json"
    if not journal.is_file():
        return
    try:
        data = json.loads(journal.read_text(encoding="utf-8"))
        target_root = Path(data["target"]).resolve()
        with db() as c:
            referenced = {Path(row[0]).resolve() for row in c.execute("SELECT storage_path FROM library_plasmids")}
        for name in data.get("created", []):
            path = Path(name).resolve()
            if target_root in path.parents and path not in referenced:
                path.unlink(missing_ok=True)
        journal.unlink()
        LOGGER.info("已清理中断的存储迁移")
    except Exception:
        LOGGER.exception("无法清理中断的存储迁移")


def set_storage_directory(path, progress=None, cancel=None):
    global STORAGE_ROOT
    target_dir = Path(path).expanduser().resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    old_dir = STORAGE_ROOT.resolve()
    if target_dir == old_dir:
        return {"path": str(target_dir), "moved": 0}
    with LOCK:
        with db() as c:
            items = c.execute("SELECT id,stored_name,storage_path,sha256,file_size FROM library_plasmids ORDER BY id").fetchall()
        required = sum(item["file_size"] for item in items) + 16 * 1024 * 1024
        if items and shutil.disk_usage(target_dir).free < required:
            raise OSError("目标磁盘空间不足，无法安全迁移整个仓库")
        moved_rows, created_paths = [], []
        journal = {"target": str(target_dir), "created": []}
        _write_migration_journal(journal)
        try:
            for index, item in enumerate(items, 1):
                check_cancel(cancel)
                report_progress(progress, "move", index - 1, len(items), "正在迁移质粒原件")
                old_path = Path(item["storage_path"])
                if not old_path.is_file():
                    raise FileNotFoundError(f"找不到已导入文件：{old_path}")
                name = safe_storage_name(item["stored_name"], target_dir)
                new_path = target_dir / name
                if old_path.resolve() != new_path.resolve() and not new_path.exists():
                    journal["created"].append(str(new_path))
                    _write_migration_journal(journal)
                    fd, temp_name = tempfile.mkstemp(prefix=".plasmid-move-", suffix=".tmp", dir=target_dir)
                    os.close(fd)
                    temp_path = Path(temp_name)
                    try:
                        with old_path.open("rb") as source, temp_path.open("wb") as output:
                            digest, size = _copy_with_hash(source, output)
                        if digest != item["sha256"] or size != item["file_size"]:
                            raise ValueError(f"迁移校验失败：{old_path.name}")
                        os.replace(temp_path, new_path)
                    finally:
                        temp_path.unlink(missing_ok=True)
                    created_paths.append(new_path)
                elif new_path.exists():
                    # safe_storage_name should have selected a free name; guard against a race.
                    raise FileExistsError(f"目标文件已存在：{new_path}")
                moved_rows.append((str(new_path), name, item["id"], old_path))
            check_cancel(cancel)
            with db() as c:
                c.executemany("UPDATE library_plasmids SET storage_path=?,stored_name=? WHERE id=?",
                              [(new_path, name, item_id) for new_path, name, item_id, _ in moved_rows])
                c.execute("INSERT INTO app_settings(key,value) VALUES('storage_dir',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(target_dir),))
            STORAGE_ROOT = target_dir
        except Exception:
            for created in created_paths:
                created.unlink(missing_ok=True)
            (LOCAL_DATA / "storage-migration.json").unlink(missing_ok=True)
            raise
        # Remove only managed copies still inside the old storage directory.
        for _, _, _, old_path in moved_rows:
            try:
                if old_dir in old_path.resolve().parents:
                    old_path.unlink(missing_ok=True)
            except OSError:
                pass
        (LOCAL_DATA / "storage-migration.json").unlink(missing_ok=True)
    report_progress(progress, "move", len(items), len(items), "迁移完成")
    return {"path": str(target_dir), "moved": len(moved_rows)}


def migrate_legacy_database():
    legacy_candidates = []
    if LEGACY_ROOT:
        legacy_candidates.append(LEGACY_ROOT / "plasmid_manager.sqlite3")
    legacy_candidates.append(SOURCE_ROOT / "plasmid_manager.sqlite3")
    legacy = next((p for p in legacy_candidates if p.is_file() and p.resolve() != DB_PATH.resolve()), None)
    if not legacy:
        return
    key = "legacy_migration:" + str(legacy.resolve())
    with db() as c:
        if c.execute("SELECT 1 FROM app_settings WHERE key=?", (key,)).fetchone():
            return
    try:
        old = sqlite3.connect(legacy)
        old.row_factory = sqlite3.Row
        tables = {r[0] for r in old.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "plasmids" not in tables:
            old.close()
            return
        old_plasmids = old.execute("SELECT id,file_name,file_path FROM plasmids").fetchall()
        groups = old.execute("SELECT id,name,created_at FROM groups").fetchall() if "groups" in tables else []
        aliases = old.execute("SELECT canonical,alias FROM aliases").fetchall() if "aliases" in tables else []
        memberships = old.execute("SELECT plasmid_id,group_id FROM plasmid_groups").fetchall() if "plasmid_groups" in tables else []
        old.close()
        old_root = legacy.parent
        id_map, group_map = {}, {}
        for group in groups:
            with db() as c:
                c.execute("INSERT OR IGNORE INTO groups(name,created_at) VALUES(?,?)", (group["name"], group["created_at"] or datetime.now().isoformat(timespec="seconds")))
                group_map[group["id"]] = c.execute("SELECT id FROM groups WHERE name=?", (group["name"],)).fetchone()["id"]
        for alias in aliases:
            with db() as c:
                if str(alias["canonical"]).strip().casefold() != str(alias["alias"]).strip().casefold():
                    save_synonym_cluster(c, [alias["canonical"], alias["alias"]])
        for record in old_plasmids:
            candidate = Path(record["file_path"])
            source = candidate if candidate.is_absolute() else old_root / candidate
            if not source.is_file():
                continue
            result = import_one(source)
            id_map[record["id"]] = result["id"]
        for membership in memberships:
            if membership["plasmid_id"] in id_map and membership["group_id"] in group_map:
                with db() as c:
                    c.execute("INSERT OR IGNORE INTO plasmid_groups(plasmid_id,group_id) VALUES(?,?)", (id_map[membership["plasmid_id"]], group_map[membership["group_id"]]))
        with db() as c:
            c.execute("INSERT INTO app_settings(key,value) VALUES(?,?)", (key, "done"))
    except Exception as exc:
        print(f"旧版数据迁移未完成：{exc}")


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(RESOURCE_ROOT), **kwargs)

    def log_message(self, fmt, *args):
        pass

    def send_json(self, obj, status=200):
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def body(self):
        size = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(size) or b"{}")

    def do_GET(self):
        route = urlparse(self.path).path
        if route == "/api/plasmids":
            self.send_json({"plasmids": get_plasmids()})
        elif route == "/api/storage":
            self.send_json({"path": str(STORAGE_ROOT)})
        elif route == "/api/about":
            self.send_json({"version": APP_VERSION, "projectUrl": PROJECT_URL,
                            "license": "MIT", "logPath": str(LOG_PATH)})
        elif route == "/api/trash":
            self.send_json({"items": get_trash()})
        elif route == "/api/rollback":
            self.send_json({"items": list_rollback_backups()})
        elif route == "/api/settings/theme":
            with db() as c:
                row = c.execute("SELECT value FROM app_settings WHERE key='theme'").fetchone()
            self.send_json({"theme": row["value"] if row else "nocturne"})
        elif route == "/api/settings/sort":
            with db() as c:
                row = c.execute("SELECT value FROM app_settings WHERE key='sort_order'").fetchone()
            self.send_json({"sort": row["value"] if row and row["value"] in SORT_ORDERS else "name_asc"})
        elif route == "/api/settings/close_behavior":
            self.send_json({"behavior": get_close_behavior()})
        elif route == "/api/groups":
            with db() as c:
                rows = c.execute("SELECT g.id,g.name,COUNT(pg.plasmid_id) AS count FROM groups g LEFT JOIN plasmid_groups pg ON pg.group_id=g.id GROUP BY g.id ORDER BY g.name COLLATE NOCASE").fetchall()
            self.send_json([dict(r) for r in rows])
        elif route == "/api/synonym-clusters":
            self.send_json(get_synonym_clusters())
        elif route.startswith("/api/plasmids/") and route.endswith("/preview"):
            try:
                item_id = int(route.split("/")[3])
                self.send_json(preview_plasmid(item_id))
            except FileNotFoundError as exc:
                self.send_json({"error": str(exc)}, 404)
            except Exception as exc:
                self.send_json({"error": str(exc)}, 400)
        elif route.startswith("/api/file/"):
            try:
                item_id = int(route.split("/")[3])
                with db() as c:
                    row = c.execute("SELECT file_name,storage_path FROM library_plasmids WHERE id=?", (item_id,)).fetchone()
                if not row or not Path(row["storage_path"]).is_file():
                    self.send_error(404)
                    return
                target = Path(row["storage_path"])
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(row['file_name'])}")
                self.send_header("Content-Length", str(target.stat().st_size))
                self.end_headers()
                with target.open("rb") as f:
                    shutil.copyfileobj(f, self.wfile)
            except Exception:
                self.send_error(404)
        elif route in ("/", "/index.html", "/app.js", "/enhancements.js", "/styles.css", "/compat.css", "/themes.css", "/app-icon.png"):
            super().do_GET()
        else:
            self.send_error(404)

    def do_POST(self):
        route = urlparse(self.path).path
        data = self.body()
        try:
            if route == "/api/groups":
                name = str(data.get("name", "")).strip()
                if not name:
                    self.send_json({"error": "分组名称不能为空"}, 400)
                    return
                with db() as c:
                    c.execute("INSERT INTO groups(name,created_at) VALUES(?,?)", (name, datetime.now().isoformat(timespec="seconds")))
                self.send_json({"ok": True})
            elif route == "/api/settings/theme":
                theme = str(data.get("theme", "nocturne"))
                if theme not in {"nocturne", "ocean", "forest", "violet", "amber", "paper", "corporate-clean"}:
                    self.send_json({"error": "未知的配色方案"}, 400)
                    return
                with db() as c:
                    c.execute("INSERT INTO app_settings(key,value) VALUES('theme',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (theme,))
                self.send_json({"ok": True, "theme": theme})
            elif route == "/api/settings/sort":
                sort_order = data.get("sort")
                if sort_order not in SORT_ORDERS:
                    raise ValueError("未知的排序方式")
                with db() as c:
                    c.execute("INSERT INTO app_settings(key,value) VALUES('sort_order',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (sort_order,))
                self.send_json({"ok": True, "sort": sort_order})
            elif route == "/api/settings/close_behavior":
                self.send_json({"ok": True, "behavior": set_close_behavior(data.get("behavior"))})
            elif route == "/api/sync":
                self.send_json(sync_changed_plasmids())
            elif re.fullmatch(r"/api/trash/\d+/restore", route):
                self.send_json(restore_trash(int(route.split("/")[3])))
            elif re.fullmatch(r"/api/groups/\d+/members", route):
                group_id = int(route.split("/")[3])
                raw_ids = data.get("plasmid_ids")
                if not isinstance(raw_ids, list):
                    raise ValueError("请选择要加入分组的质粒")
                ids = sorted({int(item) for item in raw_ids})
                with LOCK, db() as c:
                    if not c.execute("SELECT 1 FROM groups WHERE id=?", (group_id,)).fetchone():
                        raise FileNotFoundError("分组不存在")
                    if ids:
                        placeholders = ",".join("?" for _ in ids)
                        count = c.execute(f"SELECT COUNT(*) FROM library_plasmids WHERE id IN ({placeholders})", ids).fetchone()[0]
                        if count != len(ids):
                            raise ValueError("选择中包含不存在的质粒")
                    c.execute("DELETE FROM plasmid_groups WHERE group_id=?", (group_id,))
                    c.executemany("INSERT INTO plasmid_groups(plasmid_id,group_id) VALUES(?,?)", [(item_id, group_id) for item_id in ids])
                self.send_json({"ok": True, "count": len(ids)})
            elif route.startswith("/api/groups/") and route.endswith("/plasmids"):
                group_id = int(route.split("/")[3])
                with db() as c:
                    c.execute("INSERT OR IGNORE INTO plasmid_groups(plasmid_id,group_id) VALUES(?,?)", (int(data["plasmid_id"]), group_id))
                self.send_json({"ok": True})
            elif re.fullmatch(r"/api/plasmids/\d+/view", route):
                viewed_at = mark_plasmid_viewed(int(route.split("/")[3]))
                self.send_json({"ok": True, "lastViewedAt": viewed_at})
            elif route == "/api/synonym-clusters":
                with LOCK, db() as c:
                    cluster_id = save_synonym_cluster(c, data.get("terms"))
                self.send_json({"ok": True, "id": cluster_id})
            else:
                self.send_error(404)
        except sqlite3.IntegrityError:
            self.send_json({"error": "这个名称已经存在"}, 409)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 400)

    def do_PATCH(self):
        route = urlparse(self.path).path
        try:
            data = self.body()
            if re.fullmatch(r"/api/plasmids/\d+/note", route):
                note = update_plasmid_note(int(route.split("/")[3]), data.get("note"))
                self.send_json({"ok": True, "note": note})
            elif re.fullmatch(r"/api/plasmids/\d+/favorite", route):
                favorite = set_plasmid_favorite(int(route.split("/")[3]), data.get("favorite"))
                self.send_json({"ok": True, "favorite": favorite})
            elif re.fullmatch(r"/api/plasmids/\d+", route):
                name = rename_plasmid(int(route.rsplit("/", 1)[1]), data.get("name", ""))
                self.send_json({"ok": True, "name": name})
            elif re.fullmatch(r"/api/groups/\d+", route):
                group_id = int(route.rsplit("/", 1)[1])
                name = str(data.get("name", "")).strip()
                if not name or len(name) > 40:
                    raise ValueError("分组名称应为 1–40 个字符")
                with db() as c:
                    cur = c.execute("UPDATE groups SET name=? WHERE id=?", (name, group_id))
                    if not cur.rowcount:
                        raise FileNotFoundError("分组不存在")
                self.send_json({"ok": True, "name": name})
            elif re.fullmatch(r"/api/synonym-clusters/\d+", route):
                with LOCK, db() as c:
                    cluster_id = save_synonym_cluster(c, data.get("terms"), int(route.rsplit("/", 1)[1]))
                self.send_json({"ok": True, "id": cluster_id})
            else:
                self.send_error(404)
        except FileNotFoundError as exc:
            self.send_json({"error": str(exc)}, 404)
        except sqlite3.IntegrityError:
            self.send_json({"error": "这个名称已经存在"}, 409)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 400)

    def do_DELETE(self):
        route = urlparse(self.path).path
        try:
            if re.fullmatch(r"/api/plasmids/\d+", route):
                self.send_json({"ok": True, **delete_plasmid(int(route.rsplit("/", 1)[1]))})
            elif re.fullmatch(r"/api/trash/\d+", route):
                purge_trash(int(route.rsplit("/", 1)[1]))
                self.send_json({"ok": True})
            elif route.startswith("/api/groups/") and "/plasmids/" in route:
                parts = route.split("/")
                with db() as c:
                    c.execute("DELETE FROM plasmid_groups WHERE group_id=? AND plasmid_id=?", (int(parts[3]), int(parts[5])))
                self.send_json({"ok": True})
            elif re.fullmatch(r"/api/groups/\d+", route):
                with db() as c:
                    cur = c.execute("DELETE FROM groups WHERE id=?", (int(route.split("/")[3]),))
                    if not cur.rowcount:
                        raise FileNotFoundError("分组不存在")
                self.send_json({"ok": True})
            elif re.fullmatch(r"/api/synonym-clusters/\d+", route):
                with LOCK, db() as c:
                    cur = c.execute("DELETE FROM synonym_clusters WHERE id=?", (int(route.rsplit("/", 1)[1]),))
                    if not cur.rowcount:
                        raise FileNotFoundError("同义词集群不存在")
                self.send_json({"ok": True})
            else:
                self.send_error(404)
        except FileNotFoundError as exc:
            self.send_json({"error": str(exc)}, 404)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 400)


def run_server():
    init_logging()
    load_legacy_settings()
    init_db()
    return ThreadingHTTPServer(("127.0.0.1", 0), Handler)


if __name__ == "__main__":
    httpd = run_server()
    httpd.serve_forever()
