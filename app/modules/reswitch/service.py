"""换序 API 编排与入站校验。

入站报文：
{
  "job_id": "W-20261124-01",
  "core_book": {"cores": [{"id","reconciled","spare"} ...]},
  "orders": [{"id","moves":[{"service","from_core","to_core"}]} ...],
  "occupancy": {"services":[{"service","core"}]}   # 可选，恢复时传入
}

换序页面只采用双端对账合格的芯位，合格性以台账 ``reconciled`` 为准；
不合格芯位在规划阶段即被隔离，不产生任何占芯步骤。
"""

from app.errors import InspectionError
from .executor import create_job, resume_job, completed_steps, pending_steps, is_finished
from .planner import plan_orders
from .signoff import issue
from .store import archive_job, update_job, get_job

_CORE_FIELDS = {'id', 'reconciled', 'spare'}
_MOVE_FIELDS = {'service', 'from_core', 'to_core'}


def parse_payload(payload):
    if not isinstance(payload, dict):
        raise InspectionError('JSON_OBJECT_REQUIRED', status=400)
    job_id = payload.get('job_id')
    if not isinstance(job_id, str) or not job_id:
        raise InspectionError('JOB_ID_REQUIRED', status=400)

    book = payload.get('core_book')
    if not isinstance(book, dict) or not isinstance(book.get('cores'), list) or not book['cores']:
        raise InspectionError('CORE_BOOK_REQUIRED', status=400)
    cores = []
    for core in book['cores']:
        if not isinstance(core, dict) or set(core) > _CORE_FIELDS or not isinstance(core.get('id'), str):
            raise InspectionError('CORE_ROW_INVALID', source=core.get('id'), status=400)
        if not isinstance(core.get('reconciled'), bool) or not isinstance(core.get('spare'), bool):
            raise InspectionError('CORE_FLAG_INVALID', source=core.get('id'), status=400)
        cores.append({'id': core['id'], 'reconciled': core['reconciled'], 'spare': core['spare']})
    if len({c['id'] for c in cores}) != len(cores):
        raise InspectionError('CORE_ID_DUPLICATED', status=400)

    orders = payload.get('orders')
    if not isinstance(orders, list) or not orders:
        raise InspectionError('ORDER_REQUIRED', status=400)
    clean_orders = []
    for order in orders:
        if not isinstance(order, dict) or not isinstance(order.get('id'), str):
            raise InspectionError('ORDER_ID_REQUIRED', status=400)
        moves = order.get('moves')
        if not isinstance(moves, list) or not moves:
            raise InspectionError('MOVE_REQUIRED', source=order.get('id'), status=400)
        clean_moves = []
        for move in moves:
            if not isinstance(move, dict) or set(move) != _MOVE_FIELDS \
                    or not all(isinstance(move.get(k), str) and move.get(k) for k in _MOVE_FIELDS):
                raise InspectionError('MOVE_ROW_INVALID', source=order.get('id'), status=400)
            clean_moves.append(dict(move))
        clean_orders.append({'id': order['id'], 'moves': clean_moves})
    if len({o['id'] for o in clean_orders}) != len(clean_orders):
        raise InspectionError('ORDER_ID_DUPLICATED', status=400)

    occupancy = payload.get('occupancy')
    if occupancy is not None:
        _validate_occupancy(occupancy)
    return job_id, {'cores': cores}, clean_orders, occupancy


def _validate_occupancy(occupancy):
    if not isinstance(occupancy, dict) or not isinstance(occupancy.get('services'), list):
        raise InspectionError('OCCUPANCY_INVALID', status=400)
    for item in occupancy['services']:
        if not isinstance(item, dict) or not isinstance(item.get('service'), str) \
                or not isinstance(item.get('core'), str):
            raise InspectionError('OCCUPANCY_ROW_INVALID', status=400)


def schedule(payload):
    """纯规划：返回每个换序单的开工/挂起结论与步骤，不落库。"""
    job_id, book, orders, _ = parse_payload(payload)
    return {'job_id': job_id, **plan_orders(book, orders)}


def open_job(database, payload):
    """建任务；若带现场占位则按实际占位续排，再留档实际完成步骤。"""
    job_id, book, orders, occupancy = parse_payload(payload)
    job = create_job(job_id, book, orders)
    if occupancy:
        resume_job(job, occupancy)
    record, fresh = archive_job(database, job)
    return {'archive_id': record['archive_id'], 'created': fresh,
            'job': job_view(job)}, 201 if fresh else 200


def resume(database, archive_id, occupancy):
    """中断恢复：从档案重建任务，按最新实际占位续排后覆盖同一档案。"""
    _validate_occupancy(occupancy)
    try:
        record = get_job(database, archive_id)
    except KeyError:
        raise InspectionError('RESWITCH_ARCHIVE_NOT_FOUND', status=404)
    job = create_job(record['job_id'], record['core_book'], record['orders'])
    # 沿用首次仲裁确定的备用芯，恢复绝不重新抢占
    if record['spare_assignments']:
        for order_id, spare in record['spare_assignments'].items():
            job['spare_assignments'][order_id] = spare
            job['plans'][order_id]['spare'] = spare
    resume_job(job, occupancy)
    update_job(database, job)
    return {'archive_id': archive_id, 'job': job_view(job)}


def signoff(database, archive_id):
    return issue(database, archive_id)


def job_view(job):
    return {'job_id': job['job_id'], 'finished': is_finished(job),
            'spare_assignments': job['spare_assignments'],
            'blocked': job['blocked'],
            'plans': job['plans'],
            'completed_steps': completed_steps(job),
            'pending_steps': pending_steps(job)}
