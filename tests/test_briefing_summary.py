"""extract_tldr_summary must never truncate mid-word.

Pins that the >300-char fallback is not a naive char slice, which would end an
Executive Summary mid-word (e.g. '…while equities ho…').
"""
from morning_engine.briefing import extract_tldr_summary


def test_summary_never_cuts_midword():
    # A long single bullet with NO sentence boundary, and a long word straddling
    # char 300, so a naive `result[:300]` slice would cut that word in half.
    filler = "word " * 56  # 280 chars incl. trailing space
    longword = "supercalifragilisticexpialidocious"  # 34 chars, spans position 300
    tldr = "## TL;DR\n\n- " + filler + longword + " tail end here"
    out = extract_tldr_summary(tldr)
    assert out is not None and out.endswith("…")
    core = out[:-1].rstrip()
    # the long word is never sliced: it is either wholly present or wholly absent
    assert "supercalifragilistic" not in core or core.endswith(longword)
    # and the kept text ends on a real word, not a fragment
    assert core.split()[-1] in (filler + longword + " tail end here").split()


def test_summary_breaks_on_sentence_when_past_threshold():
    # A first sentence longer than the 80-char floor, ending in '. ', then filler.
    first = "The broad dollar index is climbing today and squeezing every commodity priced in dollars hard."
    tldr = "## TL;DR\n\n- " + first + " " + ("filler " * 40)
    out = extract_tldr_summary(tldr)
    assert out == first  # cut exactly at the sentence boundary, no ellipsis


def test_short_summary_is_unchanged():
    tldr = "## TL;DR\n\n- A short single clear line about the dollar and metals today.\n"
    out = extract_tldr_summary(tldr)
    assert out == "A short single clear line about the dollar and metals today."
