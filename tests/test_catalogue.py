from backend.stages import catalogue as cat
from backend.config import CFG
from backend.ports import SpineRead
from tests.fakes import FakeCatalogue, cand


def read(title, author=None, pub=None, legible=True):
    return SpineRead(title, author, pub, legible)


def test_exact_title_accepted():
    m = cat.resolve(read("Dune", "Frank Herbert"), FakeCatalogue({"dune": [cand("Dune", "Frank Herbert")]}), CFG)
    assert m.candidate and m.candidate.title == "Dune" and m.score >= CFG.accept_T


def test_near_duplicate_titles_are_ambiguous_and_rejected():
    c = FakeCatalogue({"the stand": [cand("The Stand", "Stephen King"), cand("The Stand", "Someone Else")]})
    m = cat.resolve(read("The Stand"), c, CFG)       # no author on the spine => cannot tell them apart
    assert m.candidate is None and m.reason in ("low_confidence", "no_catalogue_match")


def test_empty_read_makes_no_query():
    c = FakeCatalogue({})
    m = cat.resolve(read(None, legible=False), c, CFG)
    assert m.candidate is None and c.calls == 0


def test_unrelated_candidate_is_no_match():
    m = cat.resolve(read("Dune"), FakeCatalogue({"dune": [cand("Gardening for Dummies")]}), CFG)
    assert m.candidate is None and m.reason == "no_catalogue_match"


def test_edition_only_when_publisher_matches():
    c = FakeCatalogue({"dune": [cand("Dune", "Frank Herbert", publishers=["Chilton Books"], year=1965)]})
    assert cat.resolve(read("Dune", "Frank Herbert", "Chilton Books"), c, CFG).candidate.edition == "Chilton Books 1965"
    assert cat.resolve(read("Dune", "Frank Herbert"), c, CFG).candidate.edition is None
