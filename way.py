"""
way.py — "The Way" section: how to live, not what to trade.

Six rotating daily tracks, deliberately narrow. The Desk compounds the
profession and The Mind sharpens the operator; this one is about conduct —
owning less, behaving well, sitting still, thinking in models, and putting in
one honest rep a day.

    minimalism  — own less so you decide less
    etiquette   — the manners that actually compound
    stillness   — monk practice, stripped of the monastery
    model       — one mental model, Munger latticework
    arabic      — one phrase, Gulf-usable, for the Dubai move
    drill       — one deliberate-practice rep, ~10 minutes

Each list rotates on the ordinal date with a different offset, so the six tracks
never move in lockstep and the same combination does not recur.

Entry shape is (title, body, action) throughout except ARABIC, which is
(script, translit, meaning, when_to_use).
"""
from __future__ import annotations

from datetime import date

# ── MINIMALISM ───────────────────────────────────────────────────────────────
# Not aesthetics. Every object is a small recurring tax on attention.

MINIMALISM = [
    ("One In, One Out",
     "Nothing enters the house until something leaves it. Not a rule about tidiness — a rule about equilibrium. Without it, possessions only ever accumulate, because buying is a decision and discarding never is.",
     "Buy nothing today without naming what it replaces."),
    ("The 90/90 Rule",
     "Have you used it in the last 90 days? Will you in the next 90? If both answers are no, it is storage, not ownership. You are paying rent on it in square feet and attention.",
     "Pick one drawer. Apply 90/90 to every item in it."),
    ("Decision Fatigue Is The Real Cost",
     "A cluttered wardrobe costs you a decision every morning. Obama and Jobs wore the same thing daily for a reason — not style, bandwidth. Every trivial choice you delete is one more available for the choices that matter.",
     "Choose tomorrow's clothes tonight. Notice the morning it buys back."),
    ("Digital Minimalism First",
     "Physical clutter is visible and therefore self-limiting. Digital clutter is infinite. 40,000 unread emails and 200 apps cost more attention than a messy room ever did.",
     "Delete every app you have not opened in 30 days. No exceptions for 'might need'."),
    ("The Empty Surface",
     "Keep one surface in your home completely empty. A desk, a counter, a shelf. It becomes a visual reference point — the moment things start accumulating elsewhere, you notice.",
     "Clear one surface fully. Keep it clear for a week."),
    ("Buy Once, Cry Once",
     "Minimalism is not cheapness. One well-made pair of shoes at 4x the price that lasts 10x as long is both cheaper and lighter. Fewer, better beats more, adequate.",
     "List three things you have replaced more than twice. Buy the good version once."),
    ("The Box Test",
     "Pack a category into a box and seal it. If you have not opened it in a month, you do not need what is inside. Most people cannot name the contents a week later.",
     "Box one category — cables, kitchen gadgets, old chargers. Date the box."),
    ("Owning Is A Liability",
     "Every object requires storage, maintenance, insurance, cleaning, moving, and eventual disposal. The purchase price is the smallest number in that list. Accountants of all people should price the full cost.",
     "Before the next purchase, write the 5-year total cost of ownership."),
    ("Space Is A Feature",
     "Empty space in a room is not wasted space, the same way white space on a page is not wasted paper. Both exist so the things that matter can be seen.",
     "Remove one item from your most crowded shelf. Add nothing back."),
    ("The Someday Pile",
     "'I'll fix it someday.' 'I'll read it someday.' Someday is not a date. The pile is not a to-do list, it is a monument to intentions you have already declined.",
     "Give every someday item a date this month, or let it go today."),
    ("Sentimental ≠ Storage",
     "You are keeping the object to keep the memory. The memory is not in the object. Photograph it, write one line about why it mattered, and release the thing.",
     "Photograph one sentimental item you have not looked at in a year."),
    ("Gifts Are Not Obligations",
     "A gift's job ended the moment it was given. Keeping something unwanted out of guilt honours nobody and costs you space every day.",
     "Release one guilt-kept item. The giver will never know."),
    ("The Uniform",
     "Pick a small set of clothes that always work and buy multiples. Ends the morning decision, ends the mismatched purchase, ends the wardrobe that is 80% unworn.",
     "Define your uniform in one sentence. Buy toward it, not around it."),
    ("Subscription Audit",
     "Recurring charges are the most invisible clutter you own — designed to be forgotten. The average person carries several they cannot name.",
     "Open your statement. Cancel every subscription you cannot justify out loud."),
    ("One Screen, One Task",
     "Seventeen open tabs is not research, it is deferred decisions rendered in pixels. Each one is a small unfinished thought.",
     "Close every tab. Bookmark what genuinely matters — usually two."),
    ("The Cost Of A Bigger House",
     "More space does not reduce clutter; it raises the threshold at which clutter becomes visible. People fill whatever they have. Then they pay to heat, clean, and furnish it.",
     "Before upgrading space, try emptying the space you have."),
    ("Travel Light As Practice",
     "Pack for a week in a carry-on. It teaches, in three days, exactly how little you require — a lesson that transfers directly home.",
     "Next trip: carry-on only. Note what you never opened."),
    ("Say No To Free",
     "Free samples, conference tote bags, hotel toiletries. Free acquisitions carry the same ongoing cost as bought ones. The price tag was never the expensive part.",
     "Decline the next free item you are offered."),
    ("The Deathbed Filter",
     "Nobody has ever wished for a larger television. Possessions almost never make the list of what mattered. Use that as a purchasing filter while there is still time to act on it.",
     "Name three things that would make your list. Spend accordingly."),
    ("Maintenance Is The Tell",
     "If you own more than you can maintain, you do not own it — you are storing it on its way to landfill. Count what you actually maintain.",
     "Identify one thing you own but never maintain. Decide: maintain it or release it."),
]

