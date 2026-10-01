"""群名片包含规则、每日多个时间点与 OneBot 提醒消息。"""

from __future__ import annotations

import heapq
import math
import random
import re
from datetime import datetime, timedelta, timezone

BEIJING = timezone(timedelta(hours=8))
BATCH_SIZE = 20
LOG_2 = math.log(2)


def parse_group_ids(raw: list[str] | str) -> list[int]:
    """读取逐项填写的群号，也兼容旧版逗号分隔字符串。"""
    if isinstance(raw, str):
        entries = re.split(r"[\s,，;；]+", raw.strip()) if raw.strip() else []
    elif isinstance(raw, list):
        entries = raw
    else:
        raise ValueError("目标群号必须是列表")
    groups = []
    for entry in entries:
        if not isinstance(entry, str):
            raise ValueError("每个目标群号都必须是文本")
        value = entry.strip()
        if not value:
            continue
        if ":" in value:
            parts = value.split(":")
            if len(parts) != 3 or not parts[0] or parts[1] != "GroupMessage":
                raise ValueError(f"目标群号格式无效：{value}")
            value = parts[2]
        if not value.isascii() or not value.isdigit() or int(value) <= 0:
            raise ValueError(f"目标群号格式无效：{entry}")
        groups.append(int(value))
    return list(dict.fromkeys(groups))


def parse_excluded_qq_ids(raw: list[str]) -> set[int]:
    """读取不进入 @ 名单的 QQ 号，与账号是否为机器人无关。"""
    if not isinstance(raw, list):
        raise ValueError("排除的 QQ 号必须是列表")
    excluded_ids = set()
    for index, entry in enumerate(raw, 1):
        if not isinstance(entry, str):
            raise ValueError(f"第 {index} 个排除的 QQ 号必须是文本")
        value = entry.strip()
        if not value:
            continue
        if not value.isascii() or not value.isdigit() or int(value) <= 0:
            raise ValueError(f"第 {index} 个排除的 QQ 号无效：{entry}")
        excluded_ids.add(int(value))
    return excluded_ids


MODE_MISSING = "缺少关键词时提醒"
MODE_PRESENT = "包含关键词时提醒"


def parse_keywords(raw: list[str]) -> list[str]:
    """每项都是普通文本片段；去掉空行与重复项。"""
    if not isinstance(raw, list):
        raise ValueError("关键词必须逐条添加为列表")
    keywords = []
    for index, entry in enumerate(raw, 1):
        if not isinstance(entry, str):
            raise ValueError(f"第 {index} 条关键词必须是文本")
        value = entry.strip()
        if value:
            keywords.append(value)
    return list(dict.fromkeys(keywords))


def parse_match_mode(raw: str) -> str:
    if raw not in (MODE_MISSING, MODE_PRESENT):
        raise ValueError("检查方向应为“缺少关键词时提醒”或“包含关键词时提醒”")
    return raw


