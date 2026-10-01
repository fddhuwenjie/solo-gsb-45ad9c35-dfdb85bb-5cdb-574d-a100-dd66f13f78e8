"""现场签到快照：面向尚未生成的下一轮的可更新到场/缺席快照。

运营方在轮次生成前提交完整的到场/缺席参赛者编号集合：

- 目标必须是「尚未生成的下一轮」，目标轮次已生成或不是下一轮
  一律 409 且不落库；
- 到场/缺席集合的并集必须恰好覆盖当前报名名单（编号不得重复、
  不得遗漏、不得出现名单外编号），否则 409 且不落库；
- expected_event_seq 断言提交时赛事当前最大事件序号，序号冲突
  409 且不落库、不占事件序号；
- request_id 提供赛事内幂等：重复请求原样返回首次结果；同键不同
  参数 409。

首次提交版本为 1（draft），此后名单实际变更版本 +1 并回到 draft；
名单无变化的重复提交（不同 request_id）原样返回当前快照、不推进
版本。确认（confirm）置为 confirmed；确认后再次变更回到 draft。
快照变更（attendance.recorded）与确认（attendance.confirmed）与
快照本身在同一写事务内写入事件；被拒绝的请求不留任何记录。

生成下一轮（见 services/rounds.py）消费快照：仅到场者进入瑞士
配对与轮空选择；缺席者保留原有积分与历史对局但本轮不得分，也不
因缺席改变已结束轮次。未提交快照时沿用现有全员编排。

注意：本模块的写函数只在调用方事务内写入并 flush，不提交；快照、
名单行、幂等记录与 attendance.* 事件由路由层在同一事务中统一提交。
所有调用均处于进程级 writer_lock 内。
"""
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..events.service import (
    ATTENDANCE_CONFIRMED,
    ATTENDANCE_RECORDED,
    append_event,
)
from ..models import (
    CheckinAction,
    CheckinEntry,
    CheckinSnapshot,
    Event,
    Participant,
    Round,
)
from ..schemas import (
    CheckinConfirmRequest,
    CheckinSnapshotRequest,
    CheckinSnapshotResponse,
)
from .announcements import current_event_seq

RECORD = "record"
CONFIRM = "confirm"

DRAFT = "draft"
CONFIRMED = "confirmed"
CONSUMED = "consumed"


def next_round_no(db: Session, tournament_id: int) -> int:
    """赛事的下一轮轮次号（尚无任何轮次时为 1）。"""
    last = (
        db.query(Round.round_no)
        .filter(Round.tournament_id == tournament_id)
        .order_by(Round.round_no.desc())
        .first()
    )
    return (last[0] + 1) if last is not None else 1


def get_snapshot(
    db: Session, tournament_id: int, round_no: int
) -> CheckinSnapshot | None:
    return (
        db.query(CheckinSnapshot)
        .filter(
            CheckinSnapshot.tournament_id == tournament_id,
            CheckinSnapshot.round_no == round_no,
        )
        .first()
    )


def list_snapshots(
    db: Session, tournament_id: int
) -> list[CheckinSnapshot]:
    return list(
        db.query(CheckinSnapshot)
        .filter(CheckinSnapshot.tournament_id == tournament_id)
        .order_by(CheckinSnapshot.round_no)
        .all()
    )


def _participant_number_map(
    db: Session, tournament_id: int
) -> dict[int, Participant]:
    return {
        p.number: p
        for p in db.query(Participant)
        .filter(Participant.tournament_id == tournament_id)
        .all()
    }


def _validate_roster(
    number_map: dict[int, Participant],
    present_numbers: list[int],
    absent_numbers: list[int],
) -> dict[int, bool]:
    """校验两个集合恰好覆盖当前报名名单，返回 participant_id -> present。

    任一系列内编号重复、跨系列重复、出现名单外编号或遗漏名单内
    编号，均抛 409（调用方保证此时尚未写入任何内容）。
    """
    if len(present_numbers) != len(set(present_numbers)):
        raise HTTPException(status_code=409, detail="到场参赛者编号存在重复")
    if len(absent_numbers) != len(set(absent_numbers)):
        raise HTTPException(status_code=409, detail="缺席参赛者编号存在重复")

    present_set = set(present_numbers)
    absent_set = set(absent_numbers)
    overlap = present_set & absent_set
    if overlap:
        dup = ", ".join(str(n) for n in sorted(overlap))
        raise HTTPException(
            status_code=409, detail=f"参赛者编号同时出现在到场与缺席集合：{dup}"
        )

    roster = set(number_map)
    union = present_set | absent_set
    unknown = union - roster
    if unknown:
        nums = ", ".join(str(n) for n in sorted(unknown))
        raise HTTPException(
            status_code=409, detail=f"参赛者编号不在当前报名名单：{nums}"
        )
    missing = roster - union
    if missing:
        nums = ", ".join(str(n) for n in sorted(missing))
        raise HTTPException(
            status_code=409, detail=f"签到集合未覆盖以下报名参赛者：{nums}"
        )

    return {
        number_map[n].id: (n in present_set)
        for n in sorted(roster)
    }


