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
from datetime import datetime, timezone
from pathlib import Path

import discord

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
WALLETS_PATH = HERE / "discord-wallets.json"
LOG_PATH = HERE / "rewards-log.md"

def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default

CONFIG = load_json(CONFIG_PATH, {})
BYRON_DISCORD_ID = int(CONFIG.get("byron_discord_id") or 0)
ALLOWED_CHANNEL_IDS = set(CONFIG.get("allowed_channel_ids") or [])
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
    with LOG_PATH.open("a") as f:
        f.write(f"- {ts} - {line}\n")

def wallets() -> dict:
    data = load_json(WALLETS_PATH, {})
    data.pop("_comment", None)
    return data

def save_wallets(data: dict) -> None:
    data["_comment"] = "Discord user ID -> WAX account. Filled in when a person gives Marrow their WAX name."
    WALLETS_PATH.write_text(json.dumps(data, indent=2) + "\n")

class Marrow(discord.Client):
    async def on_ready(self):
        print(f"Marrow logged in as {self.user}")
        print(f"Byron Discord ID set: {bool(BYRON_DISCORD_ID)}")

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if ALLOWED_CHANNEL_IDS and message.channel.id not in ALLOWED_CHANNEL_IDS:
            return
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
                "Only Byron can trigger rewards."
            )
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
