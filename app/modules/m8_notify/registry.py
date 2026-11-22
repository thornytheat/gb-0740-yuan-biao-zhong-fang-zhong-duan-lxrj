import json
from pathlib import Path

from app.keys import ReplayError, identifier


def load_registry(path):
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        # 登记被误删时新批次按全部待归属生成，不得谎报完成。
        return {}
    try:
        data = json.loads(raw.decode("utf-8-sig"))
        groups = data["groups"]
        if not isinstance(groups, dict) or len(groups) > 200:
            raise ValueError("责任登记结构无效")
        result = {}
        for name, group in groups.items():
            identifier(name)
            if not isinstance(group, str) or not 1 <= len(group) <= 40:
                raise ValueError("责任组名无效")
            result[name] = group
    except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
        raise ReplayError("OWNERS", "责任登记无效") from exc
    return result
