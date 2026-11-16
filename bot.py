"""
Marrow — Discord listener bot for Byron / the Horde.
Python 3.12, discord.py 2.7.1.

Model, Byron 14 Nov 2026:
  Marrow listens in Discord and tells Muse what is going on.
  Marrow NEVER sends anything on WAX. No WAX key on the host.
  When Byron says "Hey Marrow give Tony a reward NFT" or
  "give Cryptozeug some DHC", Marrow logs the request and
  replies that it passed it to Muse. Muse, on their own
  computer, sends the reward directly from horderewards
  (50 DHC, 5 WAX, Bot Hunter NFT #33), on chain, then reports back.

  Anyone can register their own WAX name for the
  WANT ON THE LIST drop:
    Hey Marrow my WAX is ylab4.wam
    Hey Marrow register me as ylab4.wam
  Marrow saves Discord ID -> WAX in discord-wallets.json
  and logs it, so Muse can add them to the AtomicHub
  drop whitelist.

  Marrow is gender-neutral, they/them. Bot name is Marrow,
  it answers to "Hey Marrow" / "Marrow", also "Hey Muse" / "Muse"
  for backwards compatibility.

What it does NOT do:
  - It never sends DHC / WAX / NFT itself. Ever.
  - It never rewards anyone except on Byron's command.
  - It never answers ordinary chat unless spoken to directly.
  - It never stores a Discord token, a WAX private key, or a seed.
    Token = env var DISCORD_BOT_TOKEN on the host.

Run on a host (FadeHost free):
  pip install -r requirements.txt
  DISCORD_BOT_TOKEN=<token> python3 bot.py
"""

import asyncio
import json
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord.ext import tasks

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
# FadeHost keeps /data across deploys, repo files do not. Wallets and
# the rewards log must live in /data on the host, or a redeploy
# would wipe every WAX name people registered in Discord.
DATA_DIR = Path("/data") if Path("/data").exists() else HERE
WALLETS_PATH = DATA_DIR / "discord-wallets.json"
LOG_PATH = DATA_DIR / "rewards-log.md"

def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default

CONFIG = load_json(CONFIG_PATH, {})
BYRON_DISCORD_ID = int(CONFIG.get("byron_discord_id") or 0)
ALLOWED_CHANNEL_IDS = set(CONFIG.get("allowed_channel_ids") or [])
LOG_CHANNEL_ID = int(CONFIG.get("log_channel_id") or 0)
MAX_DHC = float(CONFIG.get("max_dhc_per_reward", 100))
MAX_WAX = float(CONFIG.get("max_wax_per_reward", 5))
MAX_NFT = int(CONFIG.get("max_nft_per_reward", 1))
DAILY_DHC_CAP = float(CONFIG.get("daily_dhc_cap", 500))

# Marrow first, Muse kept for backwards compatibility.
PREFIX = r"(?:hey\s+marrow|marrow|hey\s+muse|muse)"

REWARD_RE = re.compile(
    rf"{PREFIX}\s+give\s+(?:<@!?(\d+)>|@?([A-Za-z0-9_]+))\s+"
    r"(?:an?\s+reward\s+nft|a\s+prize\s+nft|an?\s+nft|a\s+nft|(?P<amount>\d+(?:\.\d+)?)\s*(?P<token>dhc|wax))",
    re.IGNORECASE,
)
# "give Cryptozeug some DHC" with no amount
REWARD_SOME_RE = re.compile(
    rf"{PREFIX}\s+give\s+(?:<@!?(\d+)>|@?([A-Za-z0-9_]+))\s+some\s+(dhc|wax)",
    re.IGNORECASE,
)
SELF_REGISTER_RE = re.compile(
    rf"{PREFIX}\s+(?:my\s+wax\s+is|register(?:\s+me)?(?:\s+as)?)\s+([a-z1-5.]{{1,12}})",
    re.IGNORECASE,
)
BYRON_REGISTER_RE = re.compile(
    rf"{PREFIX}\s+register\s+<@!?(\d+)>\s+([a-z1-5.]{{1,12}})",
    re.IGNORECASE,
)
HELP_RE = re.compile(rf"{PREFIX}\s+(?:help|status)", re.IGNORECASE)
PUZZLE_START_RE = re.compile(rf"{PREFIX}\s+(?:start|new)\s+(?:a\s+)?puzzle", re.IGNORECASE)
PUZZLE_STOP_RE = re.compile(rf"{PREFIX}\s+stop\s+(?:the\s+)?puzzle", re.IGNORECASE)
PUZZLE_SHOW_RE = re.compile(rf"{PREFIX}\s+puzzle", re.IGNORECASE)

