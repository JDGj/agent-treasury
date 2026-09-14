#!/usr/bin/env python3
"""External revenue, read from Stripe. Root only.

The v1 post-mortem's fourth line: "sem verificador externo — nada no loop
distinguia progresso de receita". This is the verifier. It asks Stripe what
customers actually paid and answers in cents; it never reads ledger.jsonl,
never reads anything the agent writes, and has no code path that could.

That inversion is the point. The agent can write whatever it likes about how
well it is doing; the number that decides whether the business may spend more,
and whether the hypothesis is dead, comes from the payment processor.

Two distinctions the ADR's `revenue_source: stripe_api` leaves implicit and
that decide whether the number means anything:

  EXTERNAL, not gross. A refund is negative revenue and is subtracted. A payout
  to the owner's bank is the business's own money moving between its own
  accounts - not revenue, and counting it would let the business "earn" by
  moving money it already had.

  RECEIVED, not promised. Only balance transactions Stripe has actually
  settled. An invoice sent is not revenue; this is the difference between a
  business and a hope.
"""
import json
import os
import sys
import urllib.parse
import urllib.request

API = "https://api.stripe.com/v1/balance_transactions"

# What a customer paying looks like, in Stripe's vocabulary. Deliberately a
# short allow-list rather than "everything that is not a transfer": a type
# nobody has classified must not silently count as money earned.
EARNED = {"charge", "payment"}
GIVEN_BACK = {"refund", "payment_refund", "payment_failure_refund",
              "adjustment", "stripe_fee", "application_fee"}


def fetch(key, opener=None, limit=100):
    """Every settled balance transaction. Newest first, as Stripe returns them."""
    out, cursor = [], None
    for _ in range(20):                     # bounded: 2000 transactions is plenty
        params = {"limit": limit}
        if cursor:
            params["starting_after"] = cursor
        req = urllib.request.Request(
            f"{API}?{urllib.parse.urlencode(params)}",
            headers={"Authorization": f"Bearer {key}"})
        with (opener or urllib.request.urlopen)(req, timeout=30) as r:
            page = json.load(r)
        out += page.get("data", [])
        if not page.get("has_more") or not page.get("data"):
            break
        cursor = page["data"][-1]["id"]
    return out


def external_cents(txns):
    """Net euros customers paid, in cents. Refunds and fees subtract."""
    total = 0
    for t in txns:
        if t.get("currency") != "eur":
            continue
        if t.get("type") in EARNED:
            total += t.get("net", 0)
        elif t.get("type") in GIVEN_BACK:
            total += t.get("net", 0)        # already negative in Stripe's data
    return total


def revenue_eur(key=None, opener=None):
    """The number spend_ceiling() needs. None when Stripe cannot be asked.

    None, not zero. A business whose processor is unreachable has not earned
    nothing - it is unknown, and the caller must decide. Returning 0 here would
    quietly shrink the spend ceiling to the starting capital every time the
    network hiccuped.
    """
    key = key or os.environ.get("STRIPE_SECRET_KEY", "")
    if not key:
        return None
    try:
        return external_cents(fetch(key, opener)) / 100.0
    except Exception:
        return None


# Modules whose presence would mean this file is reading what the agent wrote,
# and the call that would mean it is reading a file at all.
FORBIDDEN_IMPORTS = ("treasury", "pathlib", "metrics", "dashboard")


def reads_agent_state(module_src):
    """Complaints about a source that would let the verifier be talked to.

    Takes source text rather than inspecting the running module, so the check
    can be tested against a deliberately sabotaged copy without executing it -
    executing a saboteur only proves it crashes, which is not the same as
    proving the guard fires.

    Checked on the syntax tree, not the text: a substring match trips over the
    docstring that explains the rule, which is exactly how the first version of
    this guard failed.
    """
    import ast
    tree = ast.parse(module_src)
    imported = {n.name.split(".")[0]
                for node in ast.walk(tree) if isinstance(node, ast.Import)
                for n in node.names}
    imported |= {node.module.split(".")[0] for node in ast.walk(tree)
                 if isinstance(node, ast.ImportFrom) and node.module}
    out = [f"importa {m} - seria o agente a avaliar-se"
           for m in FORBIDDEN_IMPORTS if m in imported]
    if any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
           and n.func.id == "open" for n in ast.walk(tree)):
        out.append("abre um ficheiro: a unica fonte e a API da Stripe")
    return out


def demo():
    import io

    def page(data, has_more=False):
        class R(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return R(json.dumps({"data": data, "has_more": has_more}).encode())

    charge = {"id": "txn_1", "type": "charge", "net": 4753, "currency": "eur"}
    refund = {"id": "txn_2", "type": "refund", "net": -4900, "currency": "eur"}
    payout = {"id": "txn_3", "type": "payout", "net": -4753, "currency": "eur"}
    usd = {"id": "txn_4", "type": "charge", "net": 9999, "currency": "usd"}
    weird = {"id": "txn_5", "type": "some_new_stripe_type", "net": 5000,
             "currency": "eur"}

    assert external_cents([charge]) == 4753
    assert external_cents([charge, refund]) == 4753 - 4900, "um reembolso subtrai"
    assert external_cents([charge, payout]) == 4753, \
        "um payout e dinheiro do negocio a mudar de conta, nao receita"
    assert external_cents([charge, usd]) == 4753, "so euros"
    assert external_cents([charge, weird]) == 4753, \
        "um tipo que ninguem classificou nao pode contar como dinheiro ganho"
    assert external_cents([]) == 0

    # Unreachable Stripe is unknown, never zero - zero would silently shrink
    # the spend ceiling to the starting capital on every network hiccup.
    def boom(*a, **k):
        raise OSError("connection reset")
    assert revenue_eur("sk_test_x", boom) is None
    assert revenue_eur("", None) is None, "sem chave, nao se inventa um numero"

    # And the happy path, end to end, through the same shape Stripe returns.
    assert revenue_eur("sk_test_x", lambda *a, **k: page([charge])) == 47.53

    # This file must never read what the agent writes - the whole point of a
    # verifier is that it cannot be talked to.
    import inspect
    me = inspect.getsource(sys.modules[__name__])
    assert reads_agent_state(me) == [], reads_agent_state(me)

    # And the guard itself catches both routes in, tested on source rather than
    # by running a saboteur: a saboteur that crashes proves only that it
    # crashes.
    assert any("treasury" in c for c in reads_agent_state(
        "import treasury\nx = 1")), "um import de estado do agente tem de ser apanhado"
    assert any("treasury" in c for c in reads_agent_state(
        "from treasury import balance")), "tambem na forma from-import"
    assert any("abre um ficheiro" in c for c in reads_agent_state(
        "def f():\n    return open('ledger.jsonl').read()"))
    assert reads_agent_state("import json, os\nx = 1") == []

    print("self-check ok")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "demo":
        sys.exit(demo())
    r = revenue_eur()
    print("receita externa: " + ("desconhecida (Stripe inacessivel ou sem chave)"
                                 if r is None else f"{r:.2f} EUR"))
