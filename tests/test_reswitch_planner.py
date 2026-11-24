"""换序规划器规则测试。

每条用例对应需求中的一条现场规则，函数名里带规则编号便于追溯。
"""

import pytest

from app.modules.reswitch.planner import (
    plan_orders, HOLD, MOVE, RESTORE,
    SOURCE_NOT_RECONCILED, TARGET_NOT_RECONCILED, SWAP_WITHOUT_SPARE,
    SPARE_CONTENDED, SPARE_NOT_RECONCILED, MOVE_CROSSES_PLAN,
)


def book(*rows, spares=()):
    """rows：承载业务的合格芯位；spares：合格备用芯位。"""
    cores = [{'id': cid, 'reconciled': True, 'spare': False} for cid in rows]
    cores += [{'id': cid, 'reconciled': True, 'spare': True} for cid in spares]
    return {'cores': cores}


def swap_order(oid, a='C1', b='C2', sa='SVC-A', sb='SVC-B'):
    return {'id': oid, 'moves': [
        {'service': sa, 'from_core': a, 'to_core': b},
        {'service': sb, 'from_core': b, 'to_core': a}]}


# R1 互换必须借合格备用芯，拆成 HOLD/MOVE/RESTORE 三步
def test_R1_swap_uses_qualified_spare_in_three_steps():
    result = plan_orders(book('C1', 'C2', spares=['S1']), [swap_order('O1')])
    plan = result['orders']['O1']
    assert plan['status'] == 'READY' and plan['spare'] == 'S1'
    actions = [(s['action'], s['service'], s['from_core'], s['to_core']) for s in plan['steps']]
    assert actions == [
        (HOLD, 'SVC-A', 'C1', 'S1'),
        (MOVE, 'SVC-B', 'C2', 'C1'),
        (RESTORE, 'SVC-A', 'S1', 'C2'),
    ]


# R2 换序页面只采用双端对账合格的芯位：源/目标不合格即隔离，零占芯
def test_R2_unreconciled_cores_are_isolated_without_occupying_anything():
    core_book = {'cores': [
        {'id': 'C1', 'reconciled': False, 'spare': False},
        {'id': 'C2', 'reconciled': True, 'spare': False},
        {'id': 'S1', 'reconciled': True, 'spare': True}]}
    result = plan_orders(core_book, [swap_order('O1')])
    plan = result['orders']['O1']
    assert plan['status'] == 'BLOCKED' and plan['steps'] == []
    assert SOURCE_NOT_RECONCILED in plan['reasons']
    assert result['spare_assignments'] == {}     # 没开工就不占备用芯


def test_R2_unreconciled_target_blocks_move():
    core_book = {'cores': [
        {'id': 'C1', 'reconciled': True, 'spare': False},
        {'id': 'C2', 'reconciled': False, 'spare': False}]}
    order = {'id': 'O1', 'moves': [{'service': 'S', 'from_core': 'C1', 'to_core': 'C2'}]}
    result = plan_orders(core_book, [order])
    assert TARGET_NOT_RECONCILED in result['orders']['O1']['reasons']
    assert result['steps'] == []


# R3 互换缺合格备用芯：两条业务都挂起，互不占位
def test_R3_swap_without_spare_holds_both_services_without_occupying():
    result = plan_orders(book('C1', 'C2'), [swap_order('O1')])
    plan = result['orders']['O1']
    assert plan['status'] == 'BLOCKED' and plan['steps'] == [] and plan['spare'] is None
    assert plan['reasons'] == [SWAP_WITHOUT_SPARE]
    assert result['spare_assignments'] == {}


def test_R3_spare_exists_but_unreconciled_is_rejected():
    core_book = {'cores': [
        {'id': 'C1', 'reconciled': True, 'spare': False},
        {'id': 'C2', 'reconciled': True, 'spare': False},
        {'id': 'S1', 'reconciled': False, 'spare': True}]}
    result = plan_orders(core_book, [swap_order('O1')])
    assert result['orders']['O1']['reasons'] == [SPARE_NOT_RECONCILED]
    assert result['steps'] == []


