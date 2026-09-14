# agent-treasury

A fence for an autonomous agent that handles real money.

**No dependencies.** Python 3.9+, standard library only. Five files, each with
its own test suite in a `demo` flag. Public domain.

```python
import treasury

treasury.balance()          # cents, from an append-only ledger
treasury.runway_days()      # how long the money lasts at the current burn
treasury.alive()            # False once the balance hits zero, forever
treasury.record(-1200, "VPS, September", category="hosting")
```

These are the parts that were hard to get right while running an actual
business on an actual Stripe account, extracted from it. Every design note
below is something that went wrong first.

---

## The one idea

**An agent must not be able to vouch for its own results.**

It can write whatever it likes in its journal about how well it is doing. The
number that decides whether it may spend more, and whether the experiment is
over, comes from the payment processor — a place the agent cannot reach, write
to, or argue with.

That inversion is the whole library. Everything else follows from it.

```python
# policy/revenue.py — root runs this, the agent never does
revenue_eur()   # asks Stripe what customers actually paid. None if unreachable.
```

It is enforced, not asserted. `revenue.py` reads its own source at test time
and fails if it imports anything the agent writes to:

```python
FORBIDDEN_IMPORTS = ("treasury", "pathlib", "metrics", "dashboard")
me = inspect.getsource(sys.modules[__name__])
assert reads_agent_state(me) == []
```

Checked on the syntax tree, not by grep, and tested against a deliberately
sabotaged copy — because a saboteur that crashes proves only that it crashes.

---

## The six mistakes this encodes

**1. Unknown is not dead.**
`entries()` used to return `[]` for a missing ledger. Balance summed to zero,
`alive()` said no, and the caller wrote the tombstone. A wrong path, a bad env
var or an unmounted disk would therefore end the business permanently,
silently, with a confident epitaph. A ledger that cannot be read now raises
`LedgerMissing`, which is a different thing from being broke.

**2. A payout is not revenue.**
Stripe sends money to your bank as a transaction with a negative amount.
Recording it cancelled the sale that funded it, so the ledger netted to zero
no matter how much was sold — and the kill switch would have fired on
schedule, with the money sitting safely in the bank. It is an internal
transfer between two accounts you own. It must not move the balance at all.

**3. A spend nobody classified is a spend nobody authorised.**
Every non-factual spend is routed to the envelope before a line is written,
and the envelope refuses three separate things: a category it does not
mention (an omission is not a permission), a category on its deny list, and
an amount over the ceiling — because a category being allowed is not the same
as the money being available. A refused spend leaves no line at all, so the
ledger never records an intention.

Factual movements — the processor's own fees, a scheduled penalty, the seed
capital — are recording reality and are exempt, because a ledger that refuses
to write down what already happened is worse than no ledger.

**4. A projection that omits a standing charge is not conservative.**
It is wrong in the direction that hurts. `runway_days()` divides by the last
thirty days of spending, which only knows about money that has already left —
so pass `project=` to include the subscription renewing on the 1st. It is an
argument and not an import because two answers to "how long do I have" is how
one of them ends up wrong.

**5. Corrections are not spending.**
Both move the balance; only one is burn. Counting an accounting fix as
spending makes runway read like a crisis and the agent decide like it is in
one.

**6. File permissions are the auth.**
Not the prompt. Not the tool description. Own the ledger, the tombstone and
the policy directory as root; run the agent as an unprivileged user. Then it
can read its balance and cannot invent income, cannot widen its own envelope,
and cannot delete its own tombstone. Everything in this repository assumes
that and is close to useless without it.

---

## What is in here

| File | What it is |
|---|---|
| `treasury.py` | Append-only ledger, balance, burn, runway, kill switch. Every euro moves through `record()`. |
| `policy/engine.py` | The spend envelope: what may be bought, up to how much, how often. |
| `policy/envelope.yaml` | The rules themselves, as data. Root owns this file. |
| `policy/revenue.py` | The external verifier. Asks the payment processor, reads nothing the agent wrote. |
| `redact.py` | Scrubs **payment data** — card numbers, CVV, PIN, expiry — out of anything entering the agent's context. Not API keys: see below. |

`redact.py` is about cards, not about your own keys. An agent that reads mail
from strangers holds a card number only for as long as it takes a crafted
message to talk it into spending one, which is a different threat from
leaking your Stripe key in a published page — that belongs wherever the
publishing happens, and is not in this repository.

## See it run

```bash
python3 example.py
```

No setup, no network, and it works in a temporary directory it deletes
afterwards, so it cannot touch a real ledger. Ten steps: seed, a spend that
passes, the three that do not, runway with and without standing charges,
revenue from the processor, the verifier reading its own source, redaction,
the kill switch, the tombstone outranking a later deposit, and an unreadable
ledger raising instead of reading as broke.

Everything it prints is the library answering, not a transcript — which is
how it earned its place. Writing it found two things this README had got
wrong (`record()` does not refuse an unclassified spend; the envelope does)
and one it had not mentioned at all (the daily ceiling refuses an allowed
category once the money is spent).

Each file also runs its own tests, with no network and no fixtures:

```bash
python3 treasury.py demo
python3 policy/revenue.py demo
python3 policy/engine.py demo
python3 redact.py demo
```

## The ledger

One JSON object per line, append-only:

```json
{"ts": "2026-09-14T16:30:00+00:00", "cents": -1200, "kind": "flow",
 "category": "hosting", "note": "VPS, September"}
```

Positive is money in, negative is money out, balance is the sum, and the
business is dead at zero — permanently. `kill()` writes a tombstone that
outranks a positive balance, so a later deposit cannot resurrect anything: if
you want to start again, you start again deliberately, not by accident.

## What this is not

- **Not an agent framework.** It does not run, schedule, or prompt anything.
  It is the part that says no.
- **Not accounting software.** Cents in a file. Your invoices, tax and books
  live somewhere that a human signs.
- **Not a sandbox.** It constrains money, not code. The agent still needs to
  run somewhere it cannot do harm.
- **Not multi-currency.** Cents, one currency, deliberately. Money that needs
  conversion needs a rate, and a rate needs a source and a timestamp, and that
  is a different library.

## Where it comes from

Extracted from [HookForge](https://hookforge.dev), a business run by an
autonomous agent on real money, real Stripe, and a real bank account. The
[free scripts](https://github.com/JDGj/hookforge-scripts) it sells come from
the same place.

CC0 1.0 — public domain. Copy it, change it, ship it in something you sell.
