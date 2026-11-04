"""Каждый обработчик кнопок/команд получает только то, что ему умеет передать aiogram.
Ловит ошибку, когда вспомогательную функцию случайно вставили между декоратором и обработчиком
(так 16.10 сломалась кнопка «💸 Вернуть деньги»)."""
import ast
import glob

from app.bot.handlers import payments as ph

ALLOWED = {"cb", "m", "message", "user", "state", "bot", "command", "s", "event", "target", "msg"}


def test_router_handlers_take_only_injectable_args():
    bad = []
    for f in glob.glob("app/bot/**/*.py", recursive=True):
        for n in ast.walk(ast.parse(open(f, encoding="utf-8").read())):
            if isinstance(n, ast.AsyncFunctionDef) and any("router" in ast.unparse(d) for d in n.decorator_list):
                extra = {a.arg for a in n.args.args + n.args.kwonlyargs} - ALLOWED
                if extra:
                    bad.append(f"{f}:{n.name} {sorted(extra)}")
    assert not bad, bad


def test_refund_button_goes_to_refund_do():
    names = {h.callback.__name__ for h in ph.router.callback_query.handlers}
    assert "refund_do" in names and "explain" not in names
