"""The rules a judge's score is held to (market_agent.policy)."""
from market_agent import brief as briefs
from market_agent.models import Candidate, Check, Evaluation, Listing, Signal
from market_agent.policy import effective, rank


def _brief():
    return briefs.Brief.model_validate({
        "name": "w", "title": "Winter car", "kind": "vehicle", "location": {"home": "Springfield, IL"},
        "price": {"min": 2500, "max": 6000}, "search": {"queries": ["x"]},
        "vehicle": {"drivetrain": ["awd", "4wd"], "title": "clean"},
    })


def _c(score, verdict="consider", scam="low", seller="private", checks=(), signals=(), title="2009 Honda CR-V", lid="1"):
    return Candidate(
        listing=Listing(id=lid, title=title, price_text="$4,500", price=4500),
        checks=list(checks), signals=list(signals),
        evaluation=Evaluation(score=score, verdict=verdict, headline="h", scam_risk=scam, seller_kind=seller),
    )


def test_verdict_follows_the_score_not_the_judge():
    assert effective(_c(4.0, verdict="consider"), _brief()).verdict == "skip"
    assert effective(_c(5.0), _brief()).verdict == "consider"
    assert effective(_c(7.5, verdict="consider"), _brief()).verdict == "top"


def test_scam_risk_caps():
    assert effective(_c(4.5, scam="medium"), _brief()).score == 3.0
    assert effective(_c(6.0, scam="high"), _brief()).verdict == "skip"


def test_down_payment_and_financing_ads_are_capped():
    sig = Signal(name="dealer or flipper", detail="offers financing; quotes a down payment")
    eff = effective(_c(4.0, signals=[sig]), _brief())
    assert eff.score == 3.0 and eff.verdict == "skip"
    assert effective(_c(5.0, title="2013 Toyota RAV4 2500 down payment"), _brief()).verdict == "skip"


def test_dealer_can_never_be_top():
    sig = Signal(name="dealer or flipper", detail="names a dealership or stock number")
    assert effective(_c(8.5, signals=[sig]), _brief()).verdict == "consider"


def test_unconfirmed_awd_is_not_a_candidate():
    # The 2002 S10 crew cab: scored 5.5 by the judge, drivetrain never established.
    unknown = Check(name="drivetrain", status="unknown", detail="not established: listing doesn't say")
    eff = effective(_c(5.5, checks=[unknown], title="2002 Chevrolet S10 Crew Cab LS"), _brief())
    assert eff.verdict == "confirm" and "drivetrain" in eff.confirm[0]
    # ...but an unconfirmed title is a question, not a reason to hide the car.
    title_unknown = Check(name="title", status="unknown", detail="title status not stated")
    assert effective(_c(6.0, checks=[title_unknown]), _brief()).verdict == "consider"


def test_rank_orders_by_effective_verdict():
    good = _c(6.0, lid="good")
    scam = _c(6.5, scam="medium", lid="scam")
    assert [c.listing.id for c in rank([scam, good], _brief())] == ["good", "scam"]
