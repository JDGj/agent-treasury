#!/usr/bin/env python3
"""Treasury + kill switch.

Append-only ledger, one JSON object per line:
    {"ts": "<iso8601>", "cents": <int>, "note": "<str>", "kind": "<str>"}
Positive cents = money in, negative = money out. Balance = sum. Dead at <= 0.

`kind` is "flow" for real money and "correction" for an accounting fix. Both
count towards the balance; only "flow" counts towards burn, because a correction
is not something the business spent. Getting that wrong makes runway read like a
crisis and the agent decide like it is in one.

ponytail: file permissions ARE the auth. Own this file, ledger.jsonl and DEAD
as root; run the agent as an unprivileged user. Then the agent can read its
balance but cannot invent income or delete its own tombstone.
"""
import json, os, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

HOME = Path(os.environ.get("BIZ_HOME", Path(__file__).resolve().parent))
LEDGER = HOME / "ledger.jsonl"
DEAD = HOME / "DEAD"
JOURNAL = HOME / "JOURNAL.md"


class LedgerMissing(Exception):
    """The ledger could not be read. Not the same thing as a zero balance.

    It used to be: entries() returned [] for a missing file, balance() summed
    to 0, alive() said no, and run.sh wrote the tombstone. A wrong path, a bad
    env var or an unmounted disk would therefore end the business permanently,
    silently, with a confident epitaph. Unknown must never read as dead.
    """


def entries(ledger=LEDGER):
    if not ledger.exists():
        raise LedgerMissing(f"no ledger at {ledger}")
    return [json.loads(l) for l in ledger.read_text().splitlines() if l.strip()]


def balance(ledger=LEDGER):
    return sum(e["cents"] for e in entries(ledger))


# Movements that record something that already happened, rather than
# authorising something about to happen. Stripe telling us a fee was taken is
# not a purchase the agent decided to make, and refusing it would leave the
# ledger disagreeing with reality - which is the one thing a ledger may not do.
# `subscription` belongs here for the same reason: run.sh debits the fixed
# monthly cost of the Claude plan and the VPS, which is a bill arriving, not a
# purchase anyone decided on. Leaving it out meant `run.sh monthly` died on its
# own first line - no Wise check, no report, no publish - and nobody had seen
# it because the monthly had not fired since the fence went in.
FACTUAL = {"stripe", "penalty", "seed", "subscription"}


def record(cents, note, ledger=LEDGER, kind="flow", category=None,
           gate=None, revenue_eur=None):
    """Append one movement. Spending is checked against the envelope first.

    The envelope and the policy engine existed for a day before anything
    called them - a fence built and not installed, which is the same failure
    as an untested deny line one level up. This is where they get installed,
    because this function is the only way a euro leaves.

    `category` says what kind of movement this is. A negative amount with no
    category is REFUSED: fail closed, because a spend nobody classified is a
    spend nobody authorised. Factual movements (Stripe's own fees, the owner's
    penalty, the seed) are recording reality and are exempt - a ledger that
    refuses to write down what already happened is worse than no ledger.
    """
    if not isinstance(cents, int) or isinstance(cents, bool):
        raise TypeError("cents must be an int (avoid float money)")
    if not note.strip():
        raise ValueError("note is required - an unexplained movement is a bug")
    if kind not in ("flow", "correction"):
        raise ValueError("kind must be 'flow' or 'correction'")

    # kind="correction" is exempt by definition: it says "the record was wrong
    # and I am fixing it". Gating a correction would mean the ledger could be
    # left knowingly false because the envelope was spent, which inverts what
    # the envelope is for.
    if cents < 0 and kind != "correction" and category not in FACTUAL:
        check = gate or _envelope_gate()
        if check is not None:
            check(category, amount_eur=abs(cents) / 100.0,
                  spent_today_eur=abs(spent_today(ledger)) / 100.0,
                  spent_total_eur=abs(spent_total(ledger)) / 100.0,
                  revenue_eur=(revenue_eur if revenue_eur is not None
                               else _revenue()))
    entry = {"ts": datetime.now(timezone.utc).isoformat(),
             "cents": cents, "note": note.strip(), "kind": kind}
    # Write down what it was gated on. Every other field survives in the
    # ledger and this one did not, so the record of a spend could not say
    # which rule let it through.
    if category:
        entry["category"] = category
    line = json.dumps(entry)
    with open(ledger, "a") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())
    return balance(ledger)


