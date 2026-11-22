from app.web import notice_for, record
from flask import Blueprint, jsonify, url_for

api = Blueprint("notify", __name__)


@api.get("/api/notify/<plan>")
def inspection(plan):
    batch, _ = notice_for(record(plan))
    return jsonify({"通知": batch})


@api.post("/api/notify/<plan>")
def delivery(plan):
    batch, reused = notice_for(record(plan))
    return jsonify(
        id=batch["批次"],
        reused=reused,
        覆盖=batch["覆盖"],
        next=url_for("console", section="notify", plan=plan),
    ), 200 if reused else 201