# Small puzzle game, Byron starts it, anyone can answer.
# One active puzzle per channel. First correct answer wins.
# Marrow never sends a reward itself, it just logs the winner
# to #bot-logs so Muse can see it. Rewards are Byron-only.
# Questions live in puzzles.json (100+ easy zombie ones).
# No question repeats until every question has been used once.
# If the channel goes quiet for 2 hours, Marrow asks a question
# on its own, and asks no more until someone answers it.
PUZZLES_PATH = HERE / "puzzles.json"
USED_PATH = DATA_DIR / "puzzle-used.json"
LAST_ACTIVITY_PATH = DATA_DIR / "last-activity.json"

def normalize_answer(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9 ]", "", text)
    return re.sub(r"\s+", " ", text).strip()

def load_puzzles():
    data = load_json(PUZZLES_PATH, [])
    out = []
    for item in data:
        q = (item.get("q") or "").strip()
        answers = {normalize_answer(a) for a in item.get("answers") or []}
        if q and answers:
            out.append({"q": q, "answers": answers})
    return out

PUZZLES = load_puzzles()

def used_questions() -> set:
    return set(load_json(USED_PATH, []))

def save_used(used: set) -> None:
    USED_PATH.parent.mkdir(parents=True, exist_ok=True)
    USED_PATH.write_text(json.dumps(sorted(used), indent=2) + "\n")

def next_puzzle():
    """Pick a random question that has not been used yet.
    When every question has been used, start the list over."""
    used = used_questions()
    fresh = [p for p in PUZZLES if p["q"] not in used]
    if not fresh:
        save_used(set())
        fresh = list(PUZZLES)
    puzzle = random.choice(fresh)
    used.add(puzzle["q"])
    save_used(used)
    return puzzle

_active_puzzles = {}
_pending_another = {}   # channel id -> time a win was announced

# Last message time per channel, as wall-clock seconds (time.time),
# saved to disk. The old version kept this only in memory on the
# monotonic clock, so every restart or reconnect reset the 2 hour
# quiet timer to zero and the auto-ask never fired.
def _load_last_activity():
    raw = load_json(LAST_ACTIVITY_PATH, {})
    out = {}
    for k, v in raw.items():
        try:
            if float(v) > 1_000_000_000:
                out[int(k)] = float(v)
        except (TypeError, ValueError):
            pass
    return out

def save_last_activity():
    LAST_ACTIVITY_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAST_ACTIVITY_PATH.write_text(json.dumps({str(k): v for k, v in _last_activity.items()}, indent=2) + "\n")

_last_activity = _load_last_activity()
IDLE_SECONDS = 2 * 60 * 60
PUZZLE_CHANNEL_ID = int((CONFIG.get("allowed_channel_ids") or [0])[0] or 0)

_last_chat_at = {}

CHAT_REPLIES = [
    (("clown", "monday"), "Monday 16 Nov, 1pm CST: Horde's Clown #1, free for 24 hours on WAX, 1 per wallet, for Tony and the founders first."),
    (("imcollectibles", "free drop", "generative"), "Same day, Monday 16 Nov: a free generative drop on IMCollectibles on XRP, free for holders of both Big Paws and Lost in Contemplation."),
    (("colour me", "color me", "contest"), "Colour Me contest: colour a page yourself, no AI, post it and drop the link in a Ko-fi comment. Ends 20 November. Prize is a small sticker / print pack."),
    (("ko-fi", "kofi", "coffee"), "Byron's Ko-fi is ko-fi.com/artistbyron, home base for Loot Crates, memberships and the Colour Me contest."),
    (("loot crate", "crate"), "Horde Loot Crates: last one of the year, orders until near end of November, shipping first two weeks of December. His merch team can do 50 a month."),
    (("want on the list", "whitelist", "on the list"), "Want on the list? Tell me your WAX name here, like 'Hey Marrow my WAX is yourname.wam', and I will write it down for Muse to add you to the drop whitelist."),
    (("who are you", "what are you", "your name"), "I am Marrow, Byron's little helper zombie. I listen here, pass reward requests to Muse, and take WAX names for the list. Byron is the artist, I am just the zombie in the corner."),
    (("help", "what can you do"), "Say 'Hey Marrow' and ask me about Monday, Colour Me, Ko-fi or Loot Crates. To get on the list, say 'Hey Marrow my WAX is yourname.wam'. Rewards are Byron-only."),
]

