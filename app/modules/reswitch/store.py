"""换序留档。

留档以实际完成的换序步骤为准：未完成/挂起的计划步骤不计入完成清单，
也不参与完整性摘要。但为支持中断后仅凭档案续排，文档同时保存重建任务
所需的台账、换序单、备用芯仲裁结果与最近一次现场占位。

任务以确定性 JSON 规范化后存 SQLite，同一任务重放（中断恢复后续跑）
复用同一标识，不产生重复档案。
"""

import hashlib
import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .executor import completed_steps, is_finished
from ..archive.identity import canonical


@contextmanager
def session(database):
    Path(database).parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database, timeout=15)) as connection, connection:
        connection.execute(
            'CREATE TABLE IF NOT EXISTS reswitch_jobs '
            '(id TEXT UNIQUE NOT NULL, document BLOB NOT NULL)')
        yield connection


def job_identity(job):
    """任务身份只由台账、换序单与备用芯仲裁结果决定，与进度无关。"""
    basis = [job['job_id'], job['core_book'], job['orders'], job['spare_assignments']]
    return hashlib.sha256(canonical(basis)).hexdigest()


def manifest(job):
    steps = completed_steps(job)
    return {
        'job_id': job['job_id'],
        'archive_id': job_identity(job),
        'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'finished': is_finished(job),
        # 重建材料：恢复时据此还原规划，再用实际占位续排
        'core_book': job['core_book'],
        'orders': job['orders'],
        'spare_assignments': job['spare_assignments'],
        'blocked': job['blocked'],
        'actual_occupancy': job.get('actual_occupancy'),
        # 留档与签发唯一认可的部分
        'completed_step_count': len(steps),
        'completed_steps': steps,
        'integrity_sha256': hashlib.sha256(canonical(steps)).hexdigest(),
    }


def archive_job(database, job):
    doc = manifest(job)
    with session(database) as db:
        changed = db.execute(
            'INSERT INTO reswitch_jobs VALUES (?, ?) ON CONFLICT(id) DO NOTHING',
            (doc['archive_id'], canonical(doc))).rowcount
        stored = db.execute('SELECT document FROM reswitch_jobs WHERE id=?',
                            (doc['archive_id'],)).fetchone()[0]
    return json.loads(stored), changed == 1


def update_job(database, job):
    """恢复续排后覆盖同一档案（身份不变，进度推进）。"""
    doc = manifest(job)
    with session(database) as db:
        db.execute('INSERT INTO reswitch_jobs VALUES (?, ?) '
                   'ON CONFLICT(id) DO UPDATE SET document=excluded.document',
                   (doc['archive_id'], canonical(doc)))
    return doc


def get_job(database, archive_id):
    with session(database) as db:
        row = db.execute('SELECT document FROM reswitch_jobs WHERE id=?',
                         (archive_id,)).fetchone()
    if not row:
        raise KeyError(archive_id)
    return json.loads(row[0])