# ── ETIQUETTE ────────────────────────────────────────────────────────────────
# Manners as compound interest on trust. Especially the Gulf-relevant ones.

ETIQUETTE = [
    ("Learn The Name, Use The Name",
     "Names are the cheapest respect available and the most noticed when withheld. Repeat it immediately on hearing it. Ask the spelling. Ask the pronunciation — getting it wrong twice is careless, asking once is respect.",
     "Learn one colleague's name properly today. Use it twice."),
    ("Reply Within 24 Hours, Even If Only To Acknowledge",
     "A three-word acknowledgement beats a perfect reply a week late. Silence is read as disregard, never as busyness. 'Got this — proper answer Thursday' costs eight seconds.",
     "Clear every message older than 24 hours with an acknowledgement."),
    ("The Right Hand Rule",
     "In the Gulf and much of Asia, give and receive with the right hand — cards, food, documents, money. The left hand carries a specific connotation. This is not a preference; it is the baseline.",
     "Hand over your next document or card with the right hand, deliberately."),
    ("Accept The Coffee",
     "In Gulf business culture, refusing offered coffee or dates can read as refusing the relationship. Take it, hold it, sip it. The transaction is not the point of the meeting.",
     "Accept the next hospitality offered, even if you do not want it."),
    ("Never Be The Loudest In The Room",
     "Volume signals insecurity, not authority. The person everyone leans in to hear holds the room. Competence rarely needs amplification.",
     "In your next meeting, speak one notch quieter than feels natural."),
    ("Praise Publicly, Correct Privately",
     "Reverse it and you buy resentment that outlasts the correction by years. This costs nothing and is violated constantly.",
     "Send one specific public thank-you today. Not generic — specific."),
    ("Punctuality Is A Statement About Whose Time Matters",
     "Being late says your time is worth more than theirs, whatever you intended. Arrive five minutes early and use the wait. Note that Gulf meetings may start late — you still arrive on time.",
     "Add 10 minutes of buffer to every appointment this week."),
    ("Phone Face Down, Or Away",
     "A visible phone halves the perceived quality of a conversation — measurably, in studies. Face down is the minimum. Out of sight is the standard.",
     "Phone in your bag, not on the table, for every conversation today."),
    ("Ask One More Question",
     "Most people wait to talk. The one who asks a second follow-up is remembered as the best conversationalist in the room, having said almost nothing.",
     "Ask a second follow-up before offering your own view."),
    ("Introduce People Properly",
     "Do not just exchange names — give each person a reason to care. 'Sara, this is Ahmed, he rebuilt their close process in six weeks.' You have just made both look good.",
     "Make one introduction today with a reason attached."),
    ("Dress One Notch Above The Room",
     "Not two — two reads as trying. One notch signals you took the occasion seriously. In the Gulf, conservative beats fashionable in every professional setting.",
     "Check tomorrow's outfit against the room you will be in."),
    ("Never Discuss What Someone Earns",
     "Not theirs, not yours, not a third party's. It creates comparison where none existed and it always travels. Especially in the Gulf, where packages vary enormously by nationality and contract.",
     "Deflect the next compensation question with warmth, not information."),
    ("Say The Difficult Thing Kindly And Once",
     "Softening it into unrecognisability is not kindness — it guarantees you say it again, worse, later. Direct and warm. Once.",
     "Name one thing you have been avoiding saying. Say it plainly today."),
    ("Handle The Bill Without Theatre",
     "Decide before you arrive. Settle it quietly. The public wrestle over a bill embarrasses everyone at the table.",
     "Settle the next bill before it reaches the table."),
    ("Remember The Detail",
     "Their daughter's exam. Their father's surgery. Ask next time. This is the single highest-return social habit and almost nobody keeps notes.",
     "Write down one personal detail after your next conversation."),
    ("Ramadan Awareness",
     "Do not eat, drink, or smoke in public during fasting hours. Schedule meetings earlier. Expect shortened hours. Wishing colleagues Ramadan Kareem costs nothing and is noticed.",
     "Check the Islamic calendar. Know what month it is."),
    ("Thank The Invisible People",
     "The security guard, the cleaner, the driver, the tea boy. How you treat people who cannot advance your career is the only honest read on your character — and senior people watch for it specifically.",
     "Learn the name of one person whose name you have never asked."),
    ("Do Not Correct Trivia In Public",
     "Being right about something small in front of an audience costs more in goodwill than the correction is worth. Let it go, or say it privately later.",
     "Let one minor inaccuracy pass uncorrected today."),
    ("Write The Follow-Up",
     "After any meeting that mattered: three lines confirming what was agreed and who does what. It prevents disputes and marks you as the reliable one. Almost nobody does it.",
     "Send a three-line recap after your next meeting."),
    ("Leave Well",
     "How you exit a job, a room, or a conversation is what people remember. Notice periods honoured, handovers documented, no bridges burned. The world is smaller than it feels.",
     "Leave your next meeting with a clear closing sentence, not a trailing off."),
]

