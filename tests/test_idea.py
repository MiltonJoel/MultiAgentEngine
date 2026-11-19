"""The multimillion idea lives in ideas/, is iterated, and its numbers are realistic and consistent."""

import re
from pathlib import Path

IDEA = Path(__file__).resolve().parent.parent / "ideas" / "multimillion_idea.md"
TEXT = IDEA.read_text()


def _num(s):
    return float(s.replace(",", "").replace("$", ""))


def _assumption(label):
    return _num(re.search(rf"^- {label}: \$?([\d,.]+)", TEXT, re.MULTILINE).group(1))


def test_iterations_are_numbered_in_order():
    its = [int(n) for n in re.findall(r"^## Iteration (\d+)", TEXT, re.MULTILINE)]
    assert len(its) >= 4 and its == list(range(1, len(its) + 1))


def test_reality_check_covers_competition_churn_and_cac():
    low = TEXT.lower()
    for word in ("competitor", "churn", "cac", "kill criteria"):
        assert word in low, word


def test_unit_economics_hold_up():
    price, margin = _assumption("Price"), _assumption("Gross margin") / 100
    churn, cac = _assumption("Monthly churn") / 100, _assumption("CAC")
    ltv = price * margin / churn
    assert ltv / cac >= 3
    assert f"${ltv:,.0f}" in TEXT  # stated LTV matches the assumptions


def test_arr_table_matches_price_and_reaches_millions_gradually():
    price = _assumption("Price")
    rows = re.findall(r"^\| (\d+) \| ([\d,]+) \| (\$[\d,]+) \|$", TEXT, re.MULTILINE)
    assert len(rows) >= 3
    customers = [_num(c) for _, c, _ in rows]
    arr = [_num(a) for *_, a in rows]
    for c, a in zip(customers, arr):
        assert a == c * price * 12
    assert customers == sorted(customers)
    assert arr[0] < 1_000_000 <= arr[-1]  # not millions on day one


def test_growth_sentence_accounts_for_churn():
    churn = _assumption("Monthly churn") / 100
    m = re.search(
        r"from ([\d,]+) to ([\d,]+) means winning ~([\d,]+) new\s+restaurants.*?about ([\d,]+) are lost to churn",
        TEXT,
        re.DOTALL,
    )
    start, end, new, lost = (_num(g) for g in m.groups())
    assert abs(lost - (start + end) / 2 * churn * 12) <= 0.15 * lost
    assert abs(new - (end - start + lost)) <= 0.15 * new
