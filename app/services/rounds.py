"""轮次编排：生成下一轮（轮空决策 + 瑞士配对）。

若该轮存在现场签到快照（services/checkins.py），则只让快照中的
到场者进入瑞士配对与轮空选择；缺席者保留原有积分与历史对局但本轮
不得分（不为其创建任何场次，也不因此改动已结束轮次）。未提交
快照时沿用全员编排。生成时把快照置为 consumed（与轮次、事件同一
事务）。

注意：generate_next_round 只在调用方事务内写入并 flush，不提交；
轮次/对局与 round.generated（必要时 round.closed）事件由路由层
在同一事务中统一提交。
"""
from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..events.service import (
    ROUND_CLOSED,
    ROUND_GENERATED,
    append_event,
)
from ..models import Event, Match, Participant, Round
from .checkins import CONSUMED, get_snapshot
from .result_reviews import has_pending_review_for_tournament
from .scoring import compute_records
from .swiss import WIN, swiss_pair


def generate_next_round(
    db: Session, tournament_id: int
) -> tuple[Round, list[Event]]:
    tournament_participants = (
        db.query(Participant)
        .filter(Participant.tournament_id == tournament_id)
        .order_by(Participant.number)
        .all()
    )
    if len(tournament_participants) < 1:
        raise HTTPException(status_code=400, detail="赛事还没有参赛者，无法生成轮次")

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
            detail=f"第 {open_round.round_no} 轮尚未完成，请先录入全部赛果",
        )

    # 赛事存在待处理赛果复核时禁止生成下一轮：复核可能更正已提交
    # 比分并改变后续配对依据，必须先等复核关闭
    if has_pending_review_for_tournament(db, tournament_id):
        raise HTTPException(
            status_code=409,
            detail="赛事存在待处理的赛果复核，需待复核关闭后才能生成下一轮",
        )

    rounds = (
        db.query(Round)
        .filter(Round.tournament_id == tournament_id)
        .order_by(Round.round_no)
        .all()
    )
    next_no = (rounds[-1].round_no + 1) if rounds else 1

    # 下一轮的现场签到快照（可能不存在 -> 全员编排）
    snapshot = get_snapshot(db, tournament_id, next_no)
    present_ids: set[int] | None = None
    if snapshot is not None:
        if snapshot.status == CONSUMED:
            # 理论上不会发生：consumed 与轮次唯一轮次号互相对应
            raise HTTPException(
                status_code=409,
                detail=f"第 {next_no} 轮的签到快照已被使用，不能重复生成",
            )
        snapshot_pids = {e.participant_id for e in snapshot.entries}
        roster_pids = {p.id for p in tournament_participants}
        if snapshot_pids != roster_pids:
            # 首轮名单锁定前可能继续报名：快照提交后报名名单发生变化
            raise HTTPException(
                status_code=409,
                detail=(
                    "签到快照的名单与当前报名名单不一致（首轮生成前名单"
                    "可能有新增），请按当前名单重新提交签到快照"
                ),
            )
        present_ids = {e.participant_id for e in snapshot.entries if e.present}

    # 当前排名（仅统计已结束轮次）：排名积分降序 -> Buchholz 降序 ->
    # 编号升序；下一轮积分分组以排名积分（对局积分 - 有效扣分）为准。
    # 有签到快照时仅到场者参与本轮配对与轮空；缺席者保留积分但不进
    # 本轮名单（points_by_id 仍保留全员积分供需要时查阅）
    records = compute_records(db, tournament_id)
    points_by_id = {r.participant_id: r.rank_points for r in records}
    if present_ids is None:
        ranking_ids = [r.participant_id for r in records]
    else:
        ranking_ids = [r.participant_id for r in records if r.participant_id in present_ids]

    # 已经轮空过的选手（轮空在生成轮次时即生成并记分）
    closed_round_ids = [r.id for r in rounds if r.status == "closed"]
    byed_ids: set[int] = set()
    if closed_round_ids:
        bye_matches = (
            db.query(Match)
            .filter(
                Match.round_id.in_(closed_round_ids),
                Match.player_black_id.is_(None),
            )
            .all()
        )
        byed_ids = {m.player_white_id for m in bye_matches}

    bye_id: int | None = None
    if len(ranking_ids) % 2 == 1:
        # 轮空给「尚未轮空且排名最低」的选手；
        # 若所有人都轮空过，则取排名最低者。
        # 有签到快照时候选只含本轮到场者
        eligible = [pid for pid in ranking_ids if pid not in byed_ids]
        bye_id = eligible[-1] if eligible else ranking_ids[-1]

    pairing_players = [
        pid for pid in ranking_ids if pid != bye_id
    ]

    # 禁止重复交手：取所有已结束轮次中出现过的对阵
    forbidden: set[tuple[int, int]] = set()
    if closed_round_ids:
        played_matches = (
            db.query(Match)
            .filter(
                Match.round_id.in_(closed_round_ids),
                Match.player_black_id.is_not(None),
            )
            .all()
        )
        for m in played_matches:
            forbidden.add(
                (min(m.player_white_id, m.player_black_id),
                 max(m.player_white_id, m.player_black_id))
            )

    try:
        result = swiss_pair(
            players=pairing_players,
            points=points_by_id,
            forbidden=forbidden,
            bye_player=bye_id,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    round_obj = Round(
        tournament_id=tournament_id,
        round_no=next_no,
        status="open",
    )
    db.add(round_obj)
    db.flush()  # 取得 round_obj.id

    # 普通对局（swiss_pair 已按小白号升序返回），暂不记分
    for white_id, black_id in result.pairs:
        db.add(
            Match(
                round_id=round_obj.id,
                player_white_id=white_id,
                player_black_id=black_id,
                score_white=None,
                score_black=None,
            )
        )
    db.flush()  # 取得各场对局 id，供事件使用

    # 轮空对局：轮空方直接记 1 分，无对手
    bye_match_id: int | None = None
    if bye_id is not None:
        bye_match = Match(
            round_id=round_obj.id,
            player_white_id=bye_id,
            player_black_id=None,
            score_white=WIN,
            score_black=None,
        )
        db.add(bye_match)
        db.flush()
        bye_match_id = bye_match.id

    # 没有需要录入的普通对局（全员缺席、或仅 1 名到场者等）时直接
    # 结束本轮；缺席者不因本轮得到任何积分
    if not result.pairs:
        round_obj.status = "closed"

    # 快照随轮次生成而消费：此后不可再提交/变更（同一事务置位）
    if snapshot is not None:
        snapshot.status = CONSUMED

    db.flush()

    # 事件与轮次/对局在同一事务：生成事件（含即时封盘状态）；
    # 即时封盘时追加一条 round.closed，序号紧随 round.generated
    generated_payload = {
        "round_id": round_obj.id,
        "round_no": round_obj.round_no,
        "status": round_obj.status,
        # 本轮全部场次 id（普通对局 + 轮空对局，按 Match.id 排序）
        "match_ids": [m.id for m in round_obj.matches],
    }
    if bye_match_id is not None:
        generated_payload["bye_match_id"] = bye_match_id
    if snapshot is not None:
        # 本轮编排所依据的签到快照（编号按参赛者编号升序）
        participant_no = {p.id: p.number for p in tournament_participants}
        generated_payload["attendance_snapshot"] = {
            "snapshot_id": snapshot.id,
            "version": snapshot.version,
            "present_numbers": sorted(
                participant_no[pid] for pid in present_ids
            ),
            "absent_numbers": sorted(
                participant_no[pid]
                for pid in roster_pids - present_ids
            ),
        }
    events = [
        append_event(db, tournament_id, ROUND_GENERATED, generated_payload)
    ]

    if not result.pairs:
        events.append(
            append_event(
                db,
                tournament_id,
                ROUND_CLOSED,
                {
                    "round_id": round_obj.id,
                    "round_no": round_obj.round_no,
                    "reason": "no_matches",
                },
            )
        )

    return round_obj, events
