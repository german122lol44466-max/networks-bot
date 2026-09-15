"""Единый вид карточек бота."""
import discord

ORANGE = 0xFF6A00
CYAN = 0x2FF0FF
RED = 0xFF2A6D
YELLOW = 0xFCEE0A
GREY = 0x84878D
FOOTER = "Network's Bot"


ICONS = {
    "серии": "🔥", "счёт": "🔢", "приглашения": "📡", "слова": "🔤", "репутация": "⚖️",
    "приветствие": "👋", "змейка": "🐍", "проекты": "🎯", "настройки": "⚙️", "система": "🛰️", "профиль": "🪪",
}


def card(title: str | None = None, desc: str | None = None, *, color: int = ORANGE,
         user: discord.abc.User | None = None, footer: str | None = None, thumb: discord.abc.User | None = None,
         tag: str | None = None) -> discord.Embed:
    """Единая карточка. tag — модуль ("серии", "счёт" …): иконка в заголовке и подпись в футере."""
    if tag and title and not title[:1] in "🔥🔢📡🔤⚖️👋🐍🎯⚙️🛰️🪪✅❌💀":
        title = f"{ICONS.get(tag, '')} {title}".strip()
    e = discord.Embed(title=title, description=desc, color=color)
    if tag:
        footer = f"{tag.upper()}" + (f"  ·  {footer}" if footer else "")
    if user:
        e.set_author(name=user.display_name, icon_url=user.display_avatar.url)
    if thumb:
        e.set_thumbnail(url=thumb.display_avatar.url)
    e.set_footer(text=f"{FOOTER}  ·  {footer}" if footer else FOOTER)
    e.timestamp = discord.utils.utcnow()
    return e


MEDALS = ("🥇", "🥈", "🥉")


def place(i: int) -> str:
    """Номер места в топе: медали за первые три, дальше `04.`"""
    return MEDALS[i] if i < 3 else f"`{i + 1:>2}.`"


def top_lines(rows, fmt) -> str:
    """Список топа: fmt(row) -> текст строки без номера."""
    return "\n".join(f"{place(i)} {fmt(r)}" for i, r in enumerate(rows)) or "—"


def stat(label: str, value) -> str:
    """Строка «▸ Метка · **значение**» для аккуратных списков внутри поля."""
    return f"▸ {label} · **{value}**"


def sep() -> str:
    """Тонкая линия-разделитель между блоками."""
    return "─" * 22


def field(e: discord.Embed, name: str, value: str, inline: bool = True):
    """Поле с защитой от пустых значений и лимита Discord (1024 символа)."""
    value = (value or "—")
    if len(value) > 1024:
        value = value[:1000].rstrip() + "…"
    e.add_field(name=name, value=value, inline=inline)
    return e


def bar(value: int, total: int, width: int = 12) -> str:
    """Полоска прогресса из блоков: ▰▰▰▱▱"""
    total = max(total, 1)
    filled = min(width, round(value / total * width))
    return "▰" * filled + "▱" * (width - filled)


def days(n: int) -> str:
    n = abs(n) % 100
    if 11 <= n <= 19:
        return "дней"
    n %= 10
    if n == 1:
        return "день"
    if 2 <= n <= 4:
        return "дня"
    return "дней"


# ---------- безопасные обёртки: Discord может запретить/потерять — бот не должен падать ----------
import logging as _logging
_log = _logging.getLogger("networks.ui")


async def react(msg: discord.Message, emoji: str) -> bool:
    try:
        await msg.add_reaction(emoji)
        return True
    except discord.HTTPException as e:
        _log.warning("Не смог поставить реакцию %s в #%s: %s", emoji, getattr(msg.channel, "name", "?"), e)
        return False


async def reply(msg: discord.Message, content: str | None = None, **kw) -> discord.Message | None:
    """Ответ на сообщение. Если исходное уже удалено — пишем просто в канал."""
    kw.setdefault("mention_author", False)
    try:
        return await msg.reply(content, **kw)
    except discord.NotFound:
        kw.pop("mention_author", None)
        return await send(msg.channel, content, **kw)
    except discord.HTTPException as e:
        _log.warning("Не смог ответить в #%s: %s", getattr(msg.channel, "name", "?"), e)
        return None


async def send(channel, content: str | None = None, **kw) -> discord.Message | None:
    try:
        return await channel.send(content, **kw)
    except discord.HTTPException as e:
        _log.warning("Не смог написать в #%s: %s", getattr(channel, "name", "?"), e)
        return None


async def delete(msg: discord.Message) -> bool:
    try:
        await msg.delete()
        return True
    except discord.HTTPException as e:
        _log.warning("Не смог удалить сообщение в #%s: %s", getattr(msg.channel, "name", "?"), e)
        return False
