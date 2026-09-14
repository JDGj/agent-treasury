#!/usr/bin/env python3
"""The capability boundary. Read the envelope, refuse deterministically.

The ADR's line is "o prompt define intenção, o código define capacidade". This
file is the second half. Until it existed, every prohibition in AGENT.md was a
sentence an agent could read, reason about, and talk itself past — which is the
whole failure mode a policy engine exists to remove.

Two properties matter more than features:

  Deterministic. `check()` takes an action and returns allow or refuse. No
  model in the path, no judgement call, no override argument. A refusal cannot
  be argued with because there is nobody to argue with.

  Fail closed. An action nobody classified is refused, not allowed. A category
  the envelope does not mention is not permission — it is an omission, and an
  omission that defaults to yes is how an envelope leaks.

The ADR proposes Stripe Issuing instead of this. Issuing is not confirmed
available for a Portuguese sole trader and requires approval; this is what
holds either way, and it costs 200 lines.
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENVELOPE = HERE / "envelope.yaml"


def load(path=None):
    """Parse the envelope without a yaml dependency.

    Deliberately a small hand parser rather than pyyaml: this file is the
    fence, and a fence that fails to load because a package is missing on the
    box is a fence that is not there. The subset used is fixed by the envelope
    itself — nested maps, scalars, `- item` lists and `[a, b]` inline lists.
    """
    text = (Path(path) if path else ENVELOPE).read_text()
    root, stack = {}, [(-1, {})]
    stack[0][1].update(root := {})
    cur = root
    path_stack = [(-1, root)]

    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip()
        while len(path_stack) > 1 and indent <= path_stack[-1][0]:
            path_stack.pop()
        cur = path_stack[-1][1]

        if line.startswith("- "):
            key = path_stack[-1][2] if len(path_stack[-1]) > 2 else None
            cur.setdefault("__list__", []).append(_scalar(line[2:]))
            continue

        k, _, v = line.partition(":")
        k, v = k.strip(), v.split(" #")[0].strip()
        if v == "":
            child = {}
            cur[k] = child
            path_stack.append((indent, child, k))
        else:
            cur[k] = _scalar(v)

    env = _flatten_lists(root)
    if problems := incoherent(env):
        raise ValueError("envelope incoerente: " + "; ".join(problems))
    return env


def _scalar(v):
    v = v.strip().strip('"').strip("'")
    if v.startswith("[") and v.endswith("]"):
        return [x.strip() for x in v[1:-1].split(",") if x.strip()]
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    # Floats too. Without this a fraction like 0.4 stayed a string and every
    # comparison against it raised TypeError inside the coherence check -
    # which is to say the fence failed open on a malformed number until the
    # test below existed.
    if re.fullmatch(r"-?\d*\.\d+", v):
        return float(v)
    return v


def _flatten_lists(node):
    """A map whose only key is __list__ was really a list."""
    if not isinstance(node, dict):
        return node
    if set(node) == {"__list__"}:
        return node["__list__"]
    return {k: _flatten_lists(v) for k, v in node.items()}


def incoherent(env):
    """Caps that can never bind, as a list of complaints.

    per_tx_cap_eur was 50 with daily_spend_cap_eur at 20: no single transaction
    could ever reach 50 without the daily cap refusing it first, so the 50 was
    decoration. A limit that cannot fire reads like protection and is not, and
    that is worse than no limit at all — it is the same failure as a test that
    passes because it asserts nothing.
    """
    b = env.get("budget") or {}
    out = []
    tx_f, day_f = b.get("per_tx_fraction"), b.get("daily_spend_fraction")
    if tx_f is not None and day_f is not None and tx_f > day_f:
        out.append(f"per_tx_fraction ({tx_f}) > daily_spend_fraction ({day_f}): "
                   f"o limite por transacao nunca pode disparar")
    for name in ("per_tx_fraction", "daily_spend_fraction"):
        f = b.get(name)
        if f is not None and not 0 < f <= 1:
            out.append(f"{name} ({f}) tem de estar entre 0 e 1")
    tx_min, day_min = b.get("per_tx_min_eur"), b.get("daily_spend_min_eur")
    if tx_min is not None and day_min is not None and tx_min > day_min:
        out.append(f"per_tx_min_eur ({tx_min}) > daily_spend_min_eur ({day_min})")
    return out


def caps(env, revenue_eur=0):
    """(per-transaction, per-day) in euros, at the current ceiling.

    Both scale with what the business has earned, so the envelope can say
    "spend more once you sell more" without anyone editing a number.
    """
    b = env.get("budget") or {}
    ceiling = spend_ceiling(env, revenue_eur)
    return (max(b.get("per_tx_min_eur", 0), ceiling * b.get("per_tx_fraction", 1)),
            max(b.get("daily_spend_min_eur", 0),
                ceiling * b.get("daily_spend_fraction", 1)))


def spend_ceiling(env, revenue_eur=0):
    """How many euros the business may ever spend: what it started with, plus
    what it earned. Nothing else.

    This is the owner's rule made mechanical - "ele comeca com 50 EUR e depois,
    enquanto se aprimora, pode comprar coisas melhores desde que tenha saldo".
    A fixed hard_cap would say the opposite: it would let a business that has
    earned nothing spend up to the cap, and stop a business that is earning
    from reinvesting. The ceiling moves with revenue in one direction only.

    revenue_eur comes from the Stripe API, never from the agent's own account
    of itself. That inversion is the whole correction from the v1 post-mortem:
    nothing in the old loop distinguished "progress" from "revenue".
    """
    return (env.get("budget") or {}).get("initial_capital_eur", 0) + max(0.0, revenue_eur)


class Refused(Exception):
    """Raised instead of doing the thing. Carries why, for the log."""


def check(action, env=None, amount_eur=0, spent_today_eur=0, spent_total_eur=0,
          revenue_eur=0):
    """Allow or refuse one action. Returns None; raises Refused with a reason.

    `action` is a plain string category, not a sentence: the caller names what
    it is doing and the envelope decides. Matching a free-text description
    against a policy would put a language model back in the path, which is the
    thing this file exists to avoid.
    """
    env = env or load()
    deny = env.get("deny") or []
    allow = (env.get("allow") or {}).get("spend_categories") or []
    budget = env.get("budget") or {}

    if action in deny:
        raise Refused(f"'{action}' esta na lista deny do envelope")

    if amount_eur:
        if action not in allow:
            raise Refused(
                f"'{action}' nao esta em allow.spend_categories — uma categoria "
                f"que o envelope nao menciona e uma omissao, nao uma permissao")
        ceiling = spend_ceiling(env, revenue_eur)
        tx_cap, day_cap = caps(env, revenue_eur)
        for limit, label, running in (
                (tx_cap, "por transacao", amount_eur),
                (day_cap, "por dia", spent_today_eur + amount_eur),
                (ceiling, "no saldo", spent_total_eur + amount_eur)):
            if limit is not None and running > limit:
                extra = ""
                cap0 = budget.get("initial_capital_eur", 0)
                if label == "no saldo":
                    extra = (f" ({cap0} de capital + {revenue_eur:.2f} de "
                             f"receita). Para gastar mais, fatura mais.")
                else:
                    extra = (f" ao saldo actual de {ceiling:.2f} EUR. "
                             f"Sobe com a receita.")
                raise Refused(
                    f"{running:.2f} EUR excede o limite {label} de "
                    f"{limit:.2f} EUR{extra}")
    return None


def allowed(action, **kw):
    """check() as a boolean, for callers that want to branch rather than catch."""
    try:
        check(action, **kw)
        return True
    except Refused:
        return False


def demo():
    env = load()

    # Every scalar shape the envelope uses, including the one that broke it.
    assert _scalar("0.4") == 0.4 and isinstance(_scalar("0.4"), float)
    assert _scalar("-1.5") == -1.5
    assert _scalar("20") == 20 and isinstance(_scalar("20"), int)
    assert _scalar("true") is True and _scalar("false") is False
    assert _scalar("[a, b]") == ["a", "b"]
    assert _scalar("claude-sonnet-5") == "claude-sonnet-5"

    # The envelope parsed into the shape the rest of the code assumes.
    assert isinstance(env["deny"], list) and len(env["deny"]) == 9, env["deny"]
    assert env["allow"]["spend_categories"][0] == "domains"
    assert env["budget"]["initial_capital_eur"] == 50
    assert env["budget"]["token_cost_counts_as_spend"] is False, \
        "os tokens sao a subscricao dele, nao capital do negocio"
    assert env["death_condition"]["no_external_revenue_after_days"] == 14
    assert env["budget"]["model"] == "claude-sonnet-5"

    # THE test that makes the envelope real: every deny line refuses. The ADR's
    # checklist item 6 asks for exactly this, and without it each line is a
    # comment that an agent can read past.
    for action in env["deny"]:
        try:
            check(action, env)
        except Refused as e:
            assert action in str(e), (action, str(e))
        else:
            raise AssertionError(f"'{action}' esta em deny e NAO foi recusada")

    # An allowed purchase inside every cap goes through.
    check("domains", env, amount_eur=12)

    # Fail closed: a category nobody listed is refused, not allowed. This is
    # the one that decides whether the envelope leaks.
    for unknown in ("consultoria", "equipamento", "", "DOMAINS"):
        try:
            check(unknown, env, amount_eur=1)
        except Refused as e:
            assert "omissao" in str(e), (unknown, str(e))
        else:
            raise AssertionError(f"categoria desconhecida '{unknown}' passou")

    # The three caps, each on its own. The order matters: per-transaction is
    # checked first, so its test has to use an amount the daily cap allows.
    capital = env["budget"]["initial_capital_eur"]
    tx, day = caps(env)
    assert (tx, day) == (20.0, 20.0), \
        f"a 50 EUR os caps tem de dar exactamente os 20/20 do ADR, deram {tx}/{day}"
    for kw, expect in (
            (dict(amount_eur=tx + 1), "por transacao"),
            (dict(amount_eur=day - 1, spent_today_eur=2), "por dia"),
            (dict(amount_eur=1, spent_total_eur=capital), "no saldo")):
        try:
            check("hosting", env, **kw)
        except Refused as e:
            assert expect in str(e), (kw, str(e))
        else:
            raise AssertionError(f"{kw} devia ter sido recusada ({expect})")

    # Exactly at a cap is allowed; one cent over is not.
    check("hosting", env, amount_eur=tx)
    assert not allowed("hosting", env=env, amount_eur=tx + 0.01)

    # And the coherence check itself, which is what caught the dead cap.
    assert incoherent({"budget": {"per_tx_fraction": 0.9,
                                  "daily_spend_fraction": 0.4}}), \
        "um cap por transacao acima do diario nunca dispara e tem de ser dito"
    assert incoherent({"budget": {"per_tx_fraction": 1.5}})
    assert incoherent({"budget": {"per_tx_min_eur": 40,
                                  "daily_spend_min_eur": 15}})
    assert incoherent({"budget": {"per_tx_fraction": 0.4,
                                  "daily_spend_fraction": 0.4,
                                  "per_tx_min_eur": 15,
                                  "daily_spend_min_eur": 15}}) == []

    # The ceiling is earned. This is the owner's rule, as arithmetic.
    assert spend_ceiling(env) == 50, "sem receita, so o capital inicial"
    assert spend_ceiling(env, revenue_eur=49) == 99
    assert spend_ceiling(env, revenue_eur=-10) == 50, \
        "um numero negativo nao pode ALARGAR nem encolher o tecto"

    # The caps themselves grow with revenue - without this, "pode comprar
    # coisas melhores" was false in code however much the business earned.
    assert caps(env, revenue_eur=0) == (20.0, 20.0)
    tx2, _ = caps(env, revenue_eur=98)          # two sales in: ceiling 148
    assert abs(tx2 - 59.2) < 0.01, tx2
    # A 30 EUR VPS: impossible on day one, possible after two sales.
    assert not allowed("hosting", env=env, amount_eur=30)
    check("hosting", env, amount_eur=30, revenue_eur=98)
    # The floor keeps a domain affordable even with nothing earned.
    assert caps(env)[0] >= env["budget"]["per_tx_min_eur"]

    # A business that has earned nothing cannot spend past its capital...
    assert not allowed("hosting", env=env, amount_eur=1, spent_total_eur=50)
    # ...and one that has sold something can spend what it sold. "Coisas
    # melhores desde que tenha saldo", in one assertion.
    check("hosting", env, amount_eur=10, spent_total_eur=45, revenue_eur=49)
    # But not a cent more than it has.
    try:
        check("hosting", env, amount_eur=10, spent_total_eur=90, revenue_eur=49)
    except Refused as e:
        assert "no saldo" in str(e) and "fatura mais" in str(e), str(e)
    else:
        raise AssertionError("gastou mais do que tinha")

    # A spend with no amount is not a spend — it must not be caught by the
    # category check, or every non-financial action becomes a purchase.
    check("build_landing_page", env)

    print("self-check ok")


if __name__ == "__main__":
    sys.exit(demo() if len(sys.argv) > 1 and sys.argv[1] == "demo" else 0)
