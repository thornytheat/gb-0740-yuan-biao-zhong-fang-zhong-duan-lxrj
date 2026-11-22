from collections import defaultdict
from datetime import datetime, timezone

from app.config import VERSION
from app.modules.m3_fingerprint.service import fingerprint

DISPOSITIONS = {"blocked": "停批", "write": "重做", "reuse": "审计"}
ORDER = ["停批", "重做", "审计"]


def notice_id(plan_id, basis):
    payload = {"计划": plan_id, "规则": VERSION, "责任依据": basis}
    return "notice-" + fingerprint(payload)[:26]


def provenance(tasks, sources):
    # 排程按（拓扑序, 分区日）排列，任一上游分区必先于读取它的分区出现。
    origins = {}
    for task in tasks:
        key = task["分区"]
        if key in sources:
            origins[key] = {key}
            continue
        found = set()
        for parent in task["上游"]:
            found |= origins.get(parent, set())
        origins[key] = found
    return origins


def build(plan, owners):
    tasks = plan["排程"]
    registry = {row["分区"]: row for row in plan["登记"]}
    origins = provenance(tasks, registry)
    datasets = {task["分区"].split("@")[0] for task in tasks}
    basis = [
        {"数据集": name, "责任组": owners[name]} for name in sorted(datasets) if name in owners
    ]
    owner_of = {row["数据集"]: row["责任组"] for row in basis}
    groups = defaultdict(list)
    pending = []
    for task in tasks:
        key = task["分区"]
        entry = {"分区": key, "处置": DISPOSITIONS[task["状态"]]}
        if task["原因码"]:
            entry["原因码"] = task["原因码"]
        entry["来源"] = sorted(origins[key])
        name = key.split("@")[0]
        if name in owner_of:
            groups[owner_of[name]].append(entry)
        else:
            pending.append({"数据集": name, **entry, "归属": "缺责任组"})
    notices = []
    for group in sorted(groups):
        entries = sorted(groups[group], key=lambda e: (ORDER.index(e["处置"]), e["分区"]))
        notices.append(
            {
                "责任组": group,
                "停批": sum(e["处置"] == "停批" for e in entries),
                "重做": sum(e["处置"] == "重做" for e in entries),
                "审计": sum(e["处置"] == "审计" for e in entries),
                "分区": entries,
            }
        )
    pending.sort(key=lambda e: (ORDER.index(e["处置"]), e["分区"]))
    return {
        "批次": notice_id(plan["批次"]["编号"], basis),
        "计划": plan["批次"]["编号"],
        "规则": VERSION,
        "生成时间": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "处置次序": ORDER,
        "责任依据摘要": fingerprint(basis),
        "责任依据": basis,
        "组通知": notices,
        "待归属": pending,
        "影响来源": [
            {
                "分区": key,
                "版本": row["version"],
                "上一版": row["previous"],
                "行数": row["rows"],
                "源行": row["line"],
            }
            for key, row in sorted(registry.items())
        ],
        "覆盖": {
            "受影响": len(tasks),
            "已归属": len(tasks) - len(pending),
            "待归属": len(pending),
            "通知完成": not pending,
        },
    }


def deliver(store, plan, load_owners):
    key = plan["批次"]["编号"]
    existing = store.notice(key)
    if existing is not None:
        return existing, True
    # 仅在确需生成时读取责任登记：旧批次不依赖仍存在的登记文件。
    return store.save_notice(key, build(plan, load_owners()))
