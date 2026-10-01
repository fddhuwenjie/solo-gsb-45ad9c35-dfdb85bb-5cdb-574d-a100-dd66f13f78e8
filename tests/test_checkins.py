"""现场签到快照模块测试。

覆盖需求：
- 提交快照：request_id 幂等键 + expected_event_seq 序号快照 +
  目标轮次（必须是尚未生成的下一轮）+ 完整到场/缺席编号集合；
  集合必须恰好覆盖当前报名名单（编号重复/跨集合重复/名单外编号/
  遗漏均 409 不落库）；目标轮次已生成或不是下一轮 409 不落库；
  序号冲突 409 不落库不占号；首次版本 1、状态 draft；
- 更新：名单实际变更版本 +1 并回到 draft（confirmed 后变更亦然）；
  名单无变化（换 request_id）不推进版本、不产生事件；
- 确认：confirmed + confirmed_at；重复确认（新 request_id）不
  重复产生事件；未提交快照不能确认；
- 幂等：同一 request_id 重放原样返回首次结果（版本/状态不变、
  不产生新事件）；同键不同参数 409；
- 查询：返回版本与完整名单；404；赛事快照列表；
- 生成下一轮：仅到场者进入瑞士配对与轮空选择；缺席者不获得本轮
  积分（含奇数到场者时轮空给到场者）；全员缺席/单人到场即时
  封盘；快照置 consumed 后拒绝再变更；未提交快照沿用全员编排；
  快照提交后新增参赛者（首轮前）生成时 409；
- 缺席者保留原有积分与历史对局，已结束轮次不受影响；
- 事件：attendance.recorded / attendance.confirmed 同事务进入
  事件流，round.generated 载荷带 attendance_snapshot；被拒绝
  请求不产生事件、不占序号；
- 并发：同快照并发更新仅一成（写锁串行 + 唯一约束）。
"""
import concurrent.futures
import threading


# ---------- 工具 ----------

def create_tournament(client, name="签到测试赛"):
    r = client.post("/tournaments", json={"name": name})
    assert r.status_code == 201
    return r.json()["id"]


def add_players(client, tid, names):
    r = client.post(
        f"/tournaments/{tid}/participants/bulk", json={"names": names}
    )
    assert r.status_code == 201
    return r.json()


def event_seq(client, tid):
    return client.get(f"/tournaments/{tid}/events").json()["last_seq"]


def events_of(client, tid, type_=None):
    items = client.get(f"/tournaments/{tid}/events?limit=200").json()["items"]
    return [e for e in items if type_ is None or e["type"] == type_]


def next_round(client, tid, expected=201):
    r = client.post(f"/tournaments/{tid}/rounds/next")
    assert r.status_code == expected, r.text
    return r.json()


def put_result(client, match, sw, sb):
    mid = match["id"] if isinstance(match, dict) else match
    r = client.put(
        f"/matches/{mid}/result", json={"score_white": sw, "score_black": sb}
    )
    assert r.status_code == 200, r.text
    return r.json()


def close_round(client, rnd):
    for m in rnd["matches"]:
        if not m["is_bye"]:
            put_result(client, m, 1.0, 0.0)


def checkin(client, tid, round_no, request_id, seq, present, absent, expected=None):
    body = {
        "request_id": request_id,
        "expected_event_seq": seq,
        "round_no": round_no,
        "present_numbers": present,
        "absent_numbers": absent,
    }
    r = client.put(f"/tournaments/{tid}/rounds/{round_no}/checkin", json=body)
    if expected is not None:
        assert r.status_code == expected, r.text
    return r


def confirm(client, tid, round_no, request_id, seq, expected=None):
    body = {"request_id": request_id, "expected_event_seq": seq}
    r = client.post(
        f"/tournaments/{tid}/rounds/{round_no}/checkin/confirm", json=body
    )
    if expected is not None:
        assert r.status_code == expected, r.text
    return r


def get_checkin(client, tid, round_no):
    return client.get(f"/tournaments/{tid}/rounds/{round_no}/checkin")


