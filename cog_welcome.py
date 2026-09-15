"""Приветствия и прощания в тематике сервера (Half-Life: Alyx)."""
import random
import logging
import discord
from discord.ext import commands
from ui import card, ORANGE, CYAN, RED, GREY

log = logging.getLogger("networks.welcome")

# короткие тематические фразы — свои, не цитаты из игры
LINES = [
    "Сити-24 стал на одного жителя больше. Гражданская лояльность пока не проверена.",
    "Сканеры Альянса засекли новую подпись. Сопротивление уже знает.",
    "Ворт нашептал, что ты придёшь. Ворты редко ошибаются.",
    "Проверь гравитационные перчатки — тут они пригодятся.",
    "Хедкрабы в подвале, кофе в штабе. Добро пожаловать.",
    "Карантинная зона открыта. Респиратор не обязателен, чувство юмора — да.",
    "Ещё один против Альянса. Хорошо, что на нашей стороне.",
    "Транслятор Сопротивления поймал твой сигнал. Мы тебя слышим.",
]

BYE = [
    "покинул Сити-24. Поезд ушёл на север.",
    "ушёл в карантинную зону и не вернулся.",
    "потерял связь с Сопротивлением.",
    "телепортировался неизвестно куда. Ворты молчат.",
]


def ordinal(n: int) -> str:
    return f"{n}-й"


class Welcome(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        try:
            await self._welcome(member)
        except Exception:
            log.exception("Приветствие: ошибка для %s", member)

    async def _welcome(self, member: discord.Member):
        cfg = await self.bot.db.get_config(member.guild.id)
        ch = member.guild.get_channel(cfg["welcome_channel"]) if cfg.get("welcome_channel") else None
        if not ch:
            return
        guild = member.guild
        custom = cfg.get("welcome_text")

        e = discord.Embed(color=ORANGE)
        e.title = "ДОБРО ПОЖАЛОВАТЬ НА NETWORK"
        e.description = (
            f"### {member.mention}\n"
            f"{custom.replace('{user}', member.mention).replace('{server}', guild.name) if custom else random.choice(LINES)}"
        )
        e.set_thumbnail(url=member.display_avatar.replace(size=256).url)
        e.add_field(name="Ты", value=f"**{ordinal(guild.member_count)}** участник", inline=True)
        e.add_field(name="Аккаунт создан", value=f"<t:{int(member.created_at.timestamp())}:D>", inline=True)
        e.add_field(name="Тематика", value="Half-Life: Alyx · RP", inline=True)
        rules = discord.utils.find(lambda c: "правил" in c.name.lower() or "rules" in c.name.lower(), guild.text_channels)
        tips = "Загляни в правила" + (f" — {rules.mention}" if rules else "") + ", представься и не корми хедкрабов."
        e.add_field(name="С чего начать", value=tips, inline=False)
        if guild.icon:
            e.set_footer(text=f"Network's Bot · {guild.name}", icon_url=guild.icon.url)
        else:
            e.set_footer(text=f"Network's Bot · {guild.name}")
        try:
            await ch.send(content=member.mention, embed=e)
        except discord.HTTPException as err:
            log.warning("Не смог отправить приветствие в %s: %s", ch.name, err)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        cfg = await self.bot.db.get_config(member.guild.id)
        ch = member.guild.get_channel(cfg["welcome_channel"]) if cfg.get("welcome_channel") else None
        if not ch:
            return
        e = card("Связь потеряна", f"**{member.display_name}** {random.choice(BYE)}", color=GREY, thumb=member,
                 footer=f"осталось участников: {member.guild.member_count}")
        try:
            await ch.send(embed=e)
        except discord.HTTPException:
            pass


async def setup(bot):
    await bot.add_cog(Welcome(bot))
