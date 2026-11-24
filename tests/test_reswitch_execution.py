"""中断恢复、留档与签发规则测试。"""

import pytest

from app.errors import InspectionError
from app.modules.reswitch.executor import create_job, resume_job, advance, DONE, PENDING
from app.modules.reswitch.store import archive_job, get_job, job_identity, update_job
from app.modules.reswitch.signoff import issue
from app.modules.reswitch.planner import HOLD, MOVE, RESTORE


def core_book(spare_reconciled=True):
    return {'cores': [
        {'id': 'C1', 'reconciled': True, 'spare': False},
        {'id': 'C2', 'reconciled': True, 'spare': False},
        {'id': 'S1', 'reconciled': spare_reconciled, 'spare': True}]}


def swap_payload():
    return {'id': 'O1', 'moves': [
        {'service': 'SVC-A', 'from_core': 'C1', 'to_core': 'C2'},
        {'service': 'SVC-B', 'from_core': 'C2', 'to_core': 'C1'}]}


def occupancy(a_core, b_core=...):
    """完整现场占位：覆盖计划涉及的每条业务。"""
    rows = [{'service': 'SVC-A', 'core': a_core}]
    if b_core is not ...:
        rows.append({'service': 'SVC-B', 'core': b_core})
    return {'services': rows}


# R6 现场已换、记录中断：按实际占位续排，已完成步骤幂等跳过
def test_R6_resume_from_actual_occupancy_marks_done_and_keeps_remaining():
    job = create_job('J1', core_book(), [swap_payload()])
    # 中断时 A 已落到备用芯 S1（第一步完成），B 仍在 C2
    job = resume_job(job, occupancy('S1', 'C2'))
    statuses = [(s['action'], s['status']) for s in job['steps']]
    assert statuses == [(HOLD, DONE), (MOVE, PENDING), (RESTORE, PENDING)]


def test_R6_resume_distinguishes_each_prefix_state():
    job = create_job('J1', core_book(), [swap_payload()])
    resume_job(job, occupancy('S1', 'C1'))      # 第二步完成
    assert [s['status'] for s in job['steps']] == [DONE, DONE, PENDING]
    resume_job(job, occupancy('C2', 'C1'))      # 全部完成
    assert [s['status'] for s in job['steps']] == [DONE, DONE, DONE]


def test_R6_retry_does_not_reoccupy_spare():
    job = create_job('J1', core_book(), [swap_payload()])
    resume_job(job, occupancy('S1', 'C2'))
    spare_before = job['spare_assignments']['O1']
    resume_job(job, occupancy('S1', 'C2'))     # 同快照重放：幂等
    assert job['spare_assignments']['O1'] == spare_before
    assert len(job['steps']) == 3
    assert [s['status'] for s in job['steps']].count(DONE) == 1


def test_R6_full_recovery_completes_all_steps():
    job = create_job('J1', core_book(), [swap_payload()])
    job = resume_job(job, occupancy('C2', 'C1'))
    assert all(s['status'] == DONE for s in job['steps'])


def test_R6_zero_progress_leaves_all_pending():
    job = create_job('J1', core_book(), [swap_payload()])
    resume_job(job, occupancy('C1', 'C2'))
    assert all(s['status'] == PENDING for s in job['steps'])


def test_R6_incomplete_occupancy_rejected():
    job = create_job('J1', core_book(), [swap_payload()])
    with pytest.raises(InspectionError) as exc:
        resume_job(job, occupancy('S1'))       # 缺 SVC-B
    assert exc.value.code == 'OCCUPANCY_INCOMPLETE'
    with pytest.raises(InspectionError) as exc2:
        resume_job(job, {'services': []})
    assert exc2.value.code == 'OCCUPANCY_INCOMPLETE'


def test_R6_contradictory_occupancy_rejected():
    job = create_job('J1', core_book(), [swap_payload()])
    with pytest.raises(InspectionError) as exc:
        resume_job(job, occupancy('C9', 'C2'))  # 计划中不可达的芯位
    assert exc.value.code == 'OCCUPANCY_CONTRADICTS_PLAN'


