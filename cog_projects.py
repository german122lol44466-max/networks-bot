"""«Любимый проект»: карточка с реакциями, реакция = роль проекта. Одна роль на человека."""
import logging
import discord
from discord import app_commands
from discord.ext import commands
from ui import card as _card, ORANGE, CYAN, RED

log = logging.getLogger("networks.projects")


def card(*a, **k):
    k.setdefault("tag", "проекты")
    return _card(*a, **k)


def emoji_key(e: discord.PartialEmoji | str) -> str:
    return str(e)


class Projects(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def build_embed(self, guild: discord.Guild) -> discord.Embed:
        rows = await self.bot.db.project_roles(guild.id)
        e = card("Любимый проект", "Нажми на реакцию, чтобы получить роль своего любимого проекта.\n"
                 "Одна роль на человека: выбрал другую — предыдущая снимется.", color=CYAN)
        if rows:
            e.add_field(name="Проекты", value="\n".join(f"{r['emoji']} — **{r['name']}** · <@&{r['role_id']}>" for r in rows), inline=False)
        else:
            e.add_field(name="Проекты", value="Список пуст. Админ: `/project add`.", inline=False)
        return e

    async def refresh(self, guild: discord.Guild):
        cfg = await self.bot.db.get_config(guild.id)
        ch = guild.get_channel(cfg["projects_channel"]) if cfg.get("projects_channel") else None
        if not ch:
            return None
        msg = None
        if cfg.get("projects_msg"):
            try:
                msg = await ch.fetch_message(cfg["projects_msg"])
            except discord.HTTPException:
                msg = None
        e = await self.build_embed(guild)
        if msg:
            try:
                await msg.edit(embed=e)
            except discord.HTTPException as err:
                log.warning("Проекты: не смог обновить карточку: %s", err)
        else:
            msg = await ch.send(embed=e)
            await self.bot.db.set_config(guild.id, "projects_msg", msg.id)
        wanted = [r["emoji"] for r in await self.bot.db.project_roles(guild.id)]
        present = [str(r.emoji) for r in msg.reactions]
        for em in wanted:
            if em not in present:
                try:
                    await msg.add_reaction(em)
                except discord.HTTPException:
                    log.warning("Не могу поставить реакцию %s", em)
        return msg

    async def _mapping(self, guild_id: int) -> dict[str, int]:
        return {r["emoji"]: r["role_id"] for r in await self.bot.db.project_roles(guild_id)}

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.bot.user.id or not payload.guild_id:
            return
        try:
            await self._on_add(payload)
        except Exception:
            log.exception("Проекты: ошибка при реакции")

    async def _on_add(self, payload: discord.RawReactionActionEvent):
        cfg = await self.bot.db.get_config(payload.guild_id)
        if payload.message_id != cfg.get("projects_msg"):
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        member = payload.member or guild.get_member(payload.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(payload.user_id)
            except discord.HTTPException:
                return
        if member.bot:
            return
        mapping = await self._mapping(guild.id)
        key = emoji_key(payload.emoji)
        ch = guild.get_channel(payload.channel_id)
        if ch is None:
            return
        try:
            msg = await ch.fetch_message(payload.message_id)
        except discord.HTTPException as e:
            log.warning("Проекты: не смог прочитать карточку: %s", e)
            return
        if key not in mapping:
            try:
                await msg.remove_reaction(payload.emoji, member)
            except discord.HTTPException:
                pass
            return
        role = guild.get_role(mapping[key])
        if role is None:
            return
        # снять другие роли проектов и их реакции
        others = [guild.get_role(rid) for em, rid in mapping.items() if em != key]
        to_remove = [r for r in others if r and r in member.roles]
        try:
            if to_remove:
                await member.remove_roles(*to_remove, reason="Смена любимого проекта")
            if role not in member.roles:
                await member.add_roles(role, reason="Любимый проект")
        except discord.Forbidden:
            log.warning("Нет права выдавать роль %s (роль бота должна быть выше в списке ролей)", role.name)
            return
        for em, rid in mapping.items():
            if em != key:
                for reaction in msg.reactions:
                    if str(reaction.emoji) == em:
                        try:
                            await msg.remove_reaction(reaction.emoji, member)
                        except discord.HTTPException:
                            pass

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if not payload.guild_id:
            return
        cfg = await self.bot.db.get_config(payload.guild_id)
        if payload.message_id != cfg.get("projects_msg"):
            return
        guild = self.bot.get_guild(payload.guild_id)
        member = guild.get_member(payload.user_id) if guild else None
        if member is None or member.bot:
            return
        mapping = await self._mapping(guild.id)
        rid = mapping.get(emoji_key(payload.emoji))
        role = guild.get_role(rid) if rid else None
        if role and role in member.roles:
            try:
                await member.remove_roles(role, reason="Убрал реакцию любимого проекта")
            except discord.Forbidden:
                pass

    # ---------- команды ----------
    project = app_commands.Group(name="project", description="Роли любимых проектов (админ)",
                                 default_permissions=discord.Permissions(administrator=True))

    @project.command(name="add", description="Привязать реакцию к роли проекта")
    @app_commands.describe(emoji="Эмодзи-реакция", role="Роль проекта", name="Название проекта (по умолчанию — имя роли)")
    async def add(self, inter: discord.Interaction, emoji: str, role: discord.Role, name: str | None = None):
        await inter.response.defer(ephemeral=True)
        emoji = emoji.strip()
        if role >= inter.guild.me.top_role:
            await inter.followup.send(f"Роль {role.mention} выше моей — не смогу её выдавать. Подними роль бота в настройках сервера.", ephemeral=True)
            return
        await self.bot.db.project_set(inter.guild_id, emoji, role.id, name or role.name)
        msg = await self.refresh(inter.guild)
        await inter.followup.send(f"{emoji} → {role.mention}." + ("" if msg else " Карточка появится после `/setup projects #канал`."), ephemeral=True)

    @project.command(name="remove", description="Убрать реакцию/проект")
    async def remove(self, inter: discord.Interaction, emoji: str):
        await inter.response.defer(ephemeral=True)
        ok = await self.bot.db.project_del(inter.guild_id, emoji.strip())
        if ok:
            msg = await self.refresh(inter.guild)
            if msg:
                for r in msg.reactions:
                    if str(r.emoji) == emoji.strip():
                        try:
                            await r.clear()
                        except discord.HTTPException:
                            pass
        await inter.followup.send("Убрал." if ok else "Такой реакции нет в списке.", ephemeral=True)

    @project.command(name="list", description="Показать привязки")
    async def list_(self, inter: discord.Interaction):
        rows = await self.bot.db.project_roles(inter.guild_id)
        text = "\n".join(f"{r['emoji']} — {r['name']} · <@&{r['role_id']}>" for r in rows) or "Пусто."
        await inter.response.send_message(embed=card("Проекты", text), ephemeral=True)

    @project.command(name="refresh", description="Пересоздать/обновить карточку в канале")
    async def refresh_cmd(self, inter: discord.Interaction):
        await inter.response.defer(ephemeral=True)
        msg = await self.refresh(inter.guild)
        await inter.followup.send("Обновил." if msg else "Сначала `/setup projects #канал`.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Projects(bot))