def setup_tournament(client, n=5):
    """n 人赛事，返回 (tid, players)。"""
    tid = create_tournament(client)
    players = add_players(client, tid, [f"选手{i}" for i in range(1, n + 1)])
    return tid, players


def close_first_round(client, tid, n=5):
    """生成并以白胜录完第 1 轮，返回第 1 轮响应。"""
    rnd = next_round(client, tid)
    close_round(client, rnd)
    rounds = client.get(f"/tournaments/{tid}/rounds").json()
    assert rounds[-1]["status"] == "closed"
    return rnd


# ---------- 提交快照 ----------

def test_create_snapshot_basic(client):
    tid, players = setup_tournament(client, 5)
    seq = event_seq(client, tid)  # 1 created + 5 registered = 6

    r = checkin(client, tid, 1, "ci-1", seq, [1, 2, 3, 4], [5])
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["version"] == 1
    assert data["status"] == "draft"
    assert data["round_no"] == 1
    assert data["present_numbers"] == [1, 2, 3, 4]
    assert data["absent_numbers"] == [5]
    assert data["confirmed_at"] is None
    # 名单完整且按编号升序
    assert [row["number"] for row in data["roster"]] == [1, 2, 3, 4, 5]
    by_no = {row["number"]: row for row in data["roster"]}
    assert by_no[5]["present"] is False
    assert by_no[1]["present"] is True
    assert by_no[1]["participant_id"] == players[0]["id"]


def test_all_absent_snapshot_ok(client):
    tid, _ = setup_tournament(client, 3)
    seq = event_seq(client, tid)
    r = checkin(client, tid, 1, "ci-all-absent", seq, [], [1, 2, 3])
    assert r.status_code == 201, r.text
    assert r.json()["present_numbers"] == []
    assert r.json()["absent_numbers"] == [1, 2, 3]


def test_duplicate_numbers_within_present_rejected(client):
    tid, _ = setup_tournament(client, 5)
    seq = event_seq(client, tid)
    r = checkin(client, tid, 1, "ci-dup", seq, [1, 1, 2, 3], [4, 5])
    assert r.status_code == 409
    assert "重复" in r.json()["detail"]
    # 不落库
    assert get_checkin(client, tid, 1).status_code == 404
    # 不占事件序号
    assert event_seq(client, tid) == seq


def test_duplicate_numbers_within_absent_rejected(client):
    tid, _ = setup_tournament(client, 5)
    seq = event_seq(client, tid)
    r = checkin(client, tid, 1, "ci-dup2", seq, [1, 2, 3], [4, 5, 5])
    assert r.status_code == 409
    assert event_seq(client, tid) == seq


def test_overlap_present_absent_rejected(client):
    tid, _ = setup_tournament(client, 5)
    seq = event_seq(client, tid)
    r = checkin(client, tid, 1, "ci-overlap", seq, [1, 2, 3], [3, 4, 5])
    assert r.status_code == 409
    assert "3" in r.json()["detail"]
    assert event_seq(client, tid) == seq
    assert get_checkin(client, tid, 1).status_code == 404


def test_unknown_number_rejected(client):
    tid, _ = setup_tournament(client, 5)
    seq = event_seq(client, tid)
    r = checkin(client, tid, 1, "ci-unknown", seq, [1, 2, 3, 4, 6], [])
    assert r.status_code == 409
    assert "不在当前报名名单" in r.json()["detail"]
    assert event_seq(client, tid) == seq


def test_incomplete_coverage_rejected(client):
    tid, _ = setup_tournament(client, 5)
    seq = event_seq(client, tid)
    # 漏了 5
    r = checkin(client, tid, 1, "ci-missing", seq, [1, 2, 3], [4])
    assert r.status_code == 409
    assert "未覆盖" in r.json()["detail"]
    assert event_seq(client, tid) == seq
    assert get_checkin(client, tid, 1).status_code == 404


