#!/usr/bin/env python3
"""
daily_learning.py — the daily curriculum behind THE DAILY SIGNAL.

Five tracks, all rotating on the IST date so the set is fixed for the day and
changes at midnight:

  interview_tech   CFO-track technical questions — retail, IFRS, Gulf
  interview_soft   the non-technical questions that actually decide the offer
  spanish          word or phrase, English ↔ Spanish
  english          vocabulary + a spoken-delivery drill
  father           one thing to do with a 7-month-old, and why
  wisdom           Jainism and Buddhism, applied rather than admired

Content is authored, not generated: a wrong IFRS 16 answer in an interview
costs more than the effort of writing it down correctly. Selection is a
deterministic function of the date, so every device shows the same set.

Rotation is offset per track by a different stride, so two tracks never move
in lockstep and the pairings stay fresh.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


def _today() -> date:
    return datetime.now(IST).date()


def _pick(bank: list, n: int, stride: int, day: date | None = None) -> list:
    """n items from bank, rotating by date. Walks the list by `stride` so the
    daily pair is not simply adjacent entries."""
    if not bank:
        return []
    d = day or _today()
    base = d.toordinal() * n
    out, seen = [], set()
    i = 0
    while len(out) < min(n, len(bank)) and i < len(bank) * 3:
        idx = (base + i * stride) % len(bank)
        if idx not in seen:
            seen.add(idx)
            out.append(bank[idx])
        i += 1
    return out


# ═══════════════════════════════════════════════════════════════════════
# INTERVIEW — TECHNICAL
# Q, A, and who asks it. Weighted to retail, the Gulf, and the controller →
# CFO transition, because that is the actual target.
# ═══════════════════════════════════════════════════════════════════════

INTERVIEW_TECH = [
    ("Walk me through how a $1M inventory write-down flows through all three statements.",
     "P&L: COGS up $1M, so gross profit, EBIT and pre-tax income each fall $1M. At a 25% rate, "
     "net income falls $750K. Cash flow: start from net income −$750K, add back the $1M non-cash "
     "write-down, so operating cash flow rises $250K — the tax shield. Balance sheet: inventory "
     "−$1M, cash +$250K, so assets fall $750K; retained earnings fall $750K. It balances. "
     "The point they are testing: you know the write-down is non-cash and that the only real cash "
     "effect is tax.",
     "Amazon · Walmart · Majid Al Futtaim"),

    ("A store does AED 12M revenue at 38% gross margin. Rent is AED 1.8M, staff AED 2.1M, other "
     "fixed AED 900K. Is it worth keeping?",
     "Gross profit = 12 × 0.38 = AED 4.56M. Fixed costs = 1.8 + 2.1 + 0.9 = AED 4.8M. Store "
     "contribution = −AED 240K. It loses money — but do not close it on that number alone. Ask "
     "three things: is the rent a committed lease you pay whether or not you trade; what does the "
     "store contribute to online fulfilment and brand presence in that catchment; and what is the "
     "exit cost. A negative-contribution store with four years left on a non-cancellable lease is "
     "often cheaper to run than to close.",
     "Landmark Group · Alshaya · Inditex"),

    ("How does IFRS 16 change the picture for a retailer with 60 leases?",
     "Operating leases come on balance sheet as a right-of-use asset and a lease liability. "
     "Rent expense is replaced by depreciation plus interest, so EBITDA rises — often materially "
     "for retail — while EBIT moves little. Interest is front-loaded, so early-life leases depress "
     "net income versus the old treatment. Watch three consequences: gearing ratios jump and can "
     "breach covenants written pre-IFRS 16; EBITDA-based multiples are no longer comparable to "
     "history; and store-level return metrics need restating or you will misjudge performance.",
     "Al-Futtaim · Carrefour MAF · H&M"),

    ("What is the cash conversion cycle for a fashion retailer, and how would you cut it 15 days?",
     "CCC = DIO + DSO − DPO. Fashion retail is inventory-heavy and near-zero DSO, so it is mostly "
     "DIO minus DPO. To cut 15 days: shorten the buying calendar and increase in-season "
     "replenishment so you commit later; push slow lines to markdown earlier rather than holding "
     "for full price; renegotiate supplier terms from 60 to 90 days in exchange for volume "
     "commitment; and consolidate SKUs — every incremental SKU carries safety stock. "
     "Quantify it: on AED 400M COGS, 15 days is roughly AED 16M of cash released.",
     "Zara · Uniqlo · Landmark Group"),

    ("Like-for-like sales are +3% but total sales are −2%. What is happening?",
     "The existing estate is growing while the total base shrank — you closed stores, or a "
     "material store left the LFL set. LFL strips out openings and closures, so the gap is the "
     "net space effect. Ask: was the closure deliberate portfolio pruning, in which case −2% total "
     "with +3% LFL and better margin is a good outcome; or did leases expire without replacement, "
     "which is a pipeline failure. Follow with sales density per sqm — that tells you whether the "
     "remaining estate is genuinely stronger.",
     "Marks & Spencer · Americana · Alshaya"),

    ("UAE corporate tax is 9%. How does that change your group structure thinking?",
     "It ends the era of treating the UAE as a zero-tax base. Key points: 9% applies above "
     "AED 375K taxable income; qualifying free zone persons can still get 0% on qualifying income "
     "but the conditions are strict and substance-tested; transfer pricing documentation is now "
     "mandatory and OECD-aligned. Practically: intercompany charges between a UAE HQ and operating "
     "markets need defensible benchmarking, and management fee structures designed purely for tax "
     "will be challenged. Model the effective rate, not the headline rate.",
     "PwC Gulf · Chalhoub · Emaar"),

    ("Your CEO wants to grow revenue 20%. Finance says cash cannot fund it. How do you frame that?",
     "Not as a refusal. Compute the sustainable growth rate: ROE × retention ratio. If the business "
     "sustainably funds 12% and the ask is 20%, the gap is 8 points that must come from somewhere — "
     "debt, equity, working capital release, or margin. Present the three routes with the cost of "
     "each and the covenant headroom. Then let the CEO choose. Finance's job is to price the "
     "options, not to be the department that says no.",
     "Unilever · P&G · Nestlé"),

    ("How do you value a new store before signing a 10-year lease?",
     "Build an unlevered free cash flow model over the lease term. Revenue from catchment "
     "population, comparable sales density and a ramp curve — never assume year-one maturity. "
     "Deduct COGS, rent with escalation clauses, staff, and allocated overhead only if genuinely "
     "incremental. Capex is fit-out plus initial inventory. Discount at WACC and test IRR versus "
     "hurdle, payback, and NPV. Then sensitise the two variables that actually decide it: year-one "
     "sales and the ramp. A store that only clears the hurdle at 100% of forecast is a no.",
     "IKEA · Majid Al Futtaim · Costco"),

    ("Gross margin fell 180bps. Walk me through the bridge.",
     "Decompose into four drivers: mix — did the sales split shift toward lower-margin categories; "
     "price — did you discount more, measured as markdown as a percent of gross sales; cost — did "
     "landed cost rise from FX, freight or supplier price; and shrink/wastage. Quantify each in "
     "basis points so they sum to 180. The bridge is the answer, not the total. In retail the most "
     "common culprit is markdown depth on aged stock, which is really an buying error surfacing "
     "one season late.",
     "Target · Tesco · Max Fashion"),

    ("Explain working capital optimisation to a non-finance retail board.",
     "Working capital is cash tied up in running the business — stock on shelves and money owed to "
     "you, less money you owe suppliers. Every day of stock is cash sitting in a warehouse instead "
     "of in the bank. Frame it in days and rupees, not ratios: 'we hold 95 days of stock; every "
     "day we cut releases AED 1.1M in cash we can spend on new stores instead of borrowing.' "
     "Boards act on that. They do not act on 'DIO improved 4%.'",
     "Landmark Group · Lulu · Walmart"),

    ("What is the difference between EBITDA and free cash flow, and which do you manage to?",
     "EBITDA ignores working capital movements, capex, tax and interest. A retailer can grow "
     "EBITDA while burning cash by stuffing stores with inventory and opening capital-hungry sites. "
     "Free cash flow = EBITDA − capex − change in working capital − tax − interest. Manage the "
     "business to free cash flow; report EBITDA because that is what lenders and multiples use. "
     "Where they diverge for more than two quarters, something structural is wrong.",
     "Berkshire · Amazon · Al-Futtaim"),

    ("You are consolidating Malaysia and Indonesia. Where do FX translation differences land?",
     "Functional currency results are translated at the closing rate for assets and liabilities, "
     "average rate for P&L, and the difference goes to other comprehensive income as a foreign "
     "currency translation reserve — not through P&L. It only recycles to P&L on disposal of the "
     "foreign operation. The trap: intercompany balances that are effectively permanent investment "
     "belong in the net investment, so their FX also goes to OCI. Getting that wrong puts noise "
     "through earnings every quarter.",
     "Landmark Group · Unilever · Shell"),

    ("How do you build a rolling 13-week cash forecast?",
     "Direct method, receipts and payments, not derived from P&L. Start with confirmed opening "
     "bank. Layer in: daily sales collections by tender type with settlement lag; supplier payment "
     "runs from the AP ageing; payroll on fixed dates; rent on quarter days; tax and VAT on "
     "statutory dates; capex from approved commitments. Roll it weekly and track forecast versus "
     "actual variance by line — the discipline is in explaining last week's miss, which is what "
     "makes week 13 credible.",
     "Every treasury function"),

    ("Same-store sales are flat but the store is more profitable. Is that good?",
     "Depends entirely on the cause. If margin rose from better buying, less markdown, or "
     "labour-scheduling efficiency — excellent, that is real operating leverage. If it rose because "
     "you cut staff hours below service thresholds, deferred maintenance, or stopped local "
     "marketing, you are harvesting the store and flat sales will turn negative within three "
     "quarters. Ask for the conversion rate and units per transaction, not just the margin.",
     "Costco · Zara · Alshaya"),

    ("What is ROIC and why would a CFO care more than about net margin?",
     "ROIC = NOPAT ÷ invested capital. Net margin tells you profitability per rupee of sales; ROIC "
     "tells you profitability per rupee of capital committed — which is the only question when you "
     "are deciding where to put the next AED 50M. A 3% margin business turning capital four times "
     "beats an 8% margin business turning it once. In retail this is exactly the discount-versus-"
     "premium format argument, and it is why space productivity matters more than headline margin.",
     "Costco · Walmart · Berkshire"),

    ("Auditors propose an adjustment you disagree with. What do you do?",
     "Separate judgement from fact. If it is a factual error, show the evidence and it goes away. "
     "If it is a judgement — provisioning, impairment, lease term — set out your basis in a "
     "technical memo: the standard, the specific paragraph, the assumptions and their support. "
     "Escalate to the engagement partner if needed, and involve the audit committee chair early "
     "rather than late. If they still disagree and it is material, you record it. What you never "
     "do is trade an adjustment against a different one.",
     "Any listed group"),

    ("Model the impact of a 10% AED appreciation on a UAE retailer sourcing from China.",
     "Buying gets cheaper in AED terms, so landed cost falls and gross margin expands — but only "
     "after existing stock at old cost clears, so the benefit lags by roughly one inventory turn. "
     "If the AED is pegged to the USD, the real question is USD/CNY, not AED specifically. On the "
     "revenue side, a stronger currency makes the destination more expensive for tourists, which "
     "matters in Dubai where tourist spend is a meaningful share of mall retail. Net effect is "
     "usually positive on margin, mildly negative on footfall.",
     "Chalhoub · Majid Al Futtaim"),

    ("What breaks first when a retailer scales from 60 to 200 stores?",
     "Not the finance system — the master data and the operating cadence. Specifically: SKU and "
     "price master governance, because inconsistent data makes every report wrong; the supply "
     "chain's ability to replenish without central stock-outs; and the management reporting rhythm, "
     "because a weekly review that works for 60 stores becomes noise at 200 without exception-based "
     "reporting. Finance's job is to install store-level P&L discipline and exception thresholds "
     "before the growth, not after.",
     "Landmark Group · Americana · Alshaya"),

    ("How would you decide between renting and buying a distribution centre?",
     "Compare NPV of both over the same horizon at the same discount rate. Rent is an operating "
     "commitment — now on balance sheet under IFRS 16 anyway — with flexibility to exit. Buying "
     "ties up capital and carries residual value risk, but fixes occupancy cost and creates an "
     "asset. The real question is strategic, not arithmetic: how confident are you in the network "
     "design over 15 years. If the answer is 'not very', pay the premium for optionality.",
     "Amazon · Lulu · DP World"),

    ("Your forecast missed by 12%. The CEO asks why in the board meeting.",
     "Answer in this order: the number, the driver, the fix, the revised view. 'We missed by "
     "AED 14M. Eleven of that is two categories where sell-through ran 20 points below plan; three "
     "is timing on a store opening that slipped a quarter. The buying calendar for those categories "
     "is now locked four weeks later so we commit on actual trend. Revised full-year is AED 462M.' "
     "Never open with context and never let the explanation exceed four sentences before the number.",
     "Every board, everywhere"),

    # ── consolidation, groups and standards ──
    ("You acquire 80% of a subsidiary. Walk me through the consolidation mechanics.",
     "Recognise 100% of the subsidiary's assets and liabilities at fair value, not 80% — control, "
     "not ownership, drives consolidation. The 20% you do not own becomes non-controlling interest, "
     "shown within equity. Goodwill is consideration transferred plus NCI measured either at fair "
     "value (full goodwill) or at its share of net assets (partial goodwill) — an accounting policy "
     "choice that changes the goodwill number materially. Eliminate all intragroup balances, "
     "transactions and unrealised profit in stock. Post-acquisition profit splits between parent "
     "and NCI by ownership.",
     "Al-Futtaim · Landmark Group · Unilever"),

    ("How do you test goodwill for impairment, and why does retail make it harder?",
     "Goodwill is not amortised — it is tested annually at the cash-generating unit level. Compare "
     "the CGU's carrying amount to its recoverable amount, the higher of fair value less costs of "
     "disposal and value in use. Retail is harder for two reasons: defining the CGU, because a "
     "single store may not generate independent cash inflows once you have click-and-collect and "
     "online returns routed through it; and the discount rate, because store-level risk differs "
     "from group WACC. Auditors challenge the growth rate beyond year five hardest — keep it at or "
     "below long-run GDP for the market.",
     "Big Four · Majid Al Futtaim · Tesco"),

    ("What is the difference between an associate and a subsidiary in your accounts?",
     "Control versus significant influence. A subsidiary is controlled — you consolidate line by "
     "line and recognise NCI. An associate is significant influence, presumed at 20–50%, accounted "
     "for using the equity method: one line on the balance sheet at cost plus your share of "
     "post-acquisition reserves, and one line in P&L for your share of profit. Nothing of the "
     "associate's revenue or EBITDA enters your group numbers — a point that catches people out "
     "when a joint-venture-heavy group reports headline growth.",
     "Emaar · Chalhoub · Nestlé"),

    ("A supplier offers 5% discount for payment in 10 days instead of 60. Take it?",
     "Annualise it. You forgo 5% to accelerate payment by 50 days. The implied annual cost of not "
     "taking it is roughly 5/95 × 365/50 ≈ 38%. Unless your marginal cost of capital exceeds 38%, "
     "take the discount — this is one of the highest-return decisions in working capital. The "
     "caveat is liquidity: if taking it forces you onto an overdraft at 9% you still win, but if it "
     "breaches a covenant or leaves no buffer, the arithmetic is not the whole answer.",
     "Walmart · Lulu · P&G"),

    ("Explain deferred tax to a board member in 60 seconds.",
     "Accounting profit and taxable profit are computed under different rules, so tax paid this "
     "year rarely matches tax on this year's reported profit. Deferred tax records the future "
     "consequence of that gap. The classic retail example is accelerated capital allowances on "
     "fit-out: you get the tax relief early, so you owe more tax later — that future obligation "
     "sits as a deferred tax liability. It is not cash and it is not a provision for a dispute; it "
     "is timing. Say that last sentence, because it is what they are actually worried about.",
     "Any audit committee"),

    ("Your group operates in a country that introduces 15% VAT. What breaks?",
     "Systems and margin, in that order. Systems: tax codes on every SKU, correct treatment of "
     "zero-rated versus exempt lines, input recovery on mixed-use costs, and the reverse charge on "
     "imported services — all of which must be in the ERP before go-live, not after. Margin: if "
     "you absorb VAT rather than pass it through, gross margin falls by roughly the VAT rate on "
     "the absorbed portion. Cash: output VAT is collected before it is remitted, so there is a "
     "temporary working capital benefit that people mistake for profit.",
     "Gulf CFOs post-2018 · PwC"),

    ("How do you account for customer loyalty points under IFRS 15?",
     "Points are a separate performance obligation. You allocate part of the original transaction "
     "price to the points based on their standalone selling price, adjusted for expected breakage, "
     "and defer that portion as contract liability. Revenue is recognised when points are redeemed "
     "or expire. Practically: the deferral reduces reported revenue today, and the breakage "
     "estimate is a judgement auditors probe every year. Retailers who ignore this overstate "
     "current-period revenue and get a restatement.",
     "Alshaya · Carrefour · Starbucks"),

    # ── retail operating economics ──
    ("What is shrinkage, what causes it, and what is an acceptable level?",
     "Shrinkage is the gap between book inventory and counted inventory — theft (external and "
     "internal), administrative error, damage and supplier short-delivery. Fashion retail typically "
     "runs 1–2% of sales; grocery higher. The important split is internal versus external, because "
     "the remedies differ entirely: internal is a controls and rostering problem, external is "
     "layout, tagging and security. Any store more than 100bps above the estate average is a "
     "process failure, not bad luck — investigate the receiving dock first.",
     "Landmark Group · Target · Alshaya"),

    ("Online returns are 30% versus 8% in store. How does that change unit economics?",
     "It can invert them. Model the full return cost: outbound delivery, return shipping, "
     "inspection labour, repackaging, markdown on anything not resaleable as new, and the working "
     "capital locked in goods in transit. At 30% returns a nominally profitable online order can "
     "land negative. Fixes: better size guidance and imagery to cut the cause; charge for returns "
     "where the market allows; route returns to stores so the item re-enters saleable stock nearest "
     "demand. Report online contribution after returns, never before.",
     "Zara · ASOS · Namshi"),

    ("Compare owned retail, franchise, and concession from a finance perspective.",
     "Owned: full revenue, full margin, full capex, full lease and staffing risk. Franchise: you "
     "book a royalty and wholesale margin — far lower revenue, far lower risk, and no control over "
     "the customer experience. Concession: you occupy space inside a host retailer, pay a "
     "percentage of sales rather than fixed rent, so occupancy cost flexes with trading and "
     "downside is capped. In the Gulf, franchise is the dominant international-brand model, which "
     "is why groups like Alshaya and Al-Futtaim report differently from the brands they carry.",
     "Alshaya · Al-Futtaim · Chalhoub"),

    ("Sales per square foot is falling but total sales are rising. Diagnose it.",
     "You are adding space faster than sales. That is not automatically wrong — new stores ramp, "
     "so a growth phase depresses the average mechanically. Split the estate: stores open more than "
     "24 months versus less. If mature-store density is also falling, you have a genuine "
     "productivity problem — likely range, pricing, or footfall. If mature density is flat or up "
     "and only the blended number falls, it is a mix effect and it corrects as the new stores "
     "mature. Never let a board see the blended number alone.",
     "IKEA · Marks & Spencer · Max Fashion"),

    ("How do you measure promotional effectiveness properly?",
     "Incremental margin, not uplift. Take the sales during the promotion, subtract what the "
     "baseline would have been without it, subtract the margin given away on units that would have "
     "sold at full price anyway, and subtract any cannibalisation of adjacent full-price lines. "
     "The result is often negative for deep discounts on already-popular SKUs. Use a control group "
     "of comparable stores where operationally possible — without a counterfactual, every promotion "
     "looks successful because sales always rise.",
     "Tesco · Carrefour · Americana"),

    ("What is open-to-buy and why should finance care?",
     "Open-to-buy is the budget available to purchase stock for a future period, given planned "
     "sales, planned markdowns and target closing stock. It is where merchandise planning and "
     "finance meet. Finance should care because it is the single control that prevents "
     "over-buying — the root cause of the markdown spiral that destroys gross margin two seasons "
     "later. If buyers can commit outside OTB without sign-off, your inventory forecast is "
     "decoration.",
     "Landmark Group · H&M · Inditex"),

    ("A category has 40% gross margin but negative contribution. How?",
     "Gross margin ignores everything below the line. That category may carry disproportionate "
     "space, handling cost, markdown, shrink or return rate. Bulky low-value goods are the classic "
     "case — good percentage margin, terrible margin per square metre per week. Move to a "
     "contribution view: gross profit less directly attributable space, labour, logistics and "
     "markdown. Assortment decisions made on gross margin percentage alone are how retailers end "
     "up with profitable-looking categories that lose money.",
     "Costco · Lulu · Tesco"),

    # ── treasury, capital and risk ──
    ("Explain covenant headroom and how you monitor it.",
     "Covenants are contractual ratios — typically net debt to EBITDA, interest cover, and "
     "sometimes minimum net worth — tested quarterly. Headroom is the distance between your "
     "forecast ratio and the limit. Monitor it forward, not backward: run the covenant calculation "
     "inside the rolling forecast so you see a breach two quarters out while you can still act. "
     "Post-IFRS 16, check whether the definitions are frozen GAAP or current GAAP — lease "
     "liabilities entering net debt has breached covenants that had nothing to do with performance.",
     "Any leveraged group"),

    ("How would you hedge FX exposure for a retailer buying in USD and selling in MYR?",
     "First separate transaction exposure (committed purchase orders) from translation exposure "
     "(consolidating a foreign subsidiary). Hedge transaction exposure with forwards matched to the "
     "payment dates on the buying calendar — typically rolling 6–12 months, hedging a declining "
     "percentage the further out you go. Do not hedge translation exposure with derivatives; it is "
     "an accounting effect, not a cash one. State the policy in a treasury mandate with limits, so "
     "hedging never becomes speculation with a different name.",
     "Landmark Group · Unilever · Shell"),

    ("What is your WACC and how would you set the hurdle rate for a new store?",
     "WACC = cost of equity × equity weight + after-tax cost of debt × debt weight. Cost of equity "
     "via CAPM: risk-free rate plus beta times equity risk premium, with a country risk premium "
     "for emerging markets. But do not use group WACC as the store hurdle — a new store in a new "
     "market is riskier than the average of the existing business. Add a premium and be explicit "
     "about it. The common error is a single group hurdle applied to everything, which "
     "systematically over-invests in risky projects and under-invests in safe ones.",
     "Emaar · Majid Al Futtaim · IKEA"),

    ("Describe the difference between a cash flow forecast and a liquidity plan.",
     "The forecast is your best estimate of receipts and payments. The liquidity plan is what you "
     "do if the forecast is wrong — committed facilities, uncommitted lines, the order in which "
     "you would draw them, what you would stop spending, and how quickly you could release working "
     "capital. Boards ask for the forecast; the CFO's real job is the plan. Stress it: what happens "
     "to cash if sales fall 20% for two quarters while committed rent and payroll continue.",
     "Every treasurer since 2020"),

    ("How do you evaluate a capex request for store refurbishment versus a new store?",
     "Both need incremental cash flow, but the counterfactual differs. For a new store, the "
     "baseline is zero. For a refurbishment, the baseline is the store's declining trajectory "
     "without investment — so the benefit is the uplift over that decline, not over today. People "
     "systematically overstate refurbishment returns by comparing post-refit sales to pre-refit "
     "sales, which double-counts the natural trend and any category resets. Ask for the "
     "do-nothing case in writing.",
     "Marks & Spencer · Landmark Group"),

    ("What is the effect of a sale-and-leaseback on your financials?",
     "Cash in now, occupancy cost forever. You derecognise the asset, recognise a gain or loss "
     "limited to the rights transferred, and recognise a right-of-use asset and lease liability "
     "under IFRS 16 — so leverage does not fall by the full cash proceeds. It flatters current-year "
     "cash and often current-year profit while committing you to rent for 15–20 years. Legitimate "
     "for releasing capital from non-core property; a warning sign when used to plug an operating "
     "cash shortfall.",
     "Tesco · Carrefour · Retail REITs"),

    # ── controls, systems, and the modern finance function ──
    ("You suspect fraud in a store's cash handling. What is your first move?",
     "Do not confront and do not alert. Preserve evidence first — secure the system logs, till "
     "data, CCTV and reconciliations before anyone knows there is a question. Then escalate per "
     "policy: internal audit, legal, and whoever the fraud policy names, usually the audit "
     "committee chair for anything material or involving management. Only then investigate. The "
     "most common and most costly mistake is a well-meaning manager asking questions locally, which "
     "destroys evidence and tips off the person involved.",
     "Any group with an audit committee"),

    ("What controls matter most in a 60-store retail environment?",
     "Rank by exposure. Cash and tender reconciliation daily at store level with independent "
     "review; goods receiving matched three-way to purchase order and invoice, because that is "
     "where margin leaks; inventory counts on a rolling cycle rather than one annual heroic count; "
     "markdown authorisation limits by role; and user access in the ERP reviewed quarterly, "
     "especially for leavers. Segregation of duties is hardest at small stores — compensate with "
     "detective controls and exception reporting rather than pretending it exists.",
     "Landmark Group · Alshaya · Americana"),

    ("How would you use automation or AI in the finance function without losing control?",
     "Automate the deterministic and keep judgement human. Good candidates: bank reconciliation "
     "matching, invoice coding, intercompany elimination, variance flagging, and first-draft "
     "commentary. Bad candidates: provisioning, impairment, anything requiring an assertion to "
     "auditors. The control question is auditability — if you cannot explain how the output was "
     "produced and reperform it, it is not usable in a statutory process. Keep a human approval "
     "step on anything that posts to the ledger.",
     "Every CFO agenda, 2024 onward"),

    ("Your ERP implementation is running three months late. What do you tell the steering committee?",
     "The date, the cause, the options with costs, and your recommendation. Be specific about "
     "which workstream is late and why — usually data migration or the volume of unresolved "
     "customisation requests, not the software. Present the real choice: go live late and clean, "
     "or on time with manual workarounds and a defined remediation plan. Quantify the workaround "
     "cost in FTE months. Having led D365 across two countries, the credible line is that data "
     "quality determines the date more than configuration does.",
     "Your own D365 experience"),

    ("How do you close the books in five days instead of fifteen?",
     "Move work out of the close. Accruals estimated from a model rather than waiting for invoices; "
     "reconciliations performed continuously rather than at month end; intercompany agreed on a "
     "cut-off calendar with a hard deadline and a default position if unresolved; fixed asset and "
     "depreciation runs automated; and reporting built on the ledger rather than rebuilt in Excel. "
     "The cultural half matters more than the technical half — a fast close requires accepting a "
     "materiality threshold below which you do not adjust.",
     "Your own 40% reduction · Unilever"),

    ("What is the one number you would put on a retail CEO's daily dashboard?",
     "Like-for-like sales versus plan, with gross margin rate alongside it. One without the other "
     "is dangerous — sales up on margin collapse is a discount habit forming, and margin up on "
     "sales collapse is a range problem. If forced to a single figure, gross profit versus plan, "
     "because it moves only when something real happens. Everything else — footfall, conversion, "
     "basket, density — is diagnostic detail you reach for once that number moves.",
     "Retail CEOs everywhere"),

    ("Explain contribution margin versus gross margin and when each misleads.",
     "Gross margin is revenue less cost of goods — it answers whether you bought and priced well. "
     "Contribution margin deducts all variable costs, including logistics, payment fees, markdown "
     "and returns — it answers whether the sale was worth making. Gross margin misleads for "
     "high-return or bulky goods; contribution misleads when 'variable' costs are actually "
     "committed in the short run, which makes closure decisions look better on paper than in cash. "
     "Use gross margin for buying decisions and contribution for range and channel decisions.",
     "Amazon · Tesco · Landmark Group"),

    ("How would you build the finance case for entering Saudi Arabia?",
     "Model it as an option, not a certainty. Entry costs: legal structure and Saudisation "
     "obligations under Nitaqat, which drive a materially different payroll cost curve; local "
     "content and CITC/ZATCA compliance; e-invoicing (FATOORA) from day one. Revenue: a much "
     "larger population than the UAE with different mall economics and a younger consumer. Build "
     "the base case on 3–5 stores, not the full ambition, with an explicit go/no-go gate after "
     "24 months and a costed exit. The board decision you want is a staged commitment.",
     "Alshaya · Landmark · Americana"),

    ("What is EBITDAR and why does retail use it?",
     "EBITDA before rent. Retail and hospitality use it because the owned-versus-leased decision "
     "distorts EBITDA comparability — a company that owns its stores shows higher EBITDA than an "
     "identical company that leases them, purely from capital structure. EBITDAR strips that out. "
     "Post-IFRS 16 it matters less for reported figures since operating leases already sit below "
     "EBITDA, but it remains standard in valuation and in comparing operators across "
     "own-versus-lease models.",
     "Retail analysts · Americana · Alshaya"),

    ("Your auditor raises going concern. How do you respond?",
     "Take it seriously and answer with evidence, not reassurance. Provide a cash flow forecast "
     "covering at least twelve months from signing, with the assumptions documented and stress "
     "cases run; the status of facilities including expiry dates and covenant projections; and "
     "board-approved mitigating actions with dates. If there is material uncertainty, the honest "
     "route is disclosure rather than argument — an unqualified opinion with a material "
     "uncertainty paragraph is survivable; a fight with the auditor that ends in a qualification "
     "is not.",
     "Any group under stress"),
]

# ═══════════════════════════════════════════════════════════════════════
# INTERVIEW — NON-TECHNICAL
# The ones that decide the offer. STAR structure, but written as a Financial
# Controller with a real P&L would actually answer them.
# ═══════════════════════════════════════════════════════════════════════

INTERVIEW_SOFT = [
    ("Why do you want to be a CFO, and why in retail?",
     "Structure: motive, evidence, specificity. Weak answers say 'growth' and 'leadership'. Strong "
     "answer names the work: 'I want the seat where capital allocation is decided, not just "
     "reported. In FP&A I model the store investment case; the CFO decides which twelve of thirty "
     "get built. Retail because the feedback loop is daily — you see whether the decision worked "
     "in the sales line the next week, which is not true in most industries.' Then one proof point "
     "with a number attached.",
     "Standard opener, every CFO search"),

    ("Tell me about a time you disagreed with your CEO.",
     "They are testing whether you have spine and judgement, not whether you win. Use STAR and pick "
     "a disagreement where you were partly wrong — it reads as honest. Cover: what the disagreement "
     "was, what evidence you brought, how you escalated it appropriately, what the decision was, "
     "and crucially what you did after the decision went against you. The signal they want: "
     "you argue hard privately and commit fully publicly.",
     "Majid Al Futtaim · Unilever · Amazon"),

    ("What is your biggest professional failure?",
     "Pick a real one with a number, not a disguised strength. The structure: what you owned, what "
     "specifically you got wrong, what it cost, what you changed in your process, and evidence the "
     "change held. The disqualifying answers are 'I work too hard' and any failure where the fault "
     "lands on someone else. If you cannot name the cost in money or time, it was not a real "
     "failure and they will know.",
     "Amazon (Bar Raiser) · P&G · Nestlé"),

    ("How do you influence people who do not report to you?",
     "Retail finance is almost entirely this — Buying, Operations and Retail Heads do not report to "
     "you. The answer is currency: work out what each function is measured on and frame your ask in "
     "their terms. A buyer cares about sell-through and margin, not about your working capital "
     "target — so bring them the SKU-level analysis showing which lines tie up cash without turning. "
     "Give people the analysis they cannot build themselves, and influence follows. Name a specific "
     "instance and the outcome.",
     "Landmark Group · Alshaya · Carrefour"),

    ("Why Dubai? Why now?",
     "They are screening for whether you have thought about it or are just leaving somewhere. Be "
     "concrete: the Gulf retail market's scale and the concentration of regional HQs; UAE corporate "
     "tax making the finance function genuinely more technical than it was; and the specific fit "
     "between your multi-country consolidation experience and a group operating across GCC markets. "
     "Do not lead with tax-free income or lifestyle — every candidate says it and none of it is "
     "about the job.",
     "Every Gulf search firm"),

    ("How do you build a finance team, and how do you handle an underperformer?",
     "Building: hire for one clear gap, not a generalist; define what good looks like in writing "
     "before the first review, not after. Underperformance: the honest answer is that you probably "
     "waited too long once, and learned from it. Structure — early specific feedback with examples, "
     "an agreed plan with dates, documented check-ins, and a decision at the end of it either way. "
     "The failure mode they are probing for is a manager who avoids the conversation and lets the "
     "team carry someone for a year.",
     "GE · Unilever · Al-Futtaim"),

    ("The board pushes back hard on your numbers in front of everyone. What do you do?",
     "Do not defend and do not fold. Separate the challenge into fact and judgement. If they have "
     "spotted a factual error, concede immediately and precisely — 'you are right, that is the "
     "restated base, I will recirculate by Thursday.' Credibility survives errors; it does not "
     "survive defensiveness. If it is a judgement call, state your assumption, the sensitivity, and "
     "offer the alternative case. Never argue methodology in the room; take it offline with the "
     "one director who raised it.",
     "Any listed board"),

    ("Where do you see yourself in five years?",
     "Answer the real question: are you a flight risk and do you understand the ladder. Be "
     "specific about the next rung rather than the summit — 'Financial Controller for a multi-market "
     "group, then Finance Director with commercial ownership, then CFO. What I need in between is "
     "treasury depth and a full M&A cycle, which is part of why this role interests me.' Naming what "
     "you lack reads as self-aware, not weak.",
     "Universal"),

    ("Describe a time you had to deliver bad news to senior stakeholders.",
     "Lead with the structure you used, because that is the skill. Bad news goes: the number, the "
     "cause, the impact, the plan, the ask. No preamble. Deliver it early even when incomplete — "
     "'here is what I know now and what I will know Friday' beats a complete answer a week late. "
     "Give a specific example with the actual figure and what you did next. The tell they look for: "
     "did you surface it or did someone else find it.",
     "Amazon · Emaar · Nestlé"),

    ("How do you prioritise when everything is urgent?",
     "Give a working rule, not a philosophy. Something like: anything with a statutory deadline is "
     "non-negotiable; then anything that changes a decision being made this week; then anything that "
     "compounds — process fixes that stop the same fire recurring; then everything else. The part "
     "interviewers actually want: what you stopped doing and who you told. Prioritisation without "
     "explicit de-prioritisation is just a longer list.",
     "Universal"),

    ("What would your last manager say is your weakness?",
     "Name a real one with a boundary. The pattern that works: a genuine weakness, evidence you "
     "know its cost, and the specific mechanism you use to contain it. 'I go too deep into detail "
     "before surfacing a view — it has cost me time on low-stakes analysis. I now set a decision "
     "deadline before starting and present at that point regardless of how complete it feels.' "
     "Avoid perfectionism, avoid anything that would disqualify you for the role itself.",
     "Universal"),

    ("An employee asks you to approve an expense that is within policy but clearly wrong. What now?",
     "They are testing whether you hide behind rules. Policy is a floor, not a ceiling. Approve or "
     "reject on the substance, explain the reasoning to the person directly, and then fix the policy "
     "so the next person is not in the same position. If it involves a senior person, the answer "
     "does not change — that is precisely when it counts. Say plainly that you would escalate to the "
     "audit committee if pressured to approve something you believe is wrong.",
     "Any group with a code of conduct"),

    ("How do you explain a complex financial concept to a non-finance audience?",
     "Demonstrate it rather than describe it. Pick something like IFRS 16 or working capital and "
     "explain it in the answer, in plain words, in under 30 seconds, with a number. The technique: "
     "one analogy, one number, one consequence. Then stop. The most common failure is finance "
     "people proving they understand it instead of making the other person understand it.",
     "Retail boards · P&G · Mars"),

    ("Why should we hire you over a candidate with Big Four and listed-company experience?",
     "Do not disparage the alternative. Frame the trade: audit teaches you to verify what happened; "
     "operating finance teaches you to change what happens next. Then be specific about what you "
     "own that they do not — a live multi-country P&L, an ERP implementation you led rather than "
     "advised on, and store-level decisions you made and were held to. End with the honest gap and "
     "how you would close it. Confidence with a named weakness beats confidence alone.",
     "Every shortlist"),

    ("Tell me about a time you changed your mind after being convinced by someone junior.",
     "This is a culture question about ego. Pick a real instance, name the person's role, describe "
     "the argument they made and why it was better than yours, and say what you did afterwards to "
     "make sure they got the credit. The best version includes what you changed structurally so "
     "junior challenge became normal rather than exceptional.",
     "Amazon · Google · Unilever"),

    ("What is the hardest decision you have made in your career?",
     "Hard means genuinely two-sided, not merely unpleasant. Redundancies, closing a site, "
     "overruling someone you respected, or choosing between two people for one role. Structure: "
     "the trade-off, the information you had and did not have, how you decided, what it cost, and "
     "what you would do differently. Interviewers discount any answer where the right choice was "
     "obvious in hindsight — say what made it genuinely close.",
     "Majid Al Futtaim · GE · Unilever"),

    ("How do you handle a request from the CEO that you believe is unethical?",
     "Slow it down without escalating to confrontation. First assume miscommunication and restate "
     "what you understood — a surprising share of these resolve there. If it holds, state your "
     "objection privately, in writing, with the specific standard or law engaged. Offer the "
     "compliant alternative that gets closest to the commercial aim. If pressed, escalate to the "
     "audit committee chair. Say plainly that you would resign rather than sign something you "
     "believe is false — the willingness is the answer they are testing for.",
     "Every CFO search · post-Wirecard"),

    ("Describe a time you led a team through significant change.",
     "Use the ERP implementation — it is the strongest card in your hand. Cover what changed for "
     "the people, not just the systems: roles that disappeared, processes relearned, the period "
     "where everything took longer. Explain how you handled the dip in morale at go-live, who "
     "resisted and how you brought them round, and the measurable outcome. The 40% close reduction "
     "is the proof point; the retention of the team through it is the actual answer.",
     "Your own D365 story"),

    ("How would your team describe your management style?",
     "Answer with evidence rather than adjectives. 'They would say I am specific — I give the "
     "number I expect and the date, and I do not change it quietly. They would also say I go into "
     "detail on their work more than some managers, which is useful when they are learning and "
     "irritating when they are not, so I have had to learn where to stop.' Naming the downside of "
     "your own style is what makes the strength believable.",
     "Universal"),

    ("Why are you leaving your current role?",
     "Never criticise the employer. Frame as pull, not push, and make it structural rather than "
     "personal: the scope you want next does not exist there in the timeframe you want it. If "
     "there is an obvious reason they will infer — a merger, a new boss, relocation — name it "
     "briefly and neutrally before they ask. Anything that sounds like a grievance transfers "
     "directly onto how they imagine you will talk about them.",
     "Universal"),

    ("How do you stay current technically while managing?",
     "Be specific and verifiable. Name the standard you last read in full and why, the technical "
     "update you attended, the thing you built yourself recently rather than delegated. Vague "
     "answers about 'reading widely' land badly with technical interviewers. The strongest version "
     "connects it to a decision: 'I went back through IFRS 16 properly when we hit the lease "
     "modification question on the Indonesian stores, and that changed how we structured the "
     "renewal.'",
     "Big Four partners · Group CFOs"),

    ("A peer takes credit for your team's work in front of the CEO. What do you do?",
     "Not in the room. Afterwards, directly and without an audience: describe what you observed, "
     "not their motive, and say what you need going forward. If it repeats, correct it factually "
     "in the moment — 'just to add, the modelling on that was Priya's team' — which is a "
     "contribution rather than an accusation. Escalating first, or absorbing it silently and "
     "resenting it, are both worse. They are testing composure and whether you protect your team.",
     "Any matrixed organisation"),

    ("What do you know about our company?",
     "The disqualifying answer is anything from the homepage. Come with: their last reported "
     "results and one number from them, their format or brand mix and how it differs from peers, "
     "one strategic move in the last 18 months, and one question you genuinely cannot answer from "
     "outside. For a Gulf retail group that means knowing their franchise portfolio, which markets "
     "they operate directly versus through partners, and their exposure to mall footfall.",
     "Every interview, every time"),

    ("How do you handle stress and long hours during close or audit?",
     "They are checking sustainability, not stoicism. Do not claim you thrive on pressure. Give "
     "the operating answer: what you do to make the peak smaller (moving work out of the close), "
     "how you protect the team (rotating the worst shifts, protecting recovery time after), and "
     "what you personally do that is non-negotiable. Having a young child is relevant here and "
     "worth saying — it reads as someone with real boundaries rather than someone who has not "
     "been tested yet.",
     "Universal"),

    ("Tell me about a time you had to say no to a business partner.",
     "Retail finance says no constantly — to a buy outside open-to-buy, a promotion that destroys "
     "margin, a headcount request without a business case. Pick one and show the method: you did "
     "not just refuse, you showed the number that made it a no, and you offered what would make it "
     "a yes. The best answers end with the partner coming back with a version you could approve, "
     "because that proves the relationship survived.",
     "Landmark Group · P&G · Mars"),

    ("Where do you think retail is heading in the next five years?",
     "Have a view and defend it with numbers, not trends. Something like: physical stores stop "
     "being judged on their own sales as fulfilment and returns blur the channels, which breaks "
     "conventional store P&L and forces a catchment-level view; margin pressure shifts from price "
     "to cost-to-serve; and in the Gulf specifically, corporate tax and e-invoicing professionalise "
     "finance functions that were previously light. Then say what you would do about it in the "
     "role you are interviewing for.",
     "Gulf retail boards · Search firms"),

    ("What questions do you have for us?",
     "Never none. Ask things that reveal you are evaluating them: what does the first 12 months "
     "look like for whoever takes this; what is the finance function currently not able to answer "
     "that the board keeps asking; how is the relationship between finance and the commercial "
     "teams today; and why is the role open. The last one is the most informative and the least "
     "asked. Avoid salary and benefits at first interview.",
     "Universal"),

    ("How do you build credibility in the first 90 days in a new company?",
     "Deliver one visible thing early and be accurate about everything. Concretely: get the close "
     "done cleanly, learn the actual business by walking the stores rather than reading the "
     "reports, and find the one number everyone argues about and fix its definition. Do not "
     "restructure anything in the first quarter. Credibility in finance is cumulative and fragile "
     "— it comes from being right repeatedly, not from an early bold move.",
     "Every incoming CFO"),

    ("Tell me about a time you were wrong about a person.",
     "A quieter question than it looks — it tests self-awareness and whether you revise judgements. "
     "Both directions work: someone you underestimated who turned out strong, or someone you "
     "backed who did not work out. The second is braver and lands better if you own the hiring or "
     "promotion decision. End with what you changed about how you assess people, ideally something "
     "structural like a different interview question or a probation checkpoint.",
     "Amazon · Unilever · Alshaya"),

    ("What motivates you, besides money?",
     "Be concrete or it sounds rehearsed. Good answers name the specific texture of the work: "
     "seeing a decision you modelled play out in real numbers weeks later, building a team member "
     "into something they were not when they arrived, or the particular satisfaction of a clean "
     "close. Avoid 'challenge' and 'growth' unqualified. If money genuinely is a driver — Dubai, "
     "young family — it is fine to say it is one of several, and honest beats performed modesty.",
     "Universal"),

    ("How do you decide what to delegate?",
     "Give a rule. Something like: delegate anything where the cost of a mistake is recoverable and "
     "the learning value is high; keep anything where your specific judgement is the reason the "
     "answer is right, and anything you would have to redo. The failure mode interviewers probe is "
     "the controller who keeps the technical work because it is comfortable and delegates only the "
     "admin — which caps the team's growth and yours.",
     "GE · Unilever · Group CFOs"),

    ("Describe a situation where you had incomplete information and had to decide anyway.",
     "Forecasting and provisioning are full of these. Structure: what you knew, what you could not "
     "know in the time available, the cost of waiting, the assumption you took and why, and how you "
     "made the assumption visible rather than burying it. The mark of seniority is deciding with "
     "70% of the information and saying explicitly what would change your mind — say that phrase.",
     "Amazon · McKinsey · Boards"),

    ("How do you give feedback to someone more senior than you?",
     "Ask permission, be specific, and stay on observable behaviour and its effect. 'Can I give you "
     "a reaction to how the numbers landed in that meeting? When the forecast changed without the "
     "bridge, the room stopped trusting the rest of the pack.' No adjectives about them, one "
     "concrete instance, and a suggestion. Then stop talking. Most people either avoid this "
     "entirely or overcorrect into confrontation.",
     "Any senior finance role"),

    ("If we hire you and in a year it has not worked, what is the most likely reason?",
     "A genuinely good question and a rare one. Answer honestly with the real risk, not a "
     "disguised strength: a mismatch on decision-making pace, an unclear boundary between your "
     "scope and someone else's, or a business where the data foundations are worse than described "
     "so the first year goes on plumbing rather than insight. Then say what you would do in month "
     "one to check for it. Naming the failure mode makes you look like someone who has thought "
     "about the job rather than the offer.",
     "Senior search processes"),
]

# ═══════════════════════════════════════════════════════════════════════
# SPANISH — practical first. Ordered toward things you would actually say.
# ═══════════════════════════════════════════════════════════════════════

SPANISH = [
    ("el presupuesto", "the budget", "¿Ya aprobaron el presupuesto?", "Has the budget been approved yet?", "Business"),
    ("la ganancia", "the profit", "La ganancia creció un 12% este año.", "Profit grew 12% this year.", "Business"),
    ("la pérdida", "the loss", "Registramos una pérdida en el primer trimestre.", "We recorded a loss in the first quarter.", "Business"),
    ("el flujo de caja", "cash flow", "El flujo de caja es más importante que la ganancia.", "Cash flow matters more than profit.", "Business"),
    ("el inventario", "inventory", "Tenemos demasiado inventario en las tiendas.", "We have too much inventory in the stores.", "Business"),
    ("la reunión", "the meeting", "La reunión empieza a las nueve.", "The meeting starts at nine.", "Business"),
    ("el informe", "the report", "Necesito el informe antes del viernes.", "I need the report before Friday.", "Business"),
    ("el plazo", "the deadline", "No podemos cumplir ese plazo.", "We cannot meet that deadline.", "Business"),
    ("la deuda", "the debt", "Reducimos la deuda a la mitad.", "We cut the debt in half.", "Business"),
    ("crecer", "to grow", "La empresa creció más rápido de lo esperado.", "The company grew faster than expected.", "Business"),
    ("hoy", "today", "Hoy tengo mucho trabajo.", "Today I have a lot of work.", "Everyday"),
    ("mañana", "tomorrow / morning", "Nos vemos mañana por la mañana.", "See you tomorrow morning.", "Everyday"),
    ("¿cuánto cuesta?", "how much does it cost?", "¿Cuánto cuesta este?", "How much does this one cost?", "Everyday"),
    ("la hija", "the daughter", "Mi hija tiene siete meses.", "My daughter is seven months old.", "Family"),
    ("dormir", "to sleep", "Ella todavía no duerme toda la noche.", "She still does not sleep through the night.", "Family"),
    ("cansado", "tired", "Estoy muy cansado hoy.", "I am very tired today.", "Everyday"),
    ("gracias por todo", "thanks for everything", "Gracias por todo tu apoyo.", "Thanks for all your support.", "Everyday"),
    ("tener razón", "to be right", "Tienes razón, cambiemos el plan.", "You are right, let us change the plan.", "Everyday"),
    ("estoy de acuerdo", "I agree", "Estoy de acuerdo con tu análisis.", "I agree with your analysis.", "Business"),
    ("no estoy seguro", "I am not sure", "No estoy seguro de esos números.", "I am not sure about those numbers.", "Business"),
    ("¿me puedes ayudar?", "can you help me?", "¿Me puedes ayudar con esto?", "Can you help me with this?", "Everyday"),
    ("el equipo", "the team", "Mi equipo tiene cuatro personas.", "My team has four people.", "Business"),
    ("aprender", "to learn", "Estoy aprendiendo español.", "I am learning Spanish.", "Everyday"),
    ("poco a poco", "little by little", "Poco a poco se aprende.", "Little by little, one learns.", "Idiom"),
    ("vale la pena", "it is worth it", "Vale la pena esperar.", "It is worth waiting for.", "Idiom"),
    ("de hecho", "in fact / actually", "De hecho, los márgenes mejoraron.", "In fact, margins improved.", "Business"),
    ("sin embargo", "however", "Sin embargo, el costo subió.", "However, the cost went up.", "Business"),
    ("a largo plazo", "in the long run", "A largo plazo es la mejor decisión.", "In the long run it is the best decision.", "Business"),
    ("ahorrar", "to save (money)", "Necesitamos ahorrar más cada mes.", "We need to save more each month.", "Business"),
    ("invertir", "to invest", "Prefiero invertir a largo plazo.", "I prefer to invest for the long term.", "Business"),
    ("la tienda", "the shop / store", "Abrimos una tienda nueva en Dubái.", "We are opening a new store in Dubai.", "Business"),
    ("el cliente", "the customer", "El cliente siempre nota la diferencia.", "The customer always notices the difference.", "Business"),
    ("mejorar", "to improve", "Queremos mejorar el margen bruto.", "We want to improve the gross margin.", "Business"),
    ("la cifra", "the figure / number", "Esa cifra no cuadra.", "That figure does not add up.", "Business"),
    ("cuadrar", "to add up / reconcile", "Las cuentas no cuadran.", "The accounts do not reconcile.", "Business"),
    ("por lo tanto", "therefore", "Por lo tanto, recomiendo esperar.", "Therefore, I recommend waiting.", "Business"),
    ("¿qué opinas?", "what do you think?", "¿Qué opinas de esta propuesta?", "What do you think of this proposal?", "Business"),
    ("tomar una decisión", "to make a decision", "Tenemos que tomar una decisión hoy.", "We have to make a decision today.", "Business"),
    ("con calma", "calmly / take it easy", "Vamos con calma.", "Let us take it slowly.", "Idiom"),
    ("más vale tarde que nunca", "better late than never", "Entregó el informe — más vale tarde que nunca.", "He delivered the report — better late than never.", "Idiom"),

    # ── work: meetings, reporting, negotiation ──
    ("el margen bruto", "gross margin", "El margen bruto bajó dos puntos.", "Gross margin fell two points.", "Business"),
    ("el costo", "the cost", "El costo subió por el transporte.", "The cost went up because of shipping.", "Business"),
    ("la tasa", "the rate", "La tasa de interés cambió ayer.", "The interest rate changed yesterday.", "Business"),
    ("el impuesto", "the tax", "El impuesto corporativo es del 9%.", "Corporate tax is 9%.", "Business"),
    ("la factura", "the invoice", "Todavía no recibimos la factura.", "We have not received the invoice yet.", "Business"),
    ("el proveedor", "the supplier", "Negociamos mejores plazos con el proveedor.", "We negotiated better terms with the supplier.", "Business"),
    ("la sucursal", "the branch / outlet", "Abrimos una sucursal en Kuala Lumpur.", "We opened a branch in Kuala Lumpur.", "Business"),
    ("las ventas", "the sales", "Las ventas subieron un 8% este mes.", "Sales rose 8% this month.", "Business"),
    ("el pronóstico", "the forecast", "El pronóstico no coincide con la realidad.", "The forecast does not match reality.", "Business"),
    ("aprobar", "to approve", "El director aprobó el gasto.", "The director approved the expense.", "Business"),
    ("revisar", "to review / check", "Voy a revisar los números otra vez.", "I am going to check the numbers again.", "Business"),
    ("entregar", "to deliver / hand in", "Entregamos el reporte a tiempo.", "We delivered the report on time.", "Business"),
    ("contratar", "to hire", "Vamos a contratar dos analistas.", "We are going to hire two analysts.", "Business"),
    ("el riesgo", "the risk", "Ese riesgo es demasiado alto.", "That risk is too high.", "Business"),
    ("la junta", "the board / meeting", "La junta se reúne el martes.", "The board meets on Tuesday.", "Business"),
    ("a tiempo", "on time", "Nunca entrega a tiempo.", "He never delivers on time.", "Business"),
    ("hace falta", "it is needed / lacking", "Hace falta más información.", "More information is needed.", "Business"),
    ("me parece que", "it seems to me that", "Me parece que el plan es muy optimista.", "It seems to me the plan is very optimistic.", "Business"),
    ("en resumen", "in summary", "En resumen, recomiendo esperar un trimestre.", "In summary, I recommend waiting a quarter.", "Business"),
    ("depende de", "it depends on", "Depende del tipo de cambio.", "It depends on the exchange rate.", "Business"),

    # ── family and home ──
    ("la esposa", "the wife", "Mi esposa trabaja desde casa.", "My wife works from home.", "Family"),
    ("el bebé", "the baby", "El bebé está aprendiendo a sentarse.", "The baby is learning to sit up.", "Family"),
    ("llorar", "to cry", "Llora cuando tiene sueño.", "She cries when she is sleepy.", "Family"),
    ("comer", "to eat", "Ya empieza a comer sólidos.", "She is starting to eat solids.", "Family"),
    ("jugar", "to play", "Me gusta jugar con ella por la tarde.", "I like to play with her in the afternoon.", "Family"),
    ("crecer rápido", "to grow fast", "Crece muy rápido.", "She is growing very fast.", "Family"),
    ("el pañal", "the nappy / diaper", "Hay que cambiar el pañal.", "The nappy needs changing.", "Family"),
    ("la siesta", "the nap", "Duerme la siesta a las dos.", "She naps at two o'clock.", "Family"),
    ("tener hambre", "to be hungry", "Creo que tiene hambre.", "I think she is hungry.", "Family"),
    ("cuidar", "to look after", "Cuido a mi hija los domingos.", "I look after my daughter on Sundays.", "Family"),

    # ── everyday, travel, and the small connective words ──
    ("¿dónde está?", "where is it?", "¿Dónde está la salida?", "Where is the exit?", "Everyday"),
    ("quisiera", "I would like", "Quisiera un café, por favor.", "I would like a coffee, please.", "Everyday"),
    ("la cuenta", "the bill / account", "La cuenta, por favor.", "The bill, please.", "Everyday"),
    ("ahora mismo", "right now", "Lo hago ahora mismo.", "I will do it right now.", "Everyday"),
    ("todavía no", "not yet", "Todavía no he terminado.", "I have not finished yet.", "Everyday"),
    ("otra vez", "again", "Explícamelo otra vez, por favor.", "Explain it to me again, please.", "Everyday"),
    ("tal vez", "maybe", "Tal vez mañana.", "Maybe tomorrow.", "Everyday"),
    ("por supuesto", "of course", "Por supuesto, sin problema.", "Of course, no problem.", "Everyday"),
    ("lo siento", "I am sorry", "Lo siento, llegué tarde.", "I am sorry, I arrived late.", "Everyday"),
    ("¿qué significa?", "what does it mean?", "¿Qué significa esta palabra?", "What does this word mean?", "Everyday"),
    ("más o menos", "more or less", "Más o menos entiendo.", "I more or less understand.", "Everyday"),
    ("darse cuenta", "to realise", "Me di cuenta del error muy tarde.", "I realised the mistake too late.", "Everyday"),
    ("hace dos años", "two years ago", "Empecé hace dos años.", "I started two years ago.", "Everyday"),
    ("desde entonces", "since then", "Desde entonces todo cambió.", "Since then everything changed.", "Everyday"),
    ("mientras tanto", "meanwhile", "Mientras tanto, seguimos con el plan.", "Meanwhile, we continue with the plan.", "Everyday"),

    # ── idiom and register ──
    ("estar al día", "to be up to date", "Estoy al día con los reportes.", "I am up to date with the reports.", "Idiom"),
    ("dar en el clavo", "to hit the nail on the head", "Diste en el clavo con ese análisis.", "You hit the nail on the head with that analysis.", "Idiom"),
    ("costar un ojo de la cara", "to cost a fortune", "Ese proyecto costó un ojo de la cara.", "That project cost a fortune.", "Idiom"),
    ("no tener pelos en la lengua", "to speak bluntly", "Ella no tiene pelos en la lengua.", "She does not mince her words.", "Idiom"),
    ("ponerse las pilas", "to get moving / step up", "Hay que ponerse las pilas este trimestre.", "We need to step up this quarter.", "Idiom"),
    ("de golpe", "all at once / suddenly", "Los costos subieron de golpe.", "Costs rose all at once.", "Idiom"),
    ("a la larga", "in the long run", "A la larga, la disciplina gana.", "In the long run, discipline wins.", "Idiom"),
    ("sobre la marcha", "on the fly / as we go", "Lo ajustamos sobre la marcha.", "We adjust it as we go.", "Idiom"),
    ("echar una mano", "to lend a hand", "¿Me echas una mano con esto?", "Can you lend me a hand with this?", "Idiom"),
    ("valer la pena el esfuerzo", "to be worth the effort", "Vale la pena el esfuerzo.", "It is worth the effort.", "Idiom"),
]

# ═══════════════════════════════════════════════════════════════════════
# ENGLISH — a precise word, and a delivery drill. Vocabulary that survives a
# board room; drills that fix how you sound, not what you know.
# ═══════════════════════════════════════════════════════════════════════

VOCAB = [
    ("Salient", "SAY-lee-uhnt", "Most noticeable or important.",
     "The salient point is that margin fell before volume did.", "Use instead of 'main' when you want to signal you have filtered the noise."),
    ("Tenuous", "TEN-yoo-uhs", "Weak; barely holding together.",
     "The link between the campaign and the sales lift is tenuous.", "Precise way to challenge a claim without calling it wrong."),
    ("Prudent", "PROO-duhnt", "Sensibly cautious about risk.",
     "A prudent provision here protects the full-year number.", "Standard in audit and board language — signals judgement, not fear."),
    ("Material", "muh-TEER-ee-uhl", "Large enough to change a decision.",
     "The variance is real but not material to the group result.", "In finance this is a technical word — use it only when you mean the threshold."),
    ("Mitigate", "MIT-i-gayt", "To make less severe.",
     "We mitigated the FX exposure with forward contracts.", "Not the same as 'eliminate'. Precision matters when a board is listening."),
    ("Discretionary", "dis-KRESH-uh-nair-ee", "Spent at one's own choosing; not committed.",
     "Only 18% of the cost base is genuinely discretionary.", "The word that separates costs you can cut from costs you cannot."),
    ("Attrition", "uh-TRISH-uhn", "Gradual loss, usually of people or customers.",
     "We lost the margin to attrition, not to a single event.", "More precise than 'churn' when the loss is slow and structural."),
    ("Robust", "roh-BUST", "Strong enough to withstand stress.",
     "The forecast is robust to a 10% sales decline.", "Pair it with the stress it survives, or it is just a filler word."),
    ("Nominal", "NOM-i-nuhl", "In name only; or, not adjusted for inflation.",
     "Nominal growth was 8%; real growth was closer to 3%.", "Two meanings — make the context obvious or you will be misread."),
    ("Concede", "kuhn-SEED", "To admit a point is valid.",
     "I concede the timing assumption was optimistic.", "Conceding one point early buys credibility for the ten you defend."),
    ("Pragmatic", "prag-MAT-ik", "Dealing with things practically rather than ideally.",
     "The pragmatic route is to phase it over two quarters.", "Useful when you are choosing the workable option over the elegant one."),
    ("Onerous", "OH-nuhr-uhs", "Burdensome; costly to fulfil.",
     "The lease became onerous once footfall dropped.", "A technical term under IAS 37 — onerous contracts require provision."),
    ("Ostensibly", "os-TEN-si-blee", "Apparently, but perhaps not actually.",
     "The cost was ostensibly one-off.", "Signals scepticism without accusation. Powerful in a review meeting."),
    ("Granular", "GRAN-yuh-luhr", "Broken into fine detail.",
     "I need this granular — by store, by category, by week.", "The exact word for asking someone to stop showing you aggregates."),
    ("Contingent", "kuhn-TIN-juhnt", "Dependent on something else happening.",
     "The bonus is contingent on hitting the cash target.", "Also technical — contingent liabilities are disclosed, not recognised."),
    ("Latitude", "LAT-i-tood", "Freedom to act within limits.",
     "Give the country teams latitude on promotions, not on pricing.", "A leadership word — defines the boundary rather than the rule."),
    ("Untenable", "un-TEN-uh-buhl", "Impossible to defend or maintain.",
     "Holding 95 days of stock at this rate is untenable.", "Stronger than 'difficult'. Use it when you mean the position must change."),
    ("Commensurate", "kuh-MEN-suh-rit", "In proportion to.",
     "The return is not commensurate with the capital at risk.", "The precise way to say something is not worth what it costs."),
    ("Extrapolate", "ik-STRAP-uh-layt", "To extend a trend beyond known data.",
     "Do not extrapolate a festive quarter across the year.", "Naming the error is often the fastest way to kill a bad forecast."),
    ("Judicious", "joo-DISH-uhs", "Showing good judgement.",
     "A judicious use of markdown clears stock without training customers to wait.", "Sounds considered rather than cautious."),
    ("Precipitate", "pri-SIP-i-tayt", "To cause something to happen suddenly.",
     "The covenant test precipitated the refinancing.", "As a verb it means trigger; as an adjective (pri-SIP-i-tit) it means hasty. Different stress."),
    ("Marginal", "MAR-ji-nuhl", "Relating to the next unit; or barely sufficient.",
     "The marginal store adds revenue but no profit.", "In economics it means 'the next one'. Most people misuse it to mean 'small'."),
    ("Systemic", "sis-TEM-ik", "Affecting the whole system.",
     "This is a systemic control weakness, not an isolated error.", "Not 'systematic', which means methodical. The two get confused in audit reports constantly."),
    ("Preclude", "pri-KLOOD", "To prevent from happening.",
     "The lease terms preclude subletting.", "More precise than 'stop' when something is ruled out in advance."),
    ("Salutary", "SAL-yuh-ter-ee", "Producing a good effect, usually unpleasantly.",
     "The audit finding was a salutary reminder about access controls.", "The useful shock. Slightly formal, lands well in written commentary."),
    ("Endemic", "en-DEM-ik", "Regularly found in a particular place or group.",
     "Shrinkage at that level is endemic to the format, not to the store.", "Distinguishes a structural feature from a local failure — useful when defending a manager."),
    ("Countenance", "KOWN-tuh-nuhns", "To tolerate or approve of.",
     "The board will not countenance another delay.", "Formal, strong. One use per document, maximum."),
    ("Attenuate", "uh-TEN-yoo-ayt", "To reduce in force or effect.",
     "Hedging attenuates the FX impact but does not remove it.", "The honest word when something is dampened rather than solved."),
    ("Spurious", "SPYOOR-ee-uhs", "False; not genuine, despite appearing so.",
     "That correlation is spurious — both track store openings.", "The exact word for a relationship in the data that is real-looking and meaningless."),
    ("Requisite", "REK-wi-zit", "Required for a purpose.",
     "She has the requisite experience for the controller role.", "Slightly more formal than 'required'. Common in job specs and board papers."),
    ("Efficacy", "EF-i-kuh-see", "The ability to produce the intended result.",
     "We have no evidence on the efficacy of that promotion.", "Not the same as efficiency. Efficacy is 'does it work', efficiency is 'at what cost'."),
    ("Untoward", "un-TOR-d", "Unexpected and inconvenient or inappropriate.",
     "Nothing untoward emerged from the review.", "Standard audit and legal register for 'we found nothing concerning'."),
    ("Predicated", "PRED-i-kay-tid", "Founded or based on.",
     "The forecast is predicated on two store openings in Q3.", "The precise way to surface an assumption the whole number rests on."),
    ("Incremental", "in-kruh-MEN-tuhl", "Additional, over and above the existing base.",
     "Only AED 3M of that is incremental revenue.", "The single most important word in investment appraisal. Everything hinges on the baseline."),
    ("Ameliorate", "uh-MEEL-yuh-rayt", "To make a bad situation better.",
     "The payment plan ameliorated the cash position without fixing it.", "Implies improvement without resolution — which is often the honest description."),
    ("Cursory", "KUR-suh-ree", "Hasty and not thorough.",
     "A cursory review would not have caught this.", "Useful for defending the depth of your own work, or flagging the shallowness of someone else's."),
    ("Purview", "PUR-vyoo", "The scope of someone's responsibility or authority.",
     "Treasury policy falls within the CFO's purview.", "Cleaner than 'remit' or 'area' when you mean formal authority."),
    ("Notwithstanding", "not-with-STAN-ding", "In spite of.",
     "Notwithstanding the shortfall, cash generation held.", "Can lead the sentence or follow the noun. A single well-placed use signals command of register."),
    ("Corroborate", "kuh-ROB-uh-rayt", "To confirm with independent evidence.",
     "The stock count corroborates the system balance.", "Independence is the point. Two reports from the same system do not corroborate anything."),
    ("Ostensible", "os-TEN-si-buhl", "Stated as true, but possibly not.",
     "The ostensible reason for the variance was FX.", "Adjective form of 'ostensibly'. Signals doubt politely enough to survive minutes."),
    ("Exigency", "EK-si-juhn-see", "An urgent need or demand.",
     "Cash exigencies drove the decision, not strategy.", "Explains a decision made under pressure without excusing it."),
    ("Tantamount", "TAN-tuh-mownt", "Equivalent in effect to.",
     "Approving that is tantamount to reopening the budget.", "Names the real consequence of a decision people are framing as minor."),
    ("Perfunctory", "puhr-FUNGK-tuh-ree", "Done as a duty, without care.",
     "The reconciliation had become perfunctory.", "Describes a control that exists on paper and has stopped working."),
    ("Salience", "SAY-lee-uhns", "The quality of being most noticeable or important.",
     "The salience of the cash line rose sharply after the covenant test.", "Noun form of salient. Useful when describing shifting board attention."),
    ("Obviate", "OB-vee-ayt", "To remove the need for.",
     "Automating the match obviates the manual review.", "Stronger and more precise than 'reduces'. It means the need disappears entirely."),
]

SPEAKING_DRILLS = [
    ("Cut the runway", "Record yourself answering 'what do you do?' in 30 seconds. Count how many words come before your first concrete noun. Redo it until the answer starts with the thing itself.",
     "Most people spend 8 seconds warming up. Senior listeners have decided what they think of you by then."),
    ("Kill the filler", "Pick one filler you overuse — 'basically', 'actually', 'sort of', 'you know'. Speak for two minutes on today's market move and pause silently instead of using it.",
     "A silent pause reads as thinking. A filler reads as searching."),
    ("End the sentence", "Read three sentences aloud and drop your pitch on the final word of each. Record and check you are not rising at the end.",
     "Upward inflection turns statements into questions. It is the fastest way to sound unsure of numbers you are certain about."),
    ("One breath, one idea", "Say your headline number, then breathe. Say the driver, then breathe. Practise with today's gross margin bridge.",
     "Running clauses together is why people lose the thread of financial explanations."),
    ("The 3-sentence answer", "Answer 'how was the quarter?' in exactly three sentences: the number, the driver, the outlook. Time it — under 20 seconds.",
     "Executives interrupt long answers. Give them a complete short one and they ask for more."),
    ("Slow the first line", "Deliver your opening sentence at half your normal pace, then return to normal.",
     "Nerves speed up the first line, which is the one that sets whether people lean in."),
    ("Replace 'I think'", "Speak for 90 seconds about a decision, substituting 'I think' with 'the data shows', 'my view is', or nothing at all.",
     "'I think' before a fact undercuts the fact. Keep it for genuine opinion."),
    ("Name the number first", "Practise flipping five sentences so the figure leads: not 'because of markdown, margin fell 180bps' but 'margin fell 180bps — markdown'.",
     "Numbers-first is your written voice. Make it your spoken one."),
    ("Handle the interruption", "Rehearse: 'Let me finish this point, then I will take that.' Say it out loud five times until it sounds calm rather than defensive.",
     "Being talked over in a board meeting is a delivery problem, not a rank problem."),
    ("Explain it to a seven-year-old", "Explain IFRS 16 or working capital with no jargon, in under 40 seconds, out loud.",
     "The constraint exposes whether you understand it or have memorised it."),
    ("Answer, then stop", "Ask yourself an interview question, answer it, and physically close your mouth. Count three seconds before adding anything.",
     "Over-explaining after a complete answer is the most common interview mistake at senior level. The silence is theirs to fill."),
    ("Vary the pace", "Read a paragraph aloud twice — once flat, once slowing down on the two most important words.",
     "Emphasis by pace is more persuasive than emphasis by volume, and it survives a phone line."),
    ("Land the number", "Say five figures out loud with a small pause before each one: 'margin fell … 180 basis points'.",
     "The pause makes the listener wait for the number, which makes them remember it."),
    ("Lose the apology", "Speak for two minutes without 'sorry', 'just', or 'I might be wrong but'.",
     "Hedging language before a fact invites challenge to the fact. Reserve qualifiers for genuine uncertainty."),
    ("Record and listen back", "Record 60 seconds answering 'why do you want to be a CFO?'. Listen once for content, once for delivery only.",
     "Almost nobody does this, and it is the single fastest improvement available. You will hear things no one will tell you."),
    ("The hostile question", "Rehearse: 'That is a fair challenge — here is the assumption behind it.' Say it five times until it is automatic.",
     "Having a rehearsed opening for a hostile question buys three seconds of thinking without looking rattled."),
    ("Shorten your sentences", "Take a written paragraph of your own and read it aloud, splitting every sentence longer than 15 words.",
     "Your written voice is already short. Spoken sentences drift longer under pressure — this recalibrates it."),
    ("Name then explain", "Practise five answers that open with the conclusion: 'No — and the reason is…'",
     "Burying the verdict at the end forces the listener to hold everything in memory. Lead with it."),
    ("Own the accent", "Read a page aloud focusing only on the final consonant of each word, at normal speed.",
     "Clarity comes from finishing words, not from changing your accent. Endings are where intelligibility is lost."),
    ("The elevator version", "Explain your entire current role in 20 seconds: scope, scale, and one outcome with a number.",
     "You will need this more than any other single answer — in interviews, at conferences, and in lifts."),
    ("Breathe before the hard part", "Identify the toughest sentence in an answer and take a full breath immediately before it.",
     "Nerves shorten breath, which raises pitch and speeds delivery precisely when you most need authority."),
    ("Cut the throat-clear", "Notice and eliminate the meaningless opener: 'So…', 'Yeah, so…', 'I guess…'.",
     "It signals you started talking before you started thinking, even when you did not."),
    ("Read a wire story aloud", "Take one headline from the World section and read the first paragraph aloud, twice.",
     "Journalistic prose is tightly written. Reading it aloud daily rewires sentence rhythm faster than conversation does."),
    ("Say the difficult number", "Practise delivering bad news out loud: 'We are AED 14M behind.' No preamble, no cushion.",
     "The instinct is to soften it with context first. Context after the number is briefing; before it is stalling."),
    ("One-minute summary", "At the end of the day, summarise it out loud in 60 seconds as if to a CFO.",
     "Daily practice at compressing information is the underlying skill in every senior finance conversation."),
    ("Slow the Q&A", "Have someone ask you three questions in a row. Pause two seconds before each answer.",
     "Rapid answers read as reflexive. A deliberate pause reads as considered, and buys you the sentence you actually want."),
    ("Vary sentence length", "Speak for 90 seconds deliberately alternating one short sentence and one longer one.",
     "Monotone rhythm loses a room faster than monotone pitch. Your writing already does this — make speech match."),
    ("Kill the upspeak on numbers", "Say ten figures aloud, dropping pitch on the last syllable of each.",
     "Rising pitch on a number makes it sound like an estimate. You rarely want that."),
    ("Handle 'I do not know'", "Rehearse: 'I do not have that number in front of me. I will have it to you by Thursday.' Say it without apology.",
     "Senior people say this comfortably. Guessing to avoid it is how credibility is actually lost."),
    ("Present standing", "Deliver a two-minute update standing up, even alone at your desk.",
     "Posture changes breath, breath changes voice. It transfers to seated delivery after about a week."),
]

# ═══════════════════════════════════════════════════════════════════════
# FATHERHOOD — 7 months old (born ~Dec 2025). Milestone-aware, and honest
# that most of it is presence rather than technique.
# ═══════════════════════════════════════════════════════════════════════

FATHER = [
    ("Narrate what you are doing", "Talk through ordinary actions out loud while she watches — 'I am pouring the water, now it is warm.'",
     "At 7 months she is mapping sound to meaning long before she can speak. Volume of directed speech is the single best-evidenced predictor of later vocabulary. Passive audio does not count; it has to be you, to her."),
    ("Let her lead the play", "Follow what she reaches for instead of redirecting to the toy you chose. Sit on the floor at her level for 15 minutes.",
     "Serve-and-return interaction builds the circuitry for attention and self-regulation. Following her focus teaches that her signals produce a response — the root of secure attachment."),
    ("Practise object permanence", "Hide a toy under a cloth while she watches, then let her pull it off. Repeat with longer delays.",
     "Around 7–8 months this clicks. It is also why separation anxiety starts now — she has learned you continue to exist when you leave, but not yet that you come back."),
    ("Say goodbye properly", "Never slip out unseen. Say you are leaving, say you will be back, then go — even if she cries.",
     "Disappearing without warning teaches vigilance. A predictable goodbye, repeated, teaches that departures end in returns. The crying is not the failure; it is the process."),
    ("Offer food she can hold", "Soft strips she can grip herself — steamed carrot, banana, well-cooked pasta. Accept the mess.",
     "Self-feeding at 7 months develops the pincer grip and, more importantly, lets her control intake. Children who regulate their own portions early are less likely to over-eat later."),
    ("Read the same book again", "Read one board book daily, the same one, for a week. Point at the same picture each time.",
     "Repetition is how infants build prediction. The boredom is yours, not hers — she is learning that symbols hold steady meaning."),
    ("Get down on the floor", "Ten minutes of tummy-adjacent floor time where you are lying at her eye level, not looming above.",
     "Physical perspective changes the interaction from supervision to companionship. It also builds the core strength that precedes crawling and sitting unsupported."),
    ("Respond to the babble", "When she makes a sound, answer as if it were a sentence. Wait. Let her go again.",
     "Turn-taking is the structure of conversation, learned months before words. The pause is the important part — filling it teaches her that her turn does not matter."),
    ("Introduce one new texture", "Let her touch something unfamiliar and safe — a cold spoon, a rough towel, wet cloth.",
     "Sensory variety builds neural discrimination. At this age the mouth is a primary sense organ, so expect it to be tasted; choose accordingly."),
    ("Protect the sleep routine", "Same order, same lighting, same time — bath, feed, book, bed. Do it yourself at least twice a week.",
     "Consistency of sequence matters more than the exact timing. Fathers who own bedtime regularly build a distinct attachment relationship rather than a substitute one."),
    ("Mirror her expressions", "Copy her face — surprise, frown, smile — and hold it so she sees it.",
     "Facial mirroring is how infants learn that internal states have external signals. It is the earliest form of emotional literacy."),
    ("Name the feeling, not the behaviour", "When she cries, say what she is feeling: 'you are tired' or 'that frightened you.'",
     "She cannot parse it yet. You are building the habit in yourself — parents who label emotions early keep doing it at three and at thirteen, which is when it matters."),
    ("Let her struggle briefly", "When a toy is just out of reach, wait five seconds before helping.",
     "Effort tolerance is trainable. Instant rescue is comfortable for you and teaches her that difficulty is someone else's problem."),
    ("Sing badly and often", "One song, daily, the same one. Tune is irrelevant.",
     "Melody carries language patterns — rhythm, stress, phrase boundaries — more clearly than speech. She is not judging the singing."),
    ("Put the phone in another room", "One 20-minute block a day with the phone physically out of sight.",
     "Infants track gaze from very early. Divided attention is visible to her, and the research on 'still face' shows how quickly infants disengage when a caregiver's face goes flat."),
    ("Take her outside daily", "Fifteen minutes outdoors, describing what you both see.",
     "Daylight anchors circadian rhythm, which is the practical route to better night sleep. The narration doubles as vocabulary exposure in a changing environment."),
    ("Learn her tired signals", "Watch for the specific cues before crying — ear pulling, gaze aversion, a particular whine.",
     "Catching the window before the overtired threshold is the difference between a 5-minute settle and a 40-minute one. Every baby's signal set is different; only observation finds hers."),
    ("Do one thing badly", "Take over a task you normally leave to her mother, and accept doing it worse for a fortnight.",
     "Competence gaps between parents are self-reinforcing — the more skilled parent does more, and the gap widens. Deliberately absorbing the early incompetence is how it closes."),

    ("Point at things", "Point at an object and name it, then look at her to check she followed your finger.",
     "Following a point is a joint-attention milestone that typically emerges between 9 and 12 months. Practising the gesture now builds toward it, and delayed joint attention is one of the earlier developmental flags worth knowing about."),
    ("Give her the spoon", "Let her hold a second spoon while you feed her with the first.",
     "It converts feeding from something done to her into something she participates in. Control over the process reduces resistance later, when refusal becomes a real phase."),
    ("Wait out the frustration noise", "When she grumbles at a task, wait. Only step in if it escalates to genuine distress.",
     "There is a difference between frustration and distress, and learning to tell them apart is a skill you build now. Frustration tolerated becomes persistence; frustration always rescued becomes helplessness."),
    ("One-word labels", "Use single words for objects she sees constantly — 'cup', 'light', 'dog' — rather than full sentences.",
     "Isolated words are easier to segment from the speech stream. Mix with full sentences; she needs both the target and the grammar around it."),
    ("Vary who holds her", "Hand her to grandparents, friends, and colleagues while you stay visible in the room.",
     "Stranger wariness peaks around now. Practising handovers while you remain in sight teaches that unfamiliar people are safe, without forcing separation at the same time."),
    ("Describe her, not just the world", "Narrate what she is doing: 'you are banging the cup, you are looking at me.'",
     "Self-directed narration builds the vocabulary of internal states. It is the same mechanism as labelling emotions, applied to actions first because they are observable."),
    ("Build then let her destroy", "Stack three blocks, let her knock them down, rebuild. Repeat until she loses interest.",
     "Cause and effect is the entire lesson. Knocking down precedes building by months — do not rush to the constructive half."),
    ("Check the floor at her eye level", "Lie on the floor and look across the room from her height. Remove what you find.",
     "Mobility arrives suddenly. Anything reachable at that height will be in her mouth within days of her crawling, and you will not get advance notice."),
    ("Read her a real book", "Read something you are reading, aloud, for five minutes. Content is irrelevant.",
     "Prosody, turn-taking and attention are carried by adult speech regardless of vocabulary. It also means reading time survives on days when the board pack is due."),
    ("Sit her up and step back", "Sit her unsupported on a soft surface within arm's reach, and let her balance.",
     "Independent sitting typically consolidates around 7–8 months. Constant propping delays the postural adjustments she needs to make herself."),
    ("Introduce water in an open cup", "Offer a few sips from a small open cup, held by you. Expect most of it to spill.",
     "Open-cup drinking develops different oral motor patterns than a bottle or spout. Starting now, badly, is easier than starting later, correctly."),
    ("Do bedtime alone once this week", "Handle the entire routine solo — no handover, no rescue.",
     "Solo caregiving builds a distinct relationship rather than an assisted one. It also gives her mother an uninterrupted evening, which is not a small thing at seven months."),
    ("Notice what she is not doing", "Once a week, check the milestone list rather than assuming.",
     "Sitting, transferring objects hand to hand, responding to her name, and babbling with consonants are the seven-month markers. Knowing the list means concerns get raised early, when intervention works best."),
    ("Let her see you fail", "When you drop or fumble something in front of her, react calmly and try again visibly.",
     "Emotional regulation is learned by observation long before instruction. Your reaction to small frustrations is a template she is already recording."),
    ("Sing the same song at the same moment", "Attach one song to one transition — nappy change, bath, car seat.",
     "Predictable audio cues reduce transition resistance because she knows what comes next. It is the cheapest behavioural tool available at this age."),
    ("Massage after the bath", "Two minutes of firm, slow strokes on legs and back after bathing.",
     "Sustained touch by a father has measurable effects on infant stress response, and it gives you a reliable daily block of contact that is not feeding or settling."),
    ("Put her in front of a mirror", "Hold her at a mirror and name both of you.",
     "Self-recognition does not arrive until around 18 months, so she is currently treating the reflection as another baby — which is exactly why it holds her attention and drives social behaviours."),
    ("Talk to her about your day", "Tell her what happened at work, in full sentences, as if she understands.",
     "The content is irrelevant and the exposure is not. It also forces you to articulate the day, which is a decent end-of-work ritual in its own right."),
    ("Stop at eye contact", "When she catches your eye across the room, stop and respond — smile, wave, say her name.",
     "Social referencing is beginning: she checks your face to decide how to feel about things. Responding consistently makes you a reliable reference point."),
    ("Give her something to solve", "Put a favourite toy just inside an open container so she has to reach in.",
     "Means-end reasoning — using one action to achieve a separate goal — is developing now. Small solvable problems build it; unsolvable ones just frustrate."),
    ("Take a photo of the ordinary", "Photograph something unremarkable — her on the floor, mid-task — rather than a milestone.",
     "You will have hundreds of milestone photos and almost none of the texture of daily life at seven months, which is what you will actually want later."),
    ("Ask her mother what she needs", "Ask directly, once, and act on the answer without negotiating it.",
     "The most reliable predictor of a child's early environment is the functioning of the partnership around them. This is a fatherhood task, not a marital one."),
]

# ═══════════════════════════════════════════════════════════════════════
# WISDOM — Jainism and Buddhism, as operating instructions rather than
# theology. Each carries the source concept and a concrete application.
# ═══════════════════════════════════════════════════════════════════════

WISDOM = [
    ("Jainism", "Anekantavada", "Many-sidedness",
     "Reality has more facets than any single viewpoint can hold. The classic image is blind men describing an elephant — each is accurate and each is incomplete.",
     "In a disagreement, state the other person's position better than they did before you argue against it. If you cannot, you have not understood it yet."),
    ("Jainism", "Aparigraha", "Non-attachment / non-possessiveness",
     "Accumulation beyond need becomes a burden that owns you. The vow is not poverty; it is limiting want deliberately.",
     "Set a ceiling before you need one. Decide what income is enough now, in a number, so that lifestyle does not silently absorb every increment."),
    ("Jainism", "Ahimsa", "Non-violence in thought, word and act",
     "The most demanding version is not physical — it is refusing to injure with speech or intent, including toward yourself.",
     "Notice the sentence you would not say to a colleague, then check whether you say it to yourself about your own work. The standard should be the same."),
    ("Jainism", "Syadvada", "Conditioned assertion — 'in some respect'",
     "Every claim is true only under stated conditions. Jain logic prefixes assertions with 'in some respect' to keep the conditions visible.",
     "Attach the assumption to the forecast, out loud: 'growth is 12% — in the respect that the two new stores open in Q3.' Half of forecasting disputes are actually undeclared conditions."),
    ("Jainism", "Kshama", "Forgiveness, asked and given",
     "Once a year, at Michchhami Dukkadam, Jains ask forgiveness of everyone they may have harmed — regardless of whether they feel at fault.",
     "The ritual removes the negotiation over who was more wrong. Do not wait for the annual version: close a grudge this week without settling the scoreboard first."),
    ("Buddhism", "Dukkha", "Unsatisfactoriness",
     "The first noble truth is not 'life is suffering' but that conditioned things do not stay satisfying. The promotion satisfies briefly, then resets.",
     "Before chasing the next milestone, name honestly what the last one actually changed and for how long. Then decide if you still want it."),
    ("Buddhism", "Anicca", "Impermanence",
     "Everything that arises passes. Applied properly this cuts both ways — bad quarters end, and so do good ones.",
     "In a drawdown or a bad week, ask what will be true in six months. Most acute distress is a mis-estimate of duration."),
    ("Buddhism", "The Second Arrow", "Suffering added to pain",
     "The Buddha's image: struck by an arrow, most people fire a second one into themselves — the story about what the first arrow means.",
     "A stopped-out trade is the first arrow. 'I am bad at this' is the second. Only one of them was caused by the market."),
    ("Buddhism", "Right Speech", "Truthful, useful, timely, kind",
     "The test has four parts, and speech must pass all of them. True but cruel fails. True and kind but badly timed also fails.",
     "Before delivering hard feedback, run all four. Most feedback that lands badly is true and useful but fails on timing."),
    ("Buddhism", "Beginner's Mind", "Shoshin",
     "'In the beginner's mind there are many possibilities; in the expert's there are few.' Expertise narrows the field of what you consider.",
     "In your area of deepest expertise, ask the question you would find embarrassing to ask. Ten years in one industry is where assumptions calcify."),
    ("Buddhism", "Middle Way", "Avoiding both extremes",
     "The Buddha rejected both indulgence and severe asceticism after trying each. The insight came from testing both, not theorising.",
     "Applied to risk: neither all-cash nor all-in. Applied to work: neither burnout nor coasting. The middle is a position you have to keep adjusting, not one you find once."),
    ("Jainism", "Samyak Darshana", "Right perception",
     "Seeing things as they are, before preference distorts them. The first of the three jewels, and deliberately placed before right conduct.",
     "Separate the number from what you want the number to be. Write the actual result before writing the explanation — the order changes the explanation."),
    ("Buddhism", "Metta", "Loving-kindness, deliberately extended",
     "A practice of extending goodwill outward in rings — to yourself, someone loved, someone neutral, someone difficult.",
     "The difficult person is the point of the exercise. Pick the colleague who frustrates you most and find one thing they are genuinely right about."),
    ("Jainism", "Sallekhana's principle", "Gradual reduction, not sudden renunciation",
     "The Jain tradition values incremental letting-go over dramatic gestures — small consistent reduction beats one grand sacrifice.",
     "Do not overhaul the budget in one heroic month. Cut one recurring thing permanently. Repeat next month. Sustainable beats spectacular."),
    ("Buddhism", "Upekkha", "Equanimity",
     "The fourth of the sublime states. Not indifference — full engagement without being destabilised by the outcome.",
     "Judge the decision by the process you followed, not by how it turned out. A good decision with a bad outcome is still a good decision; conflating them is how you learn the wrong lesson."),
    ("Jainism", "Asteya", "Non-stealing, widely defined",
     "Extends beyond property to taking what was not freely given — credit, time, attention.",
     "Attribute the idea to the person who had it, in the meeting, by name. Taking credit is the most common form and the least noticed."),
    ("Buddhism", "Sati", "Mindfulness as remembering",
     "Sati literally means 'to remember' — remembering to notice what is actually happening, rather than the narrative running over it.",
     "Once today, mid-task, stop and name what you are actually doing versus what you told yourself you would be doing. The gap is the data."),
    ("Buddhism", "Appamada", "Diligence, heedfulness",
     "The Buddha's last words were reportedly about this — striving on with diligence. It is the opposite of drifting.",
     "Diligence is not intensity, it is not letting things run on autopilot. Audit one recurring commitment this week and ask whether you would start it today."),

    ("Jainism", "Satya", "Truthfulness",
     "The second great vow. Jain teaching qualifies it sharply: truth that causes harm is not virtue, so silence is sometimes the higher form of satya.",
     "Before delivering a hard truth, ask whether you are serving accuracy or serving yourself. Both can be honest; only one is useful."),
    ("Jainism", "Brahmacharya", "Restraint, classically of the senses",
     "For laypeople this is moderation rather than abstinence — the discipline of not letting appetite set the agenda.",
     "Pick the appetite that currently sets your agenda — the phone, the market screen, the second helping — and put one structural limit on it. Not willpower; a limit."),
    ("Jainism", "Karma as physical residue", "Action leaves a trace",
     "Jain metaphysics treats karma as fine matter that adheres to the soul through action and intent. Unusually, intent matters as much as the act.",
     "Judge your own decisions on intent and process, not only outcome. A profitable trade taken for bad reasons still deposits the bad habit."),
    ("Jainism", "Aparigraha for time", "Non-possessiveness applied to hours",
     "The vow limits accumulation. Applied beyond property, hoarding commitments is a form of grasping — saying yes to everything is acquisitiveness in a respectable costume.",
     "Say no to one thing this week that you would normally accept out of reflex rather than interest."),
    ("Jainism", "Samyak Charitra", "Right conduct",
     "The third jewel, and deliberately placed after right perception and right knowledge — you cannot act well on a false picture.",
     "When a decision feels hard, check whether it is genuinely a conduct problem or actually a perception problem you have not resolved. Most are the latter."),
    ("Jainism", "Ahimsa in speech", "Non-violence applied to words",
     "Jain tradition treats harsh speech as a form of violence, listing it alongside physical harm rather than as a lesser category.",
     "Review one message you sent under pressure this week. Would you send it unchanged now? The gap between those two versions is the practice."),
    ("Jainism", "Anuvrata", "The lesser vows",
     "Householders take limited versions of the great vows rather than the monastic form — the tradition explicitly designs for people with jobs and families.",
     "Do not adopt a practice you cannot sustain alongside work and a seven-month-old. A small vow kept beats a large one abandoned."),
    ("Jainism", "Pratikramana", "Ritual review and turning back",
     "A periodic practice of reviewing one's conduct, acknowledging where one fell short, and resolving to correct it. Structurally, an audit.",
     "You already run a monthly close on the business. Run a fifteen-minute one on yourself: what did I get wrong, what am I changing, what is the date."),
    ("Jainism", "Trishna", "Craving, thirst",
     "The endless wanting that survives every acquisition. Jain and Buddhist thought agree closely here — the object changes, the wanting does not.",
     "Notice the next thing you tell yourself you will be satisfied after getting. Write it down. Check in six months whether the satisfaction arrived."),

    ("Buddhism", "Anatta", "Non-self",
     "The doctrine that there is no fixed, unchanging self — what feels like a permanent 'I' is a process, continually reconstructed.",
     "When you catch yourself saying 'I am not a numbers-under-pressure person', notice that is a description of a habit, not a fact about you."),
    ("Buddhism", "Right Livelihood", "Work that does not require harm",
     "One of the eightfold path's least discussed elements: how you earn matters as much as how you behave outside earning.",
     "Ask whether any part of your work depends on someone else not understanding something. That is the test, and it applies to finance more than most fields."),
    ("Buddhism", "Right Effort", "Applied in the correct direction",
     "Effort is one of the eight, but so is rightness of effort. Energy in the wrong direction is not neutral; it entrenches the error.",
     "Before adding hours to a problem, spend ten minutes asking whether the problem is the right one. Most overwork is misdirection, not insufficiency."),
    ("Buddhism", "The Two Truths", "Conventional and ultimate",
     "Something can be true conventionally and empty ultimately — a table is a table for practical purposes and a collection of parts on analysis. Both are needed.",
     "Hold the forecast as both real enough to act on and provisional enough to abandon. Treating a model as ultimately true is how people ride a position down."),
    ("Buddhism", "Kalama Sutta", "Test it yourself",
     "The Buddha told the Kalamas not to accept teaching on authority, tradition, or scripture — but to test whether it leads to benefit when practised.",
     "Apply it to strategy advice, including from people senior to you. What is the evidence, and what would falsify it? Deference is not evidence."),
    ("Buddhism", "Mudita", "Sympathetic joy",
     "The third sublime state: genuine pleasure at another's success. Named separately because it is rarer and harder than compassion for suffering.",
     "Pick the peer whose progress you find least comfortable and find the thing they did well. Envy is the most common emotion in a competitive finance function and the least admitted."),
    ("Buddhism", "Papañca", "Mental proliferation",
     "The mind's tendency to spin a single perception into an elaborate narrative — one ambiguous email becoming a theory about your standing at work.",
     "When you notice the story running, return to what actually happened, stated in one sentence with no interpretation. The gap is usually startling."),
    ("Buddhism", "Impermanence of success", "Anicca applied upward",
     "Impermanence is usually offered as consolation in difficulty. It applies symmetrically: the good quarter, the promotion, the peak are equally conditioned.",
     "On a good week, note it explicitly and hold it lightly. The habit of not over-identifying with wins is what makes losses survivable."),
    ("Buddhism", "Dana", "Generosity, listed first",
     "In the traditional sequence of practices, generosity comes before ethics and meditation — as the foundation rather than the reward.",
     "Give something away before you feel you have surplus. Time or attention counts; the point is that it precedes abundance rather than following it."),
    ("Buddhism", "Right Intention", "The direction before the act",
     "The second element of the path, sitting between right view and right speech — the resolve that shapes what follows.",
     "Before a difficult conversation, name your intention in one sentence. If it is 'to be proved right', the conversation will fail regardless of your evidence."),
]


# ═══════════════════════════════════════════════════════════════════════
# PUBLIC API — each returns what the template renders.
# ═══════════════════════════════════════════════════════════════════════

def get_interview_tech(day: date | None = None) -> list[dict]:
    return [{"q": q, "a": a, "who": w}
            for q, a, w in _pick(INTERVIEW_TECH, 2, 7, day)]


def get_interview_soft(day: date | None = None) -> list[dict]:
    return [{"q": q, "a": a, "who": w}
            for q, a, w in _pick(INTERVIEW_SOFT, 2, 5, day)]


def get_spanish(day: date | None = None) -> list[dict]:
    return [{"word": w, "meaning": m, "es": es, "en": en, "tag": t}
            for w, m, es, en, t in _pick(SPANISH, 2, 11, day)]


def get_vocab(day: date | None = None) -> list[dict]:
    return [{"word": w, "say": s, "meaning": m, "example": e, "note": n}
            for w, s, m, e, n in _pick(VOCAB, 2, 3, day)]


def get_speaking(day: date | None = None) -> dict:
    picks = _pick(SPEAKING_DRILLS, 1, 1, day)
    if not picks:
        return {}
    t, d, w = picks[0]
    return {"title": t, "drill": d, "why": w}


def get_father(day: date | None = None) -> list[dict]:
    return [{"title": t, "do": d, "why": w}
            for t, d, w in _pick(FATHER, 2, 5, day)]


def get_wisdom(day: date | None = None) -> list[dict]:
    """One Jain and one Buddhist every day. Drawing both from a single pool let
    the rotation land on two of the same tradition, which is not what a daily
    practice across both is for — so the traditions are split and picked from
    separately, then interleaved."""
    jain = [w for w in WISDOM if w[0] == "Jainism"]
    budd = [w for w in WISDOM if w[0] == "Buddhism"]
    picked = _pick(jain, 1, 3, day) + _pick(budd, 1, 5, day)
    return [{"tradition": tr, "term": tm, "translation": tl, "teaching": te, "apply": ap}
            for tr, tm, tl, te, ap in picked]


def get_all(day: date | None = None) -> dict:
    return {
        "interview_tech": get_interview_tech(day),
        "interview_soft": get_interview_soft(day),
        "spanish": get_spanish(day),
        "vocab": get_vocab(day),
        "speaking": get_speaking(day),
        "father": get_father(day),
        "wisdom": get_wisdom(day),
        "counts": {
            "interview_tech": len(INTERVIEW_TECH), "interview_soft": len(INTERVIEW_SOFT),
            "spanish": len(SPANISH), "vocab": len(VOCAB), "speaking": len(SPEAKING_DRILLS),
            "father": len(FATHER), "wisdom": len(WISDOM),
        },
    }


def validate() -> list[str]:
    """Shape check on every bank. These are hand-authored tuples, and a dropped
    field is invisible until the page renders — a missing element in one WISDOM
    entry raised 'not enough values to unpack' only at render time. Run this in
    CI so the failure happens at commit, not in front of a reader."""
    problems = []
    expected = [
        ("INTERVIEW_TECH", INTERVIEW_TECH, 3), ("INTERVIEW_SOFT", INTERVIEW_SOFT, 3),
        ("SPANISH", SPANISH, 5), ("VOCAB", VOCAB, 5),
        ("SPEAKING_DRILLS", SPEAKING_DRILLS, 3), ("FATHER", FATHER, 3), ("WISDOM", WISDOM, 5),
    ]
    for name, bank, width in expected:
        for i, row in enumerate(bank):
            if not isinstance(row, tuple):
                problems.append(f"{name}[{i}]: expected tuple, got {type(row).__name__}")
            elif len(row) != width:
                problems.append(f"{name}[{i}]: expected {width} fields, got {len(row)} "
                                f"— starts {str(row[:1])[:60]}")
            elif any(not isinstance(f, str) or not f.strip() for f in row):
                problems.append(f"{name}[{i}]: empty or non-string field")
    # Every tradition must be able to fill its daily slot.
    for tradition in ("Jainism", "Buddhism"):
        n = len([w for w in WISDOM if w and w[0] == tradition])
        if n < 2:
            problems.append(f"WISDOM: only {n} {tradition} entries — need at least 2")
    return problems


if __name__ == "__main__":
    issues = validate()
    if issues:
        print("❌ bank validation failed:")
        for p in issues:
            print("   ", p)
        raise SystemExit(1)
    print("✅ all banks well-formed\n")

    import json
    d = get_all()
    print(json.dumps(d, indent=2, ensure_ascii=False)[:1500])
    print("\nbank sizes:", d["counts"])
    # Rotation sanity: no repeat across consecutive days for the paired tracks.
    t = _today()
    for name, fn in [("tech", get_interview_tech), ("soft", get_interview_soft),
                     ("spanish", get_spanish), ("wisdom", get_wisdom)]:
        seen, dupes = set(), 0
        for i in range(30):
            for item in fn(t + timedelta(days=i)):
                k = str(item)[:60]
                if k in seen:
                    dupes += 1
                seen.add(k)
        print(f"  {name}: {len(seen)} unique over 30 days, {dupes} repeats")
