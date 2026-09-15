"""Змейка на кнопках. Каждое нажатие — шаг. ⚡ на поле — бустер: заряд, по кнопке ⚡ змейка делает рывок на 3 клетки.
По таймеру игра просто заканчивается, очки записываются. /snakeduel @игрок — дуэль на одном поле: кто первым съест
DUEL_GOAL яблок (или больше к концу времени) — победил; врезался — проиграл."""
import asyncio
import random
import time
import logging
import discord
from discord import app_commands
from discord.ext import commands
from ui import card as _card, top_lines, CYAN, ORANGE, RED, GREY, YELLOW

log = logging.getLogger("networks.snake")


def card(*a, **k):
    k.setdefault("tag", "змейка")
    return _card(*a, **k)


W, H = 9, 9
DW, DH = 11, 9                       # поле дуэли
EMPTY, APPLE, BOOST = "⬛", "🍎", "⚡"
SKINS = {0: ("🟢", "🟩"), 1: ("🔵", "🟦")}
GAME_TIME = 180        # 3 минуты на игру
BONUS_EVERY = 5        # каждые 5 яблок…
BONUS_TIME = 30        # …+30 секунд
BOOST_STEPS = 3        # рывок по кнопке ⚡
BOOST_CHANCE = 0.25    # шанс, что после яблока на поле появится ⚡
DUEL_GOAL = 5          # яблок для победы в дуэли
DUEL_TIME = 180
DIRS = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}
OPPOSITE = {"up": "down", "down": "up", "left": "right", "right": "left"}


class Snake:
    def __init__(self, cells, d):
        self.body = list(cells)
        self.dir = d
        self.alive = True
        self.score = 0
        self.charges = 0     # заряды бустера

    @property
    def head(self):
        return self.body[0]


class Board:
    """Общая логика поля: одна или две змейки, яблоко, бустер."""

    def __init__(self, w, h, snakes: list[Snake]):
        self.w, self.h = w, h
        self.snakes = snakes
        self.apple = None
        self.boost = None
        self.apple = self._spawn()

    def _occupied(self):
        s = set()
        for sn in self.snakes:
            s.update(sn.body)
        return s

    def _spawn(self):
        occ = self._occupied() | {self.apple, self.boost}
        free = [(x, y) for x in range(self.w) for y in range(self.h) if (x, y) not in occ]
        return random.choice(free) if free else None

    def step(self, sn: Snake, d: str | None) -> dict:
        """Один шаг. Возвращает события: {'apple': bool, 'boost': bool, 'bonus': bool, 'died': bool}."""
        ev = {"apple": False, "boost": False, "bonus": False, "died": False}
        if not sn.alive:
            return ev
        if d and d != OPPOSITE[sn.dir]:
            sn.dir = d
        dx, dy = DIRS[sn.dir]
        hx, hy = sn.head
        nx, ny = hx + dx, hy + dy
        blocked = set()
        for other in self.snakes:
            if not other.alive:
                continue
            blocked.update(other.body[:-1] if other is sn else other.body)   # свой хвост уйдёт, чужой — нет
        if not (0 <= nx < self.w and 0 <= ny < self.h) or (nx, ny) in blocked:
            sn.alive = False
            ev["died"] = True
            return ev
        sn.body.insert(0, (nx, ny))
        if (nx, ny) == self.apple:
            sn.score += 1
            ev["apple"] = True
            ev["bonus"] = sn.score % BONUS_EVERY == 0
            self.apple = self._spawn()
            if self.boost is None and random.random() < BOOST_CHANCE:
                self.boost = self._spawn()
            return ev
        if (nx, ny) == self.boost:
            sn.charges += 1
            ev["boost"] = True
            self.boost = None
        sn.body.pop()
        return ev

    def dash(self, sn: Snake) -> list[dict]:
        """Рывок: до BOOST_STEPS шагов прямо. Останавливается, если умерла."""
        sn.charges -= 1
        evs = []
        for _ in range(BOOST_STEPS):
            ev = self.step(sn, None)
            evs.append(ev)
            if ev["died"]:
                break
        return evs

    def render(self) -> str:
        cells = {}
        for i, sn in enumerate(self.snakes):
            head, body = SKINS[i]
            for p in sn.body[1:]:
                cells[p] = body
            cells[sn.head] = head if sn.alive else "💀"
        if self.apple:
            cells.setdefault(self.apple, APPLE)
        if self.boost:
            cells.setdefault(self.boost, BOOST)
        return "\n".join("".join(cells.get((x, y), EMPTY) for x in range(self.w)) for y in range(self.h))