def _envelope_gate():
    """policy.engine.check, or None when the policy layer is not installed.

    None rather than an exception: this module is imported by the dashboard and
    by scripts that never spend, and a missing policy directory must not stop
    the business from reporting its own balance. A spend, however, cannot
    happen without it - see the caller.
    """
    try:
        sys.path.insert(0, str(HOME / "policy"))
        import engine
        return engine.check
    except Exception:
        return None


def _revenue():
    """External revenue in euros, from Stripe. 0 when it cannot be asked.

    Zero, not None, deliberately: an unknown revenue must shrink the ceiling to
    the starting capital, never widen it. Being unable to prove you earned
    something is not the same as having earned it.
    """
    try:
        sys.path.insert(0, str(HOME / "policy"))
        import revenue
        return revenue.revenue_eur() or 0.0
    except Exception:
        return 0.0


def spent_today(ledger=LEDGER):
    """Cents spent since midnight UTC (negative)."""
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0,
                                               microsecond=0)
    return sum(e["cents"] for e in entries(ledger)
               if e["cents"] < 0 and _ts(e) and _ts(e) >= start)


def spent_total(ledger=LEDGER):
    """Every cent ever spent (negative)."""
    return sum(e["cents"] for e in entries(ledger) if e["cents"] < 0)


def _ts(entry):
    try:
        return datetime.fromisoformat(entry["ts"].replace("Z", "+00:00"))
    except (ValueError, KeyError, AttributeError):
        return None