# ── STILLNESS ────────────────────────────────────────────────────────────────
# Simple living, high thinking. Monastic practice with the monastery removed.

STILLNESS = [
    ("The First Hour Is Yours",
     "Whoever touches your attention first sets the agenda for the day. If that is a notification, you have outsourced the day to a stranger's priorities before you were fully awake.",
     "No screen for the first 30 minutes tomorrow. Nothing else changes."),
    ("Sit For Ten Minutes",
     "Not to achieve anything. Sit, breathe, and let thoughts pass without following them. The mind wandering is not failure — noticing it wandered is the entire rep.",
     "Ten minutes. Timer on. No app required."),
    ("Eat One Meal In Silence",
     "No phone, no screen, no conversation. Taste the food. Most meals are consumed while attention is elsewhere, which is why they satisfy so little.",
     "One meal today, fully attended."),
    ("Walk Without Input",
     "No podcast, no music, no call. Boredom is the raw material of original thought, and it has been almost entirely eliminated from modern life.",
     "Twenty minutes of walking with nothing in your ears."),
    ("The Evening Shutdown",
     "A defined end to the workday — review tomorrow's three priorities, close the laptop, say a phrase that marks the boundary. Without a ritual end, work bleeds into every remaining hour.",
     "Pick a shutdown phrase. Use it tonight."),
    ("Memento Mori",
     "You will die. Marcus Aurelius wrote it to himself daily while running an empire. Not morbid — clarifying. It sorts the urgent from the important in about four seconds.",
     "Ask of one commitment: would this matter if I had a year left?"),
    ("Voluntary Discomfort",
     "The Stoics practised poverty deliberately — plain food, cold, hard beds. Not self-punishment. Rehearsal, so that losing comfort stops being frightening.",
     "Cold shower. Or skip one meal. Notice it is survivable."),
    ("The Sabbath Principle",
     "One day in seven with no optimisation, no side project, no market. Every durable tradition independently arrived at this. They were not being inefficient.",
     "Block one screen-free half-day this week. Defend it."),
    ("Silence Is A Position",
     "You are not required to have an opinion on everything you encounter. 'I don't know enough about that' is a complete and unusually strong answer.",
     "Decline to have an opinion on one thing today."),
    ("Desire Is The Contract",
     "Every want you accept is a contract to be unhappy until it is satisfied. Most were installed by advertising, not chosen. Audit the source before signing.",
     "Name one want. Trace where it came from. Decide if it is yours."),
    ("Do One Thing At A Time",
     "The monastic instruction is: when walking, walk; when eating, eat. Multitasking is the belief that attention divides without loss. It does not.",
     "Do the next task with nothing else open."),
    ("Keep A Line A Day",
     "One sentence about the day. Not a journal habit that collapses in a week — one line. In ten years it becomes the only accurate record of your life that exists.",
     "Write one line tonight. Date it."),
    ("Enough Is A Number",
     "Without a defined 'enough', more is the only direction and the target moves with every increment. AED 30K/month is a number. Undefined ambition is not a goal, it is a treadmill.",
     "Write your 'enough' figure. Date it. Revisit in a year."),
    ("Attention Is The Life",
     "What you pay attention to is, quite literally, your experience of being alive. Spend it on what you would choose to have lived through.",
     "Audit yesterday's screen time. That number is a portion of your life."),
    ("The Half-Smile",
     "Thich Nhat Hanh's practice: a slight smile, held, changes state faster than most reasoning about your mood. The body leads the mind more often than the reverse.",
     "Half-smile for one full minute. Notice the shift."),
    ("Impermanence Applies To The Bad Too",
     "The difficult period ends. So does the good one. This is not pessimism — it is the only reliable source of equanimity in both directions.",
     "Name the current difficulty. Say plainly: this also ends."),
    ("Solitude Is Not Loneliness",
     "Solitude is time free of other minds' input. It is where thinking happens. A room full of people and a phone full of voices are the same interruption.",
     "Take 30 minutes alone with no input. Not rest — thinking."),
    ("Serve Without Being Seen",
     "Do one useful thing today that nobody will attribute to you. Every tradition prescribes this because it is the fastest available correction to the ego.",
     "One anonymous useful act. Tell no one."),
    ("Simplify The Morning",
     "Same breakfast. Same order. Same route. Ritual removes decisions from the hours when your judgement is most valuable and least replaceable.",
     "Fix one morning variable permanently."),
    ("High Thinking Requires Simple Living",
     "The phrase is one instruction, not two. Complexity of life consumes exactly the capacity that deep thought requires. Cut the first to afford the second.",
     "Name the most complicated part of your week. Remove one layer."),
]

