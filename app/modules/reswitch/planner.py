"""换序规划核心。

把一批换序单（业务从源芯迁到目标芯的有向移动）分解成可在停纤窗口内
安全执行的单芯步骤。规则：

* 只有双端对账合格（``reconciled`` 为真）的芯位才能进入换序页面——源、
  目标、备用任一不合格即隔离，不产生占芯动作；
* 两条业务互换（长度 2 的环）必须借一个合格备用芯拆成三步，缺合格
  备用芯时两条业务都挂起，互不占位；
* 更长的环可直接旋转腾挪、链按逆序腾挪，都不需要备用芯；
* 多张换序单争用同一备用芯时，按换序单编号确定性仲裁，先单占用，
  后单整体挂起并标记 ``SPARE_CONTENDED``/``SPARE_NOT_RECONCILED``。
"""

# 阻塞原因码（同时也是留档/签发可识别的稳定枚举）
SOURCE_NOT_RECONCILED = 'SOURCE_NOT_RECONCILED'
TARGET_NOT_RECONCILED = 'TARGET_NOT_RECONCILED'
MOVE_CROSSES_PLAN = 'MOVE_CROSSES_PLAN'
SWAP_WITHOUT_SPARE = 'SWAP_WITHOUT_SPARE'
SPARE_CONTENDED = 'SPARE_CONTENDED'
SPARE_NOT_RECONCILED = 'SPARE_NOT_RECONCILED'

# 步骤动作码
MOVE = 'MOVE'          # 业务从一个芯位移到空芯位
HOLD = 'HOLD'          # 互换拆步中的临时落位（源 -> 备用）
RESTORE = 'RESTORE'    # 互换拆步中的回位（备用 -> 最终芯位）


def plan_orders(core_book, orders, reserved_spares=None):
    """对一批换序单排出可执行步骤。

    ``core_book``：芯位台账，``{'cores': [{'id','reconciled','spare'}...]}``，
    其中 ``spare`` 为真表示该芯位是备用芯（无承载业务）。
    ``orders``：换序单列表，每张为
    ``{'id','moves':[{'service','from_core','to_core'}...]}``。
    ``reserved_spares``：恢复/争用场景下已被占用的备用芯映射
    ``{order_id: spare_id}``，仲裁时这些芯不再参与分配。

    返回 ``{'orders': {order_id: order_plan}, 'steps': [...],
    'blocked': [...], 'spare_assignments': {...}}``。``steps`` 按执行
    顺序汇总所有开工换序单的步骤。
    """
    reserved_spares = dict(reserved_spares or {})
    cores = {c['id']: c for c in core_book['cores']}
    qualified = {cid for cid, c in cores.items() if c['reconciled']}
    free_spares = sorted(cid for cid, c in cores.items()
                         if c['spare'] and c['reconciled']
                         and cid not in reserved_spares.values())

    target_owners = {}
    blocked, plans, assignments = [], {}, {}

    for order in sorted(orders, key=lambda o: o['id']):
        order_id = order['id']
        order_block = _validate_order(order_id, order['moves'], cores,
                                      qualified, target_owners)
        if order_block:
            reasons = sorted({b['reason'] for b in order_block})
            blocked.extend(order_block)
            plans[order_id] = {'order_id': order_id, 'status': 'BLOCKED',
                               'reasons': reasons, 'steps': [], 'spare': None}
            continue

        graph = {m['from_core']: m['to_core'] for m in order['moves']}
        cycles, chains = _decompose(graph)
        needs_spare = any(len(cycle) == 2 for cycle in cycles)

        spare_id = reserved_spares.get(order_id)
        if needs_spare and not spare_id:
            if not free_spares:
                reason = SPARE_NOT_RECONCILED if any(c['spare'] for c in cores.values()) \
                    else SWAP_WITHOUT_SPARE
                blocked.append({'order_id': order_id, 'reason': reason})
                plans[order_id] = {'order_id': order_id, 'status': 'BLOCKED',
                                   'reasons': [reason], 'steps': [], 'spare': None}
                continue
            spare_id = free_spares.pop(0)
            assignments[order_id] = spare_id

        steps = _emit_steps(order_id, order['moves'], graph, cycles, chains, spare_id)
        for m in order['moves']:
            target_owners.setdefault(m['to_core'], order_id)
        plans[order_id] = {'order_id': order_id, 'status': 'READY',
                           'reasons': [], 'steps': steps, 'spare': spare_id}

    # 全局执行序列按阶段交错：先把所有互换业务临时落到备用芯（HOLD），
    # 再做腾出芯位的 MOVE，最后回位 RESTORE；阶段内按换序单编号排序。
    # 这样序列的任意前缀都对应一个现场可达成的真实状态。
    phase_rank = {HOLD: 0, MOVE: 1, RESTORE: 2}
    all_steps = sorted((step for p in plans.values() for step in p['steps']),
                       key=lambda s: (phase_rank[s['action']], s['order_id'], s['seq']))
    for index, step in enumerate(all_steps, start=1):
        step['global_seq'] = index
    return {'orders': plans, 'steps': all_steps, 'blocked': blocked,
            'spare_assignments': assignments}