# R3b 互不占位的链/长环仍可开工，不需要备用芯
def test_R3b_chain_and_long_cycle_still_start_without_spare():
    # 链 C1->C2->C3（C3 为腾空目标），倒序腾挪
    chain = {'id': 'O1', 'moves': [
        {'service': 'A', 'from_core': 'C1', 'to_core': 'C2'},
        {'service': 'B', 'from_core': 'C2', 'to_core': 'C3'}]}
    result = plan_orders(book('C1', 'C2', 'C3'), [chain])
    steps = [(s['service'], s['from_core'], s['to_core']) for s in result['steps']]
    assert steps == [('B', 'C2', 'C3'), ('A', 'C1', 'C2')]
    assert result['spare_assignments'] == {}

    # 三芯长环直接旋转
    cycle = {'id': 'O2', 'moves': [
        {'service': 'A', 'from_core': 'C1', 'to_core': 'C2'},
        {'service': 'B', 'from_core': 'C2', 'to_core': 'C3'},
        {'service': 'C', 'from_core': 'C3', 'to_core': 'C1'}]}
    result = plan_orders(book('C1', 'C2', 'C3'), [cycle])
    assert len(result['steps']) == 3
    assert all(s['action'] == MOVE for s in result['steps'])
    assert result['spare_assignments'] == {}


# R4 两张换序单争用同一备用芯：只给一单占用，后单挂起
def test_R4_two_orders_contending_one_spare_only_one_occupies():
    result = plan_orders(book('C1', 'C2', 'C3', 'C4', spares=['S1']),
                         [swap_order('O2', 'C3', 'C4', 'C', 'D'),
                          swap_order('O1', 'C1', 'C2', 'A', 'B')])
    first, second = result['orders']['O1'], result['orders']['O2']
    assert first['status'] == 'READY' and first['spare'] == 'S1'
    assert second['status'] == 'BLOCKED' and second['spare'] is None
    assert SPARE_NOT_RECONCILED in second['reasons']   # 无剩余合格备用芯
    assert result['spare_assignments'] == {'O1': 'S1'}


def test_R4_two_spares_let_both_orders_start():
    result = plan_orders(book('C1', 'C2', 'C3', 'C4', spares=['S1', 'S2']),
                         [swap_order('O1'), swap_order('O2', 'C3', 'C4', 'C', 'D')])
    assert all(p['status'] == 'READY' for p in result['orders'].values())
    assert set(result['spare_assignments']) == {'O1', 'O2'}


# R4b 确定性仲裁：换序单顺序打乱，结论仍按编号稳定
def test_R4b_arbitration_is_deterministic_under_shuffle():
    orders = [swap_order('O9'), swap_order('O1'), swap_order('O5')]
    r1 = plan_orders(book('C1', 'C2', spares=['S1']), orders)
    r2 = plan_orders(book('C1', 'C2', spares=['S1']), list(reversed(orders)))
    assert r1['spare_assignments'] == r2['spare_assignments'] == {'O1': 'S1'}


# R4c 跨单抢同一目标芯位：后单标记争用且不产生步骤
def test_R4c_cross_order_target_collision_is_flagged():
    o1 = {'id': 'O1', 'moves': [{'service': 'A', 'from_core': 'C1', 'to_core': 'C3'}]}
    o2 = {'id': 'O2', 'moves': [{'service': 'B', 'from_core': 'C2', 'to_core': 'C3'}]}
    result = plan_orders(book('C1', 'C2', 'C3'), [o1, o2])
    assert result['orders']['O1']['status'] == 'READY'
    assert SPARE_CONTENDED in result['orders']['O2']['reasons']
    assert result['orders']['O2']['steps'] == []


# R5 同一换序单内自相矛盾（一芯两目标）按交叉计划拒绝
def test_R5_duplicate_target_within_order_rejected():
    bad = {'id': 'O1', 'moves': [
        {'service': 'A', 'from_core': 'C1', 'to_core': 'C3'},
        {'service': 'B', 'from_core': 'C2', 'to_core': 'C3'}]}
    result = plan_orders(book('C1', 'C2', 'C3'), [bad])
    assert MOVE_CROSSES_PLAN in result['orders']['O1']['reasons']