# ── MENTAL MODELS ────────────────────────────────────────────────────────────
# Munger's latticework. One model a day, with the failure mode it prevents.

MENTAL_MODELS = [
    ("Inversion",
     "Solve the problem backwards. Instead of 'how do I succeed?', ask 'how would I guarantee failure?' — then avoid that. The failure list is concrete and short; the success list is vague and infinite.",
     "Invert your biggest current goal. Write the three surest ways to fail it."),
    ("Second-Order Thinking",
     "And then what? Most bad decisions are first-order correct. Price caps look like they help until supply disappears. Ask 'and then what?' three times before committing.",
     "Take today's main decision three steps forward."),
    ("Opportunity Cost",
     "The real cost of anything is the best alternative you gave up. Cash sitting idle has a cost. So does a meeting. So does a project you keep alive out of habit.",
     "Name what your largest time commitment is displacing."),
    ("Base Rates",
     "Before believing your case is special, ask what usually happens. Most restaurants fail. Most projects overrun. Most traders lose. Your plan starts from that number, not from zero.",
     "Find the base rate for your current bet before adjusting for your edge."),
    ("Circle Of Competence",
     "The boundary matters more than the size. Knowing precisely where your knowledge ends is worth more than extending it slightly. Most large losses happen just outside the edge.",
     "Draw your circle. Name one thing just outside it you have been treating as inside."),
    ("Margin Of Safety",
     "Build the bridge for 30 tonnes and drive 10 across it. Applies to leverage, deadlines, cash buffers, and assumptions in a model. The error is always in the direction you did not plan for.",
     "Add 50% buffer to your next estimate. Do not tell yourself it is padding."),
    ("Incentives",
     "Show me the incentive and I will show you the outcome. Never explain by malice or stupidity what the incentive structure fully predicts. Ask who gets paid for this to happen.",
     "For one recommendation you received, map who benefits from you following it."),
    ("Compounding",
     "Small consistent gains dominate large sporadic ones, and the effect is invisible until suddenly it is not. The counterintuitive part: nearly all the value arrives at the end.",
     "Identify one habit that compounds. Protect it above anything urgent."),
    ("Confirmation Bias",
     "You will scrutinise disagreeing evidence and wave through agreeing evidence. Knowing this does not stop it. The only defence is to seek the counter-case deliberately.",
     "Find the strongest argument against your current position. Steelman it."),
    ("Regression To The Mean",
     "Extreme results are followed by less extreme ones, with no cause required. Most 'the intervention worked' stories are regression wearing a costume.",
     "Before crediting a change, ask what would have happened anyway."),
    ("Occam's Razor",
     "The explanation requiring fewest assumptions is usually right. Elaborate theories feel like insight and are mostly the mind pattern-matching on noise.",
     "Take your most complex current explanation. Find the simpler one."),
    ("Hanlon's Razor",
     "Never attribute to malice what incompetence, tiredness, or a missed email explains. The uncharitable read is usually wrong and always expensive.",
     "Reinterpret one recent slight as a mistake rather than an intent."),
    ("Availability Heuristic",
     "You judge probability by how easily an example comes to mind. Plane crashes feel likelier than car crashes because they are memorable, not because they are common.",
     "Check one belief against actual frequency data rather than recall."),
    ("Loss Aversion",
     "Losses hurt roughly twice as much as equivalent gains please. This makes you hold losing positions and sell winners — precisely backwards, and reliably so.",
     "Review one holding you are keeping only because selling would confirm the loss."),
    ("Sunk Cost",
     "Money and time already spent are gone and should not influence the next decision. The only question is the value from here forward.",
     "Name one commitment you continue solely because of what it has already cost."),
    ("Pareto Principle",
     "80% of results come from 20% of inputs — and the distribution is often sharper than that. The work is identifying the 20%, which requires measuring rather than assuming.",
     "Find the 20% of your effort producing most of the result. Cut something from the rest."),
    ("Map Is Not The Territory",
     "The model is a simplification and will be wrong at the edges. A financial model is a map. Treating its outputs as facts is how forecasts become fiction.",
     "Name the assumption in your current model most likely to be wrong."),
    ("Via Negativa",
     "Improvement by removal. Subtracting a bad habit, a bad client, or a bad process is more reliable than adding a good one, and the effect is immediate.",
     "Remove one thing today rather than adding an improvement."),
    ("Bottleneck Theory",
     "A system moves at the speed of its slowest constraint. Optimising anything else produces zero throughput gain and feels productive, which is why it is so common.",
     "Identify the real bottleneck in your week. Work only on that."),
    ("Skin In The Game",
     "Discount any forecast from someone who bears no cost for being wrong. Advice without exposure is entertainment, however credentialed the source.",
     "For your next major input, ask what the adviser loses if they are wrong."),
]

