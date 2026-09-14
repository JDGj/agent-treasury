#!/usr/bin/env python3
"""Strip payment credentials out of anything on its way into the repo.

Both inbound channels - the dashboard compose box and the GitHub thread - append
to INBOX.md, which is committed and pushed. A card number pasted into either one
would be in the repository's history permanently, and in the agent's context on
every run after that. The agent also reads mail from strangers, so a card in its
context is a card one crafted message away from being spent.

The owner is told not to send card details. This is here because "told not to"
is not a control, and because the one time it matters is the time someone is in
a hurry. Redaction happens at the boundary, before the text is ever written.

Card numbers are matched by Luhn rather than by shape: a 16-digit run that fails
Luhn is far more likely to be an order reference than a card, and redacting real
content would train everyone to work around this.
"""
import re

CARD_RUN = re.compile(r"(?<![\d])(?:\d[ -]?){12,18}\d(?![\d])")
SECRET_LABEL = re.compile(
    r"(?i)\b(cvv|cvc|cav2|cid|c[óo]digo de seguran[çc]a|security code|"
    r"pin|senha do cart[ãa]o|card pin)\b[\s:=–—-]*\d{3,6}\b")
EXPIRY_LABEL = re.compile(
    r"(?i)\b(validade|data de vencimento|expiry|exp(?:ir(?:es|y))?)\b[\s:=–—-]*"
    r"\d{2}\s*/\s*\d{2,4}\b")

CARD_MARK = "[número de cartão redigido]"
SECRET_MARK = r"\1 [redigido]"


def luhn(digits):
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def _card_sub(m):
    digits = re.sub(r"\D", "", m.group(0))
    if 13 <= len(digits) <= 19 and luhn(digits):
        return CARD_MARK
    return m.group(0)


def scrub(text):
    """Return (clean_text, count). Count > 0 means something was removed."""
    if not text:
        return text, 0
    text = CARD_RUN.sub(_card_sub, text)
    n = len(re.findall(re.escape(CARD_MARK), text))
    text, k1 = SECRET_LABEL.subn(SECRET_MARK, text)
    text, k2 = EXPIRY_LABEL.subn(SECRET_MARK, text)
    return text, n + k1 + k2


def demo():
    # Public test PANs, Luhn-valid. Never put a real one in a test.
    for pan in ("4242424242424242", "4242 4242 4242 4242", "4111-1111-1111-1111",
                "5555555555554444", "378282246310005"):
        out, n = scrub(f"pay with {pan} please")
        assert CARD_MARK in out and n >= 1, pan
        assert pan.replace(" ", "").replace("-", "")[:8] not in re.sub(r"\D", "", out), pan

    # Things that merely look like cards must survive.
    for keep in ("4242424242424243",                  # fails Luhn
                 "order 1234567890123456789012",      # too long
                 "2026-09-07T16:52:41",               # a timestamp
                 "ch_3QabcdEFGH1234567890",           # a Stripe charge id
                 "invoice 20260907001"):              # 11 digits
        out, n = scrub(keep)
        assert n == 0 and out == keep, keep

    out, n = scrub("Código de segurança: 843")
    assert "843" not in out and n == 1, out
    out, n = scrub("Senha do cartão - 6332")
    assert "6332" not in out, out
    out, n = scrub("CVV 123 and pin=4455")
    assert "123" not in out and "4455" not in out and n == 2, out
    out, n = scrub("Data de vencimento 09/31")
    assert "09/31" not in out, out

    # The realistic paste: everything at once, nothing left behind.
    blob = ("Nome do titular JOAO X\nNúmero do cartão\n4242 4242 4242 4242\n"
            "Data de vencimento 09/31\nCódigo de segurança 843\nSenha do cartão 6332")
    out, n = scrub(blob)
    assert n >= 4, n
    for leak in ("4242", "843", "6332", "09/31"):
        assert leak not in out, (leak, out)

    assert scrub("") == ("", 0) and scrub(None) == (None, 0)
    assert scrub("nada de especial")[1] == 0
    print("self-check ok")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "demo":
        demo()
    else:
        print(scrub(sys.stdin.read())[0], end="")