async def safe_edit(inter: discord.Interaction | None, message: discord.Message | None, **kw):
    """Обновить сообщение через интеракцию, а если она протухла — напрямую."""
    try:
        if inter and not inter.response.is_done():
            await inter.response.edit_message(**kw)
            return
    except discord.HTTPException:
        pass
    if message:
        try:
            await message.edit(**kw)
        except discord.HTTPException as e:
            log.warning("Змейка: не смог обновить сообщение: %s", e)


# ==================== одиночная игра ====================
class SnakeView(discord.ui.View):
    def __init__(self, cog, owner: discord.Member):
        super().__init__(timeout=None)
        self.cog, self.owner = cog, owner
        cx, cy = W // 2, H // 2
        self.snake = Snake([(cx, cy), (cx - 1, cy), (cx - 2, cy)], "right")
        self.board = Board(W, H, [self.snake])
        self.message: discord.Message | None = None
        self.deadline = time.time() + GAME_TIME
        self.finished = False
        self.lock = asyncio.Lock()
        self.timer = asyncio.create_task(self._watch())

    async def _watch(self):
        try:
            while not self.finished:
                await asyncio.sleep(max(0.5, self.deadline - time.time()))
                if not self.finished and time.time() >= self.deadline:
                    async with self.lock:
                        await self.finish(None, "время вышло")
                    return
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("Змейка: таймер упал")

    def embed(self, note: str | None = None) -> discord.Embed:
        sn = self.snake
        color = ORANGE if sn.alive and not self.finished else RED
        e = card("Змейка", self.board.render(), color=color, user=self.owner, footer=f"длина {len(sn.body)}")
        e.add_field(name="Яблоки", value=f"**{sn.score}** 🍎", inline=True)
        e.add_field(name="Бустеры", value=f"**{sn.charges}** ⚡", inline=True)
        if sn.alive and not self.finished:
            e.add_field(name="Время", value=f"<t:{int(self.deadline)}:R>", inline=True)
        if note:
            e.add_field(name="\u200b", value=note, inline=False)
        return e

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.owner.id:
            await inter.response.send_message("Это не твоя змейка — запусти свою: `/snake`.", ephemeral=True)
            return False
        return True

    async def _play(self, inter: discord.Interaction, d: str | None, dash: bool = False):
        async with self.lock:
            if self.finished:
                try:
                    await inter.response.defer()
                except discord.HTTPException:
                    pass
                return
            if time.time() >= self.deadline:
                await self.finish(inter, "время вышло")
                return
            note = None
            if dash:
                if self.snake.charges <= 0:
                    await inter.response.send_message("Нет зарядов — собери ⚡ на поле.", ephemeral=True)
                    return
                evs = self.board.dash(self.snake)
                got = sum(ev["apple"] for ev in evs)
                bonus = any(ev["bonus"] for ev in evs)
                note = f"⚡ рывок на {len(evs)}" + (f", +{got} 🍎" if got else "")
            else:
                ev = self.board.step(self.snake, d)
                bonus = ev["bonus"]
                if ev["boost"]:
                    note = "⚡ Заряд бустера! Кнопка ⚡ — рывок на 3 клетки."
            if not self.snake.alive:
                await self.finish(inter, "врезалась")
                return
            if bonus:
                self.deadline += BONUS_TIME
                note = f"🍎 ×{self.snake.score}: **+{BONUS_TIME} сек**"
            await safe_edit(inter, self.message, embed=self.embed(note), view=self)

    async def finish(self, inter: discord.Interaction | None, why: str):
        if self.finished:
            return
        self.finished = True
        if not self.timer.done():
            self.timer.cancel()
        for item in self.children:
            item.disabled = True
        try:
            best, new_best = await self.cog.bot.db.snake_result(self.owner.guild.id, self.owner.id, self.snake.score)
            saved = "**Новый рекорд!**" if new_best else f"Рекорд: **{best}**"
        except Exception:
            log.exception("Змейка: не смог сохранить результат")
            saved = "(результат не сохранился — ошибка базы)"
        note = f"Игра окончена: {why}. Очки записаны: **{self.snake.score}** 🍎. {saved}"
        await safe_edit(inter, self.message, embed=self.embed(note), view=self)
        self.cog.active.pop(self.owner.id, None)
        self.stop()

    @discord.ui.button(label="\u200b", emoji="⬆️", style=discord.ButtonStyle.secondary, row=0)
    async def up(self, inter, _): await self._play(inter, "up")

    @discord.ui.button(label="\u200b", emoji="⚡", style=discord.ButtonStyle.success, row=0)
    async def boost(self, inter, _): await self._play(inter, None, dash=True)

    @discord.ui.button(label="\u200b", emoji="⬅️", style=discord.ButtonStyle.secondary, row=1)
    async def left(self, inter, _): await self._play(inter, "left")

    @discord.ui.button(label="\u200b", emoji="▶️", style=discord.ButtonStyle.primary, row=1)
    async def forward(self, inter, _): await self._play(inter, None)

    @discord.ui.button(label="\u200b", emoji="➡️", style=discord.ButtonStyle.secondary, row=1)
    async def right(self, inter, _): await self._play(inter, "right")

    @discord.ui.button(label="\u200b", emoji="⬇️", style=discord.ButtonStyle.secondary, row=2)
    async def down(self, inter, _): await self._play(inter, "down")

    @discord.ui.button(label="Сдаться", style=discord.ButtonStyle.danger, row=2)
    async def quit(self, inter, _):
        async with self.lock:
            await self.finish(inter, "сдалась")