# ── ARABIC ───────────────────────────────────────────────────────────────────
# (script, transliteration, meaning, when to use). Gulf-usable, MSA where it
# matters, Khaleeji where that is what people actually say.

ARABIC = [
    ("السلام عليكم", "as-salaamu alaykum", "Peace be upon you", "The universal greeting. Reply: wa alaykum as-salaam. Use it entering any room or meeting."),
    ("شكراً جزيلاً", "shukran jazeelan", "Thank you very much", "Stronger than plain shukran. Use after someone has genuinely helped you."),
    ("من فضلك", "min fadlak", "Please", "min fadlik to a woman. Attach it to every request and you will be treated differently."),
    ("إن شاء الله", "in shaa Allah", "God willing", "Attached to any future plan. Culturally expected — its absence sounds presumptuous about the future."),
    ("الحمد لله", "al-hamdu lillah", "Praise be to God", "The standard answer to 'how are you'. Also used on hearing good news."),
    ("ما شاء الله", "maa shaa Allah", "What God has willed", "Said when admiring something — a child, a success, a new car. Omitting it can read as envy."),
    ("يعطيك العافية", "ya'teek al-'aafya", "May God give you strength", "Khaleeji. Said to someone who has worked hard or is finishing a shift. Warm and very local."),
    ("تفضل", "tafaddal", "Please, go ahead / here you are", "Offering a seat, food, or the floor in a meeting. tafaddali to a woman."),
    ("كم السعر؟", "kam as-si'r?", "What is the price?", "Markets, taxis, any negotiation. Follow with a pause, not a counter."),
    ("لو سمحت", "law samaht", "Excuse me / if you please", "Getting attention politely — a waiter, a stranger, someone in your way."),
    ("عفواً", "'afwan", "You're welcome / excuse me", "Reply to shukran. Also used to apologise lightly or to interrupt."),
    ("آسف", "aasif", "Sorry", "aasifa if you are a woman. For genuine apology, not for bumping someone."),
    ("كيف حالك؟", "kayf haalak?", "How are you?", "kayf haalik to a woman. In the Gulf you will also hear shloonak — more colloquial, warmer."),
    ("بكم هذا؟", "bikam haadha?", "How much is this?", "Alternative to kam as-si'r. Common in shops."),
    ("ممكن", "mumkin", "Possible / may I", "Enormously useful on its own. 'Mumkin?' while gesturing covers most requests."),
    ("خلاص", "khalaas", "Enough / done / finished", "Ends a discussion, confirms completion, or stops a child. One of the most-used words in the Gulf."),
    ("يلا", "yalla", "Let's go / come on", "Universal. Starting a meeting, leaving a room, urging someone along."),
    ("حبيبي", "habeebi", "My dear", "habeebti to a woman. Used constantly between friends and colleagues. Read the relationship before using it."),
    ("مبروك", "mabrook", "Congratulations", "Promotions, weddings, new car, new baby. Reply: Allah yibaarik feek."),
    ("الله يعطيك الخير", "Allah ya'teek al-khayr", "May God give you good", "A warm thank-you with more weight than shukran. Good for someone senior."),
    ("ما أعرف", "maa a'rif", "I don't know", "Say it plainly. Pretending to know is worse in every culture, and obvious in all of them."),
    ("أتكلم عربي شوي", "atakallam 'arabi shwaya", "I speak a little Arabic", "Disarming and appreciated. The effort matters far more than the fluency."),
    ("على فكرة", "'ala fikra", "By the way", "Introduces a point mid-conversation without hijacking it."),
    ("إن شاء الله خير", "in shaa Allah khayr", "God willing, it will be good", "Said when an outcome is uncertain. Graceful and appropriate almost anywhere."),
    ("صباح الخير", "sabaah al-khayr", "Good morning", "Reply: sabaah an-noor. Use it before anything else in the morning."),
    ("مساء الخير", "masaa' al-khayr", "Good evening", "Reply: masaa' an-noor. From roughly midday onward."),
    ("مع السلامة", "ma'a as-salaama", "Go in peace / goodbye", "The standard farewell. Said to the person leaving."),
    ("إن شاء الله نلتقي", "in shaa Allah naltaqi", "God willing, we will meet", "Closing a first meeting warmly without committing to a date."),
    ("بالتوفيق", "bit-tawfeeq", "Good luck / wishing you success", "Before an exam, an interview, a launch. Sincere and common."),
    ("والله", "wallah", "By God / seriously", "Emphasis. 'Wallah?' means 'seriously?'. Extremely common; use lightly."),
]

