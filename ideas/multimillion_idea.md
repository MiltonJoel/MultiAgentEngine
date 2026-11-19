# Multimillion idea: ShelfLife

## Iteration 1 — the pitch
An app that photographs your fridge and tells you what to cook before food spoils.
Households waste ~30% of the food they buy; save them money and they pay a subscription.

Problem: consumer subscriptions churn fast, and photo recognition of a messy fridge is unreliable.

## Iteration 2 — sharpen the customer
Sell to small restaurants instead. They track inventory on paper, food is 28–35% of their costs,
and spoilage is a direct hit to thin margins. A tablet app reads supplier invoices (structured text,
not fridge photos) and builds stock with expiry dates automatically.

Problem: restaurants won't type in what they used each day.

## Iteration 3 — close the loop
Connect to the point-of-sale system: every dish sold deducts its recipe ingredients from stock.
Invoices add stock, sales remove it, so inventory stays current with zero manual entry.
Each morning: "Use 4 kg chicken thighs by Friday — run the curry special," plus a suggested reorder.

First money estimate (naive, top-down): 10,000 restaurants × $99/month ≈ $11.9M a year.

Problem: this ignores who already sells this, how many restaurants cancel, and what each one costs to win.

## Iteration 4 — reality check
- **Competitors exist.** Invoice-scanning and inventory tools are already sold to restaurants
  (xtraCHEF, owned by Toast; MarketMan; Restaurant365). "Inventory software" alone is not new.
- **Wedge:** they are back-office accounting tools. ShelfLife sells one daily action for the chef —
  "cook this before it spoils" — and targets independents on Square, Clover and Lightspeed,
  where Toast does not bundle its own tool.
- **Restaurants close and cancel.** Assume 2.5% of customers leave every month (closures included),
  so the average customer stays 40 months.
- **Customers are not free.** Owners buy after a demo and a recipe-setup session; assume $1,200 to win one.
- **Price** moves to $149/month: competitors charge in that range, and $99 does not cover onboarding.
- **Recipe setup is the real cost.** Mapping 40 menu items to ingredients takes ~3 hours; done by us, included in CAC.

## Iteration 5 — bottom-up plan

Assumptions:
- Price: $149/month
- Gross margin: 80%
- Monthly churn: 2.5%
- CAC: $1,200

Unit economics (from the assumptions):
- Lifetime: 1 / 2.5% = 40 months
- LTV: $149 × 80% × 40 = $4,768
- LTV/CAC: 4.0 (healthy SaaS is ≥ 3)
- CAC payback: $1,200 / ($149 × 80%) ≈ 10 months

Paying restaurants at year end, and the ARR they produce (restaurants × $149 × 12):

| Year | Restaurants | ARR |
|------|-------------|-----|
| 1 | 60 | $107,280 |
| 2 | 400 | $715,200 |
| 3 | 1,500 | $2,682,000 |
| 4 | 3,500 | $6,258,000 |
| 5 | 6,000 | $10,728,000 |

Year 1 is a hand-sold pilot on one POS. Going from 3,500 to 6,000 means winning ~3,900 new
restaurants in year 5 (about 1,400 are lost to churn that year at 2.5%/month), which needs a sales team and
a listing in each POS app marketplace — that is the funding ask.

Realistic outcome: **$2.7M ARR in year 3, $10.7M ARR in year 5** — millions, but over five years, not one.

## Kill criteria
- Pilot (10 restaurants, 8 weeks) cuts food waste by less than 15% in kg → stop.
- Fewer than 3 of 10 pilot restaurants agree to pay $149/month afterwards → rethink price or customer.
- Measured CAC above $2,400 (LTV/CAC under 2) after 100 customers → stop scaling sales.

## Next step
Pilot with 10 local restaurants on Square; measure waste in kg before and after for 8 weeks.