# ==================== дуэль ====================
class DuelView(discord.ui.View):
    """Две змейки на одном поле. Зелёные кнопки — первый игрок, синие — второй."""

    def __init__(self, cog, p1: discord.Member, p2: discord.Member):
        super().__init__(timeout=None)
        self.cog, self.players = cog, [p1, p2]
        cy = DH // 2
        s1 = Snake([(2, cy), (1, cy), (0, cy)], "right")
        s2 = Snake([(DW - 3, cy), (DW - 2, cy), (DW - 1, cy)], "left")
        self.snakes = [s1, s2]
        self.board = Board(DW, DH, self.snakes)
        self.message: discord.Message | None = None
        self.deadline = time.time() + DUEL_TIME
        self.finished = False
        self.lock = asyncio.Lock()
        self.timer = asyncio.create_task(self._watch())

    async def _watch(self):
        try:
            while not self.finished:
                await asyncio.sleep(max(0.5, self.deadline - time.time()))
                if not self.finished and time.time() >= self.deadline:
                    async with self.lock:
                        await self.finish(None, "время вышло")
                    return
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("Дуэль: таймер упал")

    def embed(self, note: str | None = None) -> discord.Embed:
        p1, p2 = self.players
        s1, s2 = self.snakes
        e = card("Дуэль", self.board.render(), color=YELLOW if not self.finished else GREY,
                 footer=f"до победы {DUEL_GOAL} 🍎")
        e.add_field(name=f"🟢 {p1.display_name}", value=f"**{s1.score}** 🍎 · ⚡ {s1.charges}" + ("" if s1.alive else " · 💀"), inline=True)
        e.add_field(name=f"🔵 {p2.display_name}", value=f"**{s2.score}** 🍎 · ⚡ {s2.charges}" + ("" if s2.alive else " · 💀"), inline=True)
        if not self.finished:
            e.add_field(name="Время", value=f"<t:{int(self.deadline)}:R>", inline=True)
        if note:
            e.add_field(name="\u200b", value=note, inline=False)
        return e

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id not in (self.players[0].id, self.players[1].id):
            await inter.response.send_message("Это чужая дуэль. Своя: `/snakeduel @игрок`.", ephemeral=True)
            return False
        return True

    async def _play(self, inter: discord.Interaction, idx: int, d: str | None, dash: bool = False):
        if inter.user.id != self.players[idx].id:
            await inter.response.send_message("Это кнопки соперника — твои другого цвета.", ephemeral=True)
            return
        async with self.lock:
            if self.finished:
                try:
                    await inter.response.defer()
                except discord.HTTPException:
                    pass
                return
            sn = self.snakes[idx]
            if not sn.alive:
                await inter.response.send_message("Твоя змейка уже выбыла.", ephemeral=True)
                return
            if time.time() >= self.deadline:
                await self.finish(inter, "время вышло")
                return
            note = None
            if dash:
                if sn.charges <= 0:
                    await inter.response.send_message("Нет зарядов — собери ⚡ на поле.", ephemeral=True)
                    return
                self.board.dash(sn)
                note = f"⚡ {self.players[idx].display_name} сделал рывок"
            else:
                ev = self.board.step(sn, d)
                if ev["boost"]:
                    note = f"⚡ {self.players[idx].display_name} взял бустер"
            if not sn.alive:
                await self.finish(inter, f"{self.players[idx].display_name} врезался")
                return
            if sn.score >= DUEL_GOAL:
                await self.finish(inter, f"{self.players[idx].display_name} съел {DUEL_GOAL} яблок первым")
                return
            await safe_edit(inter, self.message, embed=self.embed(note), view=self)

    def _winner(self) -> int | None:
        s1, s2 = self.snakes
        if s1.alive != s2.alive:
            return 0 if s1.alive else 1
        if s1.score != s2.score:
            return 0 if s1.score > s2.score else 1
        return None

    async def finish(self, inter: discord.Interaction | None, why: str):
        if self.finished:
            return
        self.finished = True
        if not self.timer.done():
            self.timer.cancel()
        for item in self.children:
            item.disabled = True
        w = self._winner()
        if w is None:
            result = "**Ничья.**"
        else:
            win, lose = self.players[w], self.players[1 - w]
            result = f"🏆 Победил **{win.display_name}**!"
            try:
                await self.cog.bot.db.duel_result(win.guild.id, win.id, lose.id)
            except Exception:
                log.exception("Дуэль: не смог сохранить результат")
        note = f"Дуэль окончена: {why}. {result}"
        e = self.embed(note)
        e.color = CYAN
        await safe_edit(inter, self.message, embed=e, view=self)
        for p in self.players:
            self.cog.active.pop(p.id, None)
        self.stop()

    # первый игрок — зелёные кнопки (ряд 0), второй — синие (ряд 1)
    @discord.ui.button(emoji="⬆️", style=discord.ButtonStyle.success, row=0)
    async def a_up(self, i, _): await self._play(i, 0, "up")
    @discord.ui.button(emoji="⬇️", style=discord.ButtonStyle.success, row=0)
    async def a_down(self, i, _): await self._play(i, 0, "down")
    @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.success, row=0)
    async def a_left(self, i, _): await self._play(i, 0, "left")
    @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.success, row=0)
    async def a_right(self, i, _): await self._play(i, 0, "right")
    @discord.ui.button(emoji="⚡", style=discord.ButtonStyle.success, row=0)
    async def a_boost(self, i, _): await self._play(i, 0, None, dash=True)

    @discord.ui.button(emoji="⬆️", style=discord.ButtonStyle.primary, row=1)
    async def b_up(self, i, _): await self._play(i, 1, "up")
    @discord.ui.button(emoji="⬇️", style=discord.ButtonStyle.primary, row=1)
    async def b_down(self, i, _): await self._play(i, 1, "down")
    @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.primary, row=1)
    async def b_left(self, i, _): await self._play(i, 1, "left")
    @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.primary, row=1)
    async def b_right(self, i, _): await self._play(i, 1, "right")
    @discord.ui.button(emoji="⚡", style=discord.ButtonStyle.primary, row=1)
    async def b_boost(self, i, _): await self._play(i, 1, None, dash=True)

    @discord.ui.button(label="Сдаться", style=discord.ButtonStyle.danger, row=2)
    async def quit(self, inter, _):
        async with self.lock:
            idx = 0 if inter.user.id == self.players[0].id else 1
            self.snakes[idx].alive = False
            await self.finish(inter, f"{self.players[idx].display_name} сдался")


