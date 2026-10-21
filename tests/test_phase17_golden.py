"""Phase 17 golden values (PLAN 17e, D-056): two companies per new industry module, on recorded
SEC data. Every expected number is a fact as filed in the company's 10-K or 10-Q XBRL (USD
millions unless noted); the tests check tag choice, dimensions, formulas and fiscal periods."""

import pytest

from tests.test_phase16_golden import _check

FY25 = ("FY", 2025, 0)


@pytest.mark.usefixtures("sec_routes")
def test_chemicals_dow_fy2025(make_sec_client) -> None:
    """DOW 10-K FY2025: capital expenditures 2,479 (PaymentsToAcquireProductiveAssets), D&A
    2,834. The environmental accrual at 2025-12-31, 1,011, is tagged only as the prior-year-end
    balance in the Q1 2026 10-Q."""
    _check(make_sec_client, "DOW", FY25, {
        "capex_to_depreciation": 2_479 / 2_834,
        "environmental_reserve": 1_011e6,
    })  # fmt: skip


@pytest.mark.usefixtures("sec_routes")
def test_chemicals_lyondellbasell_fy2025(make_sec_client) -> None:
    """LYB 10-K FY2025: environmental accrual 178, capital expenditures 1,878, D&A 1,390."""
    _check(make_sec_client, "LYB", FY25, {
        "environmental_reserve": 178e6,
        "capex_to_depreciation": 1_878 / 1_390,
    })  # fmt: skip


@pytest.mark.usefixtures("sec_routes")
def test_mining_newmont_fy2025(make_sec_client) -> None:
    """NEM 10-K FY2025: asset retirement obligations 6,800 (829 current + 5,971 non-current),
    environmental accrual 390, capital expenditures 3,035, D&A 2,521."""
    _check(make_sec_client, "NEM", FY25, {
        "asset_retirement_obligation": 6_800e6,
        "environmental_reserve": 390e6,
        "capex_to_depreciation": 3_035 / 2_521,
    })  # fmt: skip


@pytest.mark.usefixtures("sec_routes")
def test_mining_freeport_fy2025(make_sec_client) -> None:
    """FCX 10-K FY2025: asset retirement obligations 3,836, environmental accrual 2,018,
    capital expenditures 4,494, D&A 2,244."""
    _check(make_sec_client, "FCX", FY25, {
        "asset_retirement_obligation": 3_836e6,
        "environmental_reserve": 2_018e6,
        "capex_to_depreciation": 4_494 / 2_244,
    })  # fmt: skip


@pytest.mark.usefixtures("sec_routes")
def test_metals_alcoa_fy2025(make_sec_client) -> None:
    """AA 10-K FY2025: environmental accrual 282 (76 current + 206 non-current), capital
    expenditures 618, D&A 623."""
    _check(make_sec_client, "AA", FY25, {
        "environmental_reserve": 282e6,
        "capex_to_depreciation": 618 / 623,
    })  # fmt: skip


@pytest.mark.usefixtures("sec_routes")
def test_metals_cleveland_cliffs_fy2025(make_sec_client) -> None:
    """CLF 10-K FY2025 (grouped with steel by universe.yaml): environmental accrual 112,
    capital expenditures 561, D&A 1,235."""
    _check(make_sec_client, "CLF", FY25, {
        "environmental_reserve": 112e6,
        "capex_to_depreciation": 561 / 1_235,
    })  # fmt: skip


@pytest.mark.usefixtures("sec_routes")
def test_homebuilders_dr_horton_fy2025(make_sec_client) -> None:
    """DHI 10-K FY ended 2025-09-30: inventories 25,287.3, homes under construction 7,648.5,
    stockholders' equity 24,190.4, inventory and land impairments 33.0. Land and lots is the
    inventory other than homes under construction (D-056)."""
    land = 25_287.3 - 7_648.5
    _check(make_sec_client, "DHI", FY25, {
        "homebuilding_inventory": 25_287.3e6,
        "land_inventory": land * 1e6,
        "land_to_equity": land / 24_190.4,
        "inventory_impairments": 33e6,
    })  # fmt: skip


@pytest.mark.usefixtures("sec_routes")
def test_homebuilders_toll_brothers_fy2025(make_sec_client) -> None:
    """TOL 10-K FY ended 2025-10-31: inventory 10,678.460, homes under construction 2,535.219,
    stockholders' equity 8,270.663, impairments 65.914."""
    land = 10_678.460 - 2_535.219
    _check(make_sec_client, "TOL", FY25, {
        "homebuilding_inventory": 10_678.460e6,
        "land_inventory": land * 1e6,
        "land_to_equity": land / 8_270.663,
        "inventory_impairments": 65.914e6,
    })  # fmt: skip