def test_seq_conflict_rejected_without_record(client):
    tid, _ = setup_tournament(client, 5)
    seq = event_seq(client, tid)
    r = checkin(client, tid, 1, "ci-stale", seq - 1, [1, 2, 3, 4, 5], [])
    assert r.status_code == 409
    assert "赛事快照已变化" in r.json()["detail"]
    assert get_checkin(client, tid, 1).status_code == 404
    assert event_seq(client, tid) == seq
    # 被拒绝的 request_id 不占幂等记录：同键以正确序号重试应成功
    r2 = checkin(client, tid, 1, "ci-stale", seq, [1, 2, 3, 4, 5], [])
    assert r2.status_code == 201


def test_target_round_already_generated_rejected(client):
    tid, _ = setup_tournament(client, 5)
    next_round(client, tid)  # 第 1 轮已生成（奇数 5 人含轮空，仍 open）
    seq = event_seq(client, tid)
    r = checkin(client, tid, 1, "ci-late", seq, [1, 2, 3, 4], [5])
    assert r.status_code == 409
    # 第 1 轮存在且 open：先报「尚未完成」；核心是拒绝且不落库
    assert "已生成" in r.json()["detail"] or "尚未完成" in r.json()["detail"]
    assert event_seq(client, tid) == seq


def test_target_round_not_next_rejected(client):
    tid, _ = setup_tournament(client, 5)
    seq = event_seq(client, tid)
    # 首轮尚未生成，不能直接为第 2 轮提交
    r = checkin(client, tid, 2, "ci-r2", seq, [1, 2, 3, 4, 5], [])
    assert r.status_code == 409
    assert "下一轮" in r.json()["detail"]
    assert event_seq(client, tid) == seq


def test_path_body_round_mismatch(client):
    tid, _ = setup_tournament(client, 5)
    body = {
        "request_id": "ci-mismatch",
        "expected_event_seq": event_seq(client, tid),
        "round_no": 2,
        "present_numbers": [1, 2, 3, 4, 5],
        "absent_numbers": [],
    }
    r = client.put(f"/tournaments/{tid}/rounds/1/checkin", json=body)
    assert r.status_code == 409


# ---------- 更新 / 确认 ----------

def test_update_version_increment_and_status_reset(client):
    tid, _ = setup_tournament(client, 5)
    seq = event_seq(client, tid)
    r1 = checkin(client, tid, 1, "ci-1", seq, [1, 2, 3, 4, 5], [])
    assert r1.status_code == 201
    # 确认
    seq2 = event_seq(client, tid)
    rc = confirm(client, tid, 1, "ci-confirm-1", seq2)
    assert rc.status_code == 201
    assert rc.json()["status"] == "confirmed"
    assert rc.json()["confirmed_at"] is not None

    # 变更名单（5 号缺席）：版本 +1，回到 draft，confirmed_at 清空
    seq3 = event_seq(client, tid)
    r2 = checkin(client, tid, 1, "ci-2", seq3, [1, 2, 3, 4], [5])
    assert r2.status_code == 200
    data = r2.json()
    assert data["version"] == 2
    assert data["status"] == "draft"
    assert data["confirmed_at"] is None
    assert data["absent_numbers"] == [5]
    assert data["expected_event_seq"] == seq3


def test_no_change_submission_keeps_version(client):
    tid, _ = setup_tournament(client, 5)
    seq = event_seq(client, tid)
    checkin(client, tid, 1, "ci-1", seq, [1, 2, 3, 4], [5])
    assert event_seq(client, tid) == seq + 1

    # 不同 request_id、同一名单（顺序打乱）：不推进版本、不产生事件
    seq2 = event_seq(client, tid)
    r = checkin(
        client, tid, 1, "ci-resubmit", seq2,
        [4, 3, 2, 1], [5],
    )
    assert r.status_code == 200
    assert r.json()["version"] == 1
    assert event_seq(client, tid) == seq2  # 无新事件


def test_confirm_without_snapshot_rejected(client):
    tid, _ = setup_tournament(client, 5)
    seq = event_seq(client, tid)
    r = confirm(client, tid, 1, "ci-c-noop", seq, expected=409)
    assert "尚未提交" in r.json()["detail"]
    assert event_seq(client, tid) == seq


