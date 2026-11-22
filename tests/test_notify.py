import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from app.config import ROOT
from app.keys import ReplayError
from app.main import create_app
from app.modules.m7_release.service import execute
from app.modules.m7_release.store import Catalog
from app.modules.m8_notify.registry import load_registry
from app.modules.m8_notify.service import build, deliver

OWNERS = ROOT / "sample_data" / "owners.json"


@pytest.fixture
def plan(client):
    return client.post("/api/plans", json={"sample": "nightly"}).json["id"]


def test_groups_share_one_batch_with_ordered_dispositions(client, plan):
    batch = client.get(f"/api/notify/{plan}").json["通知"]
    assert batch["计划"] == plan and batch["处置次序"] == ["停批", "重做", "审计"]
    assert {g["责任组"] for g in batch["组通知"]} == {
        "订单履约组",
        "结算核查组",
        "客群运营组",
        "营收归集组",
    }
    for group in batch["组通知"]:
        ranks = [batch["处置次序"].index(e["处置"]) for e in group["分区"]]
        assert ranks == sorted(ranks)


def test_retry_never_creates_second_batch(client, plan):
    first = client.post(f"/api/notify/{plan}")
    again = client.post(f"/api/notify/{plan}")
    assert (first.status_code, again.status_code) == (201, 200)
    assert first.json["id"] == again.json["id"] and again.json["reused"]
    assert client.get(f"/api/notify/{plan}").json["通知"]["批次"] == first.json["id"]


def test_missing_responsibility_never_reported_complete(client, plan):
    batch = client.get(f"/api/notify/{plan}").json["通知"]
    cover = batch["覆盖"]
    assert cover["受影响"] == cover["已归属"] + cover["待归属"]
    assert cover["待归属"] > 0 and cover["通知完成"] is False
    assert {row["数据集"] for row in batch["待归属"]} == {"ops"}
    assert all(row["归属"] == "缺责任组" for row in batch["待归属"])


def test_fully_attributed_plan_completes(client):
    run = client.post("/api/plans", json={"sample": "clean"}).json
    batch = client.get(f"/api/notify/{run['id']}").json["通知"]
    assert batch["待归属"] == [] and batch["覆盖"]["通知完成"] is True


def test_batch_traces_back_to_impact_source(client, plan):
    batch = client.get(f"/api/notify/{plan}").json["通知"]
    sources = {row["分区"] for row in batch["影响来源"]}
    assert sources == {
        "orders@2026-11-17",
        "orders@2026-11-18",
        "orders@2026-11-19",
        "buyers@2026-11-16",
    }
    entries = [e for g in batch["组通知"] for e in g["分区"]] + batch["待归属"]
    assert all(set(e["来源"]) <= sources for e in entries)
    late = next(e for e in entries if e["分区"] == "day1@2026-11-18")
    assert late["来源"] == ["orders@2026-11-17"]
    merged = next(e for e in entries if e["分区"] == "ops@2026-11-18")
    assert merged["来源"] == ["orders@2026-11-17", "orders@2026-11-18"]


def test_provenance_from_stored_document(nightly):
    batch = build(nightly, load_registry(OWNERS))
    assert batch["覆盖"]["受影响"] == len(nightly["排程"])
    origin = {row["分区"]: row["源行"] for row in batch["影响来源"]}
    assert origin["orders@2026-11-19"] == 4


def test_export_reads_the_same_batch(client, plan):
    posted = client.post(f"/api/notify/{plan}").json
    pack = client.get(f"/api/export/{plan}").json
    assert pack["通知"]["批次"] == posted["id"]
    assert pack["通知"]["覆盖"] == posted["覆盖"]


def test_console_page_lists_groups_and_pending(client, plan):
    page = client.get(f"/console/notify?plan={plan}").get_data(as_text=True)
    assert "营收归集组" in page and "待归属分区" in page and "ops@2026-11-18" in page


def test_batch_keeps_basis_after_registry_deleted(tmp_path):
    owners = tmp_path / "owners.json"
    owners.write_text(
        json.dumps({"groups": {"orders": "订单履约组", "facts": "结算核查组"}}), encoding="utf-8"
    )
    app = create_app({"TESTING": True, "DB": tmp_path / "run.sqlite", "OWNERS": owners})
    client = app.test_client()
    plan = client.post("/api/plans", json={"sample": "clean"}).json["id"]
    batch = client.post(f"/api/notify/{plan}").json
    owners.unlink()
    kept = client.get(f"/api/notify/{plan}").json["通知"]
    assert kept["批次"] == batch["id"] and kept["覆盖"]["通知完成"] is True
    assert kept["责任依据"] == [
        {"数据集": "facts", "责任组": "结算核查组"},
        {"数据集": "orders", "责任组": "订单履约组"},
    ]
    retry = client.post(f"/api/notify/{plan}")
    assert retry.status_code == 200 and retry.json["reused"]


def test_deleted_registry_means_everything_pending(tmp_path, night):
    app = create_app(
        {"TESTING": True, "DB": tmp_path / "run.sqlite", "OWNERS": tmp_path / "missing.json"}
    )
    client = app.test_client()
    plan = client.post("/api/plans", json={"sample": "nightly"}).json["id"]
    batch = client.get(f"/api/notify/{plan}").json["通知"]
    assert batch["责任依据"] == [] and batch["组通知"] == []
    assert batch["覆盖"]["待归属"] == batch["覆盖"]["受影响"] > 0
    assert batch["覆盖"]["通知完成"] is False


def test_corrupt_registry_only_blocks_new_batches(tmp_path):
    owners = tmp_path / "owners.json"
    owners.write_text(json.dumps({"groups": {"orders": "订单履约组"}}), encoding="utf-8")
    app = create_app({"TESTING": True, "DB": tmp_path / "run.sqlite", "OWNERS": owners})
    client = app.test_client()
    clean = client.post("/api/plans", json={"sample": "clean"}).json["id"]
    client.post(f"/api/notify/{clean}")
    owners.write_text("{", encoding="utf-8")
    assert client.get(f"/api/notify/{clean}").status_code == 200
    night = client.post("/api/plans", json={"sample": "nightly"}).json["id"]
    response = client.get(f"/api/notify/{night}")
    assert response.status_code == 422 and response.json["code"] == "OWNERS"


def test_invalid_registry_rejected(tmp_path):
    bad = tmp_path / "owners.json"
    bad.write_text(json.dumps({"groups": {"bad name!": "x"}}), encoding="utf-8")
    with pytest.raises(ReplayError):
        load_registry(bad)


def test_parallel_notify_converges(tmp_path, night):
    store = Catalog(tmp_path / "archive.sqlite")
    plan, _ = execute(store, night)
    owners = load_registry(OWNERS)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: deliver(store, plan, lambda: owners), range(4)))
    assert sum(not reused for _, reused in results) == 1
    assert len({batch["批次"] for batch, _ in results}) == 1
