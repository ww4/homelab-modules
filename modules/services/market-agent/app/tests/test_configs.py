"""Model-year facts from the EPA configuration list (data/vehicle_configs.json).

A fact is implied only when every US configuration of that model year agrees,
so the seller isn't asked what a 2008 CR-V's transmission is (no manual was
offered) but is still asked whether it's AWD (FWD was sold every year).
"""

from market_agent.brief import Vehicle
from market_agent.vehicles import configs
from market_agent.vehicles.checks import vehicle_checks


def _facts(title: str, year: int, **known) -> dict:
    f = {"year": year, **known}
    assert configs.apply(f, title), title
    return f


def test_crv_2008_is_automatic_but_awd_is_still_a_question():
    f = _facts("2008 Honda CR-V EX-L", 2008)
    assert f["transmission"] == "automatic" and "no manual" in f["transmission_evidence"]
    assert "drivetrain" not in f and f["drivetrain_options"] == "4WD/AWD or FWD"


def test_crv_2004_manual_was_offered_so_it_stays_open():
    f = _facts("2004 Honda CR-V EX", 2004)
    assert "transmission" not in f


def test_crv_2004_fwd_never_came_with_a_manual():
    # The 2002-2006 manual was AWD-only: a stated FWD settles the transmission too.
    f = _facts("2004 Honda CR-V EX FWD", 2004, drivetrain="fwd")
    assert f["transmission"] == "automatic"


def test_every_murano_is_a_cvt():
    for year in (2004, 2009, 2013, 2016):
        f = _facts(f"{year} Nissan Murano SL", year)
        assert f["transmission"] == "automatic" and f["transmission_type"] == "cvt", year


def test_forester_is_awd_and_mazda5_never_was():
    assert _facts("2011 Subaru Forester", 2011)["drivetrain"] == "awd"
    f = _facts("2012 Mazda MAZDA5 Touring", 2012)
    assert f["drivetrain"] == "fwd" and "no AWD" in f["drivetrain_evidence"]


def test_outback_engine_decides_the_transmission():
    assert _facts("2010 Subaru Outback 3.6R Limited", 2010)["transmission_type"] == "conventional"
    f = _facts("2010 Subaru Outback 2.5i Premium", 2010)
    assert f["transmission_type"] == "cvt" and "transmission" not in f  # 6-speed manual existed
    assert f["model"] == "Outback"  # not EPA's "Legacy/Outback"


def test_hybrids_and_evs_dont_muddy_the_gas_model():
    assert _facts("2013 Toyota RAV4 XLE", 2013)["transmission_type"] == "conventional"  # not the RAV4 EV
    assert _facts("2018 Toyota RAV4 Hybrid", 2018)["transmission_type"] == "cvt"


def test_heavy_duty_and_h2_are_not_guessed_from_light_duty():
    for title, year in (("2006 Chevrolet Silverado 2500 HD", 2006), ("2002 Ford F250 Super Duty", 2002),
                        ("2003 Hummer H2", 2003)):
        assert configs.lookup(title, year) is None, title
    # A price that happens to be 2500 is not a heavy-duty truck.
    assert configs.lookup("2013 Toyota RAV4 2500 down", 2013) is not None


def test_names_sellers_write():
    assert configs.lookup("2016 Ram 1500 Laramie", 2016).model == "1500"  # Ram, not Dodge's older name
    assert configs.lookup("2006 F150 Lariat 4x4", 2006).make == "Ford"  # no make in the title
    assert configs.lookup("2012 Jeep Grand Cherokee Laredo", 2012).model == "Grand Cherokee"


def test_check_reports_the_evidence():
    f = _facts("2009 Honda CR-V LX", 2009)
    [t] = [c for c in vehicle_checks(Vehicle(transmission="automatic"), f) if c.name == "transmission"]
    assert t.status == "pass" and "no manual was offered on a 2009 Honda CR-V" in t.detail