# ── DRILLS ───────────────────────────────────────────────────────────────────
# One deliberate-practice rep, ~10 minutes, on a skill that pays.

DRILLS = [
    ("Mental Math: 1% Anchoring",
     "Compute 1% of a number, then scale. 1% of 847,000 is 8,470. So 3% is 25,410 and 0.5% is 4,235. Fluency here makes you visibly faster in meetings than people with better degrees.",
     "Ten numbers. Find 1%, 3%, and 15% of each. Under 60 seconds total."),
    ("Rule of 72",
     "Years to double = 72 ÷ rate. At 12%, six years. At 8%, nine. Lets you sanity-check any growth or return claim instantly, without a spreadsheet.",
     "Apply it to five return rates. Then invert: what rate doubles in 4 years?"),
    ("Read A Cash Flow Statement First",
     "Profit is an opinion; cash is a fact. Most analysts start at revenue. Start at operating cash flow and compare it to net income — the gap is where the story is.",
     "Pull one company's cash flow. Find the largest gap to net income. Explain it."),
    ("The 3-Statement Link",
     "Net income flows to retained earnings and to the top of cash flow. Closing cash lands on the balance sheet. If you cannot recite this cold, every model you build is fragile.",
     "Draw all three statements and every link from memory. No reference."),
    ("Explain It To A Ten-Year-Old",
     "Take the most technical thing you know and explain it with no jargon. If you cannot, you have memorised it rather than understood it. This is the Feynman technique and it is brutal.",
     "Pick one concept. Explain it in writing with no technical terms."),
    ("Estimate Before Calculating",
     "Guess the answer before opening the spreadsheet. Then compare. This builds the instinct that catches a model error before the client does.",
     "Estimate three figures before computing. Track how close you were."),
    ("Keyboard Only",
     "Work for ten minutes with no mouse. Excel and modelling speed is mostly navigation, and navigation is mostly shortcuts you have not learned yet.",
     "Ten minutes, mouse unplugged. Note every shortcut you had to look up."),
    ("Write The One-Line Summary",
     "Every analysis compresses to one sentence with a number in it. 'Margin fell 340bps because freight doubled.' If you cannot write that line, the analysis is not finished.",
     "Compress your most recent piece of work to one sentence with one number."),
    ("The Pre-Mortem",
     "Imagine the project failed. Write why. Doing this before starting surfaces risks that no optimistic planning session will ever reach.",
     "Pre-mortem your current priority. List three causes of death."),
    ("Steelman The Other Side",
     "Argue the opposing position better than its advocates do. Until you can, you do not understand your own position — you just prefer it.",
     "Write the strongest case against a view you hold firmly."),
    ("Recall, Don't Reread",
     "Close the material and write what you remember. Retrieval builds memory; rereading builds only the feeling of knowing, which is why students who reread do worse.",
     "Close the last thing you studied. Write everything you recall. Then check."),
    ("Sensitivity, Not Point Estimates",
     "A single number is a guess dressed up. Run it at -20%, base, and +20% and present the range. Nobody credible presents one number.",
     "Take one forecast. Build the three cases. Note which input moves it most."),
    ("Name The Assumption",
     "Every model rests on two or three assumptions doing all the work. Find them. The rest is arithmetic and does not deserve your review time.",
     "Find the two assumptions carrying your current model."),
    ("Speak For 60 Seconds, No Filler",
     "Record yourself explaining something for a minute with no 'um', 'like', or 'basically'. Silence between sentences reads as authority. Filler reads as uncertainty.",
     "Record 60 seconds. Count the filler words. Repeat until zero."),
    ("Reverse-Engineer A Good Model",
     "Take a model you admire and rebuild its structure from scratch. Copying output teaches nothing; reconstructing the logic teaches everything.",
     "Rebuild one section of a model you did not write."),
    ("The Five Whys",
     "Ask why five times to get from symptom to cause. Most analysis stops at why one and fixes a symptom, which guarantees the problem returns.",
     "Take one recurring problem. Ask why five times. Write each answer."),
    ("Number Sense: Orders Of Magnitude",
     "Is that number plausible? A company with 200 staff and AED 4bn revenue implies AED 20m per head — almost certainly wrong. This check catches more errors than any other.",
     "Sanity-check five figures by per-unit magnitude."),
    ("Write The Email In Three Lines",
     "Context, ask, deadline. Long emails do not get read, they get postponed. Brevity is a service to the reader and it gets you answers faster.",
     "Rewrite your longest pending email in three lines."),
    ("Teach One Thing",
     "Explaining to another person exposes every gap instantly. Teaching is the highest-return study method measured, and it costs ten minutes.",
     "Teach one concept to someone today. Note where you stumbled."),
    ("Review Your Own Work Cold",
     "Leave it 24 hours, then review as if a stranger wrote it and you are looking for errors. You will find them. Reviewing immediately finds nothing.",
     "Re-open yesterday's work. Find three things to fix."),
]


