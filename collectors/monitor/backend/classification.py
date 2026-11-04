"""Interpret explicit collector classification without inferring nationality."""

def korean_value(record):
    value = record.get("korean")
    if type(value) is bool:
        return value
    return {"true": True, "false": False, "1": True, "0": False}.get(str(value).strip().casefold())


def matches_korean(record, selected):
    return selected == "all" or korean_value(record) is ({"true": True, "false": False, "unknown": None}.get(selected))


def korean_selection(value):
    if value not in {"all", "true", "false", "unknown"}:
        raise ValueError("릴스 구분은 전체·한국·외국·미확인(-) 중에서 선택하세요.")
    return value
