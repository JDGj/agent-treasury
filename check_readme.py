#!/usr/bin/env python3
"""Check that the README still describes the library. Run in CI.

Written on 2026-09-14, an hour after `example.py` found three claims in this
README that were false and one that was missing. Three of the four needed a
human to notice. The fourth did not, and is the reason this file exists:

    treasury.record(-1200, "hosting", note="VPS, September")

That was the first code block in the README -- the four most-read lines in the
project -- and it raises `TypeError: got multiple values for argument 'note'`.
Nothing executed it, so nothing said so.

So every call to this library that the README shows is now checked against the
real signature, with `inspect.signature().bind()`. That binds the arguments
without running anything: no ledger is touched, no network is used, and a
renamed or reordered parameter fails the build the day it is renamed.

Also checked: anchors resolve, and every ```python block parses.

Not checked: whether the example's printed output still matches. `example.py`
is executed by CI, so if its behaviour changes the run fails there -- pinning
its text here as well would only make the same failure arrive twice.
"""
import ast
import inspect
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
README = HERE / "README.md"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "policy"))


def modules():
    import redact
    import revenue
    import treasury
    return {"treasury": treasury, "revenue": revenue, "redact": redact}


def check_calls(block, mods, bad):
    """Bind every module.function(...) call in this block to the real thing."""
    try:
        tree = ast.parse(block)
    except SyntaxError:
        return                                  # reported by the caller
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name)):
            continue
        mod = mods.get(fn.value.id)
        if mod is None:
            continue
        target = getattr(mod, fn.attr, None)
        if target is None:
            bad.append(f"{fn.value.id}.{fn.attr}() nao existe")
            continue
        if not callable(target):
            continue
        # Positional args stand in as placeholders; only the SHAPE is checked.
        args = [None] * len(node.args)
        kwargs = {}
        for kw in node.keywords:
            if kw.arg is None:                  # **something -- unknowable
                return
            kwargs[kw.arg] = None
        try:
            inspect.signature(target).bind(*args, **kwargs)
        except TypeError as exc:
            bad.append(f"{fn.value.id}.{fn.attr}(...) no README: {exc}")


def slugify(heading):
    t = heading.strip().lower()
    t = re.sub(r"`|\.|\(|\)|,|:|—|–|/", "", t)
    return re.sub(r"[^a-z0-9\s-]", "", t).strip().replace(" ", "-")


def check(text=None, mods=None):
    text = README.read_text(encoding="utf-8") if text is None else text
    mods = modules() if mods is None else mods
    bad = []

    blocks = re.findall(r"```python\n(.*?)```", text, re.S)
    for i, block in enumerate(blocks, 1):
        try:
            ast.parse(block)
        except SyntaxError as exc:
            bad.append(f"bloco python #{i} nao compila: {exc.msg} (linha {exc.lineno})")
            continue
        check_calls(block, mods, bad)

    slugs = {slugify(m.group(1)) for m in re.finditer(r"^#+\s+(.*)$", text, re.M)}
    for anchor in re.findall(r"\]\(#([^)]+)\)", text):
        if anchor not in slugs:
            bad.append(f"ancora #{anchor} nao corresponde a nenhum titulo")

    for name in re.findall(r"^\| `([\w./]+\.(?:py|yaml))` \|", text, re.M):
        if not (HERE / name).exists():
            bad.append(f"a tabela lista {name}, que nao esta no repo")
    return bad


def selftest():
    mods = modules()

    # The exact line that was wrong, and the corrected one.
    wrong = 'import treasury\ntreasury.record(-1200, "hosting", note="VPS")\n'
    right = 'import treasury\ntreasury.record(-1200, "VPS", category="hosting")\n'
    assert any("multiple values" in b for b in check(f"```python\n{wrong}```", mods)), \
        "a chamada errada tem de ser apanhada"
    assert check(f"```python\n{right}```", mods) == [], "a certa nao pode ser queixa"

    # A function that no longer exists, and a keyword that never did.
    assert any("nao existe" in b for b in
               check("```python\nimport treasury\ntreasury.naoexiste()\n```", mods))
    assert any("unexpected keyword" in b for b in
               check('```python\nimport treasury\ntreasury.balance(x=1)\n```', mods))

    # Broken syntax, a dead anchor, a file that is not here.
    assert any("nao compila" in b for b in check("```python\ndef f(\n```", mods))
    assert any("#nao-existe" in b for b in check("# t\n[x](#nao-existe)\n", mods))
    assert any("naoexiste.py" in b for b in
               check("| `naoexiste.py` | uma coisa |\n", mods))
    # And a call into a module the README does not import is left alone.
    assert check("```python\nimport os\nos.qualquercoisa(1, 2, 3)\n```", mods) == []

    assert check(mods=mods) == [], check(mods=mods)
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
        raise SystemExit(0)
    problems = check()
    for p in problems:
        print(f"  {p}", file=sys.stderr)
    print("  o README ainda descreve o que existe" if not problems
          else f"  {len(problems)} problema(s)")
    raise SystemExit(1 if problems else 0)
