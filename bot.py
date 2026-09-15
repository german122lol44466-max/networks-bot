"""Network's Bot — точка входа."""
import os
import sys
import logging
import traceback
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from db import DB
from ui import card as _card, ORANGE, CYAN, RED

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN") or os.getenv("BOT_TOKEN") or os.getenv("TOKEN")
if not TOKEN:
    raise SystemExit("Нет токена. Дома: скопируй .env.example в .env и вставь DISCORD_TOKEN. На хостинге: задай переменную окружения DISCORD_TOKEN.")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

def card(*a, **k):
    k.setdefault("tag", "настройки")
    return _card(*a, **k)


log = logging.getLogger("networks")

intents = discord.Intents.default()
intents.members = True          # нужно, чтобы видеть, кто зашёл
intents.message_content = True  # нужно для счёта и серий
intents.invites = True

COLOR = ORANGE


class Tree(app_commands.CommandTree):
    """Слэш-команды: только на сервере; любая ошибка — понятный ответ, а не «приложение не отвечает»."""

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.guild is None:
            await inter.response.send_message("Команды бота работают только на сервере, не в личке.", ephemeral=True)
            return False
        return True

    async def on_error(self, inter: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CheckFailure):
            return
        orig = getattr(error, "original", error)
        if isinstance(orig, discord.Forbidden):
            text = "У бота не хватает прав для этого действия. Проверь права бота в канале и на сервере (`/setup test`)."
        else:
            text = f"Ошибка в команде: `{type(orig).__name__}: {str(orig)[:150]}`. Подробности — в логах бота."
        log.error("Ошибка команды /%s: %s", inter.command.qualified_name if inter.command else "?", orig, exc_info=orig)
        try:
            if inter.response.is_done():
                await inter.followup.send(text, ephemeral=True)
            else:
                await inter.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass


class NetworksBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents, help_command=None, tree_cls=Tree)
        self.db = DB()

    async def on_error(self, event: str, *args, **kwargs):
        """Ошибка в любом обработчике событий — в лог, бот продолжает работать."""
        log.error("Ошибка в событии %s:\n%s", event, traceback.format_exc())

    async def setup_hook(self):
        await self.db.open()
        for ext in ("cog_invites", "cog_counting", "cog_streaks", "cog_words", "cog_rep", "cog_welcome", "cog_snake", "cog_projects", "cog_profile"):
            await self.load_extension(ext)

    async def sync_guild(self, guild: discord.Guild):
        """Регистрируем команды прямо на сервере — появляются сразу, без часового ожидания."""
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        log.info("Команды обновлены на сервере %s", guild.name)

    async def close(self):
        await self.db.close()
        await super().close()


bot = NetworksBot()


@bot.event
async def on_ready():
    log.info("Вошёл как %s (%s), серверов: %d", bot.user, bot.user.id, len(bot.guilds))
    try:
        await bot.change_presence(activity=discord.Game("Network"))
    except Exception:
        pass
    if not getattr(bot, "_synced", False):
        bot._synced = True
        for g in bot.guilds:
            try:
                await bot.sync_guild(g)
            except discord.Forbidden:
                log.error("Не могу зарегистрировать команды на %s: бот приглашён без scope applications.commands. "
                          "Перепригласи по ссылке из README.", g.name)
            except discord.HTTPException as e:
                log.error("Не смог обновить команды на %s: %s", g.name, e)
        # старые глобальные команды убираем, иначе в меню будут дубли
        try:
            await bot.http.bulk_upsert_global_commands(bot.application_id, [])
        except discord.HTTPException as e:
            log.warning("Не смог очистить глобальные команды: %s", e)
    # проверка прав по всем настроенным каналам — сразу видно в логах, что не будет работать
    for g in bot.guilds:
        try:
            await check_permissions(g)
        except Exception:
            log.exception("Проверка прав на %s не удалась", g.name)


