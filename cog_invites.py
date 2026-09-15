"""Очки за приглашения: кто кого привёл, +1 очко, красивое сообщение в канал."""
import logging
import time
import discord
from discord import app_commands
from discord.ext import commands
from ui import card as _card, send, top_lines, ORANGE, CYAN, GREY


def card(*a, **k):
    k.setdefault("tag", "приглашения")
    return _card(*a, **k)


log = logging.getLogger("networks.invites")
COLOR = ORANGE


class Invites(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.cache: dict[int, dict[str, int]] = {}   # guild_id -> {code: uses}

    async def _snapshot(self, guild: discord.Guild):
        try:
            invites = await guild.invites()
            self.cache[guild.id] = {i.code: i.uses or 0 for i in invites}
        except discord.Forbidden:
            log.warning("Нет права «Управление сервером» на %s — приглашения не отслеживаются", guild.name)
        except discord.HTTPException as e:
            log.warning("Не смог получить приглашения %s: %s", guild.name, e)

    @commands.Cog.listener()
    async def on_ready(self):
        for g in self.bot.guilds:
            await self._snapshot(g)
        log.info("Снимок приглашений сделан для %d серверов", len(self.cache))

    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        await self._snapshot(guild)
        log.info("Добавлен на сервер %s", guild.name)

    async def refresh(self, guild: discord.Guild):
        await self._snapshot(guild)

    @commands.Cog.listener()
    async def on_invite_create(self, invite):
        self.cache.setdefault(invite.guild.id, {})[invite.code] = invite.uses or 0

    @commands.Cog.listener()
    async def on_invite_delete(self, invite):
        self.cache.get(invite.guild.id, {}).pop(invite.code, None)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        try:
            await self._on_join(member)
        except Exception:
            log.exception("Приглашения: ошибка при заходе %s", member)

    async def _on_join(self, member: discord.Member):
        guild = member.guild
        log.info("Зашёл %s на %s", member, guild.name)
        if guild.id not in self.cache:
            log.warning("Не было снимка приглашений для %s — первый заход может определиться неточно", guild.name)
        old = self.cache.get(guild.id, {})
        try:
            invites = await guild.invites()
        except discord.HTTPException as e:
            log.warning("Не смог получить приглашения %s: %s", guild.name, e)
            return
        used = None
        for inv in invites:
            if (inv.uses or 0) > old.get(inv.code, 0):
                used = inv
                break
        if used is None:
            # инвайт мог быть одноразовым и уже исчезнуть из списка
            gone = set(old) - {i.code for i in invites}
            if len(gone) == 1:
                code = gone.pop()
                log.info("Одноразовое приглашение %s исчезло после захода %s", code, member)
        self.cache[guild.id] = {i.code: i.uses or 0 for i in invites}

        cfg = await self.bot.db.get_config(guild.id)
        channel = guild.get_channel(cfg["invite_channel"]) if cfg["invite_channel"] else None

        if used is None or used.inviter is None:
            if channel:
                e = card("Новый участник", f"{member.mention} зашёл, но по какому приглашению — неясно "
                         "(ссылка на сервер или приглашение удалено).", color=GREY, user=member)
                await send(channel, embed=e)
            return

        inviter = used.inviter
        if inviter.bot or inviter.id == member.id:
            return

        # защита от накрутки: один и тот же человек приносит очко только раз
        if await self.bot.db.was_invited_before(guild.id, member.id):
            await self.bot.db.log_invite(guild.id, inviter.id, member.id, used.code, int(time.time()))
            if channel:
                e = card("Возвращение", f"{member.mention} вернулся по ссылке {inviter.mention}. "
                         "Очко за повторный заход не начисляется.", color=GREY, user=member)
                await send(channel, embed=e)
            return

        total = await self.bot.db.add_point(guild.id, inviter.id, 1)
        await self.bot.db.log_invite(guild.id, inviter.id, member.id, used.code, int(time.time()))

        if channel:
            e = card("Новый игрок на сервере", None, color=ORANGE, user=inviter, thumb=member,
                     footer=f"discord.gg/{used.code} · участников: {guild.member_count}")
            e.description = f"{inviter.mention} привёл {member.mention}"
            e.add_field(name="Награда", value="**+1** очко", inline=True)
            e.add_field(name="Всего", value=f"**{total}**", inline=True)
            await send(channel, embed=e)
        log.info("%s пригласил %s (итого %d)", inviter, member, total)

    # ---------- команды ----------
    @app_commands.command(name="points", description="Очки за приглашения")
    async def points(self, inter: discord.Interaction, user: discord.Member | None = None):
        user = user or inter.user
        pts = await self.bot.db.get_points(inter.guild_id, user.id)
        e = card("Очки за приглашения", f"{user.mention}: **{pts}**", user=user)
        await inter.response.send_message(embed=e)

    @app_commands.command(name="top", description="Топ по приглашениям")
    async def top(self, inter: discord.Interaction):
        rows = await self.bot.db.top_points(inter.guild_id, 10)
        if not rows:
            await inter.response.send_message("Пока никто никого не пригласил.")
            return
        e = card("Топ приглашающих", top_lines(rows, lambda r: f"<@{r['user_id']}> — **{r['points']}** очк."), color=CYAN)
        await inter.response.send_message(embed=e)

    @app_commands.command(name="addpoints", description="Начислить или снять очки вручную")
    @app_commands.default_permissions(administrator=True)
    async def addpoints(self, inter: discord.Interaction, user: discord.Member, amount: int):
        total = await self.bot.db.add_point(inter.guild_id, user.id, amount)
        await inter.response.send_message(f"{user.mention}: {amount:+d}, теперь **{total}**.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Invites(bot))
