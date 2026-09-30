"""箱体修洗验收收口规则的回归测试。

运行（在 backend 目录）：
    python -m tests.test_repair
或：
    pytest tests/test_repair.py

用 TestClient 直接打接口；每个用例前用种子数据重置 repair 表，互不影响。
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app.seed import SEED_ROWS
from app.store import store

client = TestClient(app)

BASE = "/api/repair"
ACCEPTOR = "修洗验收员"
READONLY = "只读员"


def reset_rows() -> None:
    # store 是模块级单例，测试间把修洗表还原成种子数据
    store.rows("repair").clear()
    store.rows("repair").extend(dict(row) for row in SEED_ROWS["repair"])


def actor(name: str, yard: str, roles: list[str]) -> dict:
    return {"name": name, "yard": yard, "roles": roles}


def expect(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(label)
    print("PASS", label)


def get(entry_id: int) -> dict:
    return client.get(f"{BASE}/{entry_id}").json()


def test_cross_yard_acceptance_rejected() -> None:
    """外堆场不能替别的堆场点验收，驳回信息写明归属堆场。"""
    reset_rows()
    res = client.post(
        f"{BASE}/3/actions",
        json={"action": "验收完成", "actor": actor("钱验收", "西区堆场", [ACCEPTOR])},
    ).json()
    expect(res["ok"] is False, "跨堆场验收被驳回")
    expect("越权驳回" in res["message"] and "东区堆场" in res["message"], "驳回信息写明归属与当前堆场")
    expect(get(3)["status"] == "待验收", "被驳回不改变状态")


def test_missing_role_rejected() -> None:
    """没有修洗验收员角色，当场驳回并写明缺的角色。"""
    reset_rows()
    res = client.post(
        f"{BASE}/3/actions",
        json={"action": "验收完成", "actor": actor("路人", "东区堆场", ["调度员"])},
    ).json()
    expect(res["ok"] is False, "无验收角色被驳回")
    expect(f"缺少角色「{ACCEPTOR}」" in res["message"], "驳回信息写明缺的角色")


def test_readonly_role_strictest() -> None:
    """只读与验收角色同时具备时，按更严的只读驳回：不能验收、不能改受保护字段。"""
    reset_rows()
    both = actor("双角色", "东区堆场", [READONLY, ACCEPTOR])
    accept = client.post(f"{BASE}/3/actions", json={"action": "验收完成", "actor": both}).json()
    expect(accept["ok"] is False and READONLY in accept["message"], "双角色验收按只读驳回")

    edit = client.patch(
        f"{BASE}/3",
        json={"values": {"损伤类型": "被改", "修理等级": "小修"}, "actor": both},
    ).json()
    expect(edit["ok"] is False and "只读岗不能修改" in edit["message"], "只读岗改损伤类型/修理等级被驳回")


def test_acceptance_locks_snapshot_and_terminal_state() -> None:
    """本堆场验收成功后固化验收人；已完成不可重复验收、不可回退；交接后仍认出原验收人。"""
    reset_rows()
    ok = client.post(
        f"{BASE}/3/actions",
        json={
            "action": "验收完成",
            "actor": actor("赵验收", "东区堆场", [ACCEPTOR]),
            "request_id": "snapshot",
            "expected_version": 3,
        },
    ).json()
    expect(ok["ok"] and ok["entry"]["验收人员"] == "赵验收", "验收成功并写验收人快照")

    # 交接：换人登录后读取，验收人仍是原快照
    expect(get(3)["验收人员"] == "赵验收", "交接后明细仍认出原验收人")

    repeat = client.post(
        f"{BASE}/3/actions",
        json={"action": "验收完成", "actor": actor("赵验收", "东区堆场", [ACCEPTOR])},
    ).json()
    expect(repeat["ok"] is False and "不能重复验收" in repeat["message"], "重复验收被驳回")

    back = client.post(
        f"{BASE}/3/actions",
        json={"action": "开始修理", "actor": actor("赵验收", "东区堆场", [ACCEPTOR])},
    ).json()
    expect(back["ok"] is False, "已验收不能推回待验收（状态倒流）")


def test_idempotent_submit_counts_once() -> None:
    """同一个 request_id 的重复提交只算一次，并回放首次的验收人与结果。"""
    reset_rows()
    payload = {
        "action": "验收完成",
        "actor": actor("钱验收", "西区堆场", [ACCEPTOR]),
        "request_id": "same-request",
    }
    first = client.post(f"{BASE}/4/actions", json=payload).json()
    second = client.post(f"{BASE}/4/actions", json=payload).json()
    expect(first["ok"] and not first["deduplicated"], "首次提交生效")
    expect(second["ok"] and second["deduplicated"], "重复提交被幂等账本识别")
    expect(second["entry"]["验收人员"] == "钱验收", "幂等回放原验收人")
    expect(sum(1 for row in store.rows("repair") if row["id"] == 4) == 1, "列表里没有多出一条")

    # request_id 挪用给别的任务必须被驳回
    misused = client.post(
        f"{BASE}/1/actions",
        json={
            "action": "安排修洗",
            "actor": actor("赵验收", "东区堆场", [ACCEPTOR]),
            "request_id": "same-request",
        },
    ).json()
    expect(misused["ok"] is False and "拒绝复用" in misused["message"], "request_id 挪作他用被驳回")


def test_concurrent_acceptance_only_one_wins() -> None:
    """两班组同版本并发点同一条：一个成功，另一个当场驳回；账上只留一个验收人。"""
    reset_rows()
    a = client.post(
        f"{BASE}/3/actions",
        json={
            "action": "验收完成",
            "actor": actor("甲组验收", "东区堆场", [ACCEPTOR]),
            "request_id": "race-a",
            "expected_version": 3,
        },
    ).json()
    b = client.post(
        f"{BASE}/3/actions",
        json={
            "action": "验收完成",
            "actor": actor("乙组验收", "东区堆场", [ACCEPTOR]),
            "request_id": "race-b",
            "expected_version": 3,
        },
    ).json()
    expect(a["ok"], "并发：一方成功")
    expect(b["ok"] is False and "已被他人更新" in b["message"], "并发：落败方当场驳回而非假成功")
    expect(get(3)["验收人员"] == "甲组验收", "账上只留成功方验收人")


def test_stats_recomputed_from_rows() -> None:
    """看板各状态数随明细实时重算，与按状态过滤的列表完全对齐。"""
    reset_rows()
    stats = client.get(f"{BASE}/stats").json()
    for status in ("待修洗", "修理中", "待验收", "已完成"):
        listed = client.get(f"{BASE}", params={"status": status}).json()["total"]
        expect(stats[status] == listed, f"看板「{status}」数与列表一致（{stats[status]}）")
    expect(stats["待验收"] == 2, "初始待验收数为 2")

    # 验收掉一条后，待验收减少、已完成增加
    client.post(
        f"{BASE}/3/actions",
        json={
            "action": "验收完成",
            "actor": actor("赵验收", "东区堆场", [ACCEPTOR]),
            "request_id": "stats",
            "expected_version": 3,
        },
    )
    stats_after = client.get(f"{BASE}/stats").json()
    expect(stats_after["待验收"] == 1 and stats_after["已完成"] == 2, "看板随验收动作重算")


def test_list_and_detail_acceptor_consistent() -> None:
    """列表与明细读同一份数据，验收人两处必须一致。"""
    reset_rows()
    # id=5 在种子数据里已是已完成，带验收人“王验收”
    listed = next(row for row in client.get(f"{BASE}?status=已完成").json()["items"] if row["id"] == 5)
    detail = get(5)
    expect(listed["验收人员"] == detail["验收人员"] == "王验收", "列表与明细验收人一致")


def test_create_dedup_by_task_no() -> None:
    """重复任务编号建单只认第一条，不再多生成一条记录。"""
    reset_rows()
    values = {"任务编号": "REPA-DUP", "箱号": "C1", "损伤类型": "刮痕", "所属堆场": "东区堆场"}
    first = client.post(
        f"{BASE}",
        json={"values": values, "actor": actor("赵验收", "东区堆场", [ACCEPTOR]), "request_id": "cd1"},
    ).json()
    second = client.post(
        f"{BASE}",
        json={"values": values, "actor": actor("赵验收", "东区堆场", [ACCEPTOR]), "request_id": "cd2"},
    ).json()
    expect(first["ok"] and not first["deduplicated"], "首次建单成功")
    expect(second["ok"] and second["deduplicated"], "重复编号建单被去重")
    expect(client.get(f"{BASE}", params={"keyword": "REPA-DUP"}).json()["total"] == 1, "同编号只有一条")


def test_status_must_advance_stepwise() -> None:
    """状态只能沿序列单向前推，不能从待修洗直接验收。"""
    reset_rows()
    res = client.post(
        f"{BASE}/1/actions",
        json={"action": "验收完成", "actor": actor("赵验收", "东区堆场", [ACCEPTOR])},
    ).json()
    expect(res["ok"] is False and "只能执行" in res["message"], "跨级流转被驳回")


def test_edit_ownership_and_done_protection() -> None:
    """编辑同样只能本堆场；已完成任务不能再改损伤类型与修理等级。"""
    reset_rows()
    cross = client.patch(
        f"{BASE}/2",
        json={"values": {"修理等级": "小修"}, "actor": actor("越权", "西区堆场", [ACCEPTOR])},
    ).json()
    expect(cross["ok"] is False and "越权驳回" in cross["message"], "跨堆场编辑被驳回")

    done = client.patch(
        f"{BASE}/5",
        json={"values": {"损伤类型": "x"}, "actor": actor("钱验收", "西区堆场", [ACCEPTOR])},
    ).json()
    expect(done["ok"] is False and "已验收完成" in done["message"], "已完成任务受保护字段不可改")


def main() -> None:
    cases = [
        test_cross_yard_acceptance_rejected,
        test_missing_role_rejected,
        test_readonly_role_strictest,
        test_acceptance_locks_snapshot_and_terminal_state,
        test_idempotent_submit_counts_once,
        test_concurrent_acceptance_only_one_wins,
        test_stats_recomputed_from_rows,
        test_list_and_detail_acceptor_consistent,
        test_create_dedup_by_task_no,
        test_status_must_advance_stepwise,
        test_edit_ownership_and_done_protection,
    ]
    for case in cases:
        case()
    print(f"\n全部 {len(cases)} 个验收规则用例通过")


if __name__ == "__main__":
    main()
