"""在多个时间点检查 QQ 群名片包含条件，并汇总 @目标成员。"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

if __package__:
    from .card_rules import (
        BEIJING,
        choose_reminder_targets,
        invalid_cards,
        next_check,
        parse_check_times,
        parse_excluded_qq_ids,
        parse_group_ids,
        parse_keywords,
        parse_match_mode,
        reminder_messages,
    )
    from .daily_state import DailyState
else:
    from card_rules import (
        BEIJING,
        choose_reminder_targets,
        invalid_cards,
        next_check,
        parse_check_times,
        parse_excluded_qq_ids,
        parse_group_ids,
        parse_keywords,
        parse_match_mode,
        reminder_messages,
    )
    from daily_state import DailyState

PLUGIN_NAME = "astrbot_plugin_group_card_reminder"
API_TIMEOUT = 30


@dataclass(frozen=True)
class PluginSettings:
    group_ids: list[int]
    keywords: list[str]
    mode: str
    check_times: list[str]
    daily_mention_limit: int
    excluded_qq_ids: set[int]


@register(PLUGIN_NAME, "ma114514", "定时检查 QQ 群名片包含条件并 @提醒", "0.1.0", "")
class GroupCardReminder(Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self._state: DailyState | None = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._run_lock = asyncio.Lock()

    async def initialize(self) -> None:
        await self._migrate_config()
        data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self._state = DailyState(data_dir / "daily_state.json")
        self._task = asyncio.create_task(self._scheduler(), name=f"{PLUGIN_NAME}:daily")

    async def terminate(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        async with self._run_lock:
            pass

    async def _migrate_config(self) -> None:
        """迁移旧群号、时间和排除名单；正则不能转换为普通字符。"""
        changed = False
        groups = self.config.get("group_ids", [])
        if isinstance(groups, str):
            try:
                self.config["group_ids"] = [str(group_id) for group_id in parse_group_ids(groups)]
                changed = True
            except ValueError as exc:
                logger.warning(f"[{PLUGIN_NAME}] 旧版群号配置无法自动迁移：{exc}")
        old_time = self.config.get("check_time", "")
        if old_time:
            current_times = self.config.get("check_times", [])
            if not current_times or (current_times == ["09:00"] and old_time != "09:00"):
                self.config["check_times"] = [old_time]
            self.config["check_time"] = ""
            changed = True
        old_excluded = self.config.get("excluded_bot_ids", [])
        if old_excluded:
            if self.config.get("excluded_qq_ids", []):
                # 新名单已填写时以新名单为准，清掉旧值以免以后重新迁移。
                self.config["excluded_bot_ids"] = []
                changed = True
            else:
                try:
                    migrated = parse_excluded_qq_ids(old_excluded)
                except ValueError as exc:
                    # 保留无效旧值；_settings 会拒绝扫描，避免无声漏排。
                    logger.warning(f"[{PLUGIN_NAME}] 旧版排除名单无法自动迁移：{exc}")
                else:
                    self.config["excluded_qq_ids"] = [str(qq) for qq in sorted(migrated)]
                    self.config["excluded_bot_ids"] = []
                    changed = True
        if changed:
            save = getattr(self.config, "save_config", None)
            if callable(save):
                try:
                    result = save()
                    if inspect.isawaitable(result):
                        await result
                except Exception as exc:
                    logger.warning(f"[{PLUGIN_NAME}] 旧配置已在内存中迁移，但保存失败：{exc}")

    def _settings(self) -> PluginSettings | None:
        groups = parse_group_ids(self.config.get("group_ids", []))
        keywords = parse_keywords(self.config.get("keywords", []))
        if not groups or not keywords:
            return None
        mode = parse_match_mode(self.config.get("match_mode", "缺少关键词时提醒"))
        check_times = parse_check_times(self.config.get("check_times", []))
        raw_excluded = self.config.get("excluded_qq_ids", [])
        if not raw_excluded:
            # 即使配置保存失败，当前运行仍沿用有效旧名单；无效旧值会报错。
            raw_excluded = self.config.get("excluded_bot_ids", [])
        excluded_qq_ids = parse_excluded_qq_ids(raw_excluded)
        try:
            limit = int(self.config.get("daily_mention_limit", 0))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("每天 @ 人数限额必须是非负整数") from exc
        if limit < 0:
            raise ValueError("每天 @ 人数限额必须是非负整数")
        return PluginSettings(groups, keywords, mode, check_times, limit, excluded_qq_ids)

    async def _accounts(self) -> list[tuple[int, object]]:
        async def fetch_account(platform):
            try:
                client = platform.get_client()
                info = await asyncio.wait_for(client.call_action("get_login_info"), API_TIMEOUT)
                bot_id = int(info["user_id"])
                if bot_id > 0:
                    return bot_id, client
            except Exception as exc:
                logger.warning(f"[{PLUGIN_NAME}] 无法获取一个 QQ 账号的信息：{exc}")
            return None

        platforms = [
            platform
            for platform in self.context.platform_manager.get_insts()
            if platform.meta().name == "aiocqhttp"
        ]
        # 登录信息彼此独立，并发查询避免一个超时账号阻塞其他账号。
        accounts = await asyncio.gather(*(fetch_account(platform) for platform in platforms))
        return sorted((account for account in accounts if account is not None), key=lambda a: a[0])

    async def _scan_group(self, group_id: int, keywords, mode, accounts, excluded_qq_ids):
        for bot_id, client in accounts:
            try:
                members = await asyncio.wait_for(
                    client.call_action(
                        "get_group_member_list",
                        group_id=group_id,
                        no_cache=True,
                        self_id=bot_id,
                    ),
                    API_TIMEOUT,
                )
                invalid = invalid_cards(members, keywords, mode, excluded_qq_ids)
                return bot_id, client, invalid, len(members)
            except Exception as exc:
                logger.warning(f"[{PLUGIN_NAME}] 账号 {bot_id} 查询群 {group_id} 失败：{exc}")
        raise RuntimeError("所有已连接 QQ 账号均无法获取完整群成员列表")

    async def _run_at(self, check_time: str, now: datetime | None = None) -> None:
        async with self._run_lock:
            try:
                settings = self._settings()
            except ValueError as exc:
                logger.error(f"[{PLUGIN_NAME}] 配置无效，本次不检查：{exc}")
                return
            if settings is None:
                return
            if check_time not in settings.check_times:
                return
            day = (now or datetime.now(BEIJING)).astimezone(BEIJING).date().isoformat()
            accounts = await self._accounts()
            if not accounts:
                logger.warning(f"[{PLUGIN_NAME}] 没有已连接的 aiocqhttp 账号，本次不检查")
                return
            for group_id in settings.group_ids:
                if self._stop.is_set():
                    return
                if self._state.claimed(group_id, day, check_time):
                    continue
                try:
                    remaining = self._state.remaining(
                        group_id, day, settings.daily_mention_limit
                    )
                    if remaining == 0:
                        self._state.claim(
                            group_id, day, check_time, limit=settings.daily_mention_limit
                        )
                        logger.info(f"[{PLUGIN_NAME}] 群 {group_id} 今日 @额度已用尽，跳过 {check_time}")
                        continue
                    bot_id, client, invalid, total = await self._scan_group(
                        group_id,
                        settings.keywords,
                        settings.mode,
                        accounts,
                        settings.excluded_qq_ids,
                    )
                    # 只有人数超额才需要历史次数；不限额是最常见配置。
                    if remaining is None or len(invalid) <= remaining:
                        selected = invalid
                    else:
                        mention_counts = self._state.member_mention_counts(group_id)
                        selected = choose_reminder_targets(invalid, remaining, mention_counts)
                    messages = reminder_messages(selected, self.config.get("reminder_text", ""))
                    # 在发送前原子预留 @额度；失败或重载后不会超额或重复 @。
                    if not self._state.claim(
                        group_id,
                        day,
                        check_time,
                        limit=settings.daily_mention_limit,
                        member_ids=[item["user_id"] for item in selected],
                    ):
                        continue
                    for batch_index, message in enumerate(messages):
                        await asyncio.wait_for(
                            client.call_action(
                                "send_group_msg",
                                group_id=group_id,
                                message=message,
                                self_id=bot_id,
                            ),
                            API_TIMEOUT,
                        )
                        self._state.record_sent_members(
                            group_id,
                            [int(part["data"]["qq"]) for part in message if part["type"] == "at"],
                        )
                        if batch_index + 1 < len(messages):
                            await asyncio.sleep(1)
                    logger.info(
                        f"[{PLUGIN_NAME}] 群 {group_id} {check_time} 检查完成：{total} 位成员，"
                        f"{len(invalid)} 位命中提醒条件，本次 @ {len(selected)} 位，"
                        f"发送 {len(messages)} 条提醒"
                    )
                except Exception as exc:
                    logger.error(f"[{PLUGIN_NAME}] 群 {group_id} 检查或提醒失败：{exc}")

    async def _scheduler(self) -> None:
        cursor = datetime.now(BEIJING)
        while not self._stop.is_set():
            try:
                check_times = parse_check_times(self.config.get("check_times", []))
                if not check_times:
                    await self._stop.wait()
                    return
                # 从上一轮计划时间继续排程；上一轮执行很慢时也不漏掉后续时间点。
                target = next_check(cursor, check_times)
                delay = max(0, (target - datetime.now(BEIJING)).total_seconds())
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                    return
                except asyncio.TimeoutError:
                    if datetime.now(BEIJING).date() == target.date():
                        await self._run_at(target.strftime("%H:%M"), target)
                    cursor = target + timedelta(microseconds=1)
            except asyncio.CancelledError:
                raise
            except ValueError as exc:
                logger.error(f"[{PLUGIN_NAME}] 定时配置无效：{exc}")
                await self._stop.wait()
            except Exception:
                logger.exception(f"[{PLUGIN_NAME}] 定时检查失败")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=60)
                except asyncio.TimeoutError:
                    pass

    @filter.command("群名片检查")
    async def check_cards(self, event: AstrMessageEvent):
        """管理员在已配置的群内预览不合格成员，不发送 @。"""
        if not event.is_admin():
            yield event.plain_result("只有 AstrBot 管理员可以检查群名片。")
            return
        group_id = event.get_group_id()
        if not group_id or not str(group_id).isdigit():
            yield event.plain_result("请在目标 QQ 群中使用此指令。")
            return
        try:
            settings = self._settings()
            if settings is None:
                yield event.plain_result("请先在插件设置中添加目标群号和关键词。")
                return
            if int(group_id) not in settings.group_ids:
                yield event.plain_result("此群未列入插件的目标群号。")
                return
            accounts = await self._accounts()
            if not accounts:
                yield event.plain_result("没有已连接的 QQ 账号，暂时无法检查。")
                return
            _, _, invalid, total = await self._scan_group(
                int(group_id),
                settings.keywords,
                settings.mode,
                accounts,
                settings.excluded_qq_ids,
            )
            remaining = None
            if settings.daily_mention_limit > 0:
                day = datetime.now(BEIJING).date().isoformat()
                remaining = self._state.remaining(
                    int(group_id), day, settings.daily_mention_limit
                )
        except (ValueError, RuntimeError) as exc:
            yield event.plain_result(f"检查失败：{exc}")
            return
        shown = invalid[:30]
        lines = [f"本群 {total} 位成员中，{len(invalid)} 位符合“{settings.mode}”条件。"]
        if settings.daily_mention_limit > 0:
            lines.append(f"今日剩余 @额度：{remaining}/{settings.daily_mention_limit} 人。")
        lines.extend(
            f"{item['user_id']}：{item['card'] or '（未设置群名片）'}" for item in shown
        )
        if len(invalid) > len(shown):
            lines.append(f"……其余 {len(invalid) - len(shown)} 位未列出。")
        yield event.plain_result("\n".join(lines))
