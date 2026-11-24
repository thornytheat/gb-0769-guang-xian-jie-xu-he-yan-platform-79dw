"""换序 API 端到端测试：规划、开工、中断恢复、争用、签发。"""


def payload(spares=(('S1', True),), with_occupancy=None):
    cores = [{'id': 'C1', 'reconciled': True, 'spare': False},
             {'id': 'C2', 'reconciled': True, 'spare': False},
             {'id': 'C3', 'reconciled': True, 'spare': False},
             {'id': 'C4', 'reconciled': True, 'spare': False}]
    cores += [{'id': cid, 'reconciled': ok, 'spare': True} for cid, ok in spares]
    body = {'job_id': 'J1', 'core_book': {'cores': cores}, 'orders': [
        {'id': 'O1', 'moves': [
            {'service': 'A', 'from_core': 'C1', 'to_core': 'C2'},
            {'service': 'B', 'from_core': 'C2', 'to_core': 'C1'}]},
        {'id': 'O2', 'moves': [
            {'service': 'C', 'from_core': 'C3', 'to_core': 'C4'},
            {'service': 'D', 'from_core': 'C4', 'to_core': 'C3'}]}]}
    if with_occupancy is not None:
        body['occupancy'] = with_occupancy
    return body


def test_plan_endpoint_arbitrates_single_spare(client):
    r = client.post('/api/reswitch/plan', json=payload(spares=[('S1', True)]))
    assert r.status_code == 200
    data = r.get_json()
    assert data['orders']['O1']['status'] == 'READY'
    assert data['orders']['O2']['status'] == 'BLOCKED'
    assert data['spare_assignments'] == {'O1': 'S1'}


def test_open_and_resume_flow_keeps_one_archive(client):
    body = payload(spares=[('S1', True), ('S2', True)], with_occupancy={'services': [
        {'service': 'A', 'core': 'S1'}, {'service': 'B', 'core': 'C2'},
        {'service': 'C', 'core': 'S2'}, {'service': 'D', 'core': 'C4'}]})
    r = client.post('/api/reswitch/jobs', json=body)
    assert r.status_code == 201
    archive_id = r.get_json()['archive_id']
    # 两单各完成第一步 HOLD
    assert [s['action'] for s in r.get_json()['job']['completed_steps']] == ['HOLD', 'HOLD']

    # 中断恢复：两单全部换完
    r2 = client.post(f'/api/reswitch/jobs/{archive_id}/resume', json={
        'occupancy': {'services': [
            {'service': 'A', 'core': 'C2'}, {'service': 'B', 'core': 'C1'},
            {'service': 'C', 'core': 'C4'}, {'service': 'D', 'core': 'C3'}]}})
    assert r2.status_code == 200
    assert r2.get_json()['job']['finished'] is True

    # 签发通过，且只认实际完成的六个步骤（每单 HOLD/MOVE/RESTORE）
    r3 = client.get(f'/api/reswitch/jobs/{archive_id}/signoff')
    decision = r3.get_json()
    assert decision['approved'] is True
    assert decision['completed_step_count'] == 6


def test_resume_contradictory_occupancy_returns_409(client):
    r = client.post('/api/reswitch/jobs', json=payload(spares=[('S1', True), ('S2', True)]))
    archive_id = r.get_json()['archive_id']
    bad = client.post(f'/api/reswitch/jobs/{archive_id}/resume', json={
        'occupancy': {'services': [
            {'service': 'A', 'core': 'C9'}, {'service': 'B', 'core': 'C2'},
            {'service': 'C', 'core': 'C3'}, {'service': 'D', 'core': 'C4'}]}})
    assert bad.status_code == 409


def test_resume_incomplete_occupancy_returns_409(client):
    r = client.post('/api/reswitch/jobs', json=payload(spares=[('S1', True), ('S2', True)]))
    archive_id = r.get_json()['archive_id']
    bad = client.post(f'/api/reswitch/jobs/{archive_id}/resume', json={
        'occupancy': {'services': [{'service': 'A', 'core': 'S1'}]}})
    assert bad.status_code == 409


def test_signoff_blocked_midway(client):
    r = client.post('/api/reswitch/jobs', json=payload())
    archive_id = r.get_json()['archive_id']
    decision = client.get(f'/api/reswitch/jobs/{archive_id}/signoff').get_json()
    assert decision['approved'] is False
    assert 'REMAINING_STEPS' in decision['blockers']


def test_resume_unknown_archive_returns_404(client):
    r = client.post('/api/reswitch/jobs/nope/resume', json={
        'occupancy': {'services': [{'service': 'A', 'core': 'C1'}]}})
    assert r.status_code == 404


def test_unreconciled_spare_blocks_swap(client):
    r = client.post('/api/reswitch/plan', json=payload(spares=[('S1', False)]))
    data = r.get_json()
    assert all(p['status'] == 'BLOCKED' for p in data['orders'].values())
    assert data['spare_assignments'] == {}


def test_invalid_payload_rejected(client):
    assert client.post('/api/reswitch/plan', json={'job_id': 'x'}).status_code == 400
    bad_flag = payload()
    bad_flag['core_book']['cores'][0]['reconciled'] = 'yes'
    assert client.post('/api/reswitch/plan', json=bad_flag).status_code == 400


def test_missing_json_object_rejected(client):
    assert client.post('/api/reswitch/plan', data='not-json').status_code == 400