def burn30(ledger=LEDGER):
    """Cents spent in the last 30 days (positive number)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    return -sum(e["cents"] for e in entries(ledger)
                if e["cents"] < 0
                and e.get("kind", "flow") != "correction"
                and datetime.fromisoformat(e["ts"]) >= cutoff)


def runway_days(ledger=LEDGER, project=None):
    """Days to zero, counting what is already SCHEDULED and not only the past.

    Dividing the balance by the last 30 days looks prudent and is not. The
    owner's penalty (10 EUR every five days without a sale) and the fixed
    monthly cost are obligations that WILL happen; leaving them out reported
    "runway: 120 days" on a business with about 17 - and the agent quoted that
    number in JOURNAL.md while planning against it.

    By default this is the plain thing: balance divided by the last thirty
    days of spending. That is honest but optimistic, because it only knows
    about money that has already left -- a subscription renewing on the 1st, a
    server bill, a scheduled penalty are all certain and none of them are in
    the last thirty days until they happen.

    So pass `project` to include them. It is called as
    `project(balance_cents, burn30_cents)` and returns days, and it exists as
    an argument rather than an import because two answers to "how long do I
    have" is how one of them ends up wrong. Whatever computes the number for
    your dashboard should be the same thing that computes it here.

    A projection that raises is not allowed to take `status` down with it --
    that is the first thing an agent reads every run -- so it falls back to
    the plain rate rather than propagating.
    """
    b, burn = balance(ledger), burn30(ledger)
    if project is not None:
        try:
            return project(b, burn)
        except Exception:
            pass
    if burn <= 0:
        return None            # nothing spent yet - no rate to divide by
    return b / (burn / 30.0)


def alive(ledger=LEDGER, dead=DEAD):
    return not dead.exists() and balance(ledger) > 0


def kill(reason, ledger=LEDGER, dead=DEAD, journal=JOURNAL):
    ents = entries(ledger)
    inflow = sum(e["cents"] for e in ents if e["cents"] > 0)
    outflow = -sum(e["cents"] for e in ents if e["cents"] < 0)
    epitaph = (f"# DEAD {datetime.now(timezone.utc).isoformat()}\n"
               f"reason: {reason}\n"
               f"final balance: {balance(ledger)/100:.2f} EUR\n"
               f"lifetime in: {inflow/100:.2f} EUR\n"
               f"lifetime out: {outflow/100:.2f} EUR\n"
               f"movements: {len(ents)}\n")
    dead.write_text(epitaph)
    with open(journal, "a") as f:
        f.write("\n---\n" + epitaph)
    return epitaph


def status(ledger=LEDGER, dead=DEAD):
    r = runway_days(ledger)
    # The basis, next to the number. "runway: 17 days" alone reads as a
    # measurement of the past; what it is is a forecast that already counts the
    # penalty, and whoever reads it has to know that to plan against it.
    base = " (already counts the 10 EUR/5 days penalty)"
    return (f"balance: {balance(ledger)/100:.2f} EUR\n"
            f"burn (30d): {burn30(ledger)/100:.2f} EUR\n"
            f"runway: {'unknown (no spend yet)' if r is None else f'{r:.0f} days{base}'}\n"
            f"state: {'ALIVE' if alive(ledger, dead) else 'DEAD'}\n"
            f"movements: {len(entries(ledger))}")


def demo():
    import tempfile as _tf
    with _tf.TemporaryDirectory() as _d:
        gone = Path(_d) / "nope.jsonl"
        try:
            entries(gone)
        except LedgerMissing:
            pass
        else:
            raise AssertionError("a missing ledger must raise, never read as empty")
        try:
            alive(gone, Path(_d) / "DEAD")
        except LedgerMissing:
            pass
        else:
            raise AssertionError("alive() must not answer 'dead' for a missing ledger")

    import tempfile
    with tempfile.TemporaryDirectory() as d:
        led, dead, jrn = Path(d)/"l.jsonl", Path(d)/"DEAD", Path(d)/"J.md"
        # An EXISTING ledger with nothing in it is a real zero, and zero is
        # dead. That is the case this asserts; a file that is not there at all
        # is the LedgerMissing case above, and the two must not be conflated -
        # this test used a nonexistent path to stand for "empty", which is how
        # the conflation survived as long as it did.
        led.write_text("")
        assert balance(led) == 0 and not alive(led, dead), "empty ledger must be dead"
        assert record(5000, "seed", led) == 5000
        assert alive(led, dead)
        # gate=open_gate: this block tests ARITHMETIC - balance, burn, runway -
        # and injecting a no-op gate keeps it from also testing policy. The
        # envelope has its own tests below; a test that checks two things at
        # once fails for two reasons and tells you neither.
        open_gate = lambda *a, **k: None
        record(-2000, "server + subscription", led, gate=open_gate)
        assert balance(led) == 3000, balance(led)
        assert burn30(led) == 2000
        # 45 dias era o que este teste exigia, e era a conta errada: dividia
        # 30,00 EUR por 20,00 EUR/30d (0,667/dia) e ignorava a penalizacao de
        # 10 EUR/5 dias (2,00/dia), que e' tao certa como a subscricao enquanto
        # nao houver venda. Com ela: 30,00/(0,667+2,00) = 11,25 dias. O numero
        # certo e' este, e e' o mesmo que o dashboard mostra - deixou de haver
        # dois. A conta depende de nao haver vendas: se `state/customers.jsonl`
        # tiver linhas, a penalizacao para e o numero sobe, por isso confirma-se
        # aqui em vez de se assumir.
        # O caminho simples: 30,00 EUR a 20,00 EUR/30d sao 45 dias.
        assert abs(runway_days(led) - 45.0) < 0.2, runway_days(led)
        # E o caminho com encargos fixos, que e' a razao de `project` existir:
        # somar 2,00 EUR/dia de encargo certo da 30,00/(0,667+2,00) = 11,25.
        # Uma projeccao que omite um encargo permanente nao e' conservadora,
        # esta errada na direccao que magoa.
        with_standing = runway_days(
            led, project=lambda b, burn: b / (burn / 30.0 + 200.0))
        assert abs(with_standing - 11.25) < 0.2, with_standing
        assert with_standing < runway_days(led), "o encargo tem de encurtar"
        # E uma projeccao que rebenta nao pode derrubar o status.
        def boom(b, burn): raise RuntimeError("projeccao partida")
        assert abs(runway_days(led, project=boom) - 45.0) < 0.2, "cai no simples"
        record(-3000, "another month", led, gate=open_gate)
        assert balance(led) == 0 and not alive(led, dead), "zero balance must be dead"
        kill("test", led, dead, jrn)
        assert dead.exists() and "lifetime in: 50.00" in dead.read_text()
        record(9999, "resurrection attempt", led)
        assert not alive(led, dead), "tombstone must outrank a positive balance"
        for bad in [(1.5, "float"), (0, ""), (True, "bool")]:
            try:
                record(*bad, ledger=led); assert False, f"accepted {bad}"
            except (TypeError, ValueError):
                pass
        try:
            record(1, "bad kind", led, kind="whatever"); assert False, "accepted bad kind"
        except ValueError:
            pass

        # A correction moves the balance but is not something the business spent.
        c = Path(d) / "c.jsonl"
        record(5000, "seed", c)
        record(-1000, "server", c, gate=open_gate)
        record(-10000, "seed was misstated", c, kind="correction")
        assert balance(c) == -6000, balance(c)
        assert burn30(c) == 1000, "correction must not count as burn"
    # The fence, installed. It existed for a day with nothing calling it, and
    # a fence nobody calls is a comment. These prove the call is real.
    import tempfile as _tf
    sys.path.insert(0, str(HOME / "policy"))
    import engine as _eng
    _env = _eng.load()

    with _tf.TemporaryDirectory() as d:
        led = Path(d) / "ledger.jsonl"
        record(5000, "seed", led, category="seed")

        # A denied category cannot reach the ledger, and nothing is written.
        before = led.read_text()
        try:
            record(-500, "anuncios no Google", led, category="paid_ads")
        except _eng.Refused as e:
            assert "paid_ads" in str(e), str(e)
        else:
            raise AssertionError("paid_ads chegou ao livro-razao")
        assert led.read_text() == before, "uma recusa nao pode escrever nada"

        # Fail closed: a spend nobody classified is a spend nobody authorised.
        try:
            record(-500, "uma coisa qualquer", led)
        except _eng.Refused as e:
            assert "omissao" in str(e), str(e)
        else:
            raise AssertionError("um gasto sem categoria passou")

        # An allowed purchase inside the caps goes through.
        record(-1200, "dominio", led, category="domains")
        assert balance(led) == 3800

        # Over the ceiling is refused even when the category is allowed.
        try:
            record(-6000, "servidor enorme", led, category="hosting")
        except _eng.Refused:
            pass
        else:
            raise AssertionError("gastou mais do que o tecto")

        # Facts are not authorisations. Stripe taking its fee, and the owner's
        # penalty, must land whatever the envelope says - a ledger that refuses
        # to write down what already happened is worse than no ledger.
        record(-145, "stripe fee ch_x", led, category="stripe")
        record(-1000, "penalizacao 5 dias sem faturar", led, category="penalty")
        assert balance(led) == 3800 - 145 - 1000

        # A correction is exempt by kind, with no category at all: it fixes the
        # record rather than authorising anything.
        record(-100, "o seed estava errado", led, kind="correction")

        # Money coming IN is never gated - it is not a spend.
        record(4900, "stripe charge ch_y", led, category="stripe")

        # And revenue widens the ceiling, which is the owner's own rule
        # reaching all the way down to the ledger.
        record(-3000, "servidor melhor", led, category="hosting",
               revenue_eur=98.0)

    _demo_cli()
    print("self-check ok")


def _demo_cli():
    """Exercise the command line, not just record().

    Everything above calls record() directly with category= set by hand, so
    all of it passed while the CLI - the only spending path a human ever
    touches - refused every spend it was given. A test that cannot fail the
    way production failed is not testing production.

    The policy directory is copied into the temp home on purpose:
    _envelope_gate() returns None when it cannot import engine, so a home
    without policy/ would run these asserts with no fence at all and pass
    for the wrong reason.
    """
    import shutil, subprocess, tempfile
    home = Path(tempfile.mkdtemp())
    try:
        shutil.copytree(HOME / "policy", home / "policy")
        (home / "ledger.jsonl").write_text(json.dumps(
            {"ts": "2026-09-01T00:00:00Z", "cents": 5000, "note": "seed",
             "kind": "flow", "category": "seed"}) + "\n")
        env = {**os.environ, "BIZ_HOME": str(home)}

        def cli(*args):
            return subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), *args],
                capture_output=True, text=True, env=env, cwd=home)

        ok = cli("record", "-1100", "domains", "renovacao exemplo.dev")
        assert ok.returncode == 0, ok.stderr
        assert json.loads((home / "ledger.jsonl").read_text().splitlines()[-1]
                          )["category"] == "domains"

        # The exact line run.sh monthly runs. It used to abort the whole
        # monthly: no Wise check, no report, no publish.
        sub = cli("record", "-3428", "subscription", "subscription + server")
        assert sub.returncode == 0, sub.stderr

        # A deny line reaches the command line too.
        ads = cli("record", "-1000", "paid_ads", "google ads")
        assert ads.returncode != 0, ads.stdout
        assert "categorias validas" in ads.stderr, ads.stderr
        assert "paid_ads" not in ads.stderr.split("categorias validas")[1]

        # The old two-argument form: the note's first word lands in the
        # category slot and is refused, with the list of what would work.
        stale = cli("record", "-1100", "dominio", "exemplo.dev 1 ano")
        assert stale.returncode != 0, stale.stdout
        assert "domains" in stale.stderr, stale.stderr

        missing = cli("record", "-1100")
        assert missing.returncode != 0 and "usage" in missing.stderr
        # AGENT.md tells the agent this prints the list, so it has to.
        assert "domains" in missing.stderr, missing.stderr
        assert "{" not in missing.stderr, missing.stderr

        notnum = cli("record", "dominio", "domains", "x")
        assert notnum.returncode != 0 and "must be cents" in notnum.stderr

        # Income needs no envelope, but still records what it was.
        inc = cli("record", "4900", "stripe", "charge ch_x")
        assert inc.returncode == 0, inc.stderr
    finally:
        shutil.rmtree(home, ignore_errors=True)


def categories():
    """Every category `record` will accept, read from the live envelope.

    The envelope is the authority and it changes without this file changing,
    so this reads it rather than repeating it. If it cannot be read, the
    factual ones are still true and the rest is named rather than guessed -
    a usage message that invents a category is worse than one that admits it
    could not look.
    """
    try:
        sys.path.insert(0, str(HOME / "policy"))
        import engine
        return sorted(set(engine.load()["allow"]["spend_categories"]) | FACTUAL), True
    except Exception:
        return sorted(FACTUAL), False


def usage():
    cats, live = categories()
    tail = (", ".join(cats) if live else
            ", ".join(cats) + " (nao consegui ler o envelope; "
            "allow.spend_categories em policy/envelope.yaml tem o resto)")
    return f"""usage: treasury.py status|balance|alive|demo
       treasury.py record  <cents> <category> <note>
       treasury.py correct <cents> <note>
       treasury.py kill    <reason>