async def check_permissions(guild: discord.Guild):
    c = await bot.db.get_config(guild.id)
    me = guild.me
    if me is None:
        return
    if not me.guild_permissions.manage_guild and c.get("invite_channel"):
        log.warning("[%s] нет права «Управлять сервером» — приглашения не отслеживаются", guild.name)
    for label, key in (("Счёт", "count_channel"), ("Серии", "streak_channel"), ("Слова", "words_channel"),
                       ("Приглашения", "invite_channel"), ("Репутация", "rep_channel"),
                       ("Приветствия", "welcome_channel"), ("Проекты", "projects_channel")):
        cid = c.get(key)
        if not cid:
            continue
        ch = guild.get_channel(cid)
        if ch is None:
            log.warning("[%s] %s: канал %s не найден (удалён?) — задай заново через /setup", guild.name, label, cid)
            continue
        p = ch.permissions_for(me)
        missing = [name for ok, name in ((p.view_channel, "Просматривать канал"), (p.send_messages, "Отправлять сообщения"),
                                          (p.read_message_history, "Читать историю"), (p.add_reactions, "Добавлять реакции"),
                                          (p.embed_links, "Встраивать ссылки")) if not ok]
        if key == "count_channel" and not p.manage_messages:
            missing.append("Управлять сообщениями (удалять болтовню)")
        if missing:
            log.warning("[%s] %s в #%s: не хватает прав — %s", guild.name, label, ch.name, ", ".join(missing))


@bot.event
async def on_disconnect():
    log.warning("Связь с Discord потеряна — переподключаюсь")


@bot.event
async def on_resumed():
    log.info("Связь с Discord восстановлена")


@bot.event
async def on_guild_join(guild: discord.Guild):
    try:
        await bot.sync_guild(guild)
    except discord.HTTPException as e:
        log.error("Не смог зарегистрировать команды на новом сервере %s: %s", guild.name, e)


async def announce(inter: discord.Interaction, channel: discord.TextChannel, ok_text: str, embed: discord.Embed):
    """Ответить админу и попробовать написать в канал; если нет прав — сказать об этом."""
    p = channel.permissions_for(inter.guild.me)
    lacks = [n for ok, n in ((p.view_channel, "Просматривать канал"), (p.send_messages, "Отправлять сообщения"),
                             (p.read_message_history, "Читать историю сообщений"), (p.add_reactions, "Добавлять реакции"),
                             (p.embed_links, "Встраивать ссылки")) if not ok]
    if lacks:
        ok_text += f"\n⚠️ В {channel.mention} боту не хватает прав: {', '.join(lacks)}. Без них модуль будет работать частично."
    try:
        await channel.send(embed=embed)
        await inter.followup.send(ok_text, ephemeral=True)
    except discord.Forbidden:
        await inter.followup.send(f"{ok_text}\nНо писать в {channel.mention} мне запрещено — дай боту права «Просматривать канал» и «Отправлять сообщения» в настройках этого канала.", ephemeral=True)


# ---------- /setup ----------
setup = app_commands.Group(name="setup", description="Настройка бота (только для администраторов)",
                           default_permissions=discord.Permissions(administrator=True))


@setup.command(name="invites", description="Канал, куда бот пишет о приглашениях и очках")
async def setup_invites(inter: discord.Interaction, channel: discord.TextChannel):
    await inter.response.defer(ephemeral=True)
    await bot.db.set_config(inter.guild_id, "invite_channel", channel.id)
    cog = bot.get_cog("Invites")
    if cog:
        await cog.refresh(inter.guild)
    perms = inter.guild.me.guild_permissions
    e = card("Очки за приглашения включены", "Создай своё приглашение на сервер и кидай друзьям. "
                                  "Каждый, кто зайдёт по твоей ссылке, приносит тебе **+1** очко.\n"
                                  "Свои очки: `/points`, таблица: `/top`.")

    if not perms.manage_guild:
        e.add_field(name="Внимание", value="У бота нет права «Управлять сервером» — без него я не вижу, кто по чьей ссылке зашёл.", inline=False)
    await announce(inter, channel, f"Приглашения будут в {channel.mention}.", e)


@setup.command(name="counting", description="Канал для счёта от 1 до 999")
async def setup_counting(inter: discord.Interaction, channel: discord.TextChannel):
    await inter.response.defer(ephemeral=True)
    await bot.db.set_config(inter.guild_id, "count_channel", channel.id)
    e = card("Счёт открыт", "Пишите числа по порядку: 1, 2, 3 … до 999.\nОшибся или написал два раза подряд — всё заново.")
    await announce(inter, channel, f"Счёт идёт в {channel.mention}.", e)