def _record_fingerprint(req: CheckinSnapshotRequest) -> dict:
    return {
        "action": RECORD,
        "round_no": req.round_no,
        "expected_event_seq": req.expected_event_seq,
        "present_numbers": sorted(set(req.present_numbers)),
        "absent_numbers": sorted(set(req.absent_numbers)),
    }


def _confirm_fingerprint(round_no: int, req: CheckinConfirmRequest) -> dict:
    return {
        "action": CONFIRM,
        "round_no": round_no,
        "expected_event_seq": req.expected_event_seq,
    }


def _find_replay(
    db: Session, tournament_id: int, request_id: str, fingerprint: dict
) -> CheckinSnapshotResponse | None:
    """同 request_id 的已成功请求：参数一致返回首次响应，否则 409。"""
    action = (
        db.query(CheckinAction)
        .filter(
            CheckinAction.tournament_id == tournament_id,
            CheckinAction.request_id == request_id,
        )
        .first()
    )
    if action is None:
        return None
    if action.params == fingerprint:
        return CheckinSnapshotResponse(**action.response)
    raise HTTPException(
        status_code=409,
        detail="request_id 已被用于该赛事的其他签到请求，请更换 request_id",
    )


def serialize_snapshot(
    db: Session, snapshot: CheckinSnapshot
) -> CheckinSnapshotResponse:
    """组装快照响应：版本、状态与完整到场/缺席名单（按编号升序）。"""
    pids = [e.participant_id for e in snapshot.entries]
    participants = {
        p.id: p
        for p in db.query(Participant)
        .filter(Participant.id.in_(pids))
        .all()
    }
    rows = []
    present_numbers: list[int] = []
    absent_numbers: list[int] = []
    for entry in sorted(snapshot.entries, key=lambda e: participants[e.participant_id].number):
        p = participants[entry.participant_id]
        rows.append(
            {
                "participant_id": p.id,
                "number": p.number,
                "name": p.name,
                "present": entry.present,
            }
        )
        (present_numbers if entry.present else absent_numbers).append(p.number)

    return CheckinSnapshotResponse(
        id=snapshot.id,
        tournament_id=snapshot.tournament_id,
        round_no=snapshot.round_no,
        version=snapshot.version,
        status=snapshot.status,
        expected_event_seq=snapshot.expected_event_seq,
        roster=rows,
        present_numbers=present_numbers,
        absent_numbers=absent_numbers,
        created_at=snapshot.created_at,
        updated_at=snapshot.updated_at,
        confirmed_at=snapshot.confirmed_at,
    )


def _ensure_target_is_next_open_round(
    db: Session, tournament_id: int, round_no: int
) -> None:
    """目标必须是「尚未生成的下一轮」，否则 409（不落库）。

    存在 open 轮次时下一轮尚不可生成（必须先录完当前轮），此时也
    拒绝为任何未来轮次提交快照。
    """
    open_round = (
        db.query(Round)
        .filter(
            Round.tournament_id == tournament_id,
            Round.status == "open",
        )
        .first()
    )
    if open_round is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"第 {open_round.round_no} 轮尚未完成，"
                "当前轮次封盘后才能为下一轮提交签到快照"
            ),
        )
    target = (
        db.query(Round)
        .filter(
            Round.tournament_id == tournament_id,
            Round.round_no == round_no,
        )
        .first()
    )
    if target is not None:
        raise HTTPException(
            status_code=409,
            detail=f"第 {round_no} 轮已生成，不能再提交或变更该轮签到快照",
        )
    expected_next = next_round_no(db, tournament_id)
    if round_no != expected_next:
        raise HTTPException(
            status_code=409,
            detail=(
                f"只能为尚未生成的下一轮（第 {expected_next} 轮）提交签到快照，"
                f"请求目标为第 {round_no} 轮"
            ),
        )


def _ensure_snapshot_mutable(snapshot: CheckinSnapshot) -> None:
    if snapshot.status == CONSUMED:
        raise HTTPException(
            status_code=409,
            detail=f"第 {snapshot.round_no} 轮已生成，签到快照不可再变更",
        )


def _add_action(
    snapshot: CheckinSnapshot,
    tournament_id: int,
    request_id: str,
    action: str,
    fingerprint: dict,
    response: CheckinSnapshotResponse,
) -> None:
    snapshot.actions.append(
        CheckinAction(
            tournament_id=tournament_id,
            request_id=request_id,
            action=action,
            params=fingerprint,
            response=response.model_dump(mode="json"),
        )
    )


