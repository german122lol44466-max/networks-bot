"""Серии «Жду сервер день N». Один пост в день, пропустил день — серия сгорает. Хранится в БД.
После перезапуска бот дочитывает сообщения, которые пришли, пока его не было."""
import asyncio
import re
import logging
from datetime import datetime, timedelta, date, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import discord
from discord import app_commands
from discord.ext import commands, tasks
from ui import card as _card, bar, days, react, reply, send, top_lines, ORANGE, CYAN, RED, GREY


def card(*a, **k):
    k.setdefault("tag", "серии")
    return _card(*a, **k)


log = logging.getLogger("networks.streaks")
COLOR = ORANGE
CATCHUP_LIMIT = 300

# Засчитываем всё, что похоже на «жду сервер»: «Жду сервер день 5», «жду сервера, 5 день», «ждём сервер!!! день 12»,
# «Жду сервер» без числа, «жду сервер 7». Число — необязательно, берём первое в сообщении.
WAIT_RE = re.compile(r"жд[уёе]\w*", re.IGNORECASE)
SERVER_RE = re.compile(r"серв(?:ер|ак|ач)\w*|сервер|server", re.IGNORECASE)
NUM_RE = re.compile(r"\d{1,5}")


def parse(text: str) -> tuple[bool, int | None]:
    """(похоже на «жду сервер», заявленный день или None)."""
    t = (text or "").replace("ё", "е").replace("Ё", "Е")
    if not (WAIT_RE.search(t) and SERVER_RE.search(t)):
        return False, None
    m = NUM_RE.search(t)
    return True, (int(m.group()) if m else None)


def today_in(tz: str) -> date:
    try:
        return datetime.now(ZoneInfo(tz or "Europe/Moscow")).date()
    except (ZoneInfoNotFoundError, ValueError):
        log.error("Нет базы часовых поясов или пояс %r неверный (pip install tzdata). Считаю по Москве, UTC+3.", tz)
        return datetime.now(timezone(timedelta(hours=3))).date()


def day_in(tz: str, when: datetime) -> date:
    """Дата сообщения в поясе сервера (для дочитывания старых сообщений)."""
    try:
        return when.astimezone(ZoneInfo(tz or "Europe/Moscow")).date()
    except (ZoneInfoNotFoundError, ValueError):
        return when.astimezone(timezone(timedelta(hours=3))).date()


def compute(streak: int, last_day: str | None, today: date) -> tuple[str, int]:
    """Возвращает (статус, новая серия). Статус: 'dup' | 'continue' | 'start' | 'reset'."""
    if last_day == today.isoformat():
        return "dup", streak
    if last_day == (today - timedelta(days=1)).isoformat() and streak > 0:
        return "continue", streak + 1
    if streak == 0 or last_day is None:
        return "start", 1
    return "reset", 1