def test_R6_unknown_service_in_occupancy_rejected():
    job = create_job('J1', core_book(), [swap_payload()])
    with pytest.raises(InspectionError) as exc:
        resume_job(job, {'services': [
            {'service': 'GHOST', 'core': 'S1'},
            {'service': 'SVC-A', 'core': 'C1'},
            {'service': 'SVC-B', 'core': 'C2'}]})
    assert exc.value.status == 409


def test_advance_is_idempotent_and_specific():
    job = create_job('J1', core_book(), [swap_payload()])
    advance(job, 'SVC-A', 'S1')
    advance(job, 'SVC-A', 'S1')      # 重复回报不产生新步骤
    assert job['steps'][0]['status'] == DONE
    assert job['steps'][1]['status'] == PENDING


# R7 留档只收实际完成步骤；恢复续跑复用同一档案身份
def test_R7_archive_contains_only_completed_steps(tmp_path):
    db = tmp_path / 'r.sqlite'
    job = create_job('J1', core_book(), [swap_payload()])
    resume_job(job, occupancy('S1', 'C2'))
    record, fresh = archive_job(db, job)
    assert fresh is True
    assert record['completed_step_count'] == 1
    assert record['completed_steps'][0]['action'] == HOLD
    assert record['finished'] is False

    # 同身份重放：不新增档案
    again, fresh2 = archive_job(db, job)
    assert fresh2 is False and again['archive_id'] == record['archive_id']


def test_R7_resume_advances_same_archive_without_new_spare(tmp_path):
    db = tmp_path / 'r.sqlite'
    job = create_job('J1', core_book(), [swap_payload()])
    archive_job(db, job)
    identity_before = job_identity(job)

    resume_job(job, occupancy('S1', 'C2'))
    update_job(db, job)
    stored = get_job(db, identity_before)
    assert stored['archive_id'] == identity_before       # 身份不变
    assert stored['completed_step_count'] == 1
    assert stored['spare_assignments'] == {'O1': 'S1'}   # 备用芯没重抢

    # 续到完工
    resume_job(job, occupancy('C2', 'C1'))
    update_job(db, job)
    assert get_job(db, identity_before)['finished'] is True


# R8 签发只认实际完成步骤：未完工拒签，完工放行
def test_R8_signoff_blocked_until_all_steps_done(tmp_path):
    db = tmp_path / 'r.sqlite'
    job = create_job('J1', core_book(), [swap_payload()])
    resume_job(job, occupancy('S1', 'C2'))
    record, _ = archive_job(db, job)
    decision = issue(db, record['archive_id'])
    assert decision['approved'] is False
    assert 'REMAINING_STEPS' in decision['blockers']
    assert decision['completed_step_count'] == 1        # 只认已完成那一步


def test_R8_signoff_approves_when_finished(tmp_path):
    db = tmp_path / 'r.sqlite'
    job = create_job('J1', core_book(), [swap_payload()])
    resume_job(job, occupancy('C2', 'C1'))
    record, _ = archive_job(db, job)
    decision = issue(db, record['archive_id'])
    assert decision['approved'] is True
    assert decision['blockers'] == []
    assert [s['action'] for s in decision['completed_steps']] == [HOLD, MOVE, RESTORE]


def test_R8_signoff_refuses_when_blocked_orders_present(tmp_path):
    db = tmp_path / 'r.sqlite'
    # 无备用芯的互换：整单挂起，无步骤可完成
    book = {'cores': [{'id': 'C1', 'reconciled': True, 'spare': False},
                      {'id': 'C2', 'reconciled': True, 'spare': False}]}
    job = create_job('J2', book, [swap_payload()])
    record, _ = archive_job(db, job)
    decision = issue(db, record['archive_id'])
    assert decision['approved'] is False
    assert 'BLOCKED_ORDER_PRESENT' in decision['blockers']