`record` exige uma categoria porque e nela que o envelope decide, e um gasto
que ninguem classificou e recusado - falha fechado.

categorias: {tail}"""



def cli_record(argv):
    """`record <cents> <category> <note>`, and a refusal he can act on.

    The category used to be missing here entirely: the CLI called record()
    with cents and note only, so every negative amount was refused whatever
    the note said. That made every "paste-ready treasury.py record" line the
    agent has ever put in ASK.md non-functional, which is the whole of hard
    rule 1 - decide the spend, hand him one command. The agent found it by
    auditing its own request against the code; demo() had missed it for a day
    because it only ever called record() directly, with category= by hand.

    So the refusal path matters as much as the happy one. Whoever pastes this
    is at a terminal, not reading the envelope, and an error that says only
    "refused" sends them back here.
    """
    if len(argv) < 3:
        sys.exit(usage())
    try:
        cents = int(argv[0])
    except ValueError:
        sys.exit(f"first argument must be cents, got {argv[0]!r}\n\n{usage()}")
    category, note = argv[1], " ".join(argv[2:])
    try:
        print(record(cents, note, category=category))
    except Exception as exc:
        sys.exit(f"recusado: {exc}\n\ncategorias validas: "
                 f"{', '.join(categories()[0])}\n"
                 f"se nenhuma serve, o gasto nao esta previsto no envelope e "
                 f"quem decide isso e o dono, nao este comando.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":   print(status())
    elif cmd == "balance":print(balance())
    elif cmd == "alive":
        # Three outcomes, not two. 0 alive, 1 genuinely dead, 3 cannot tell -
        # run.sh must not treat "cannot tell" as a reason to end the business.
        try:
            sys.exit(0 if alive() else 1)
        except LedgerMissing as exc:
            print(f"cannot determine: {exc}", file=sys.stderr)
            sys.exit(3)
    elif cmd == "record": cli_record(sys.argv[2:])
    elif cmd == "correct":print(record(int(sys.argv[2]), " ".join(sys.argv[3:]), kind="correction"))
    elif cmd == "kill":   print(kill(" ".join(sys.argv[2:]) or "unspecified"))
    elif cmd == "demo":   demo()
    else: sys.exit(usage())