class ChallengeView(discord.ui.View):
    """Вызов на дуэль: принять может только вызванный."""

    def __init__(self, cog, p1: discord.Member, p2: discord.Member):
        super().__init__(timeout=90)
        self.cog, self.p1, self.p2 = cog, p1, p2
        self.message: discord.Message | None = None
        self.done = False

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id == self.p2.id:
            return True
        if inter.user.id == self.p1.id:
            await inter.response.send_message("Ждём ответа соперника.", ephemeral=True)
        else:
            await inter.response.send_message("Вызов не тебе.", ephemeral=True)
        return False

    @discord.ui.button(label="Принять", emoji="⚔️", style=discord.ButtonStyle.success)
    async def accept(self, inter: discord.Interaction, _):
        if self.done:
            return
        self.done = True
        if self.p1.id in self.cog.active or self.p2.id in self.cog.active:
            await inter.response.edit_message(embed=card("Дуэль", "Кто-то из вас уже играет — попробуйте позже.", color=GREY), view=None)
            self.stop(); return
        view = DuelView(self.cog, self.p1, self.p2)
        self.cog.active[self.p1.id] = view
        self.cog.active[self.p2.id] = view
        note = (f"🟢 {self.p1.mention} — зелёные кнопки, 🔵 {self.p2.mention} — синие. "
                f"Первый, кто съест {DUEL_GOAL} 🍎, побеждает. Врезался в стену или в змейку — проиграл.")
        await safe_edit(inter, self.message, content=None, embed=view.embed(note), view=view)
        try:
            view.message = await inter.original_response()
        except discord.HTTPException:
            view.message = self.message
        self.stop()

    @discord.ui.button(label="Отказаться", style=discord.ButtonStyle.secondary)
    async def decline(self, inter: discord.Interaction, _):
        self.done = True
        await safe_edit(inter, self.message, content=None, embed=card("Дуэль", f"{self.p2.mention} отказался от дуэли с {self.p1.mention}.", color=GREY), view=None)
        self.stop()

    async def on_timeout(self):
        if self.done or not self.message:
            return
        try:
            await self.message.edit(embed=card("Дуэль", f"{self.p2.mention} не ответил на вызов {self.p1.mention}.", color=GREY), view=None)
        except discord.HTTPException:
            pass


