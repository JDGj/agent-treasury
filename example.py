#!/usr/bin/env python3
"""The whole fence in one run. `python3 example.py` -- no setup, no network.

It works in a temporary directory and deletes it afterwards, so it cannot touch
a real ledger. Everything it prints is the library actually answering, not a
transcript: if a behaviour here changes, this output changes with it.
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "policy"))

import engine
import redact
import revenue
import treasury


def say(step, text):
    print(f"\n\033[1m{step}\033[0m  {text}" if sys.stdout.isatty()
          else f"\n{step}  {text}")


def main():
    home = Path(tempfile.mkdtemp(prefix="agent-treasury-"))
    ledger, dead, journal = home / "ledger.jsonl", home / "DEAD", home / "JOURNAL.md"

    # The real gate: policy/engine.py reading policy/envelope.yaml. An earlier
    # version of this file stubbed it out with an always-yes lambda "because
    # this file is about the ledger" -- and then step 3 below, which exists to
    # show an unclassified spend being refused, printed the spend going
    # through. The refusal does not live in record(); record() routes every
    # non-factual spend TO the envelope, and the envelope is what says no.
    # Stub the gate and you have stubbed out the thing being demonstrated.
    ok = lambda category, **kw: engine.check(category, **kw)

    try:
        say("1.", "Seed capital. Positive cents is money in.")
        treasury.record(5000, "seed capital", ledger, category="seed", gate=ok)
        print(f"    balance: {treasury.balance(ledger) / 100:.2f} EUR")

        say("2.", "A classified spend inside the envelope goes through.")
        treasury.record(-1200, "VPS, September", ledger, category="hosting", gate=ok)
        print(f"    balance: {treasury.balance(ledger) / 100:.2f} EUR")

        say("3.", "Three ways a spend is refused. All of them fail closed.")
        for cents, category, why in (
                (-500, None, "no category at all"),
                (-500, "paid_ads", "a category the envelope denies"),
                (-900, "domains", "allowed category, over the daily ceiling")):
            try:
                treasury.record(cents, "something", ledger, category=category, gate=ok)
                print(f"    {why}: went through, which is a bug")
            except Exception as exc:
                print(f"    {why}:\n      {exc}")
        print(f"    balance unchanged: {treasury.balance(ledger) / 100:.2f} EUR")
        print("    nothing was written. A refused spend leaves no line in the")
        print("    ledger, so the ledger never records an intention.")
        print("    The third one is the one people forget: the category being")
        print("    allowed is not the same as the money being available.")

        say("4.", "Runway. The plain answer, and the honest one.")
        plain = treasury.runway_days(ledger)
        # A subscription renewing on the 1st is as certain as anything already
        # spent, and the last thirty days do not know about it.
        with_standing = treasury.runway_days(
            ledger, project=lambda b, burn: b / (burn / 30.0 + 100.0))
        print(f"    on burn alone            : {plain:.0f} days")
        print(f"    counting 1.00 EUR/day fixed: {with_standing:.0f} days")
        print("    the second one is the one to plan against")

        say("5.", "Revenue comes from the processor, never from the agent.")
        print(f"    no key configured -> {revenue.revenue_eur('', None)!r}"
              "   (None, not 0.00 -- unknown is not zero)")
        charge = {"id": "txn_1", "type": "charge", "net": 4753, "currency": "eur"}
        payout = {"id": "txn_2", "type": "payout", "net": -4753, "currency": "eur"}
        print(f"    one 47.53 sale           -> {revenue.external_cents([charge]) / 100:.2f} EUR")
        print(f"    the same sale, then paid out to the bank -> "
              f"{revenue.external_cents([charge, payout]) / 100:.2f} EUR")
        print("    a payout is the business moving its own money. Counting it")
        print("    would have netted the ledger to zero however much was sold.")

        say("6.", "And the verifier cannot read what the agent writes.")
        import inspect
        me = inspect.getsource(sys.modules["revenue"])
        print(f"    forbidden imports found in revenue.py: {revenue.reads_agent_state(me)}")
        sabotaged = "import treasury\nx = 1"
        print(f"    in a sabotaged copy: {revenue.reads_agent_state(sabotaged)}")

        say("7.", "Payment data never reaches the agent's context.")
        leak = ("Cliente diz: paguei com o cartao 4242 4242 4242 4242, "
                "CVV 737, validade 03/28")
        clean, hidden = redact.scrub(leak)
        print(f"    {hidden} value(s) removed")
        print(f"    {clean}")
        print("    This is about CARDS, not API keys. An agent that reads mail")
        print("    from strangers holds a card number only for as long as it")
        print("    takes a crafted message to talk it into spending one.")
        print("    Keeping your own keys out of published output is a different")
        print("    job, done where the publishing happens.")

        say("8.", "Spend the rest. At zero, the business is dead.")
        # category="penalty" because the envelope would refuse 38 EUR in one
        # go, and rightly. A penalty is a factual movement -- it records
        # something that already happened -- so it is exempt from the gate,
        # which is the only honest way to drain an account in an example.
        treasury.record(-3800, "the last of it", ledger, category="penalty", gate=ok)
        print(f"    balance: {treasury.balance(ledger) / 100:.2f} EUR"
              f"  alive: {treasury.alive(ledger, dead)}")
        treasury.kill("balance reached zero", ledger, dead, journal)
        print(f"    tombstone written: {dead.name}")

        say("9.", "And the tombstone outranks a later deposit.")
        treasury.record(9999, "someone tops it up", ledger, category="seed", gate=ok)
        print(f"    balance: {treasury.balance(ledger) / 100:.2f} EUR"
              f"  alive: {treasury.alive(ledger, dead)}")
        print("    starting again is a decision, never an accident.")

        say("10.", "An unreadable ledger is UNKNOWN, not broke.")
        ledger.unlink()
        try:
            treasury.entries(ledger)
            print("    ...it returned something, which is the bug this prevents")
        except treasury.LedgerMissing as exc:
            print(f"    raised {type(exc).__name__}: {exc}")
        print("    returning [] here summed to zero, said 'dead', and wrote a")
        print("    confident epitaph for a business whose disk had not mounted.")

        print(f"\nThe ledger, line by line:\n")
        # Re-read from the tombstone-era copy: the file was unlinked above.
        for line in (home / "DEAD").read_text().splitlines()[:3]:
            print(f"    {line}")

    finally:
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    main()