@setup.command(name="streak", description="Канал для «Жду сервер день N»")
async def setup_streak(inter: discord.Interaction, channel: discord.TextChannel):
    await inter.response.defer(ephemeral=True)
    await bot.db.set_config(inter.guild_id, "streak_channel", channel.id)
    e = card("Серия ожидания открыта", "Каждый день пиши сюда `Жду сервер день N` — я считаю дни.\n"
                                  "Один пост в сутки. Пропустил день — серия сгорает и начинается с 1.\n"
                                  "Своя серия: `/streak`, таблица: `/topstreak`.")
    await announce(inter, channel, f"Серии считаются в {channel.mention}.", e)


@setup.command(name="words", description="Канал для игры «Слова»")
async def setup_words(inter: discord.Interaction, channel: discord.TextChannel):
    await inter.response.defer(ephemeral=True)
    await bot.db.set_config(inter.guild_id, "words_channel", channel.id)
    await bot.db.reset_words(inter.guild_id)
    e = card("Игра «Слова»", "Пиши слово на последнюю букву предыдущего. Буквы ь, ъ, ы, й не считаются — берём предыдущую.\n"
                                  "Повторять слова нельзя, два слова подряд от одного человека — тоже.\n"
                                  "Только существительные из словаря, в единственном числе. Первое слово — любое. Статистика: `/words`.")
    await announce(inter, channel, f"Игра «Слова» идёт в {channel.mention}.", e)


@setup.command(name="rep", description="Канал, куда дублировать все +rep / -rep (необязательно)")
async def setup_rep(inter: discord.Interaction, channel: discord.TextChannel):
    await inter.response.defer(ephemeral=True)
    await bot.db.set_config(inter.guild_id, "rep_channel", channel.id)
    e = card("Репутация", "Здесь появляются все оценки игроков.\n"
             "Поставить: `/repdplayer` (выбор из списка) или `/repsplayer` (по нику). Посмотреть: `/rep`, топ: `/reptop`.")
    await announce(inter, channel, f"Репутация дублируется в {channel.mention}.", e)


@setup.command(name="welcome", description="Канал для приветствий новых участников")
async def setup_welcome(inter: discord.Interaction, channel: discord.TextChannel):
    await inter.response.defer(ephemeral=True)
    await bot.db.set_config(inter.guild_id, "welcome_channel", channel.id)
    e = card("Приветствия включены", "Каждый новый участник получит карточку «ДОБРО ПОЖАЛОВАТЬ НА NETWORK» здесь. "
             "Свой текст: `/setup welcometext`. Проверить: `/setup welcometest`.")
    await announce(inter, channel, f"Приветствия будут в {channel.mention}.", e)


@setup.command(name="welcometext", description="Свой текст приветствия ({user} — упоминание, {server} — имя сервера). Пусто — тематические фразы")
async def setup_welcometext(inter: discord.Interaction, text: str | None = None):
    await bot.db.set_config(inter.guild_id, "welcome_text", text.strip() if text else None)
    await inter.response.send_message("Текст приветствия сохранён." if text else "Вернул тематические фразы.", ephemeral=True)


@setup.command(name="welcometest", description="Показать, как будет выглядеть приветствие (на тебе)")
async def setup_welcometest(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True)
    cog = bot.get_cog("Welcome")
    c = await bot.db.get_config(inter.guild_id)
    if not c.get("welcome_channel"):
        await inter.followup.send("Сначала `/setup welcome #канал`.", ephemeral=True); return
    await cog.on_member_join(inter.user)
    await inter.followup.send("Отправил пробное приветствие.", ephemeral=True)


@setup.command(name="projects", description="Канал с карточкой «Любимый проект» (реакция = роль)")
async def setup_projects(inter: discord.Interaction, channel: discord.TextChannel):
    await inter.response.defer(ephemeral=True)
    await bot.db.set_config(inter.guild_id, "projects_channel", channel.id)
    await bot.db.set_config(inter.guild_id, "projects_msg", None)
    cog = bot.get_cog("Projects")
    try:
        await cog.refresh(inter.guild)
        await inter.followup.send(f"Карточка «Любимый проект» в {channel.mention}. Добавляй проекты: `/project add эмодзи @роль`.", ephemeral=True)
    except discord.Forbidden:
        await inter.followup.send(f"Не могу писать в {channel.mention} — дай боту права там.", ephemeral=True)


