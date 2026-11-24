"""换序签发。

签发只认留档里实际完成的换序步骤：计划中未执行、挂起或被阻塞的部分
一律不计入。整单全部步骤完成才放行；只要还有待执行步骤，就给出阻塞
原因，避免把半截换序当作完工签发。
"""

from app.errors import InspectionError
from ..archive.identity import canonical, digest
from .store import get_job

ALL_STEPS_DONE = 'REMAINING_STEPS'
BLOCKED_ORDER_PRESENT = 'BLOCKED_ORDER_PRESENT'
ARCHIVE_TAMPERED = 'ARCHIVE_TAMPERED'


def issue(database, archive_id):
    try:
        record = get_job(database, archive_id)
    except KeyError:
        raise InspectionError('RESWITCH_ARCHIVE_NOT_FOUND', status=404)
    blockers = []

    if digest(canonical(record['completed_steps'])) != record['integrity_sha256']:
        blockers.append(ARCHIVE_TAMPERED)
    if not record['finished']:
        blockers.append(ALL_STEPS_DONE)
    if record['blocked']:
        blockers.append(BLOCKED_ORDER_PRESENT)

    return {
        'archive_id': archive_id,
        'approved': not blockers,
        'blockers': blockers,
        # 签发内容只列实际完成步骤，不回带任何待执行计划
        'completed_step_count': record['completed_step_count'],
        'completed_steps': record['completed_steps'],
        'spare_assignments': record['spare_assignments'],
        'integrity_sha256': record['integrity_sha256'],
    }