def _validate_order(order_id, moves, cores, qualified, target_owners):
    blocks = []
    seen_sources, seen_targets = set(), set()
    for m in moves:
        src, dst = m['from_core'], m['to_core']
        if src not in cores or src not in qualified:
            blocks.append({'order_id': order_id, 'reason': SOURCE_NOT_RECONCILED,
                           'service': m['service'], 'core': src})
        if dst not in cores or dst not in qualified:
            blocks.append({'order_id': order_id, 'reason': TARGET_NOT_RECONCILED,
                           'service': m['service'], 'core': dst})
        if src in seen_sources or dst in seen_targets:
            blocks.append({'order_id': order_id, 'reason': MOVE_CROSSES_PLAN,
                           'service': m['service'], 'core': dst})
        seen_sources.add(src)
        seen_targets.add(dst)
        owner = target_owners.get(dst)
        if owner and owner != order_id:
            blocks.append({'order_id': order_id, 'reason': SPARE_CONTENDED,
                           'service': m['service'], 'core': dst})
    return blocks


def _decompose(graph):
    """把有向移动图拆成环与链。合法图每个芯位入度/出度至多为 1。"""
    indegree = {node: 0 for node in graph}
    for dst in graph.values():
        indegree[dst] = indegree.get(dst, 0) + 1
        indegree.setdefault(dst, indegree[dst])

    # 链：从入度为 0 的源头沿边走到尾
    chains, in_chain = [], set()
    for start in sorted(n for n, d in indegree.items() if d == 0 and n in graph):
        path, node = [], start
        while node in graph:
            path.append(node)
            node = graph[node]
        chains.append(path)
        in_chain.update(path)

    # 环：剩余节点沿边走一圈
    cycles, visited = [], set(in_chain)
    for start in graph:
        if start in visited:
            continue
        cycle, node = [], start
        while node not in visited:
            visited.add(node)
            cycle.append(node)
            node = graph[node]
        cycles.append(cycle)
    return cycles, chains


def _emit_steps(order_id, moves, graph, cycles, chains, spare_id):
    service = {m['from_core']: m['service'] for m in moves}
    steps, seq = [], 0

    def add(action, svc, frm, to):
        nonlocal seq
        seq += 1
        steps.append({'seq': seq, 'order_id': order_id, 'action': action,
                      'service': svc, 'from_core': frm, 'to_core': to})

    for cycle in cycles:
        if len(cycle) == 2:                    # 互换：借合格备用芯拆三步
            a, b = cycle
            add(HOLD, service[a], a, spare_id)
            add(MOVE, service[b], b, a)
            add(RESTORE, service[a], spare_id, b)
        else:                                  # 长环：从最后一个节点起旋转腾挪
            for src in reversed(cycle):
                add(MOVE, service[src], src, graph[src])
    for chain in chains:                       # 链：从末端往源头倒序腾
        for src in reversed(chain):
            add(MOVE, service[src], src, graph[src])
    return steps
