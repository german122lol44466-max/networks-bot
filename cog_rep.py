"""Репутация: +rep / -rep игрокам с причиной, карточка профиля, топ."""
import time
import discord
from discord import app_commands
from discord.ext import commands
from ui import card as _card, bar, top_lines, ORANGE, CYAN, RED, GREY


def card(*a, **k):
    k.setdefault("tag", "репутация")
    return _card(*a, **k)

COOLDOWN = 24 * 3600          # один отзыв на одного игрока раз в сутки
REASON_MAX = 120

REP_CHOICES = [
    app_commands.Choice(name="+rep  (хороший игрок)", value=1),
    app_commands.Choice(name="-rep  (плохой игрок)", value=-1),
]


def find_member(guild: discord.Guild, name: str) -> discord.Member | None:
    q = name.strip().lstrip("@").lower()
    if not q:
        return None
    exact, prefix, inside = [], [], []
    for m in guild.members:
        names = {m.name.lower(), m.display_name.lower(), (m.global_name or "").lower()}
        if q in names:
            exact.append(m)
        elif any(n.startswith(q) for n in names if n):
            prefix.append(m)
        elif any(q in n for n in names if n):
            inside.append(m)
    for bucket in (exact, prefix, inside):
        if bucket:
            return bucket[0]
    return None


def rep_color(total: int) -> int:
    return CYAN if total >= 10 else ORANGE if total >= 0 else RED


def rep_title(total: int) -> str:
    if total >= 50: return "Легенда сервера"
    if total >= 25: return "Уважаемый"
    if total >= 10: return "Надёжный"
    if total >= 1: return "Нормальный"
    if total == 0: return "Новичок"
    if total >= -5: return "Под подозрением"
    return "Токсик"


class Rep(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ---------- общий обработчик ----------
    async def _give(self, inter: discord.Interaction, target: discord.Member, delta: int, reason: str | None):
        if target.bot:
            await inter.response.send_message("Ботам репутация ни к чему.", ephemeral=True); return
        if target.id == inter.user.id:
            await inter.response.send_message("Себе репутацию ставить нельзя.", ephemeral=True); return
        reason = (reason or "").strip()[:REASON_MAX] or None

        last = await self.bot.db.rep_last_from(inter.guild_id, inter.user.id, target.id)
        now = int(time.time())
        if last and now - last < COOLDOWN:
            left = COOLDOWN - (now - last)
            h, m = divmod(left // 60, 60)
            await inter.response.send_message(f"Ты уже оценивал {target.mention}. Снова можно через {h} ч {m} мин.", ephemeral=True)
            return

        if not inter.guild.get_member(target.id):
            await inter.response.send_message("Этого игрока уже нет на сервере.", ephemeral=True); return
        await self.bot.db.rep_add(inter.guild_id, inter.user.id, target.id, delta, reason, now)
        s = await self.bot.db.rep_summary(inter.guild_id, target.id)
        sign = "+rep" if delta > 0 else "-rep"
        e = card(f"{sign} для {target.display_name}", None, color=CYAN if delta > 0 else RED, user=inter.user, thumb=target,
                 footer=f"всего у {target.display_name}: {s['total']:+d}")
        e.description = f"{inter.user.mention} → {target.mention}" + (f"\n> {reason}" if reason else "")
        await inter.response.send_message(embed=e)

        cfg = await self.bot.db.get_config(inter.guild_id)
        if cfg.get("rep_channel") and cfg["rep_channel"] != inter.channel_id:
            ch = inter.guild.get_channel(cfg["rep_channel"])
            if ch:
                try:
                    await ch.send(embed=e)
                except discord.HTTPException:
                    pass

    # ---------- команды ----------
    @app_commands.command(name="repdplayer", description="Поставить +rep / -rep игроку (выбор из списка)")
    @app_commands.describe(player="Игрок", rep="Плюс или минус", reason="Причина (необязательно)")
    @app_commands.choices(rep=REP_CHOICES)
    async def repdplayer(self, inter: discord.Interaction, player: discord.Member, rep: app_commands.Choice[int], reason: str | None = None):
        await self._give(inter, player, rep.value, reason)

    @app_commands.command(name="repsplayer", description="Поставить +rep / -rep игроку по имени")
    @app_commands.describe(name="Ник игрока (можно часть)", rep="Плюс или минус", reason="Причина (необязательно)")
    @app_commands.choices(rep=REP_CHOICES)
    async def repsplayer(self, inter: discord.Interaction, name: str, rep: app_commands.Choice[int], reason: str | None = None):
        m = find_member(inter.guild, name)
        if m is None:
            await inter.response.send_message(f"Не нашёл игрока «{name}» на сервере.", ephemeral=True); return
        await self._give(inter, m, rep.value, reason)

    @app_commands.command(name="rep", description="Репутация игрока (или своя)")
    async def rep(self, inter: discord.Interaction, player: discord.Member | None = None):
        target = player or inter.user
        s = await self.bot.db.rep_summary(inter.guild_id, target.id)
        rank = await self.bot.db.rep_rank(inter.guild_id, target.id)
        total, plus, minus = s["total"], s["plus"], s["minus"]
        votes = plus + minus
        ratio = plus / votes if votes else 0
        e = card(f"{target.display_name} · {rep_title(total)}", None, color=rep_color(total), thumb=target,
                 footer=f"место в топе: {rank if rank else '—'} · выдал другим: {s['given']}")
        e.description = f"**{total:+d}** репутации\n{bar(int(ratio * 100), 100)} {int(ratio * 100)}% положительных"
        e.add_field(name="+rep", value=f"**{plus}**", inline=True)
        e.add_field(name="-rep", value=f"**{minus}**", inline=True)
        e.add_field(name="Всего отзывов", value=f"**{votes}**", inline=True)
        if s["recent"]:
            lines = []
            for r in s["recent"]:
                sign = "🟢 +rep" if r["delta"] > 0 else "🔴 -rep"
                lines.append(f"{sign} от <@{r['giver_id']}> <t:{r['ts']}:R>" + (f" — {r['reason']}" if r["reason"] else ""))
            e.add_field(name="Последние отзывы", value="\n".join(lines), inline=False)
        else:
            e.add_field(name="Последние отзывы", value="Пока никто не оценивал.", inline=False)
        await inter.response.send_message(embed=e)

    @app_commands.command(name="reptop", description="Топ по репутации")
    async def reptop(self, inter: discord.Interaction):
        rows = await self.bot.db.rep_top(inter.guild_id, 10)
        if not rows:
            await inter.response.send_message("Пока никто никого не оценивал."); return
        e = card("Топ по репутации", top_lines(rows, lambda r: f"<@{r['target_id']}> — **{r['t']:+d}** ({r['n']} отз.)"), color=CYAN)
        await inter.response.send_message(embed=e)

    @app_commands.command(name="repclear", description="Обнулить репутацию игрока")
    @app_commands.default_permissions(administrator=True)
    async def repclear(self, inter: discord.Interaction, player: discord.Member):
        n = await self.bot.db.rep_clear(inter.guild_id, player.id)
        await inter.response.send_message(f"Удалено отзывов: {n}. Репутация {player.mention} обнулена.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Rep(bot))