def record_checkin(
    db: Session, tournament_id: int, req: CheckinSnapshotRequest
) -> tuple[CheckinSnapshotResponse, Event | None]:
    """提交/更新下一轮签到快照（flush，不提交），返回（响应, 事件）。

    幂等重放时返回（首次响应, None），不产生任何写入；序号冲突、
    编号重复/覆盖不全或目标轮次已生成抛 409，且在任何写入之前
    抛出（不落库）。名单与当前快照一致时不推进版本、不产生事件。
    """
    fingerprint = _record_fingerprint(req)
    # 幂等判定先于业务校验：重试已成功请求必须返回原结果
    replay = _find_replay(db, tournament_id, req.request_id, fingerprint)
    if replay is not None:
        return replay, None

    # 写锁可能经过等待才取得：以锁内已提交的最新状态重新校验
    _ensure_target_is_next_open_round(db, tournament_id, req.round_no)

    number_map = _participant_number_map(db, tournament_id)
    if not number_map:
        raise HTTPException(status_code=400, detail="赛事还没有参赛者，无法签到")
    presence = _validate_roster(
        number_map, req.present_numbers, req.absent_numbers
    )

    actual_seq = current_event_seq(db, tournament_id)
    if req.expected_event_seq != actual_seq:
        raise HTTPException(
            status_code=409,
            detail=(
                f"赛事快照已变化：当前最大事件序号为 {actual_seq}，"
                f"请求期望 {req.expected_event_seq}"
            ),
        )

    snapshot = get_snapshot(db, tournament_id, req.round_no)
    now = datetime.utcnow()
    changed: bool

    if snapshot is None:
        snapshot = CheckinSnapshot(
            tournament_id=tournament_id,
            round_no=req.round_no,
            version=1,
            status=DRAFT,
            expected_event_seq=req.expected_event_seq,
            created_at=now,
            updated_at=now,
            confirmed_at=None,
        )
        db.add(snapshot)
        db.flush()  # 取得 snapshot.id
        for pid, present in presence.items():
            snapshot.entries.append(
                CheckinEntry(participant_id=pid, present=present)
            )
        changed = True
    else:
        _ensure_snapshot_mutable(snapshot)
        current = {e.participant_id: e.present for e in snapshot.entries}
        changed = current != presence
        if changed:
            # 整体替换名单：先删旧行再写新行（同事务）
            for entry in list(snapshot.entries):
                db.delete(entry)
            db.flush()
            for pid, present in presence.items():
                snapshot.entries.append(
                    CheckinEntry(participant_id=pid, present=present)
                )
            snapshot.version += 1
            # 变更后原确认失效，回到 draft
            snapshot.status = DRAFT
            snapshot.confirmed_at = None
            snapshot.updated_at = now
        # 无论是否实际变更，最近一次提交断言的序号都刷新
        snapshot.expected_event_seq = req.expected_event_seq
        db.flush()

    response = serialize_snapshot(db, snapshot)
    _add_action(snapshot, tournament_id, req.request_id, RECORD, fingerprint, response)
    db.flush()

    event = None
    if changed:
        event = append_event(
            db,
            tournament_id,
            ATTENDANCE_RECORDED,
            {
                "snapshot_id": snapshot.id,
                "round_no": snapshot.round_no,
                "version": snapshot.version,
                "present_numbers": response.present_numbers,
                "absent_numbers": response.absent_numbers,
                "expected_event_seq": req.expected_event_seq,
            },
        )
    return response, event


def confirm_checkin(
    db: Session, tournament_id: int, round_no: int, req: CheckinConfirmRequest
) -> tuple[CheckinSnapshotResponse, Event | None]:
    """确认下一轮签到快照（flush，不提交），返回（响应, 事件）。

    幂等重放（含已确认后的任何确认请求）返回（当前响应, None）；
    快照不存在、目标轮次已生成或序号冲突抛 409，且不留记录。
    """
    fingerprint = _confirm_fingerprint(round_no, req)
    # 幂等判定先于业务校验：重试已成功请求必须返回原结果
    replay = _find_replay(db, tournament_id, req.request_id, fingerprint)
    if replay is not None:
        return replay, None

    _ensure_target_is_next_open_round(db, tournament_id, round_no)

    snapshot = get_snapshot(db, tournament_id, round_no)
    if snapshot is None:
        raise HTTPException(
            status_code=409,
            detail=f"第 {round_no} 轮尚未提交签到快照，不能确认",
        )
    _ensure_snapshot_mutable(snapshot)

    actual_seq = current_event_seq(db, tournament_id)
    if req.expected_event_seq != actual_seq:
        raise HTTPException(
            status_code=409,
            detail=(
                f"赛事快照已变化：当前最大事件序号为 {actual_seq}，"
                f"请求期望 {req.expected_event_seq}"
            ),
        )

    changed = snapshot.status != CONFIRMED
    now = datetime.utcnow()
    if changed:
        snapshot.status = CONFIRMED
        snapshot.confirmed_at = now
        snapshot.updated_at = now
    snapshot.expected_event_seq = req.expected_event_seq
    db.flush()

    response = serialize_snapshot(db, snapshot)
    _add_action(snapshot, tournament_id, req.request_id, CONFIRM, fingerprint, response)
    db.flush()

    event = None
    if changed:
        event = append_event(
            db,
            tournament_id,
            ATTENDANCE_CONFIRMED,
            {
                "snapshot_id": snapshot.id,
                "round_no": snapshot.round_no,
                "version": snapshot.version,
                "present_numbers": response.present_numbers,
                "absent_numbers": response.absent_numbers,
                "expected_event_seq": req.expected_event_seq,
            },
        )
    return response, event
