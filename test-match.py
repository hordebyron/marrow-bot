"""Test the puzzle answer matcher without the real discord.py.

Run: python3 test-match.py
Stubs discord and discord.ext.tasks so bot.py imports cleanly.
"""
import importlib.util
import sys
import types

discord = types.ModuleType("discord")


class _Client:
    def __init__(self, *a, **k):
        pass


class _Message:
    pass


class _Intents:
    def __init__(self):
        self.message_content = True

    @staticmethod
    def default():
        return _Intents()


discord.Client = _Client
discord.Message = _Message
discord.Intents = _Intents
sys.modules["discord"] = discord

ext = types.ModuleType("discord.ext")
tasks = types.ModuleType("discord.ext.tasks")


def _loop(*a, **k):
    def deco(fn):
        fn.before_loop = lambda *x, **y: (lambda f: f)(lambda: None)
        fn.start = lambda *x, **y: None
        fn.is_running = lambda: False
        return fn

    return deco


tasks.loop = _loop
ext.tasks = tasks
sys.modules["discord.ext"] = ext
sys.modules["discord.ext.tasks"] = tasks

spec = importlib.util.spec_from_file_location("bot", "bot.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)

should_win = [
    ("a zombie", {"a zombie", "zombie"}),
    ("A ZOMBIE!", {"a zombie", "zombie"}),
    ("I think it is a zombie", {"a zombie", "zombie"}),
    ("zombies", {"a zombie", "zombie"}),
    ("the answer is george romero", {"george romero", "romero"}),
    ("it came out in 1968", {"1968"}),
    ("the answer is r", {"r"}),
    ("four", {"4", "four"}),
    ("I say 4", {"4", "four"}),
    ("what's up?", {"a zombie", "zombie"}),  # placeholder, replaced below
]
should_win = should_win[:-1]  # "what's up?" is not an answer

should_lose = [
    ("red", {"ed"}),
    ("maybe", {"may"}),
    ("i love dead rising", {"ed"}),
    ("thank you", {"tank"}),
    ("i like the same one", {"sam"}),
    ("smash it", {"ash"}),
    ("sunday", {"sun"}),
    ("hello there", {"a zombie", "zombie"}),
    ("what's up?", {"a zombie", "zombie"}),
    ("", {"a zombie"}),
]

ok = True
for text, ans in should_win:
    got = bot.matches_answer(text, ans)
    print(("WIN  " if got else "FAIL "), repr(text), "->", sorted(ans))
    ok = ok and got
for text, ans in should_lose:
    got = bot.matches_answer(text, ans)
    print(("LOSE " if not got else "FAIL "), repr(text), "->", sorted(ans))
    ok = ok and not got
print("ALL OK" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
