"""Игра «Слова»: каждое следующее слово на последнюю букву предыдущего."""
import asyncio
import os
import re
import logging
import discord
from discord import app_commands
from discord.ext import commands
from ui import card as _card, react, reply, send, top_lines, ORANGE, CYAN, RED

COLOR = ORANGE
SKIP = set("ьъый")
WORD_RE = re.compile(r"^[а-яё]+(?:-[а-яё]+)*$")
CATCHUP_LIMIT = 300

def card(*a, **k):
    k.setdefault("tag", "слова")
    return _card(*a, **k)


log = logging.getLogger("networks.words")
DICT_PATH = os.path.join(os.path.dirname(__file__), "nouns.txt")
EXTRA_PATH = os.path.join(os.path.dirname(__file__), "modern_words.txt")


def load_dict() -> set[str]:
    try:
        with open(DICT_PATH, encoding="utf-8") as f:
            words = {w.strip().lower().replace("ё", "е") for w in f if w.strip()}
        try:
            with open(EXTRA_PATH, encoding="utf-8") as f:
                words |= {w.strip().lower().replace("ё", "е") for w in f if w.strip()}
        except FileNotFoundError:
            pass
        log.info("Словарь: %d слов", len(words))
        return words
    except FileNotFoundError:
        log.warning("Словарь nouns.txt не найден — принимаю любые слова")
        return set()


def next_letter(word: str) -> str:
    """Буква, с которой должно начинаться следующее слово."""
    for ch in reversed(word):
        if ch not in SKIP:
            return ch
    return word[-1]


def normalize(text: str) -> str | None:
    """Одно слово русскими буквами. Терпим знаки препинания, кавычки, жирный шрифт, эмодзи по краям."""
    w = (text or "").strip().lower().replace("ё", "е")
    w = re.sub(r"^[^а-я]+|[^а-я]+$", "", w)    # срезаем всё нерусское по краям («**кот**», «кот!!», «кот 🐱»)
    return w if len(w) >= 2 and WORD_RE.match(w) else None