@pytest.mark.usefixtures("sec_routes")
def test_healthcare_hca_fy2025(make_sec_client) -> None:
    """HCA 10-K FY2025: salaries and benefits 32,859, revenues 75,600."""
    _check(make_sec_client, "HCA", FY25, {"labor_to_revenue": 32_859 / 75_600})


@pytest.mark.usefixtures("sec_routes")
def test_healthcare_universal_health_fy2025(make_sec_client) -> None:
    """UHS 10-K FY2025: salaries, wages and benefits 8,084.582, net revenues 17,364.829 (tagged
    as contract revenue, the second revenue candidate)."""
    _check(make_sec_client, "UHS", FY25, {
        "revenue": 17_364.829e6,
        "labor_to_revenue": 8_084.582 / 17_364.829,
    })  # fmt: skip


@pytest.mark.usefixtures("sec_routes")
def test_gaming_mgm_fy2025(make_sec_client) -> None:
    """MGM 10-K FY2025: casino revenue 9,450.887 (contract revenue, ProductOrServiceAxis =
    CasinoMember, from the XBRL instance), total revenues 17,537.683."""
    _check(make_sec_client, "MGM", FY25, {"casino_revenue_share": 9_450.887 / 17_537.683})


@pytest.mark.usefixtures("sec_routes")
def test_gaming_las_vegas_sands_q2_2026(make_sec_client) -> None:
    """LVS 10-Q Q2 2026: casino revenue 2,341 (CasinoMember), total revenues 3,154."""
    _check(make_sec_client, "LVS", ("Q", 2026, 2), {"casino_revenue_share": 2_341 / 3_154})


@pytest.mark.usefixtures("sec_routes")
def test_restaurants_mcdonalds_fy2025(make_sec_client) -> None:
    """MCD 10-K FY2025: revenues from franchised restaurants 16,548 (its own tag,
    company_overrides/MCD.yaml), total revenues 26,885."""
    _check(make_sec_client, "MCD", FY25, {"franchise_revenue_share": 16_548 / 26_885})


@pytest.mark.usefixtures("sec_routes")
def test_restaurants_starbucks_q3_fy2026(make_sec_client) -> None:
    """SBUX 10-Q quarter ended 2026-06-28 (fiscal 2026 Q3): licensed-store revenue 1,200.8
    (FranchisorDisclosureAxis = FranchisedUnitsMember; the 10-K does not tag it), total net
    revenues 9,322.7, store operating labor 2,457.8."""
    _check(make_sec_client, "SBUX", ("Q", 2026, 3), {
        "franchise_revenue_share": 1_200.8 / 9_322.7,
        "labor_to_revenue": 2_457.8 / 9_322.7,
    })  # fmt: skip


@pytest.mark.usefixtures("sec_routes")
def test_clinical_stage_viking_q2_2026(make_sec_client) -> None:
    """VKTX 10-Q Q2 2026 (trait clinical_stage, D-058): cash 125.750, available-for-sale
    securities (current) 375.913; operating cash flow FY2025 -278.685, first half 2025 -99.392,
    first half 2026 -239.148, so the last four quarters -418.441; Q2 R&D 115.754 of operating
    expenses 132.597."""
    four_quarters = -278.685 + 99.392 - 239.148
    _check(make_sec_client, "VKTX", ("Q", 2026, 2), {
        "monthly_cash_burn": -four_quarters / 12 * 1e6,
        "cash_runway_months": (125.750 + 375.913) / (-four_quarters / 12),
        "rnd_to_opex": 115.754 / 132.597,
    })  # fmt: skip


@pytest.mark.usefixtures("sec_routes")
def test_clinical_stage_xenon_q2_2026_latest_quarter_burn(make_sec_client) -> None:
    """XENE 10-Q Q2 2026: its Q4 2025 operating cash flow is N/M (restated year), so the burn is
    the latest quarter's: first half 2026 -184.287 less Q1 -88.897 = -95.390. Cash 119.527,
    marketable securities 811.159; Q2 R&D 99.224 of operating expenses 123.155."""
    quarter = -184.287 + 88.897
    _check(make_sec_client, "XENE", ("Q", 2026, 2), {
        "monthly_cash_burn": -quarter / 3 * 1e6,
        "cash_runway_months": (119.527 + 811.159) / (-quarter / 3),
        "rnd_to_opex": 99.224 / 123.155,
    })  # fmt: skip
