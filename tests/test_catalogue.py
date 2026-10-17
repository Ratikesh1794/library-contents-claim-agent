"""Strict identity: a book's title / author / edition / ISBN are recorded only when the spine confirms them
against a catalogue record. Anything unconfirmed stays None. Never a guess."""
from backend.config import CFG
from backend.ports import SpineRead
from backend.stages import catalogue as cat
from tests.fakes import FakeCatalogue, cand


def read(title, author=None, pub=None, legible=True):
    return SpineRead(title, author, pub, legible)


def resolve(r, records):
    c = FakeCatalogue({(r.title or "").lower(): records})
    return cat.resolve(r, c, CFG), c


def test_title_and_author_both_matching_is_identified():
    m, _ = resolve(read("Dune", "Frank Herbert"), [cand("Dune", "Frank Herbert")])
    assert m.candidate and (m.candidate.title, m.candidate.author) == ("Dune", "Frank Herbert") and m.reason is None


def test_no_author_on_the_spine_stays_undefined_and_makes_no_call():
    m, c = resolve(read("Dune"), [cand("Dune", "Frank Herbert")])
    assert m.candidate is None and m.reason == "author_not_visible" and c.calls == 0


def test_a_contained_title_is_not_a_match():
    m, _ = resolve(read("Dune", "Herbert"), [cand("Dune Messiah", "Frank Herbert")])
    assert m.candidate is None


def test_wrong_author_is_not_a_match():
    m, _ = resolve(read("Dune", "Jane Austen"), [cand("Dune", "Frank Herbert")])
    assert m.candidate is None


def test_surname_only_truncated_author_and_main_title_still_match():
    assert resolve(read("Dune", "HERBERT"), [cand("Dune", "Frank Herbert")])[0].candidate
    assert resolve(read("How to Win Friends and Influence People", "DALE CARNEG"),
                   [cand("How to Win Friends and Influence People", "Dale Carnegie")])[0].candidate
    assert resolve(read("Sapiens", "Harari"), [cand("Sapiens: A Brief History of Humankind", "Yuval Noah Harari")])[0].candidate


def test_two_matching_records_that_are_different_books_stay_undefined():
    m, _ = resolve(read("Emma", "Austen"), [cand("Emma", "Jane Austen"), cand("Emma", "Jane Austen Leigh")])
    assert m.candidate is None and m.reason == "ambiguous_match"
    same, _ = resolve(read("Dune", "Herbert"), [cand("Dune", "Frank Herbert"), cand("Dune", "Herbert, Frank")])
    assert same.candidate                                                     # one book written two ways is fine


def test_edition_year_and_isbn_only_when_verified():
    one_edition = cand("Dune", "Frank Herbert", isbn="9780441013593", publishers=["Ace"], year=2005, edition_level=True)
    m, _ = resolve(read("Dune", "Frank Herbert", "Ace"), [one_edition])
    assert (m.candidate.edition, m.candidate.year, m.candidate.isbn) == ("Ace 2005", 2005, "9780441013593")
    m, _ = resolve(read("Dune", "Frank Herbert"), [one_edition])               # publisher not visible on the spine
    assert (m.candidate.edition, m.candidate.year, m.candidate.isbn) == (None, None, None)
    merged = cand("Dune", "Frank Herbert", isbn="0441172717", publishers=["Ace"], year=1965)   # work-level record
    m, _ = resolve(read("Dune", "Frank Herbert", "Ace"), [merged])
    assert m.candidate.edition == "Ace" and m.candidate.isbn is None and m.candidate.year is None


def test_illegible_spine_makes_no_call():
    m, c = resolve(read(None, legible=False), [cand("Dune", "Frank Herbert")])
    assert m.candidate is None and c.calls == 0


def test_one_work_whatever_the_catalogue_does_to_the_authors_name():
    from backend.stages.catalogue import work_key
    from tests.fakes import cand
    keys = {work_key(cand("The Millionaire Fastlane", a)) for a in ("M. J. DeMarco", "MJ DeMarco", "DeMarco, M. J.", "MJ DeMarco ")}
    assert len(keys) == 1
    assert work_key(cand("Dune", "Frank Herbert")) == work_key(cand("Dune", "Herbert, Frank"))
    assert work_key(cand("Dune", "Frank Herbert")) != work_key(cand("Dune Messiah", "Frank Herbert"))     # a different work
    assert work_key(cand("Emma", "Jane Austen")) != work_key(cand("Emma", "Emma Thompson"))               # a different author
