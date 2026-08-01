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


if __name__ == "__main__":
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