# ── Accessors ────────────────────────────────────────────────────────────────
# Distinct offsets so the six tracks do not rotate in lockstep.

def _pick(items, offset):
    idx = (date.today().toordinal() + offset) % len(items)
    return idx, items[idx]


def _std(items, offset, label):
    idx, (title, body, action) = _pick(items, offset)
    return {"title": title, "body": body, "action": action,
            "index": idx + 1, "total": len(items), "label": label}


def get_minimalism() -> dict:
    return _std(MINIMALISM, 5, "Minimalism")


def get_etiquette() -> dict:
    return _std(ETIQUETTE, 11, "Etiquette")


def get_stillness() -> dict:
    return _std(STILLNESS, 29, "Stillness")


def get_mental_model() -> dict:
    return _std(MENTAL_MODELS, 41, "Mental Model")


def get_drill() -> dict:
    return _std(DRILLS, 53, "Drill")


def get_arabic() -> dict:
    idx, (script, translit, meaning, use) = _pick(ARABIC, 67)
    return {"script": script, "translit": translit, "meaning": meaning,
            "use": use, "index": idx + 1, "total": len(ARABIC),
            "label": "Arabic"}


def get_way() -> dict:
    """Everything The Way section needs, in one call."""
    return {
        "minimalism": get_minimalism(),
        "etiquette":  get_etiquette(),
        "stillness":  get_stillness(),
        "model":      get_mental_model(),
        "arabic":     get_arabic(),
        "drill":      get_drill(),
    }


def coverage() -> dict:
    return {"minimalism": len(MINIMALISM), "etiquette": len(ETIQUETTE),
            "stillness": len(STILLNESS), "models": len(MENTAL_MODELS),
            "arabic": len(ARABIC), "drills": len(DRILLS)}