def parse_check_time(raw: str) -> tuple[int, int]:
    if not isinstance(raw, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", raw):
        raise ValueError("检查时间应为北京时间 HH:MM，例如 09:00")
    hour, minute = raw.split(":")
    return int(hour), int(minute)


def parse_check_times(raw: list[str] | str) -> list[str]:
    """时间逐项填写，去重后按北京时间先后排序。"""
    if isinstance(raw, str):
        entries = [raw] if raw.strip() else []
    elif isinstance(raw, list):
        entries = raw
    else:
        raise ValueError("提醒时间必须是列表")
    times = []
    for index, entry in enumerate(entries, 1):
        if not isinstance(entry, str):
            raise ValueError(f"第 {index} 个提醒时间必须是文本")
        value = entry.strip()
        if not value:
            continue
        try:
            parse_check_time(value)
        except ValueError as exc:
            raise ValueError(f"第 {index} 个提醒时间无效：{exc}") from exc
        times.append(value)
    return sorted(set(times))


def next_check(now: datetime, check_times: list[str]) -> datetime:
    if not check_times:
        raise ValueError("请先添加至少一个提醒时间")
    local_now = now.astimezone(BEIJING)
    targets = []
    for check_time in check_times:
        hour, minute = parse_check_time(check_time)
        target = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if local_now >= target:
            target += timedelta(days=1)
        targets.append(target)
    return min(targets)


def invalid_cards(
    members: list[dict], keywords: list[str], mode: str, excluded_qq_ids: set[int]
) -> list[dict]:
    """成员列表缺字段时拒绝整次扫描，避免将未知名片误判为空名片。"""
    if not keywords:
        raise ValueError("请先添加至少一条关键词")
    parse_match_mode(mode)
    if not isinstance(members, list) or not members:
        raise ValueError("群成员列表为空或格式错误")
    invalid = []
    seen = set()
    for member in members:
        if not isinstance(member, dict) or "user_id" not in member or "card" not in member:
            raise ValueError("群成员数据缺少 user_id 或 card")
        try:
            user_id = int(member["user_id"])
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("群成员 QQ 号无效") from exc
        if user_id <= 0:
            raise ValueError("群成员 QQ 号无效")
        if user_id in seen:
            continue
        seen.add(user_id)
        # 只依据用户配置的 QQ 号排除；协议端的机器人标记不参与判断。
        if user_id in excluded_qq_ids:
            continue
        card = member["card"]
        if card is None:
            card = ""
        if not isinstance(card, str):
            raise ValueError("群名片字段格式错误")
        contains = any(keyword in card for keyword in keywords)
        should_remind = not contains if mode == MODE_MISSING else contains
        if should_remind:
            invalid.append({"user_id": user_id, "card": card})
    return sorted(invalid, key=lambda item: item["user_id"])


def choose_reminder_targets(
    invalid: list[dict], remaining: int | None, mention_counts: dict[int, int], rng=None
) -> list[dict]:
    """超出额度时先选从未 @ 的成员，再按历史 @次数减半的权重抽取。"""
    if remaining is None or len(invalid) <= remaining:
        return invalid[:]
    if remaining <= 0:
        return []
    unseen = [item for item in invalid if mention_counts.get(item["user_id"], 0) == 0]
    selected = unseen[:remaining]
    if len(selected) == remaining:
        return selected
    candidates = [item for item in invalid if mention_counts.get(item["user_id"], 0) > 0]
    if not candidates:
        return selected
    random_source = rng if rng is not None else random
    minimum = min(mention_counts[item["user_id"]] for item in candidates)
    races = []
    for item in candidates:
        # 指数竞赛等价于按 2^-历史次数逐人加权抽取，且每人只需一次随机数。
        # 在对数域比较时间，避免历史次数较大时权重下溢。
        draw = random_source.random()
        race = (
            float("-inf")
            if draw == 0
            else math.log(-math.log1p(-draw))
            + (mention_counts[item["user_id"]] - minimum) * LOG_2
        )
        races.append((race, item["user_id"], item))
    selected.extend(
        item for _, _, item in heapq.nsmallest(remaining - len(selected), races)
    )
    return selected


def reminder_messages(invalid: list[dict], reminder_text: str) -> list[list[dict]]:
    """生成真正的 OneBot at 消息段，而非文本形式的 @昵称。"""
    if not invalid:
        return []
    text = reminder_text.strip() or "请按群规定修改群名片。"
    batches = [invalid[index : index + BATCH_SIZE] for index in range(0, len(invalid), BATCH_SIZE)]
    messages = []
    for index, batch in enumerate(batches, 1):
        message = [
            {
                "type": "text",
                "data": {"text": f"{text}\n群名片不符合要求：共 {len(invalid)} 人（第 {index}/{len(batches)} 批）\n"},
            }
        ]
        for member in batch:
            message.extend(
                [
                    {"type": "at", "data": {"qq": str(member["user_id"])}},
                    {"type": "text", "data": {"text": " "}},
                ]
            )
        messages.append(message)
    return messages