CHAT_FALLBACK = "I am still learning, so I only know Horde stuff, Monday, Colour Me, Ko-fi, Loot Crates. For anything else, ask Byron."

# Random replies for a plain "hey Marrow" / "Marrow what's up?" with no topic.
GREETINGS = [
    "Yes?",
    "What's up?",
    "Hey. I am here, lurking in the corner.",
    "Yes, I am here. What do you need?",
    "Boo. Just kidding, I am here.",
    "Hey hey. I am listening.",
    "I am here. Ask me about Monday, Colour Me, Ko-fi or Loot Crates.",
    "Yo. What is going on?",
    "Here. Still a zombie, still listening.",
    "Yes? Come for the art, stay for the Horde.",
]

GREETING_RE = re.compile(
    r"^(?:hey\s+marrow|marrow|hey\s+muse|muse)\s*"
    r"(?:,|!|\?)?\s*"
    r"(are you there|you there|what'?s up|whats up|hello|hi|hey|yo|sup|how are you|how'?s it going|how is it going|anything|yes)?"
    r"\s*[?!.]*$",
    re.IGNORECASE,
)

def log_line(line: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a") as f:
        f.write(f"- {ts} - {line}\n")

def wallets() -> dict:
    data = load_json(WALLETS_PATH, {})
    data.pop("_comment", None)
    return data

def save_wallets(data: dict) -> None:
    data["_comment"] = "Discord user ID -> WAX account. Filled in when a person gives Marrow their WAX name."
    WALLETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    WALLETS_PATH.write_text(json.dumps(data, indent=2) + "\n")

class Marrow(discord.Client):
    async def on_ready(self):
        print(f"Marrow logged in as {self.user}")
        print(f"Byron Discord ID set: {bool(BYRON_DISCORD_ID)}")
        print(f"Puzzle questions loaded: {len(PUZZLES)}")
        # Only set it if there is no saved value. A restart or a
        # reconnect must not restart the 2 hour quiet timer.
        _last_activity.setdefault(PUZZLE_CHANNEL_ID, time.time())
        save_last_activity()
        if PUZZLE_CHANNEL_ID and not self._idle_puzzle_check.is_running():
            self._idle_puzzle_check.start()

    @tasks.loop(minutes=5)
    async def _idle_puzzle_check(self):
        """If the puzzle channel has been quiet for 2 hours, ask a
        question on Marrow's own. Never ask a second one while the
        first is still unanswered, or while waiting on a
        'do you want another question' reply."""
        if not PUZZLE_CHANNEL_ID or not PUZZLES:
            return
        now = time.time()
        last = _last_activity.get(PUZZLE_CHANNEL_ID, now)
        if now - last < IDLE_SECONDS:
            return
        if PUZZLE_CHANNEL_ID in _active_puzzles:
            return
        pending_since = _pending_another.get(PUZZLE_CHANNEL_ID)
        if pending_since and now - pending_since < IDLE_SECONDS:
            return
        _pending_another.pop(PUZZLE_CHANNEL_ID, None)
        try:
            channel = self.get_channel(PUZZLE_CHANNEL_ID) or await self.fetch_channel(PUZZLE_CHANNEL_ID)
        except Exception as e:
            print(f"idle puzzle channel fetch failed: {e}")
            return
        puzzle = next_puzzle()
        _active_puzzles[PUZZLE_CHANNEL_ID] = puzzle
        await channel.send(
            f"Puzzle time! It has been quiet in here.\n{puzzle['q']}\n"
            "First correct answer wins. Just type your answer in this channel."
        )
        log_line(f"PUZZLE AUTO-ASK in #{channel.name}: {puzzle['q']}")
        await self.report(f"PUZZLE AUTO-ASK in #{channel.name}: {puzzle['q']}")

    @_idle_puzzle_check.before_loop
    async def _wait_ready(self):
        await self.wait_until_ready()


    async def report(self, line: str) -> None:
        """Post a short note to #bot-logs so Muse can read it later.
        No log channel set yet means no report, just the local file log."""
        if not LOG_CHANNEL_ID:
            return
        try:
            channel = self.get_channel(LOG_CHANNEL_ID) or await self.fetch_channel(LOG_CHANNEL_ID)
            await channel.send(f"Marrow log: {line}")
        except Exception as e:
            print(f"report to bot-logs failed: {e}")

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if ALLOWED_CHANNEL_IDS and message.channel.id not in ALLOWED_CHANNEL_IDS:
            return
        # Any message counts as activity, for the 2 hour quiet check.
        _last_activity[message.channel.id] = time.time()
        save_last_activity()
        text = message.content.strip()

        if HELP_RE.search(text):
            if message.author.id != BYRON_DISCORD_ID:
                return
            await message.reply(
                "Marrow, Horde helper.\n"
                "`Hey Marrow my WAX is yourname.wam` - get on the list, anyone can do this\n"
                "`Hey Marrow give @user 10 DHC` - Byron only, I pass it to Muse, I send nothing myself\n"
                "`Hey Marrow give @user a reward NFT` - Byron only, same, passed to Muse\n"
                "`Hey Marrow register @user waxaccount` - Byron only\n"
                "`Hey Marrow start puzzle` - Byron only, anyone can answer in the channel\n"
                "`Hey Marrow stop puzzle` - Byron only\n"
                "Only Byron can trigger rewards."
            )
            return

        m = PUZZLE_START_RE.search(text)
        if m:
            if message.author.id != BYRON_DISCORD_ID:
                await message.reply("Only Byron can start a puzzle.")
                return
            if message.channel.id in _active_puzzles:
                await message.reply("A puzzle is already running here. Say 'Hey Marrow stop puzzle' to end it first.")
                return
            _pending_another.pop(message.channel.id, None)
            puzzle = next_puzzle()
            _active_puzzles[message.channel.id] = puzzle
            await message.reply(
                f"Puzzle time!\n{puzzle['q']}\nFirst correct answer wins. Just type your answer in this channel, no need to say my name."
            )
            log_line(f"PUZZLE START by Byron in #{message.channel.name}: {puzzle['q']}")
            await self.report(f"PUZZLE START in #{message.channel.name}: {puzzle['q']}")
            return

        m = PUZZLE_STOP_RE.search(text)
        if m:
            if message.author.id != BYRON_DISCORD_ID:
                await message.reply("Only Byron can stop a puzzle.")
                return
            puzzle = _active_puzzles.pop(message.channel.id, None)
            if not puzzle:
                await message.reply("No puzzle is running here right now.")
                return
            await message.reply("Puzzle stopped. The answer was: " + sorted(puzzle["answers"])[0] + ".")
            log_line(f"PUZZLE STOP by Byron in #{message.channel.name}")
            return

        m = PUZZLE_SHOW_RE.search(text)
        if m:
            puzzle = _active_puzzles.get(message.channel.id)
            if puzzle:
                await message.reply(puzzle["q"] + "\nFirst correct answer wins.")
            else:
                await message.reply("No puzzle is running right now. Byron can start one with 'Hey Marrow start puzzle'.")
            return

        # "Do you want another question?" reply, after a win.
        if message.channel.id in _pending_another:
            ans = normalize_answer(text)
            if ans in {"yes", "yeah", "yep", "sure", "ok", "okay", "please", "yes please"}:
                _pending_another.pop(message.channel.id, None)
                puzzle = next_puzzle()
                _active_puzzles[message.channel.id] = puzzle
                await message.reply(
                    f"Puzzle time!\n{puzzle['q']}\nFirst correct answer wins. Just type your answer in this channel."
                )
                log_line(f"PUZZLE ANOTHER in #{message.channel.name}: {puzzle['q']}")
                return
            if ans in {"no", "nah", "nope", "not right now"}:
                _pending_another.pop(message.channel.id, None)
                await message.reply("No problem, I will ask again later.")
                return

        # Puzzle answers: anyone can answer, no need to say Marrow's name.
        puzzle = _active_puzzles.get(message.channel.id)
        if puzzle and normalize_answer(text) in puzzle["answers"]:
            _active_puzzles.pop(message.channel.id, None)
            _pending_another[message.channel.id] = time.time()
            winner = message.author.mention
            await message.reply(
                f"Yeah, you're right, {winner}! Do you want another question?"
            )
            log_line(f"PUZZLE WINNER {message.author} ({message.author.id}) in #{message.channel.name}: {puzzle['q']}")
            await self.report(f"PUZZLE WINNER {message.author} in #{message.channel.name}. Muse to check with Byron about a prize.")
            return

        m = BYRON_REGISTER_RE.search(text)
        if m:
            if message.author.id != BYRON_DISCORD_ID:
                await message.reply("Only Byron can register wallets for other people.")
                return
            did, wax = m.group(1), m.group(2)
            data = wallets()
            data[did] = wax
            save_wallets(data)
            await message.reply(f"Registered <@{did}> as WAX account `{wax}`. I will pass that to Muse for the whitelist.")
            log_line(f"REGISTER <@{did}> -> {wax} (by Byron)")
            await self.report(f"Byron registered <@{did}> as WAX `{wax}`.")
            return

        m = SELF_REGISTER_RE.search(text)
        if m:
            wax = m.group(1)
            did = str(message.author.id)
            data = wallets()
            data[did] = wax
            save_wallets(data)
            await message.reply(f"Got it, {message.author.mention}. I wrote down your WAX name `{wax}`, Muse will add you to the whitelist for the WANT ON THE LIST drop.")
            log_line(f"WHITELIST {message.author} ({did}) -> {wax}")
            await self.report(f"{message.author} registered WAX `{wax}` for the WANT ON THE LIST drop.")
            return

        m = REWARD_RE.search(text) or REWARD_SOME_RE.search(text)
        if m:
            if message.author.id != BYRON_DISCORD_ID:
                await message.reply("Nice try, only Byron can tell me to send rewards.")
                return
            target_id = m.group(1)
            target_name = m.group(2)
            target = f"<@{target_id}>" if target_id else f"@{target_name}"
            amount = None
            token = None
            is_nft = False
            try:
                amount = m.group("amount")
                token = (m.group("token") or "").upper()
            except IndexError:
                pass
            if not amount and not token:
                # "some DHC" / NFT form
                if "nft" in text.lower():
                    is_nft = True
                else:
                    sm = re.search(r"some\s+(dhc|wax)", text, re.IGNORECASE)
                    token = sm.group(1).upper() if sm else "DHC"
                    amount = "10"
            if is_nft or "nft" in text.lower():
                label = "a reward NFT"
                is_nft = True
            else:
                label = f"{amount} {token}"
                if token == "DHC" and float(amount) > MAX_DHC:
                    await message.reply(f"That is over the per-reward limit ({MAX_DHC:g} DHC). Byron, confirm a larger amount first.")
                    return
                if token == "WAX" and float(amount) > MAX_WAX:
                    await message.reply(f"That is over the per-reward limit ({MAX_WAX:g} WAX).")
                    return
            await message.reply(f"Passed to Muse: send {label} to {target}. I send nothing myself, Muse sends it from horderewards and will report back.")
            log_line(f"REWARD REQUEST by Byron: send {label} to {target} - Muse to send from horderewards")
            await self.report(f"REWARD REQUEST by Byron: send {label} to {target}. Muse to send from horderewards.")
            return

        # Listening / replying: only when spoken to directly.
        addressed = bool(getattr(message, "mentions", None) and self.user in message.mentions) or bool(
            re.match(r"(?:hey\s+marrow|marrow|hey\s+muse|muse)\b", text, re.IGNORECASE)
        )
        if not addressed:
            return
        now = asyncio.get_event_loop().time()
        last = _last_chat_at.get(message.channel.id, 0.0)
        if now - last < 15:
            return
        _last_chat_at[message.channel.id] = now
        lowered = text.lower()
        if GREETING_RE.match(text):
            await message.reply(random.choice(GREETINGS))
            return
        reply = next(
            (r for keys, r in CHAT_REPLIES if any(k in lowered for k in keys)),
            CHAT_FALLBACK,
        )
        await message.reply(reply)

def main():
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        print("ERROR: DISCORD_BOT_TOKEN is not set.")
        raise SystemExit(1)
    if not BYRON_DISCORD_ID:
        print("WARNING: byron_discord_id is not set in config.json.")
    intents = discord.Intents.default()
    intents.message_content = True
    client = Marrow(intents=intents)
    client.run(token)

if __name__ == "__main__":
    main()
