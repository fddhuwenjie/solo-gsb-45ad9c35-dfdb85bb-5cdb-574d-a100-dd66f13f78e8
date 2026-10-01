"""现场签到快照接口。

- PUT /tournaments/{id}/rounds/{round_no}/checkin：为尚未生成的
  下一轮提交/更新签到快照（到场 + 缺席的完整参赛者编号集合，
  幂等键 request_id + 序号快照 expected_event_seq）。首次提交
  版本为 1，名单实际变更版本 +1；序号冲突、编号重复/覆盖不全、
  目标轮次已生成一律 409 且不留记录。
- POST /tournaments/{id}/rounds/{round_no}/checkin/confirm：确认
  快照（幂等 + 序号快照）。
- GET /tournaments/{id}/rounds/{round_no}/checkin：查询快照
  （版本与完整名单）。
- GET /tournaments/{id}/checkins：赛事下全部签到快照。

重复 request_id（参数一致）原样返回首次结果，不产生新写入与事件；
同键不同参数返回 409。快照变更（attendance.recorded）与确认
（attendance.confirmed）与快照本身在同一事务写入事件。
"""
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from ..database import get_db
from ..events.service import commit_and_publish
from ..locking import writer_lock
from ..schemas import (
    CheckinConfirmRequest,
    CheckinSnapshotRequest,
    CheckinSnapshotResponse,
)
from ..services.checkins import (
    confirm_checkin,
    get_snapshot,
    list_snapshots,
    record_checkin,
    serialize_snapshot,
)
from .tournaments import get_tournament_or_404

router = APIRouter()


@router.put(
    "/tournaments/{tournament_id}/rounds/{round_no}/checkin",
    response_model=CheckinSnapshotResponse,
)
def put_checkin(
    tournament_id: int,
    round_no: int,
    payload: CheckinSnapshotRequest,
    response: Response,
    db: Session = Depends(get_db),
):
    get_tournament_or_404(db, tournament_id)
    if payload.round_no != round_no:
        raise HTTPException(
            status_code=409,
            detail="路径中的轮次号与请求体 round_no 不一致",
        )
    # 写请求进程级串行：目标轮检查、覆盖校验、序号确认、快照落库、
    # 事件序号与「提交→发布」全程持锁
    with writer_lock():
        db.expire_all()
        result, event = record_checkin(db, tournament_id, payload)
        if event is None:
            # 幂等重放或名单无变化：没有事务写入需要提交
            return result
        # 首次提交（版本 1）返回 201；后续实际变更返回 200
        response.status_code = 201 if result.version == 1 else 200
        commit_and_publish(db, [event])
        return result


@router.post(
    "/tournaments/{tournament_id}/rounds/{round_no}/checkin/confirm",
    response_model=CheckinSnapshotResponse,
)
def confirm(
    tournament_id: int,
    round_no: int,
    payload: CheckinConfirmRequest,
    response: Response,
    db: Session = Depends(get_db),
):
    get_tournament_or_404(db, tournament_id)
    with writer_lock():
        db.expire_all()
        result, event = confirm_checkin(db, tournament_id, round_no, payload)
        if event is None:
            # 幂等重放或此前已确认：不产生事件
            return result
        response.status_code = 201
        commit_and_publish(db, [event])
        return result


@router.get(
    "/tournaments/{tournament_id}/rounds/{round_no}/checkin",
    response_model=CheckinSnapshotResponse,
)
def get_checkin(
    tournament_id: int, round_no: int, db: Session = Depends(get_db)
):
    get_tournament_or_404(db, tournament_id)
    snapshot = get_snapshot(db, tournament_id, round_no)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="该轮次没有签到快照")
    return serialize_snapshot(db, snapshot)


@router.get(
    "/tournaments/{tournament_id}/checkins",
    response_model=list[CheckinSnapshotResponse],
)
def list_checkins(tournament_id: int, db: Session = Depends(get_db)):
    get_tournament_or_404(db, tournament_id)
    return [
        serialize_snapshot(db, s) for s in list_snapshots(db, tournament_id)
    ]
