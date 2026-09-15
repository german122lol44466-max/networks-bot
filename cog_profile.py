"""Профиль игрока: /profile [игрок] — всё, что бот о нём знает, в одной карточке. Кнопка — аватарка во весь размер."""
import logging
import discord
from discord import app_commands
from discord.ext import commands
from ui import card as _card, bar, days, field, sep, stat, ORANGE, CYAN, RED, GREY
from cog_rep import rep_title, rep_color

log = logging.getLogger("networks.profile")


def card(*a, **k):
    k.setdefault("tag", "профиль")
    return _card(*a, **k)


def pos(rank: int | None, total: int) -> str:
    """«🥇 1 из 12», «#4 из 12» или «—»."""
    if not rank:
        return "—"
    medal = {1: "🥇 ", 2: "🥈 ", 3: "🥉 "}.get(rank, "#")
    return f"{medal}{rank} из {total}"


class AvatarView(discord.ui.View):
    def __init__(self, target: discord.Member):
        super().__init__(timeout=300)
        self.target = target

    @discord.ui.button(label="Аватарка", emoji="🖼️", style=discord.ButtonStyle.secondary)
    async def avatar(self, inter: discord.Interaction, _):
        t = self.target
        e = card(f"Аватарка {t.display_name}", None, color=t.color.value or ORANGE)
        e.set_image(url=t.display_avatar.replace(size=1024).url)
        links = [f"[PNG]({t.display_avatar.replace(size=1024, format='png').url})"]
        if t.display_avatar.is_animated():
            links.append(f"[GIF]({t.display_avatar.replace(size=1024, format='gif').url})")
        if t.guild_avatar and t.avatar:
            links.append(f"[Глобальная]({t.avatar.replace(size=1024).url})")
        e.description = " · ".join(links)
        try:
            await inter.response.send_message(embed=e, ephemeral=True)
        except discord.HTTPException as err:
            log.warning("Не смог показать аватарку: %s", err)


class Profile(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def build(self, guild: discord.Guild, t: discord.Member) -> discord.Embed:
        db = self.bot.db
        gid = guild.id
        pts = await db.get_points(gid, t.id)
        invited = await db.invited_count(gid, t.id)
        r_pts, n_pts = await db.rank_points(gid, t.id)
        st = await db.get_streak(gid, t.id)
        r_st, n_st = await db.rank_streak(gid, t.id)
        r_best, n_best = await db.rank_best_streak(gid, t.id)
        words = await db.words_count(gid, t.id)
        r_w, n_w = await db.rank_words(gid, t.id)
        nums = await db.count_numbers(gid, t.id)
        r_c, n_c = await db.rank_count(gid, t.id)
        s_best, s_games = await db.snake_stats(gid, t.id)
        r_s, n_s = await db.rank_snake(gid, t.id)
        d_w, d_l = await db.duel_stats(gid, t.id)
        rep = await db.rep_summary(gid, t.id)
        r_rep, n_rep = await db.rank_rep(gid, t.id)

        total = rep["total"]
        votes = rep["plus"] + rep["minus"]
        ratio = int(rep["plus"] / votes * 100) if votes else 0
        joined = f"<t:{int(t.joined_at.timestamp())}:D>" if t.joined_at else "—"

        e = card(f"{t.display_name}", None, color=rep_color(total), thumb=t,
                 footer=f"на сервере с {t.joined_at.strftime('%d.%m.%Y') if t.joined_at else '?'}")
        e.description = (f"{t.mention} · **{rep_title(total)}**\n"
                         f"Аккаунт создан <t:{int(t.created_at.timestamp())}:D> · зашёл {joined}\n{sep()}")

        # --- репутация ---
        field(e, "⚖️ Репутация",
              f"**{total:+d}** · место {pos(r_rep, n_rep)}\n"
              f"{bar(ratio, 100, 10)} {ratio}% положительных\n"
              f"🟢 {rep['plus']}  🔴 {rep['minus']}  · выдал другим: {rep['given']}", inline=False)

        # --- серия ---
        cur_s, best_s = st["streak"], st["best"]
        s_text = "\n".join([stat("Сейчас", f"{cur_s} {days(cur_s)}"), stat("Место", pos(r_st, n_st)),
                            stat("Лучшая", f"{best_s} {days(best_s)}"), stat("Место по лучшей", pos(r_best, n_best))])
        field(e, "🔥 Жду сервер", s_text)

        # --- приглашения ---
        field(e, "📡 Приглашения",
              "\n".join([stat("Очки", pts), stat("Привёл", f"{invited} чел."), stat("Место", pos(r_pts, n_pts))]))

        # --- игры ---
        field(e, "🔢 Счёт", "\n".join([stat("Чисел", nums), stat("Место", pos(r_c, n_c))]))
        field(e, "🔤 Слова", "\n".join([stat("Слов", words), stat("Место", pos(r_w, n_w))]))
        field(e, "🐍 Змейка", "\n".join([stat("Рекорд", f"{s_best} 🍎"), stat("Игр", s_games), stat("Место", pos(r_s, n_s)), stat("Дуэли", f"{d_w}W / {d_l}L")]))

        # --- последние отзывы ---
        if rep["recent"]:
            lines = []
            for r in rep["recent"][:5]:
                sign = "🟢" if r["delta"] > 0 else "🔴"
                lines.append(f"{sign} <@{r['giver_id']}> <t:{r['ts']}:R>" + (f" — {r['reason']}" if r["reason"] else ""))
            field(e, "💬 Что о нём пишут", "\n".join(lines), inline=False)
        else:
            field(e, "💬 Что о нём пишут", "Пока никто не оценивал. `/repdplayer`", inline=False)

        # --- проекты (роли) ---
        proj = {r["role_id"]: (r["emoji"], r["name"]) for r in await db.project_roles(gid)}
        mine = [f"{em} {nm}" for rid, (em, nm) in proj.items() if any(role.id == rid for role in t.roles)]
        if mine:
            field(e, "🎯 Любимый проект", " · ".join(mine), inline=False)
        return e

    @app_commands.command(name="profile", description="Профиль игрока: серия, репутация, места в топах, игры")
    @app_commands.describe(player="Чей профиль (пусто — свой)")
    async def profile(self, inter: discord.Interaction, player: discord.Member | None = None):
        await inter.response.defer()
        t = player or inter.user
        if not isinstance(t, discord.Member):
            t = inter.guild.get_member(t.id) or await inter.guild.fetch_member(t.id)
        e = await self.build(inter.guild, t)
        await inter.followup.send(embed=e, view=AvatarView(t))

    @app_commands.command(name="avatar", description="Аватарка игрока во весь размер")
    async def avatar(self, inter: discord.Interaction, player: discord.Member | None = None):
        t = player or inter.user
        e = card(f"Аватарка {t.display_name}", None, color=getattr(t, "color", None) and t.color.value or ORANGE)
        e.set_image(url=t.display_avatar.replace(size=1024).url)
        await inter.response.send_message(embed=e)


async def setup(bot):
    await bot.add_cog(Profile(bot))