class SnakeCog(commands.Cog, name="Snake"):
    def __init__(self, bot):
        self.bot = bot
        self.active: dict[int, discord.ui.View] = {}   # user_id -> идущая игра/дуэль

    @app_commands.command(name="snake", description="Змейка: 3 минуты, каждые 5 яблок +30 сек, ⚡ — рывок")
    async def snake(self, inter: discord.Interaction):
        if inter.user.id in self.active:
            await inter.response.send_message("У тебя уже идёт игра — доиграй или нажми «Сдаться».", ephemeral=True)
            return
        view = SnakeView(self, inter.user)
        self.active[inter.user.id] = view
        await inter.response.send_message(embed=view.embed(f"{GAME_TIME // 60} минуты на игру, каждые {BONUS_EVERY} яблок +{BONUS_TIME} сек. "
                                                           f"Собирай ⚡ — кнопка ⚡ делает рывок на {BOOST_STEPS} клетки."), view=view)
        try:
            view.message = await inter.original_response()
        except discord.HTTPException:
            pass

    @app_commands.command(name="snakeduel", description="Вызвать игрока на дуэль в змейке: кто первым съест 5 яблок")
    @app_commands.describe(player="Соперник")
    async def snakeduel(self, inter: discord.Interaction, player: discord.Member):
        if player.bot or player.id == inter.user.id:
            await inter.response.send_message("Нужен живой соперник, не ты сам и не бот.", ephemeral=True); return
        if inter.user.id in self.active or player.id in self.active:
            await inter.response.send_message("Кто-то из вас уже играет — дождитесь конца игры.", ephemeral=True); return
        view = ChallengeView(self, inter.user, player)
        e = card("Вызов на дуэль", f"⚔️ {inter.user.mention} вызывает {player.mention}!\n"
                 f"Одно поле, две змейки. Кто первым съест **{DUEL_GOAL}** 🍎 — победил. У соперника 90 секунд, чтобы принять.",
                 color=YELLOW, user=inter.user, thumb=player)
        await inter.response.send_message(content=player.mention, embed=e, view=view)
        try:
            view.message = await inter.original_response()
        except discord.HTTPException:
            pass

    @app_commands.command(name="snaketop", description="Таблица лидеров змейки и дуэлей")
    async def snaketop(self, inter: discord.Interaction):
        rows = await self.bot.db.snake_top(inter.guild_id, 10)
        duels = await self.bot.db.duel_top(inter.guild_id, 10)
        if not rows and not duels:
            await inter.response.send_message("Никто ещё не играл. `/snake`!"); return
        e = card("Лидеры змейки", None, color=CYAN)
        e.add_field(name="🍎 Одиночная", value=top_lines(rows, lambda r: f"<@{r['user_id']}> — **{r['best']}** 🍎 ({r['games']} игр)"), inline=False)
        e.add_field(name="⚔️ Дуэли", value=top_lines(duels, lambda r: f"<@{r['user_id']}> — **{r['wins']}** побед / {r['losses']} пораж."), inline=False)
        await inter.response.send_message(embed=e)


async def setup(bot):
    await bot.add_cog(SnakeCog(bot))