def test_confirm_then_reconfirm_no_extra_event(client):
    tid, _ = setup_tournament(client, 5)
    seq = event_seq(client, tid)
    checkin(client, tid, 1, "ci-1", seq, [1, 2, 3, 4, 5], [])
    first_confirm_seq = event_seq(client, tid)
    confirm(client, tid, 1, "ci-c1", first_confirm_seq)
    assert event_seq(client, tid) == first_confirm_seq + 1

    # 已确认后再次确认（新 request_id、最新序号）：幂等返回，无事件
    r = confirm(
        client, tid, 1, "ci-c2", event_seq(client, tid), expected=200
    )
    assert r.json()["status"] == "confirmed"
    recorded = events_of(client, tid, "attendance.confirmed")
    assert len(recorded) == 1


def test_confirm_seq_conflict_rejected(client):
    tid, _ = setup_tournament(client, 5)
    seq = event_seq(client, tid)
    checkin(client, tid, 1, "ci-1", seq, [1, 2, 3, 4, 5], [])
    # 用过期序号确认 -> 409
    r = confirm(client, tid, 1, "ci-c-stale", seq, expected=409)
    assert "赛事快照已变化" in r.json()["detail"]
    assert events_of(client, tid, "attendance.confirmed") == []


def test_confirm_rejected_request_id_not_consumed(client):
    tid, _ = setup_tournament(client, 5)
    seq = event_seq(client, tid)
    checkin(client, tid, 1, "ci-1", seq, [1, 2, 3, 4, 5], [])
    # 过期序号失败
    confirm(client, tid, 1, "ci-c", seq, expected=409)
    # 同 request_id 以正确序号重试 -> 成功
    confirm(client, tid, 1, "ci-c", seq + 1, expected=201)


# ---------- 幂等 ----------

def test_record_idempotent_replay(client):
    tid, _ = setup_tournament(client, 5)
    seq = event_seq(client, tid)
    body = dict(
        request_id="ci-idem", expected_event_seq=seq, round_no=1,
        present_numbers=[1, 2, 3], absent_numbers=[4, 5],
    )
    r1 = client.put(f"/tournaments/{tid}/rounds/1/checkin", json=body)
    assert r1.status_code == 201
    first = r1.json()

    # 重放：即便此时赛事序号已经推进（例如其他人又提交），仍原样
    # 返回首次结果——这里序号未变，直接重放
    r2 = client.put(f"/tournaments/{tid}/rounds/1/checkin", json=body)
    assert r2.status_code == 200  # 重放沿用首次响应对象，但 HTTP 为 200
    assert r2.json() == first
    # 只有首次产生了 attendance.recorded 事件
    rec = events_of(client, tid, "attendance.recorded")
    assert len(rec) == 1
    assert rec[0]["seq"] == seq + 1


def test_same_request_id_different_params_conflict(client):
    tid, _ = setup_tournament(client, 5)
    seq = event_seq(client, tid)
    checkin(client, tid, 1, "ci-x", seq, [1, 2, 3, 4, 5], [])
    # 同 request_id 不同名单 -> 409
    r = checkin(client, tid, 1, "ci-x", seq + 1, [1, 2, 3, 4], [5])
    assert r.status_code == 409
    assert "request_id" in r.json()["detail"]
    # 快照仍是首次的全员到场
    data = get_checkin(client, tid, 1).json()
    assert data["absent_numbers"] == []


def test_same_request_id_different_action_conflict(client):
    tid, _ = setup_tournament(client, 5)
    seq = event_seq(client, tid)
    # 先把该 id 用于 record
    checkin(client, tid, 1, "shared-id", seq, [1, 2, 3, 4, 5], [])
    # 再用于 confirm -> 409
    r = confirm(client, tid, 1, "shared-id", seq + 1, expected=409)
    assert "request_id" in r.json()["detail"]


def test_confirm_idempotent_replay(client):
    tid, _ = setup_tournament(client, 5)
    seq = event_seq(client, tid)
    checkin(client, tid, 1, "ci-1", seq, [1, 2, 3, 4, 5], [])
    body = {"request_id": "ci-cidem", "expected_event_seq": seq + 1}
    r1 = client.post(f"/tournaments/{tid}/rounds/1/checkin/confirm", json=body)
    assert r1.status_code == 201
    first = r1.json()
    r2 = client.post(f"/tournaments/{tid}/rounds/1/checkin/confirm", json=body)
    assert r2.status_code == 200
    assert r2.json() == first
    assert len(events_of(client, tid, "attendance.confirmed")) == 1