class Streaks(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.locks: dict[int, asyncio.Lock] = {}
        self.caught_up: set[int] = set()
        self.last_seen: dict[int, int] = {}
        self.sweep.start()

    def cog_unload(self):
        self.sweep.cancel()

    def _lock(self, gid):
        return self.locks.setdefault(gid, asyncio.Lock())

    # ---------- дочитывание после перезапуска ----------
    async def catch_up(self, guild: discord.Guild):
        if guild.id in self.caught_up:
            return
        self.caught_up.add(guild.id)
        cfg = await self.bot.db.get_config(guild.id)
        ch = guild.get_channel(cfg["streak_channel"]) if cfg["streak_channel"] else None
        last = cfg.get("streak_last_msg")
        if not ch or not last:
            return
        try:
            missed = [m async for m in ch.history(after=discord.Object(id=last), limit=CATCHUP_LIMIT, oldest_first=True)]
        except discord.HTTPException as e:
            log.warning("Серии: не смог дочитать %s: %s (нужно право «Читать историю сообщений»)", ch.name, e)
            return
        missed = [m for m in missed if not m.author.bot]
        if not missed:
            return
        log.info("Серии: дочитываю %d пропущенных сообщений в %s", len(missed), ch.name)
        for m in missed:
            await self._handle(m, cfg, quiet=True)

    @commands.Cog.listener()
    async def on_ready(self):
        # on_ready бывает и после переподключения — дочитываем заново, что пропустили за время обрыва
        for g in self.bot.guilds:
            async with self._lock(g.id):
                self.caught_up.discard(g.id)
                try:
                    await self.catch_up(g)
                except Exception:
                    log.exception("Серии: ошибка дочитывания на %s", g.name)

    @commands.Cog.listener()
    async def on_message(self, msg: discord.Message):
        if msg.author.bot or not msg.guild:
            return
        cfg = await self.bot.db.get_config(msg.guild.id)
        if msg.channel.id != cfg["streak_channel"]:
            return
        async with self._lock(msg.guild.id):
            try:
                await self.catch_up(msg.guild)
                if msg.id <= self.last_seen.get(msg.guild.id, 0):
                    return
                await self._handle(msg, cfg)
            except Exception:
                log.exception("Ошибка при обработке серии")
                await reply(msg, "Не смог засчитать день — ошибка в боте, смотри логи.", delete_after=20)

    async def _handle(self, msg: discord.Message, cfg: dict, quiet: bool = False):
        self.last_seen[msg.guild.id] = max(self.last_seen.get(msg.guild.id, 0), msg.id)
        await self.bot.db.set_config(msg.guild.id, "streak_last_msg", msg.id)

        ok, claimed = parse(msg.content)
        if not ok:
            if not quiet:
                log.info("Сообщение в канале серий не подошло под шаблон: %r", msg.content[:60])
                await reply(msg, "Чтобы засчитать день, напиши: `Жду сервер день 1` (число — твой день).", delete_after=15)
            return

        # день считаем по дате сообщения, а не «сейчас»: при дочитывании это могло быть вчера
        today = day_in(cfg["timezone"], msg.created_at)
        st = await self.bot.db.get_streak(msg.guild.id, msg.author.id)
        status, new = compute(st["streak"], st["last_day"], today)

        if status == "dup":
            await react(msg, "🕐")
            if not quiet:
                await reply(msg, f"Сегодня ты уже отмечался. Серия: **{st['streak']}** дн. Приходи завтра.", delete_after=15)
            return

        # сообщение старше последней отметки (пришло не по порядку) — не откатываем серию
        if st["last_day"] and today.isoformat() < st["last_day"]:
            return

        await self.bot.db.set_streak(msg.guild.id, msg.author.id, new, today.isoformat())

        if status == "reset":
            e = card("Серия сгорела", f"{msg.author.mention} пропустил день. Было **{st['streak']}**, теперь снова **день 1**.",
                     color=RED, user=msg.author, footer=f"лучшая серия {max(st['best'], st['streak'])}")
            await react(msg, "💀")
            await reply(msg, embed=e)
            return

        best = max(st["best"], new)
        goal = next((g for g in (7, 30, 100, 365) if g > new), 365)
        fire = "🔥" * min(new // 7 + 1, 5)
        e = card(f"{fire} День {new}", None, color=CYAN if new >= 7 else ORANGE, user=msg.author,
                 footer=f"до отметки {goal}: {bar(new, goal)} {new}/{goal}")
        e.description = (f"{msg.author.mention} ждёт сервер уже **{new}** {days(new)} подряд."
                         if new > 1 else f"{msg.author.mention} начал серию ожидания.")
        e.add_field(name="Лучшая", value=f"**{best}** {days(best)}", inline=True)
        if claimed is not None and claimed != new:
            e.add_field(name="Поправка", value=f"Ты написал {claimed}, по счёту это день **{new}**.", inline=True)
        if new in (7, 30, 100, 365):
            e.add_field(name="Отметка", value=f"**{new} дней.** Так держать!", inline=False)
        if quiet:
            e.add_field(name="\u200b", value="Засчитано после перезапуска бота.", inline=False)
        await react(msg, "🔥" if new >= 7 else "✅")
        await reply(msg, embed=e)

    # каждые полчаса проверяем, у кого серия оборвалась, и объявляем
    @tasks.loop(minutes=30)
    async def sweep(self):
        try:
            await self._sweep()
        except Exception:
            log.exception("Серии: ошибка в проверке сгоревших серий")   # цикл не должен умирать

    async def _sweep(self):
        for row in await self.bot.db.all_guilds_with_streak_channel():
            guild = self.bot.get_guild(row["guild_id"])
            channel = guild.get_channel(row["streak_channel"]) if guild else None
            if not channel:
                continue
            today = today_in(row["timezone"] or "Europe/Moscow")
            yesterday = (today - timedelta(days=1)).isoformat()
            for s in await self.bot.db.active_streaks(row["guild_id"]):
                if s["last_day"] and s["last_day"] < yesterday:
                    await self.bot.db.set_streak(row["guild_id"], s["user_id"], 0, None)
                    e = card("Серия сгорела", f"<@{s['user_id']}> не отметился вчера — серия из **{s['streak']}** {days(s['streak'])} сгорела.", color=RED)
                    await send(channel, embed=e)

    @sweep.before_loop
    async def before_sweep(self):
        await self.bot.wait_until_ready()

    # ---------- команды ----------
    @app_commands.command(name="streak", description="Твоя серия «жду сервер»")
    async def streak(self, inter: discord.Interaction, user: discord.Member | None = None):
        user = user or inter.user
        st = await self.bot.db.get_streak(inter.guild_id, user.id)
        e = card("Серия ожидания", f"{user.mention}\nСейчас: **{st['streak']}** {days(st['streak'])} · лучшая: **{st['best']}**", user=user)
        await inter.response.send_message(embed=e)

    @app_commands.command(name="topstreak", description="Топ серий")
    async def topstreak(self, inter: discord.Interaction):
        rows = await self.bot.db.top_streaks(inter.guild_id, 10)
        if not rows:
            await inter.response.send_message("Активных серий пока нет.")
            return
        e = card("Топ серий", top_lines(rows, lambda r: f"<@{r['user_id']}> — **{r['streak']}** {days(r['streak'])} · лучшая {r['best']}"), color=CYAN)
        await inter.response.send_message(embed=e)

    @app_commands.command(name="setstreak", description="Выставить серию игроку вручную")
    @app_commands.default_permissions(administrator=True)
    async def setstreak(self, inter: discord.Interaction, user: discord.Member, days: app_commands.Range[int, 0, 10000]):
        cfg = await self.bot.db.get_config(inter.guild_id)
        today = today_in(cfg["timezone"]).isoformat() if days > 0 else None
        await self.bot.db.set_streak(inter.guild_id, user.id, days, today)
        await inter.response.send_message(f"{user.mention}: серия **{days}** дн.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Streaks(bot))
