"""换序执行与中断恢复。

规划器给出的是应然步骤；现场可能在记录没写完时中断。恢复时不信任
半截的步骤勾选，而是拿现场实际占位（哪条业务此刻在哪个芯位）与计划
逐条比对，已满足的步骤幂等跳过，从第一个未满足的步骤续排。重试同一
任务因此不会重复占用备用芯。
"""

from app.errors import InspectionError
from .planner import plan_orders

PLANNED = 'PLANNED'
DONE = 'DONE'
PENDING = 'PENDING'


def create_job(job_id, core_book, orders):
    """从台账与换序单生成一个可执行任务（含阻塞单与备用芯分配）。

    各换序单的步骤按单编号顺序拼接成一条全局执行序列并重排 ``seq``，
    中断恢复即在这条序列上取最长已完成前缀。
    """
    result = plan_orders(core_book, orders)
    steps = []
    for global_seq, step in enumerate(result['steps'], start=1):
        steps.append({**step, 'seq': global_seq, 'status': PENDING})
    job = {'job_id': job_id, 'core_book': core_book, 'orders': orders,
           'spare_assignments': result['spare_assignments'],
           'blocked': result['blocked'],
           'plans': result['orders'],
           'steps': steps}
    return job


def resume_job(job, actual_occupancy):
    """按现场实际占位续排。

    ``actual_occupancy``：``{'services': [{'service','core'}...]}``，
    描述中断瞬间每条业务实际所在芯位。

    同一条业务在互换拆步里会出现两次（先落备用芯、再回目标芯），不能
    只按"当前位置等于步骤目标"逐点判断。这里从计划的初始位置出发逐步
    模拟，取与现场快照逐点一致的**最长已完成前缀**：前缀步骤标记
    ``DONE``，从第一个对不上的步骤起保持 ``PENDING``。重试同一任务
    因此幂等，也不会重复占用备用芯。
    """
    location = {item['service']: item['core'] for item in actual_occupancy['services']}
    _require_known_services(job, location)

    planned_services = {step['service'] for step in job['steps']}
    if planned_services - set(location):
        # 现场占位必须覆盖每条在跑业务，否则无法区分相邻步骤，恢复会误判进度
        raise InspectionError('OCCUPANCY_INCOMPLETE', status=409)

    # 在合法执行路径上，完整现场快照必然等于"执行完前 k 步"的完整状态。
    # 取与快照逐点一致的最大 k 作为最长已完成前缀；都不一致即现场与
    # 计划矛盾（例如出现计划中不可达的芯位）。
    boundary = None
    state = _initial_locations(job)
    if state == location:
        boundary = 0
    for index, step in enumerate(job['steps'], start=1):
        state = {**state, step['service']: step['to_core']}
        if state == location:
            boundary = index
    if boundary is None:
        raise InspectionError('OCCUPANCY_CONTRADICTS_PLAN', status=409)

    for index, step in enumerate(job['steps']):
        step['status'] = DONE if index < boundary else PENDING
    job['actual_occupancy'] = actual_occupancy
    return job


def _initial_locations(job):
    """每条业务在计划开始前的位置 = 其第一条步骤的源芯位。"""
    initial = {}
    for step in job['steps']:
        initial.setdefault(step['service'], step['from_core'])
    return initial


def advance(job, service, core):
    """现场回报一条业务已落到某芯位，幂等推进对应步骤。"""
    for step in job['steps']:
        if step['service'] == service and step['to_core'] == core:
            step['status'] = DONE
            return step
    raise KeyError((service, core))


def completed_steps(job):
    """留档与签发唯一认可的步骤集合：实际完成的换序步骤。"""
    return [dict(step, status=DONE) for step in job['steps'] if step['status'] == DONE]


def pending_steps(job):
    return [step for step in job['steps'] if step['status'] != DONE]


def is_finished(job):
    return not pending_steps(job)


def _require_known_services(job, location):
    planned_services = {step['service'] for step in job['steps']}
    unknown = set(location) - planned_services
    if unknown:
        raise InspectionError(f'UNKNOWN_SERVICE_IN_OCCUPANCY: {sorted(unknown)}', status=409)