# ---------- 查询 ----------

def test_get_snapshot_404_and_list(client):
    tid, _ = setup_tournament(client, 5)
    assert get_checkin(client, tid, 1).status_code == 404
    seq = event_seq(client, tid)
    checkin(client, tid, 1, "ci-1", seq, [1, 2, 3], [4, 5])
    data = get_checkin(client, tid, 1).json()
    assert data["version"] == 1
    assert [row["number"] for row in data["roster"]] == [1, 2, 3, 4, 5]
    listing = client.get(f"/tournaments/{tid}/checkins").json()
    assert [s["round_no"] for s in listing] == [1]


def test_cross_tournament_isolation(client):
    t1, _ = setup_tournament(client, 3)
    t2, _ = setup_tournament(client, 3)
    seq1 = event_seq(client, t1)
    checkin(client, t1, 1, "ci-t1", seq1, [1, 2, 3], [])
    assert get_checkin(client, t2, 1).status_code == 404
    # 同 request_id 可在另一赛事使用
    seq2 = event_seq(client, t2)
    r = checkin(client, t2, 1, "ci-t1", seq2, [1, 2], [3])
    assert r.status_code == 201


# ---------- 生成下一轮消费快照 ----------

def test_generate_round_only_present_pair_and_absent_no_points(client):
    # 5 人：第 1 轮全员；第 2 轮 4 人到场（偶数），选手 5 缺席
    tid, players = setup_tournament(client, 5)
    r1 = close_first_round(client, tid, 5)
    # 全员第 1 轮：(1,2)(3,4)，5 轮空
    assert any(m["is_bye"] for m in r1["matches"])

    before = {
        row["participant_id"]: row["points"]
        for row in client.get(f"/tournaments/{tid}/standings").json()
    }

    seq = event_seq(client, tid)
    checkin(client, tid, 2, "ci-r2", seq, [1, 2, 3, 4], [5], expected=201)

    r2 = next_round(client, tid)
    # 只有两场对局，没有轮空
    real = [m for m in r2["matches"] if not m["is_bye"]]
    byes = [m for m in r2["matches"] if m["is_bye"]]
    assert len(real) == 2
    assert byes == []
    involved = {
        pid
        for m in real
        for pid in (m["player_white"]["number"], m["player_black"]["number"])
    }
    assert involved == {1, 2, 3, 4}

    # 快照已消费
    snap = get_checkin(client, tid, 2).json()
    assert snap["status"] == "consumed"

    # round.generated 载荷带签到快照
    gen = events_of(client, tid, "round.generated")[-1]
    att = gen["payload"]["attendance_snapshot"]
    assert att["present_numbers"] == [1, 2, 3, 4]
    assert att["absent_numbers"] == [5]
    assert att["version"] == 1

    # 第 2 轮全部录完后，缺席者 5 的积分不变（仍是第 1 轮轮空的 1 分）
    close_round(client, r2)
    after = {
        row["participant_id"]: row
        for row in client.get(f"/tournaments/{tid}/standings").json()
    }
    p5 = players[4]["id"]
    assert after[p5]["points"] == before[p5] == 1.0
    # played 不增加
    assert after[p5]["played"] == 0
    assert after[p5]["byes"] == 1


def test_bye_only_among_present(client):
    # 5 人第 1 轮（奇数）；第 2 轮 5 人到场 4 人 -> 4 偶数无轮空；
    # 这里构造第 2 轮到场 3 人（奇数）：轮空必须在到场者中产生，
    # 且缺席的 4、5 不获得任何场次
    tid, _ = setup_tournament(client, 5)
    close_first_round(client, tid, 5)
    seq = event_seq(client, tid)
    checkin(client, tid, 2, "ci-r2", seq, [1, 2, 3], [4, 5], expected=201)
    r2 = next_round(client, tid)
    byes = [m for m in r2["matches"] if m["is_bye"]]
    assert len(byes) == 1
    assert byes[0]["player_white"]["number"] in {1, 2, 3}
    real = [m for m in r2["matches"] if not m["is_bye"]]
    assert len(real) == 1
    involved = {
        m["player_white"]["number"] for m in real
    } | {m["player_black"]["number"] for m in real}
    assert involved <= {1, 2, 3}


