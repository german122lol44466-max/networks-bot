"""Канал счёта 1 → 999. Ошибка или два раза подряд — заново. Удалил сообщение — бот напомнит, где остановились.
После перезапуска бот дочитывает сообщения, которые пришли, пока его не было."""
import asyncio
import re
import logging
import discord
from discord import app_commands
from discord.ext import commands
from ui import card as _card, bar, react, reply, send, delete, ORANGE, CYAN, RED

log = logging.getLogger("networks.counting")


def card(*a, **k):
    k.setdefault("tag", "счёт")
    return _card(*a, **k)

COLOR = ORANGE
LIMIT = 999
CATCHUP_LIMIT = 300          # сколько пропущенных сообщений дочитываем после перезапуска
NUM_RE = re.compile(r"^\W*(\d{1,6})\W*$")   # «5», «5.», «**5**», « 5! » — всё число; «5 6» или «число 5» — нет


class Counting(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.locks: dict[int, asyncio.Lock] = {}
        self.caught_up: set[int] = set()
        self.last_seen: dict[int, int] = {}    # guild -> id последнего обработанного сообщения (любого)

    def _lock(self, gid):
        return self.locks.setdefault(gid, asyncio.Lock())

    @staticmethod
    def _parse(text: str):
        m = NUM_RE.match(text or "")
        return int(m.group(1)) if m else None

    async def _reset(self, channel, state, reason: str, who: discord.Member | None):
        await self.bot.db.set_count(channel.guild.id, 0, None, None)
        e = card("Счёт сбит", f"{who.mention + ' ' if who else ''}{reason}\nДошли до **{state['current']}**. Начинаем заново с **1**.",
                 color=RED, user=who, footer=f"рекорд {max(state['record'], state['current'])}")
        await send(channel, embed=e)

    # ---------- дочитывание после перезапуска ----------
    async def catch_up(self, guild: discord.Guild):
        """Обработать сообщения, пришедшие в канал счёта, пока бот был выключен. Вызывать под локом."""
        if guild.id in self.caught_up:
            return
        self.caught_up.add(guild.id)
        cfg = await self.bot.db.get_config(guild.id)
        ch = guild.get_channel(cfg["count_channel"]) if cfg["count_channel"] else None
        state = await self.bot.db.get_count(guild.id)
        if not ch or not state["last_msg"]:
            return
        try:
            missed = [m async for m in ch.history(after=discord.Object(id=state["last_msg"]), limit=CATCHUP_LIMIT, oldest_first=True)]
        except discord.HTTPException as e:
            log.warning("Счёт: не смог дочитать %s: %s (нужно право «Читать историю сообщений»)", ch.name, e)
            return
        missed = [m for m in missed if not m.author.bot]
        if not missed:
            return
        log.info("Счёт: дочитываю %d пропущенных сообщений в %s", len(missed), ch.name)
        for m in missed:
            await self._process(m, quiet=True)

    async def _process(self, msg: discord.Message, quiet: bool = False):
        """Одно сообщение канала счёта. quiet — режим дочитывания (болтовню не трогаем)."""
        self.last_seen[msg.guild.id] = max(self.last_seen.get(msg.guild.id, 0), msg.id)
        state = await self.bot.db.get_count(msg.guild.id)
        n = self._parse(msg.content)
        expected = state["current"] + 1

        if n is None:
            # болтовня в канале счёта просто удаляем, счёт не трогаем
            if not quiet:
                await delete(msg)
            return

        if state["last_user"] == msg.author.id:
            await self._reset(msg.channel, state, "написал два числа подряд — нельзя.", msg.author)
            await react(msg, "❌")
            return

        if n != expected:
            await self._reset(msg.channel, state, f"написал **{n}**, а ждали **{expected}**.", msg.author)
            await react(msg, "❌")
            return

        await self.bot.db.add_count_number(msg.guild.id, msg.author.id)
        await self.bot.db.set_count(msg.guild.id, n, msg.author.id, msg.id)
        await react(msg, "✅" if n % 100 else "🏁")

        if n >= LIMIT:
            await self.bot.db.set_count(msg.guild.id, 0, None, None)
            e = card("999! Досчитали!", f"Финальное число поставил {msg.author.mention}. Счёт начинается заново с **1**.", color=CYAN, user=msg.author)
            await send(msg.channel, embed=e)

    @commands.Cog.listener()
    async def on_message(self, msg: discord.Message):
        if msg.author.bot or not msg.guild:
            return
        cfg = await self.bot.db.get_config(msg.guild.id)
        if msg.channel.id != cfg["count_channel"]:
            return
        async with self._lock(msg.guild.id):
            try:
                await self.catch_up(msg.guild)
                if msg.id <= self.last_seen.get(msg.guild.id, 0):
                    return          # дочитывание уже обработало это сообщение
                await self._process(msg)
            except Exception:
                log.exception("Счёт: ошибка при обработке сообщения %s", msg.id)

    @commands.Cog.listener()
    async def on_ready(self):
        # on_ready бывает и после переподключения — дочитываем заново, что пропустили за время обрыва
        for g in self.bot.guilds:
            async with self._lock(g.id):
                self.caught_up.discard(g.id)
                try:
                    await self.catch_up(g)
                except Exception:
                    log.exception("Счёт: ошибка дочитывания на %s", g.name)

    @commands.Cog.listener()
    async def on_message_delete(self, msg: discord.Message):
        if not msg.guild:
            return
        cfg = await self.bot.db.get_config(msg.guild.id)
        if msg.channel.id != cfg["count_channel"]:
            return
        state = await self.bot.db.get_count(msg.guild.id)
        if state["last_msg"] != msg.id:
            return
        author = msg.author.mention if msg.author else "кто-то"
        e = card("Число удалено", f"{author} удалил последнее число. Остановились на **{state['current']}**, следующее — **{state['current'] + 1}**.")
        note = await send(msg.channel, embed=e)
        # сообщения уже нет — привязываем «последнее» к напоминанию, чтобы дочитывание и повторное удаление не путали
        await self.bot.db.set_count(msg.guild.id, state["current"], state["last_user"], note.id if note else None)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if not after.guild or after.author.bot or before.content == after.content:
            return
        cfg = await self.bot.db.get_config(after.guild.id)
        if after.channel.id != cfg["count_channel"]:
            return
        state = await self.bot.db.get_count(after.guild.id)
        if state["last_msg"] == after.id and self._parse(after.content) != state["current"]:
            e = card("Число изменено", f"{after.author.mention} изменил последнее число. Счёт на **{state['current']}**, следующее — **{state['current'] + 1}**.")
            await send(after.channel, embed=e)

    @app_commands.command(name="count", description="Где сейчас счёт")
    async def count(self, inter: discord.Interaction):
        state = await self.bot.db.get_count(inter.guild_id)
        e = card("Счёт", f"{bar(state['current'], LIMIT)} **{state['current']}** / {LIMIT}\nСледующее: **{state['current'] + 1}** · рекорд: **{state['record']}**")
        await inter.response.send_message(embed=e)

    @app_commands.command(name="setcount", description="Выставить счёт вручную")
    @app_commands.default_permissions(administrator=True)
    async def setcount(self, inter: discord.Interaction, number: app_commands.Range[int, 0, LIMIT - 1]):
        await self.bot.db.set_count(inter.guild_id, number, None, None)
        await inter.response.send_message(f"Счёт выставлен на **{number}**, следующее — **{number + 1}**.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Counting(bot))