class Words(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.locks: dict[int, asyncio.Lock] = {}
        self.caught_up: set[int] = set()
        self.last_seen: dict[int, int] = {}
        self.dict = load_dict()

    async def is_noun(self, guild_id: int, word: str) -> bool:
        custom = await self.bot.db.custom_word(guild_id, word)
        if custom is not None:
            return custom
        return not self.dict or word in self.dict

    def _lock(self, gid):
        return self.locks.setdefault(gid, asyncio.Lock())

    async def _reject(self, msg: discord.Message, why: str, need: str | None, quiet: bool = False):
        await react(msg, "❌")
        if quiet:
            return
        tail = f" Нужно слово на **{need.upper()}**." if need else ""
        await reply(msg, f"{why}{tail}", delete_after=12)

    # ---------- дочитывание после перезапуска ----------
    async def catch_up(self, guild: discord.Guild):
        if guild.id in self.caught_up:
            return
        self.caught_up.add(guild.id)
        cfg = await self.bot.db.get_config(guild.id)
        ch = guild.get_channel(cfg["words_channel"]) if cfg["words_channel"] else None
        st = await self.bot.db.get_words(guild.id)
        if not ch or not st["last_msg"]:
            return
        try:
            missed = [m async for m in ch.history(after=discord.Object(id=st["last_msg"]), limit=CATCHUP_LIMIT, oldest_first=True)]
        except discord.HTTPException as e:
            log.warning("Слова: не смог дочитать %s: %s (нужно право «Читать историю сообщений»)", ch.name, e)
            return
        missed = [m for m in missed if not m.author.bot]
        if not missed:
            return
        log.info("Слова: дочитываю %d пропущенных сообщений в %s", len(missed), ch.name)
        for m in missed:
            await self._process(m, quiet=True)

    @commands.Cog.listener()
    async def on_ready(self):
        # on_ready бывает и после переподключения — дочитываем заново, что пропустили за время обрыва
        for g in self.bot.guilds:
            async with self._lock(g.id):
                self.caught_up.discard(g.id)
                try:
                    await self.catch_up(g)
                except Exception:
                    log.exception("Слова: ошибка дочитывания на %s", g.name)

    @commands.Cog.listener()
    async def on_message(self, msg: discord.Message):
        if msg.author.bot or not msg.guild:
            return
        cfg = await self.bot.db.get_config(msg.guild.id)
        if msg.channel.id != cfg["words_channel"]:
            return
        async with self._lock(msg.guild.id):
            try:
                await self.catch_up(msg.guild)
                if msg.id <= self.last_seen.get(msg.guild.id, 0):
                    return
                await self._process(msg)
            except Exception:
                log.exception("Слова: ошибка при обработке сообщения %s", msg.id)

    async def _process(self, msg: discord.Message, quiet: bool = False):
        self.last_seen[msg.guild.id] = max(self.last_seen.get(msg.guild.id, 0), msg.id)
        st = await self.bot.db.get_words(msg.guild.id)
        need = next_letter(st["last_word"]) if st["last_word"] else None
        word = normalize(msg.content)

        if word is None:
            await self._reject(msg, "Это не похоже на слово — одно слово русскими буквами, без пробелов и цифр.", need, quiet)
            return
        if st["last_user"] == msg.author.id:
            await self._reject(msg, "Два слова подряд нельзя, дай сказать другим.", need, quiet)
            return
        if need and word[0] != need:
            await self._reject(msg, f"«{word}» начинается на **{word[0].upper()}**.", need, quiet)
            return
        if not await self.is_noun(msg.guild.id, word):
            await self._reject(msg, f"«{word}» нет в словаре существительных (именительный падеж, единственное число). "
                                    "Если слово точно есть — админ может добавить: `/addword`.", need, quiet)
            return
        if await self.bot.db.word_used(msg.guild.id, word):
            await self._reject(msg, f"«{word}» уже было.", need, quiet)
            return

        chain = await self.bot.db.push_word(msg.guild.id, word, msg.author.id, msg.id)
        nxt = next_letter(word)
        await react(msg, "✅")
        if chain % 25 == 0:
            e = card(f"Цепочка: {chain}", f"Следующее слово на **{nxt.upper()}**.", color=CYAN)
            await send(msg.channel, embed=e)

    @commands.Cog.listener()
    async def on_message_delete(self, msg: discord.Message):
        if not msg.guild:
            return
        cfg = await self.bot.db.get_config(msg.guild.id)
        if msg.channel.id != cfg["words_channel"]:
            return
        st = await self.bot.db.get_words(msg.guild.id)
        if st["last_msg"] == msg.id and st["last_word"]:
            e = card("Слово удалено", f"Было **{st['last_word']}**, следующее — на **{next_letter(st['last_word']).upper()}**.")
            note = await send(msg.channel, embed=e)
            if note:   # привязываем «последнее» к напоминанию, чтобы дочитывание после перезапуска не сбилось
                await self.bot.db.conn.execute("UPDATE words_state SET last_msg=? WHERE guild_id=?", (note.id, msg.guild.id))
                await self.bot.db.conn.commit()

    @app_commands.command(name="words", description="Игра «Слова»: текущее состояние и топ")
    async def words(self, inter: discord.Interaction):
        st = await self.bot.db.get_words(inter.guild_id)
        rows = await self.bot.db.top_words(inter.guild_id, 10)
        e = card("Слова", footer=f"рекорд {st['record']}")
        if st["last_word"]:
            e.description = f"Последнее: **{st['last_word']}** → следующее на **{next_letter(st['last_word']).upper()}**\nЦепочка: **{st['chain']}** · рекорд: **{st['record']}**"
        else:
            e.description = "Цепочка пуста — первое слово любое."
        if rows:
            e.add_field(name="Топ", value=top_lines(rows, lambda r: f"<@{r['user_id']}> — **{r['words']}** сл."), inline=False)
        await inter.response.send_message(embed=e)

    @app_commands.command(name="addword", description="Разрешить слово, которого нет в словаре")
    @app_commands.default_permissions(administrator=True)
    async def addword(self, inter: discord.Interaction, word: str):
        w = normalize(word)
        if not w:
            await inter.response.send_message("Это не похоже на слово.", ephemeral=True)
            return
        await self.bot.db.set_custom_word(inter.guild_id, w, True)
        await inter.response.send_message(f"«{w}» теперь принимается.", ephemeral=True)

    @app_commands.command(name="banword", description="Запретить слово в игре (даже если оно в словаре)")
    @app_commands.default_permissions(administrator=True)
    async def banword(self, inter: discord.Interaction, word: str):
        w = normalize(word)
        if not w:
            await inter.response.send_message("Это не похоже на слово.", ephemeral=True)
            return
        await self.bot.db.set_custom_word(inter.guild_id, w, False)
        await inter.response.send_message(f"«{w}» больше не принимается.", ephemeral=True)

    @app_commands.command(name="wordsreset", description="Начать игру «Слова» заново")
    @app_commands.default_permissions(administrator=True)
    async def wordsreset(self, inter: discord.Interaction):
        st = await self.bot.db.reset_words(inter.guild_id)
        await inter.response.send_message(f"Цепочка из {st['chain']} слов сброшена. Первое слово любое.")


async def setup(bot):
    await bot.add_cog(Words(bot))