def test_all_absent_generates_empty_closed_round(client):
    tid, _ = setup_tournament(client, 5)
    close_first_round(client, tid, 5)
    seq = event_seq(client, tid)
    checkin(client, tid, 2, "ci-none", seq, [], [1, 2, 3, 4, 5], expected=201)
    r2 = next_round(client, tid)
    assert r2["matches"] == []
    assert r2["status"] == "closed"
    # 即时封盘事件
    closed = events_of(client, tid, "round.closed")[-1]
    assert closed["payload"]["round_no"] == 2
    assert closed["payload"]["reason"] == "no_matches"
    standings = client.get(f"/tournaments/{tid}/standings").json()
    # 无人本轮得分：全员积分仍只来自第 1 轮
    rounds = client.get(f"/tournaments/{tid}/rounds").json()
    assert [r["status"] for r in rounds] == ["closed", "closed"]


def test_single_present_gets_bye_and_round_closes(client):
    tid, _ = setup_tournament(client, 5)
    close_first_round(client, tid, 5)
    seq = event_seq(client, tid)
    checkin(client, tid, 2, "ci-one", seq, [2], [1, 3, 4, 5], expected=201)
    r2 = next_round(client, tid)
    assert r2["status"] == "closed"
    assert len(r2["matches"]) == 1
    m = r2["matches"][0]
    assert m["is_bye"] is True
    assert m["player_white"]["number"] == 2
    assert m["score_white"] == 1.0


def test_consumed_snapshot_cannot_change_or_confirm(client):
    tid, _ = setup_tournament(client, 5)
    close_first_round(client, tid, 5)
    seq = event_seq(client, tid)
    checkin(client, tid, 2, "ci-r2", seq, [1, 2, 3, 4], [5], expected=201)
    next_round(client, tid)
    # 再提交 -> 409
    new_seq = event_seq(client, tid)
    r = checkin(
        client, tid, 2, "ci-r2-change", new_seq, [1, 2, 3, 4, 5], []
    )
    assert r.status_code == 409
    # 第 2 轮已生成且 open：先报「尚未完成」；closed 后由「已生成」兜底
    assert "已生成" in r.json()["detail"] or "尚未完成" in r.json()["detail"]
    # 确认 -> 409
    rc = confirm(client, tid, 2, "ci-r2-confirm", new_seq, expected=409)
    assert "已生成" in rc.json()["detail"] or "尚未完成" in rc.json()["detail"]
    # 不产生事件
    assert event_seq(client, tid) == new_seq


def test_no_snapshot_keeps_full_roster_pairing(client):
    # 未提交快照：行为与旧版一致（5 人奇数，一人轮空）
    tid, _ = setup_tournament(client, 5)
    r1 = next_round(client, tid)
    assert len([m for m in r1["matches"] if not m["is_bye"]]) == 2
    assert len([m for m in r1["matches"] if m["is_bye"]]) == 1
    gen = events_of(client, tid, "round.generated")[-1]
    assert "attendance_snapshot" not in gen["payload"]


def test_snapshot_stale_against_new_registration_rejected_at_generate(client):
    # 首轮生成前提交快照后继续报名：名单漂移，生成时 409
    tid, _ = setup_tournament(client, 5)
    seq = event_seq(client, tid)
    checkin(client, tid, 1, "ci-r1", seq, [1, 2, 3, 4, 5], [])
    r = client.post(
        f"/tournaments/{tid}/participants", json={"name": "新人"}
    )
    assert r.status_code == 201
    rgen = client.post(f"/tournaments/{tid}/rounds/next")
    assert rgen.status_code == 409
    assert "签到快照" in rgen.json()["detail"]