@setup.command(name="timezone", description="Часовой пояс для смены дня (например Europe/Moscow)")
async def setup_tz(inter: discord.Interaction, timezone: str):
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        await inter.response.send_message("Не знаю такой пояс. Примеры: Europe/Moscow, Europe/Amsterdam, Asia/Almaty.", ephemeral=True)
        return
    await bot.db.set_config(inter.guild_id, "timezone", timezone)
    await inter.response.send_message(f"Часовой пояс: **{timezone}**.", ephemeral=True)


@setup.command(name="show", description="Показать текущие настройки")
async def setup_show(inter: discord.Interaction):
    c = await bot.db.get_config(inter.guild_id)
    def ch(i): return f"<#{i}>" if i else "не задан"
    e = card("Настройки")
    e.add_field(name="Приглашения", value=ch(c["invite_channel"]), inline=False)
    e.add_field(name="Счёт", value=ch(c["count_channel"]), inline=False)
    e.add_field(name="Серии", value=ch(c["streak_channel"]), inline=False)
    e.add_field(name="Слова", value=ch(c["words_channel"]), inline=False)
    e.add_field(name="Репутация", value=ch(c.get("rep_channel")), inline=False)
    e.add_field(name="Приветствия", value=ch(c.get("welcome_channel")), inline=False)
    e.add_field(name="Проекты", value=ch(c.get("projects_channel")), inline=False)
    e.add_field(name="Часовой пояс", value=c["timezone"], inline=False)
    perms = inter.guild.me.guild_permissions
    e.add_field(name="Права бота", value=f"Управлять сервером: {'есть' if perms.manage_guild else 'НЕТ'} · "
                f"Управлять ролями: {'есть' if perms.manage_roles else 'НЕТ'} · Управлять сообщениями: {'есть' if perms.manage_messages else 'НЕТ'}", inline=False)
    await inter.response.send_message(embed=e, ephemeral=True)


@setup.command(name="test", description="Отправить пробную карточку во все настроенные каналы")
async def setup_test(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True)
    c = await bot.db.get_config(inter.guild_id)
    report = []
    for label, key in (("Приглашения", "invite_channel"), ("Счёт", "count_channel"), ("Серии", "streak_channel"), ("Слова", "words_channel"), ("Репутация", "rep_channel"), ("Приветствия", "welcome_channel")):
        cid = c[key]
        if not cid:
            report.append(f"{label}: канал не задан")
            continue
        ch = inter.guild.get_channel(cid)
        if ch is None:
            report.append(f"{label}: канал <#{cid}> не найден (удалён?)")
            continue
        p = ch.permissions_for(inter.guild.me)
        lacks = [n for ok, n in ((p.read_message_history, "читать историю"), (p.add_reactions, "реакции"), (p.embed_links, "встраивать ссылки")) if not ok]
        try:
            await ch.send(embed=card("Проверка связи", f"Модуль «{label}» привязан к этому каналу и может сюда писать.", color=CYAN, user=inter.user))
            report.append(f"{label}: {ch.mention} — ок" + (f" (но нет прав: {', '.join(lacks)})" if lacks else ""))
        except discord.Forbidden:
            report.append(f"{label}: {ch.mention} — нет прав писать. Дай боту «Просматривать канал» и «Отправлять сообщения» в правах канала.")
    perms = inter.guild.me.guild_permissions
    report.append(f"Право «Управлять сервером» (нужно для приглашений): {'есть' if perms.manage_guild else 'НЕТ'}")
    await inter.followup.send("\n".join(report), ephemeral=True)


bot.tree.add_command(setup)

if __name__ == "__main__":
    try:
        bot.run(TOKEN, log_handler=None)
    except discord.PrivilegedIntentsRequired:
        log.critical("Discord не пускает: в панели разработчика не включены интенты. "
                     "Открой https://discord.com/developers/applications → твой бот → Bot → включи "
                     "PRESENCE INTENT, SERVER MEMBERS INTENT и MESSAGE CONTENT INTENT, потом запусти снова.")
        sys.exit(1)
    except discord.LoginFailure:
        log.critical("Discord не принял токен. Проверь DISCORD_TOKEN (Bot → Reset Token) — токен без пробелов и кавычек.")
        sys.exit(1)
