# Marrow — Horde Discord bot

Marrow is Byron's little helper zombie in The House of the Horde on Discord. Gender-neutral, they/them.

Live since 14 Nov 2026, hosted free on FadeHost, deployed from this repo. When code is pushed here, FadeHost redeploys it.

## What Marrow does

- Listens in Discord, answers only when spoken to with Hey Marrow.
- Answers Horde questions: Monday drops, Colour Me, Ko-fi, Loot Crates.
- Takes WAX names for the WANT ON THE LIST drop: "Hey Marrow my WAX is yourname.wam", saves Discord ID to WAX name in discord-wallets.json.
- Passes reward requests to Muse: "Hey Marrow give Tony a reward NFT", Byron only. Marrow logs it in rewards-log.md and replies that it passed it on.
- Marrow never sends DHC, WAX or NFT itself. No WAX key lives on the host. Muse sends rewards directly from horderewards on their own computer, then reports back.

## Files

- `bot.py` — the bot, discord.py 2.7.1
- `config.json` — Byron Discord ID, server ID, allowed channels, reward limits
- `discord-wallets.json` — Discord user ID to WAX account, filled in as people register
- `prize-pool.json` — prize NFTs, currently example only
- `rewards-log.md` — reward requests, registrations, whitelist adds
- `requirements.txt` — discord.py==2.7.1, aiohttp==3.14.5

## Commands

```
Hey Marrow my WAX is yourname.wam      anyone, get on the list
Hey Marrow give @user 10 DHC           Byron only, passed to Muse
Hey Marrow give @user a reward NFT     Byron only, passed to Muse
Hey Marrow register @user waxaccount   Byron only
Hey Marrow help                        Byron only
```

## Safety

No secrets in this repo. No Discord token, no WAX private key, no seed. Token is the DISCORD_BOT_TOKEN environment variable on the host. Reward limits: max 100 DHC, 5 WAX, 1 NFT per reward, 500 DHC per day.
