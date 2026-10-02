"""原子记录每日 @额度和各群成员的历史 @次数。"""

from __future__ import annotations

import json
import os
from pathlib import Path


class DailyState:
    COUNT_PREFIX = "@count:"
    MEMBER_PREFIX = "@member:"

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            content = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(content, dict) or not all(
                isinstance(key, str) and isinstance(value, str) for key, value in content.items()
            ):
                raise ValueError("每日执行记录格式错误")
            self.days = content
        else:
            self.days: dict[str, str] = {}
        self._member_counts_by_group: dict[int, dict[int, int]] = {}

    def claimed(self, group_id: int, day: str, check_time: str) -> bool:
        return self.days.get(f"{group_id}@{check_time}") == day

    def mentioned_count(self, group_id: int, day: str) -> int:
        key = f"{self.COUNT_PREFIX}{group_id}:{day}"
        value = self.days.get(key, "0")
        if not value.isascii() or not value.isdigit():
            raise ValueError("每日 @ 人数记录损坏")
        return int(value)

    def remaining(self, group_id: int, day: str, limit: int) -> int | None:
        """限额为 0 时不限制每天的人数。"""
        if limit == 0:
            return None
        return max(0, limit - self.mentioned_count(group_id, day))

    def member_mention_counts(self, group_id: int) -> dict[int, int]:
        """按群缓存跨天累计次数；没有记录的成员视为尚未 @。"""
        if group_id in self._member_counts_by_group:
            return dict(self._member_counts_by_group[group_id])
        prefix = f"{self.MEMBER_PREFIX}{group_id}:"
        counts = {}
        for key, value in self.days.items():
            if not key.startswith(prefix):
                continue
            user_id = key[len(prefix) :]
            if (
                not user_id.isascii()
                or not user_id.isdigit()
                or int(user_id) <= 0
                or not value.isascii()
                or not value.isdigit()
                or int(value) <= 0
            ):
                raise ValueError("成员 @次数记录损坏")
            counts[int(user_id)] = int(value)
        # 首次按群扫描后缓存；返回副本，避免调用方修改内部计数。
        self._member_counts_by_group[group_id] = counts
        return dict(counts)

    def record_sent_members(self, group_id: int, member_ids: list[int]) -> None:
        """仅在发送接口确认成功后记录这一批实际 @ 的成员。"""
        if not member_ids:
            return
        if any(type(user_id) is not int or user_id <= 0 for user_id in member_ids):
            raise ValueError("已 @ 成员 QQ 号无效")
        if len(set(member_ids)) != len(member_ids):
            raise ValueError("同一批提醒包含重复成员")
        next_counts = self.member_mention_counts(group_id)
        updated = dict(self.days)
        for user_id in member_ids:
            next_counts[user_id] = next_counts.get(user_id, 0) + 1
            updated[f"{self.MEMBER_PREFIX}{group_id}:{user_id}"] = str(next_counts[user_id])
        self._save(updated)
        # 只有文件替换成功后才推进缓存；失败时下次读取仍看到旧记录。
        self._member_counts_by_group[group_id] = next_counts

    def _save(self, updated: dict[str, str]) -> None:
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            temporary.write_text(json.dumps(updated, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
        self.days = updated

    def claim(
        self,
        group_id: int,
        day: str,
        check_time: str,
        limit: int = 0,
        member_ids: list[int] | None = None,
    ) -> bool:
        key = f"{group_id}@{check_time}"
        if self.claimed(group_id, day, check_time):
            return False
        member_ids = member_ids or []
        if any(type(user_id) is not int or user_id <= 0 for user_id in member_ids):
            raise ValueError("待 @ 成员 QQ 号无效")
        if len(set(member_ids)) != len(member_ids):
            raise ValueError("本次提醒包含重复成员")
        if limit < 0:
            raise ValueError("每日 @ 人数限额无效")
        count = self.mentioned_count(group_id, day)
        if limit > 0 and count + len(member_ids) > limit:
            raise ValueError("本次提醒超出每日 @ 人数限额")
        updated = dict(self.days)
        # 只保留今天的计数；时间点状态每项仅存最近一次执行日期。
        for old_key in list(updated):
            if old_key.startswith(self.COUNT_PREFIX) and not old_key.endswith(f":{day}"):
                del updated[old_key]
        updated[key] = day
        updated[f"{self.COUNT_PREFIX}{group_id}:{day}"] = str(count + len(member_ids))
        self._save(updated)
        return True