def test_absent_player_history_and_closed_rounds_unchanged(client):
    tid, players = setup_tournament(client, 5)
    close_first_round(client, tid, 5)
    # 5 号首轮轮空：第 1 轮封盘后留档
    r1_closed = client.get(f"/tournaments/{tid}/rounds/1").json()
    assert r1_closed["status"] == "closed"

    seq = event_seq(client, tid)
    checkin(client, tid, 2, "ci-r2", seq, [1, 2, 3, 4], [5])
    r2 = next_round(client, tid)
    rounds = client.get(f"/tournaments/{tid}/rounds").json()
    first = rounds[0]
    assert len(first["matches"]) == len(r1_closed["matches"])
    for m_new, m_old in zip(first["matches"], r1_closed["matches"]):
        assert m_new["id"] == m_old["id"]
        assert m_new["score_white"] == m_old["score_white"]
        assert m_new["score_black"] == m_old["score_black"]
    # 5 号没出现在第 2 轮
    assert not any(
        (m["player_white"] and m["player_white"]["number"] == 5)
        or (m["player_black"] and m["player_black"]["number"] == 5)
        for m in r2["matches"]
    )


def test_snapshot_creation_not_allowed_when_round_open(client):
    # 第 1 轮已生成但未封盘（open）：为第 2 轮提交快照应被拒绝
    tid, _ = setup_tournament(client, 5)
    next_round(client, tid)
    seq = event_seq(client, tid)
    r = checkin(client, tid, 2, "ci-open", seq, [1, 2, 3, 4], [5])
    assert r.status_code == 409
    assert "下一轮" in r.json()["detail"] or "尚未" in r.json()["detail"]


# ---------- 事件 ----------

def test_events_emitted_in_same_txn_and_ordered(client):
    tid, _ = setup_tournament(client, 5)
    seq = event_seq(client, tid)
    checkin(client, tid, 1, "ci-1", seq, [1, 2, 3], [4, 5])
    confirm(client, tid, 1, "ci-c", seq + 1)
    rec = events_of(client, tid, "attendance.recorded")
    con = events_of(client, tid, "attendance.confirmed")
    assert len(rec) == 1 and len(con) == 1
    assert rec[0]["seq"] == seq + 1
    assert con[0]["seq"] == seq + 2
    p1 = rec[0]["payload"]
    assert p1["round_no"] == 1 and p1["version"] == 1
    assert p1["present_numbers"] == [1, 2, 3]
    assert p1["absent_numbers"] == [4, 5]
    assert p1["expected_event_seq"] == seq
    # 更新（实际变更）再产生一条 recorded
    checkin(client, tid, 1, "ci-2", seq + 2, [1, 2, 3, 4], [5])
    rec2 = events_of(client, tid, "attendance.recorded")
    assert len(rec2) == 2
    assert rec2[-1]["payload"]["version"] == 2


# ---------- 并发 ----------

def test_concurrent_updates_only_one_wins(client):
    tid, _ = setup_tournament(client, 5)
    base_seq = event_seq(client, tid)
    checkin(client, tid, 1, "ci-init", base_seq, [1, 2, 3, 4, 5], [])

    # 两个并发更新以相同期望序号提交：写库前快照检查最多一成
    barrier = threading.Barrier(2)

    def update(req_id, absent_no):
        barrier.wait()
        return checkin(
            client, tid, 1, req_id, base_seq + 1,
            [n for n in range(1, 6) if n != absent_no], [absent_no],
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(update, "ci-u1", 4)
        f2 = pool.submit(update, "ci-u2", 5)
        r1, r2 = f1.result(), f2.result()

    statuses = {r1.status_code, r2.status_code}
    assert statuses == {200, 409}
    # 输家明确是序号冲突，且没有任何落库
    loser = r1 if r1.status_code == 409 else r2
    assert "赛事快照已变化" in loser.json()["detail"]
    # 版本只推进一次
    assert get_checkin(client, tid, 1).json()["version"] == 2
    # 只有一条新 recorded 事件
    assert len(events_of(client, tid, "attendance.recorded")) == 2
