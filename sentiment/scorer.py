"""Score a headline by its likely effect on GBP/USD.

Two scorers share one output shape:
  direction:  +1 bullish GBP/USD, -1 bearish, 0 neutral
  confidence: 0..1
  driver:     rates | inflation | jobs | growth | politics | risk | other
  reason:     one short sentence

score_rules() is free and offline: a transparent baseline.
score_llm() handles nuance; it needs ANTHROPIC_API_KEY.
score() uses the LLM when available and falls back to rules on any failure.
"""
import json
import re

import config

# ---------------------------------------------------------------- rule scorer
UK_MARKERS = (r"\b(uk|u\.k\.|britain|british|bank of england|boe|bailey|mpc|gilts?|sterling|pound|ons|"
              r"starmer|reeves|healey|downing street|westminster)\b")
US_MARKERS = (r"\b(us|u\.s\.|american|federal reserve|the fed|fed|fomc|powell|treasury|"
              r"payrolls?|nonfarm|dollar|wall street|trump|white house|washington|pentagon)\b")

# "Strong" = hawkish / better economy for that currency. "Weak" = the opposite.
STRONG = (r"\b(hikes?|raises? (interest )?rates?|hawkish|higher than expected|hotter|above forecasts?|beats?|"
          r"surges?|jumps?|rises?|expands?|accelerates?|rebounds?|strong(er)?|robust|rallies|rally|"
          r"grows|grew|faster (pace|growth)|holds? (interest )?rates? (high|steady))\b")
# NB: the trailing \b on STRONG and WEAK matters. Without it a short word like "miss" matched the
# start of "missiles" (and "hike" the start of "hiker"...): "Missiles hit Kyiv after Russia agrees
# to US call to resume Ukraine peace talks" was scored bullish GBP/USD, "US news reads weak".
WEAK = (r"\b(cuts? (interest )?rates?|rate cuts?|dovish|lower than expected|cooler|below forecasts?|miss(es)?|"
        r"falls?|slumps?|contracts?|stalls?|recession|slows?|slowdown|weak(er)?|shrinks?|"
        r"plunges?|sell-?off|downgrades?|signals? (more |further )?(cuts?|easing))\b")

# Phrases where the usual word direction flips for the currency.
FLIPS = [
    # Surprise wording beats any other word in the text ("grew despite a fall in spending").
    (r"unexpectedly (continu\w+ to )?(grow|grew|expand\w*|rise|rose|accelerat\w+|rebound\w*|strengthen\w*|improv\w+)", "STRONG"),
    (r"unexpectedly (fall|fell|shrink|shrank|contract\w*|slow\w*|drop\w*|weaken\w*|stall\w*|deteriorat\w+)", "WEAK"),
    (r"(confound\w*|defy|defied|defies|beat|beats|smash\w*|top(s|ped)?) (a )?(the )?(forecasts?|expectations?|consensus)(?!.{0,40}(miss|below))", "STRONG"),
    (r"(gdp|economy|growth|output|sales) (rose|grew|expanded|jumped) (by )?\d", "STRONG"),
    (r"(gdp|economy|growth|output|sales) (fell|shrank|contracted|dropped) (by )?\d", "WEAK"),
    (r"(no hurry|not in a hurry|in no rush) to cut", "STRONG"),
    (r"higher for longer", "STRONG"),
    (r"rate cut bets? (fade|fades|faded|recede|recedes|wane|wanes)", "STRONG"),
    (r"unemployment( rate)? (rises?|jumps?|climbs?|increases?)", "WEAK"),
    (r"unemployment( rate)? (falls?|drops?|declines?)", "STRONG"),
    (r"jobless claims (rise|jump|climb)", "WEAK"),
    (r"jobless claims (fall|drop)", "STRONG"),
    (r"inflation (falls?|slows?|cools?|eases?|drops?)", "WEAK"),
    (r"inflation (rises?|jumps?|accelerates?|surges?|climbs?)", "STRONG"),
    (r"(gilt|treasury) yields? (jump|rise|surge)", "STRONG"),
    (r"(gilt|treasury) yields? (fall|drop|slide)", "WEAK"),
]

DRIVERS = [
    ("inflation", r"inflation|cpi|ppi|prices"),
    ("rates", r"rates?|hike|cut|mpc|fomc|yield|gilt|hawkish|dovish"),
    ("jobs", r"jobs|payrolls?|unemployment|wages|employment|jobless"),
    ("growth", r"gdp|growth|retail sales|recession|pmi|output|economy"),
    ("politics", r"budget|election|government|chancellor|tariff|shutdown"),
    ("energy", r"\b(oil|gas|crude|brent|opec|lng|energy)\b"),
]

# Energy channel (owner's rule): any rise in oil or gas prices, or tighter supply, is negative
# for the pound (bearish GBP/USD); falling prices or easing supply are positive. Skipped when a
# central bank is named, so "BoE raises rates as energy prices jump" is scored by the rate logic.
ENERGY = r"\b(oil|crude|brent|wti|natural gas|gas|lng|opec|tankers?|hormuz|refiner(y|ies)|pipelines?|energy (prices?|bills?|costs?|price cap))\b"
ENERGY_UP = (r"(\b(rise|rises|rising|rose|jump|jumps|jumped|surge|surges|surged|soar|soars|soared|"
             r"spike|spikes|spiked|climb|climbs|climbed|higher|record high)\b"
             r"|\b(tighten|tightens|tightened|impose|imposes|imposed|new|fresh|more|tougher|further) sanctions?\b"
             r"|\bcuts? (\w+ ){0,2}(output|production|supply|supplies)\b"
             r"|\b(disrupt|disrupts|disrupted|disruption|disruptions|blockade|blockades|halt|halts|halted|attack|attacks|"
             r"attacked|outage|hit|hits|struck|seized|seizes|explosion|explosions|sabotage|sabotaged|"
             # owner 2026-10-26: "Oil spill washes up on UAE beaches after tanker strike" = -1 (an attacked tanker
             # leaking oil pushes the price up). A strike on a tanker / an oil workers' strike, a spill or a slick
             # all mean disrupted supply.
             r"strike|strikes|spill|spills|slick|slicks)\b"
             r"|\bsupply (fears|disruption|shortage|crunch)\b)")
ENERGY_DOWN = (r"(\b(fall|falls|falling|fell|drop|drops|dropped|slump|slumps|slumped|tumble|tumbles|"
               r"tumbled|plunge|plunges|plunged|sink|sinks|slide|slides|lower|cheaper|lowest)\b"
               r"|\b(ease|eases|eased|easing|lift|lifts|lifted|lifting|relax|relaxes|relaxed|waive|waives|"
               r"waived) (\w+ ){0,3}sanctions?\b"
               r"|\b(raise|raises|boost|boosts|increase|increases) (\w+ ){0,2}(output|production|supply|supplies)\b"
               r"|\bsupply glut\b)")
CENTRAL_BANK = (r"\b(bank of england|boe|mpc|federal reserve|the fed|fomc|interest rates?|"
                r"rate (cut|hike)s?|powell|bailey)\b")


def _driver(text):
    for name, pat in DRIVERS:
        if re.search(pat, text):
            return name
    return "other"


# Volumes: when exports/output/supply FALL that is tighter supply (bearish GBP/USD), the reverse of
# a price fall. Owner rated "Middle East crude exports fall sharply ... Brent touched $110" negative.
_VOL = r"(exports?|output|production|supply|supplies|shipments|flows?|stocks|inventories|reserves?|storage)"
SUPPLY_FALL = (r"\b" + _VOL + r"\s+(\w+\s+)?(fall|falls|fell|falling|drop|drops|dropped|slump|slumps|"
               r"slumped|plunge|plunges|plunged|decline|declines|declined|shrink|shrinks|shrank|tumble|tumbles|"
               r"sink|sinks|sank|slide|slides|slid|dip|dips|dipped)\b")
SUPPLY_RISE = (r"\b" + _VOL + r"\s+(\w+\s+)?(rise|rises|rose|jump|jumps|jumped|surge|surges|surged|"
               r"increase|increases|increased|recover|recovers|recovered)\b")
ENERGY_UP_EXTRA = (r"\b(tighter|tightening|touch(es|ed)? \$?\d|(security|shipping) (threats?|concerns?|risks?)|"
                   r"safety of shipping)\b")
# Owner's $100 rule (ratings.csv, 2026-10-20): "100$ or over per barrel is bad for gbp". Wording that
# says a price topped / reached a level or hit a multi-month high, and a stated oil price of $100+,
# all count as a rise. Missed before: "U.S. crude oil tops $105, Brent at five-month highs" scored 0.
ENERGY_UP_OWNER = (r"\b(tops?|topped|exceeds?|exceeded|surpass\w*|reach(es|ed)?)\b"
                   r"|\bhighs?\b"
                   r"|\b(oil|crude|brent|wti)\b[^.;:]{0,40}\$\s?(1\d\d|[2-9]\d\d)\b")


# Owner rating 2026-10-23: "Sharp drops for AI stocks overwhelm markets worldwide as oil prices jump" = -1
# ("oil price increasing"). The word "drops" (about stocks) used to cancel "jump" (about oil) and the headline
# scored neutral. An energy noun directly followed by a move word is the real energy signal, so it counts
# once more, and the same for falls, so an unrelated "drops/jumps" elsewhere in the headline can't tie it.
ENERGY_NOUN = r"(oil|crude|brent|wti|natural gas|gas|lng|energy)( prices?)?"
ENERGY_UP_DIRECT = (r"\b" + ENERGY_NOUN + r" (jump|jumps|jumped|rise|rises|rose|surge|surges|surged|soar|soars|"
                    r"soared|spike|spikes|spiked|climb|climbs|climbed)\b")
ENERGY_DOWN_DIRECT = (r"\b" + ENERGY_NOUN + r" (fall|falls|fell|drop|drops|dropped|slump|slumps|slumped|plunge|"
                      r"plunges|plunged|tumble|tumbles|tumbled|slide|slides|sink|sinks)\b")


# "lowest gas reserves on record" / "storage at record low" is TIGHTER supply (bearish GBP/USD), not a
# price fall: ENERGY_DOWN's "lowest/lower" is about prices. Reserves, stocks, storage and inventories that are
# low or depleted count as supply tightening, and the "lowest" they contain is not counted as a price fall.
_RESERVES = r"(reserves?|stocks|stockpiles?|storage|inventories|supplies)"
RESERVES_LOW = (r"\b(lowest|low|record low|depleted|dwindl\w+|run(s|ning)? (low|dry))\b.{0,30}\b" + _RESERVES + r"\b"
                r"|\b" + _RESERVES + r"\b.{0,25}\b(lowest|record low|depleted|dwindl\w+|run(s|ning)? (low|dry)|at a low)\b")


_MIDEAST_ATTACK = (r"\b(attacks?|attacked|strikes?|struck|drones?|missiles?|sabotage\w*|bomb\w*)\b"
                   r"(?=.*\b(saudi|aramco|iran|iranian|iraq|gulf|middle east|mideast|houthis?|yemen|hormuz|uae|kuwait|qatar)\b)"
                   r"|\b(saudi|aramco|iran|iranian|iraq|gulf|middle east|mideast|houthis?|yemen|hormuz|uae|kuwait|qatar)\b"
                   r".*\b(attacks?|attacked|strikes?|struck|drones?|missiles?|sabotage\w*|bomb\w*)\b")


def _energy(text):
    if not re.search(ENERGY, text) or re.search(CENTRAL_BANK, text):
        return None
    sf = len(re.findall(SUPPLY_FALL, text))
    sr = len(re.findall(SUPPLY_RISE, text))
    rl = len(re.findall(RESERVES_LOW, text))
    stripped = re.sub(SUPPLY_FALL + "|" + SUPPLY_RISE + "|" + RESERVES_LOW, " ", text)
    sf += rl
    up = (len(re.findall(ENERGY_UP, stripped)) + len(re.findall(ENERGY_UP_EXTRA, stripped))
          + len(re.findall(ENERGY_UP_OWNER, stripped)) + sf + len(re.findall(ENERGY_UP_DIRECT, stripped)))
    down = len(re.findall(ENERGY_DOWN, stripped)) + sr + len(re.findall(ENERGY_DOWN_DIRECT, stripped))
    # Owner ruling 2026-10-27: an attack (or the threat of one) on Middle East oil supply outweighs a falling price --
    # "Oil slides over 2% as traders weigh Saudi attack risks, demand destruction" = -1, and the earlier
    # "Brent crude edges lower, even after Houthi attacks on Saudi infrastructure" re-rated from 0 to -1.
    if down >= up and re.search(_MIDEAST_ATTACK, text):
        iran = bool(re.search(r"\b(iran|iranian|tehran|hormuz)\b", text))
        return dict(direction=-1, confidence=0.7 if iran else 0.6, driver="energy",
                    reason="Attack on Middle East oil supply (owner ruling: outweighs a falling price): "
                           "negative for the pound, bearish for GBP/USD.")
    if up == down:
        return None
    if up > down:
        conf, why = 0.5, "Energy prices rising or supply tightening"
        if re.search(r"\b(iran|iranian|tehran|hormuz)\b", text):
            conf, why = 0.7, "Iran-linked oil disruption (owner: especially negative for the pound)"
        elif re.search(r"\b(middle east|mideast|gulf|red sea|saudi|iraq|yemen|houthis?|israel|uae|u\.a\.e\.)\b", text):
            conf, why = 0.6, "Middle East oil disruption (owner: generally negative for the pound)"
        return dict(direction=-1, confidence=conf, driver="energy",
                    reason=why + ": negative for the pound, bearish for GBP/USD.")
    return dict(direction=1, confidence=0.5, driver="energy",
                reason="Energy prices falling or supply easing: positive for the pound, bullish for GBP/USD.")


# War channel (owner's rule): any assault on Russia by Ukraine is positive for the pound.
# Word order matters: Ukraine must come first, then the attack word, then Russia, with no "by"
# in between, so "Russia strikes Ukraine" and "Ukraine hit by Russian drones" do not match.
_NOT_BY = r"(?:(?!\bby\b).)"
WAR_UA_ATTACK = (r"\bukrain\w*\b" + _NOT_BY + r"{0,50}\b(strike|strikes|struck|attack|attacks|attacked|hit|hits|"
                 r"drone|drones|missile|missiles|shell|shells|shelling|bomb|bombs|bombed|assault|assaults|"
                 r"offensive|raid|raids)\b" + _NOT_BY + r"{0,60}\b(russia|russian|moscow|kremlin|crimea|kursk|"
                 r"belgorod)\b")

# Policy channel (owner's rule): serious US policy U-turns are negative for the dollar. A headline
# cannot show how serious a reversal is, so confidence stays low.
US_ACTOR = r"\b(trump|white house|the administration|us administration|washington)\b"
US_REVERSAL = (r"\b(u-?turns?|reverses|reversed|reversal|backtracks?|backtracked|climbs? down|climbed down|"
               r"walks? back|walked back|abandons?|abandoned|flip-?flops?|doubles? back|doubled back)\b")


def _war(text):
    if re.search(WAR_UA_ATTACK, text):
        return dict(direction=1, confidence=0.45, driver="war",
                    reason="Ukrainian assault on Russia: positive for the pound, bullish for GBP/USD.")
    return None


# UK policy going backwards (owner 2026-10-26: "Could A Major Part Of Brexit Be Reversed?" = -1, "Going back on
# policy changes"). Limited to Brexit reversal and an explicit UK U-turn: a plain "overturn" also appears in
# "Badenoch urges PM to overturn ban on new oil and gas licences", which the owner rated +1 (UK drilling).
_UK_REVERSAL = (r"\bbrexit\b.{0,60}\b(revers\w*|undo\w*|undone|unravel\w*|rejoin\w*|u-?turns?)\b"
                r"|\b(revers\w*|undo\w*|unravel\w*|rejoin\w*)\b.{0,60}\bbrexit\b")


def _uk_policy_reversal(text):
    if re.search(_UK_REVERSAL, text) or (re.search(UK_MARKERS, text) and re.search(r"\bu-?turns?\b", text)
                                          and not re.search(US_MARKERS, text)):
        return dict(direction=-1, confidence=0.5, driver="politics",
                    reason="UK policy reversal (owner ruling): uncertainty for the pound, bearish for GBP/USD.")
    return None


# Wealth / people leaving the UK over tax (owner 2026-10-26: "Former Goldman Sachs chief leaves Britain amid tax hike
# fears" = -1, "Wealth leaving UK due to tax"). It scored +1 before: "tax hike" looked like a rate HIKE.
_LEAVES_UK = (r"\b(leaves?|leaving|left|quits?|quitting|flees?|fleeing|flee|exodus|moves? abroad|moving abroad|"
              r"relocat\w+|emigrat\w+)\b.{0,40}\b(uk|britain|london)\b"
              r"|\b(uk|britain|london)\b.{0,40}\b(exodus|brain drain)\b")


def _wealth_leaving_uk(text):
    if re.search(_LEAVES_UK, text) and re.search(r"\btax(es|ation)?\b", text):
        return dict(direction=-1, confidence=0.6, driver="growth",
                    reason="Wealth leaving the UK over tax (owner ruling): negative for the pound, bearish for GBP/USD.")
    return None


# Firms holding back on hiring (owner flag 2026-10-25: "Firms 'holding back' on new jobs ahead of Budget" = -1, BBC
# Business, so a UK story). Weak hiring is weak news for that country's currency.
_HIRING_WEAK = (r"\b(holding back|hold(s|ing)? back|hold(s|ing)? off|freez\w+|cut\w* back|pull\w* back|paus\w+|slow\w*)"
                r"\b.{0,15}\b(new )?(jobs|hiring|recruit\w*)\b"
                r"|\bhiring (freeze|slows?|slowdown|slumps?|falls?|fell|drops?)\b")


def _hiring_weak(text):
    if not re.search(_HIRING_WEAK, text):
        return None
    uk, us = bool(re.search(UK_MARKERS, text)), bool(re.search(US_MARKERS, text))
    if uk == us:
        return None
    return dict(direction=-1 if uk else 1, confidence=0.5, driver="jobs",
                reason=f"Firms holding back on hiring in the {'UK' if uk else 'US'} (owner ruling): "
                       f"{'bearish' if uk else 'bullish'} for GBP/USD.")


# UK government money committed to investment (owner flag 2026-10-25: "Budget to include £1bn for social and affordable
# housing" = +1). A sterling sum going FOR / INTO a productive area; anything that is a cut is left alone.
_UK_SPENDING = (r"£\s?\d[\d,.]*\s?(bn|billion|m|million)\b.{0,40}\b(for|on|into|to)\b.{0,40}"
                r"\b(housing|homes|infrastructure|rail\w*|roads?|hospitals?|schools?|nhs|energy|investment|growth|"
                r"research|defence|ai)\b")


def _uk_spending(text):
    if re.search(_UK_SPENDING, text) and not re.search(r"\b(cuts?|cutting|slash\w*|scrap\w*|axe\w*)\b", text):
        return dict(direction=1, confidence=0.5, driver="growth",
                    reason="UK money committed to investment (owner ruling): positive for the pound, bullish for GBP/USD.")
    return None


def _policy(text):
    if re.search(US_ACTOR, text) and re.search(US_REVERSAL, text):
        return dict(direction=1, confidence=0.4, driver="politics",
                    reason="US policy reversal: negative for the dollar, bullish for GBP/USD.")
    return None


# US political-weakness channel (owner's rule): a weaker Trump / Republican position (Democrats
# favoured in the midterms, negative Trump approval) is negative for the dollar, so bullish
# GBP/USD. The owner rated a midterm forecast article +1 even though it discussed surging oil
# prices, so this overrides the energy rule. Only the Democrat-gain side is scored; the mirror
# case (Republicans favoured) is not rated by the owner yet.
_DEM = r"\bdemocrats?\b.{0,90}\b(favou?rites?|lead|leads|leading|ahead|chance to (gain|win|take)|gain control|win control|take control|flip)\b"
_REP = r"\brepublicans?\b.{0,90}\b(lose control|likely to lose|huge losses|heavy losses|trail|trails|trailing|wipeout)\b"
_APPROVAL = r"\btrump.{0,3}s? (net )?approval\b.{0,40}(-\s?\d|falls?|drops?|slumps?|record low|negative|underwater)"


def _us_weakness(text):
    hits = sum(bool(re.search(p, text, re.S)) for p in (_DEM, _REP, _APPROVAL))
    if not hits:
        return None
    return dict(direction=1, confidence=min(0.7, 0.4 + 0.1 * hits), driver="politics",
                reason="Weaker US administration / Republican position: negative for the dollar, "
                       "bullish for GBP/USD. Overrides the energy rule (owner ruling).")


# Political-uncertainty channel (owner's rule): any political uncertainty (elections, leadership
# challenges, hung parliaments, shutdowns...) is negative for that country's currency. UK
# uncertainty = bearish GBP/USD, US uncertainty = bullish. Needs exactly one region named.
POLITICAL_UNCERTAINTY = (r"\b(elections?|snap election|general election|midterms?|referendum|hung parliament|"
                         r"no[- ]confidence|leadership (challenge|contest|race|crisis)|coalition|"
                         r"political (uncertainty|turmoil|crisis|instability|chaos)|government (collapse|collapses|"
                         r"collapsed|crisis)|shutdown|impeach\w*|resign(s|ed|ation)?|reshuffle|"
                         r"budget (standoff|deadlock|row|rebellion)|debt ceiling|rebels?|rebellion|"
                         r"uncertainty|toppl\w+|ousted?|"
                         # Legal / court turmoil around a government (owner, 2026-10-22: "Trump sued over
                         # Canadian tariffs in latest legal challenge" = +1, "political unrest in the
                         # country of USD").
                         r"sued|sues|lawsuits?|legal challenges?|struck down|strikes down)\b")


def _uncertainty(text):
    if not re.search(POLITICAL_UNCERTAINTY, text):
        return None
    uk = bool(re.search(UK_MARKERS, text))
    us = bool(re.search(US_MARKERS, text))
    if uk == us:
        return None
    return dict(direction=-1 if uk else 1, confidence=0.45, driver="politics",
                reason=f"{'UK' if uk else 'US'} political uncertainty: negative for that currency, "
                       f"{'bearish' if uk else 'bullish'} for GBP/USD.")


# Investment channel (owner's rule): loss of investment in a country is negative for its currency.
# Investment coming in is the mirror (+). Needs exactly one of UK/US named.
_INV_OBJ = r"(invest\w*|data cent\w+|factory|factories|plant|plants|project|plans|site|hub|headquarters|jobs|deal|gigafactory)"
INVEST_LOSS = (r"\b(pull(s|ed|ing)? (out|back)|withdraw(s|n|ing)?|scrap(s|ped|ping)?|axe(s|d)?|cancel(s|led|ling)?|"
               r"shelve(s|d)?|scale(s|d)? back|abandon(s|ed)?|halt(s|ed)?|freeze(s)?|cut(s)? (\w+ ){0,2}(investment|jobs))\b"
               r".{0,60}\b" + _INV_OBJ + r"\b|\b(investment|jobs) (loss|losses|cuts?|lost|exodus)\b")
INVEST_GAIN = (r"\b(invest(s|ing)?|announces? (new )?(\w+ )?(investment|plant|factory|data cent\w+)|"
               r"creat(es|ing) [\d,]+ (new )?jobs|expands? (operations )?in|opens? (a )?new (plant|factory|data cent\w+|hub))\b")


def _investment(text, hinted=False):
    # Owner rating 2026-10-23: "Elon Musk reportedly invests $1bn in David Ellison's Skydance" = 0 (a US
    # company, nothing to do with UK investment). A source's region hint (Guardian = UK) must not turn
    # an investment GAIN into "new investment in the UK"; only a country named in the headline can.
    # Losses keep the hint ("Paddy Power cuts 1,400 jobs", owner -1).
    uk = bool(re.search(UK_MARKERS, text))
    us = bool(re.search(US_MARKERS, text))
    if uk == us:
        return None
    if re.search(INVEST_LOSS, text):
        sign = -1
    elif re.search(INVEST_GAIN, text):
        if hinted:
            return None
        sign = 1
    else:
        return None
    return dict(direction=sign if uk else -sign, confidence=0.5, driver="growth",
                reason=f"{'Loss of' if sign < 0 else 'New'} investment in the {'UK' if uk else 'US'}: "
                       f"{'negative' if sign < 0 else 'positive'} for that currency.")


# ---- Channels added 2026-10-21 from the owner's ratings.csv (each cites the rated headline) ----

# UK's own gas/oil (owner: "UK are mining their own gas"; "Green energy ... makes fuel more
# expensive"). New UK domestic supply being approved / announced eases energy supply, which the
# energy rule already treats as positive for the pound. Five rated headlines: Jackdaw gas field
# announcement, North Sea drilling approval (x3 wordings), Jackdaw "undermines green credentials".
UK_SUPPLY_OBJ = (r"\b(north sea|jackdaw|rosebank|cambo|gas ?fields?|oil ?fields?|offshore (gas|oil)|"
                 r"drilling|licen[cs]es?)\b")
UK_SUPPLY_GO = (r"\b(approv\w+|go-ahead|green[- ]?light\w*|backs?|clears?|cleared|announce\w*|"
                r"announcement|develop\w*|expan\w+|decision|defend\w*|new (drilling|licen[cs]es?|gas|oil))\b")
UK_SUPPLY_BLOCK = r"\b(bans?|banned|scrap\w*|blocks?|blocked|halts?|halted|freezes?|ends?|stops?|rejects?|refuses?)\b"
# Removing a ban on UK drilling is a green light, not a block (owner, ratings.csv 2026-10-22: "Burnham
# defends Jackdaw decision as Badenoch urges PM to overturn ban on new oil and gas licences" = +1).
UK_SUPPLY_REMOVES_BAN = r"\b(overturn|lift|scrap|end|reverse|repeal)\w*\s+(the\s+|a\s+|this\s+)?ban\b"


def _uk_energy_supply(text):
    if not (re.search(UK_SUPPLY_OBJ, text) and re.search(UK_SUPPLY_GO, text)):
        return None
    if re.search(UK_SUPPLY_BLOCK, text) and not re.search(UK_SUPPLY_REMOVES_BAN, text):
        return None
    if re.search(US_MARKERS, text):
        return None
    return dict(direction=1, confidence=0.5, driver="energy",
                reason="UK developing its own gas/oil (owner ruling): easing energy supply, positive "
                       "for the pound, bullish for GBP/USD.")


# Job cuts blamed on tax rises in one headline (owner, Paddy Power: "Jobs cuts and tax rises in
# the same headline" -> -1). A UK-news default: with no region named it is assumed UK, since the
# feeds are UK-centric; a US marker flips it (that mirror is an assumption, not rated).
JOB_CUTS = (r"\b(cuts?|axes?|axed|slash(es|ed)?|losing|loses?|lost) ([\d,]+ |\w+ ){0,2}jobs\b"
            r"|\bjob (cuts|losses)\b")
TAX_RISES = (r"\btax(es)? (rises?|hikes?|increases?|rose|raised)\b"
             r"|\b(rise|rises|hike|hikes|increase|increases) in (\w+ )?tax(es)?\b")


def _jobs_and_tax(text):
    if not (re.search(JOB_CUTS, text) and re.search(TAX_RISES, text)):
        return None
    us = bool(re.search(US_MARKERS, text)) and not re.search(UK_MARKERS, text)
    return dict(direction=1 if us else -1, confidence=0.45, driver="jobs",
                reason=f"Job cuts blamed on tax rises (owner ruling): negative for the "
                       f"{'dollar' if us else 'pound'}, {'bullish' if us else 'bearish'} for GBP/USD.")


# UK inflation (owner, "Will UK Inflation Rise to 3.5% This Week?" -> -1): "This is very bad
# inflation data for the UK ... far above our target. If this type of article appears often gbp
# will surely fall ... It should be weighted on the most." This REVERSES the textbook reading
# (hotter inflation -> hawkish BoE -> stronger pound) that this scorer used before for UK
# inflation, so it takes priority over the generic strong/weak words. "Weighted the most" =
# confidence 0.9, so these items dominate the time-decayed average. The mirror (UK inflation
# FALLING = bullish) is an assumption, not rated -- kept at low confidence. US inflation is
# untouched. NOTE: surprise.py (manual data releases) still uses the textbook reading.
INFLATION = r"\b(inflation|cpi)\b"
INFL_UP = (r"\b(rise|rises|rising|rose|jump\w*|accelerat\w+|surg\w+|climb\w*|hotter|higher|highest|soar\w*|"
           r"spike\w*|peak\w*|exceeds?|above (forecasts?|expectations?|target))\b")
INFL_DOWN = (r"\b(fall|falls|falling|fell|drop\w*|slow\w*|cool\w*|eas(e|es|ed|ing)|slid\w*|declin\w+|"
             r"lowest|below (forecasts?|expectations?|target))\b")


def _uk_inflation(text):
    if not re.search(INFLATION, text):
        return None
    if not re.search(UK_MARKERS, text) or re.search(US_MARKERS, text):
        return None
    up, down = len(re.findall(INFL_UP, text)), len(re.findall(INFL_DOWN, text))
    if up == down:
        return None
    if up > down:
        return dict(direction=-1, confidence=0.9, driver="inflation",
                    reason="UK inflation rising (owner ruling: very bad for the UK, weighted most): "
                           "negative for the pound, bearish for GBP/USD.")
    return dict(direction=1, confidence=0.45, driver="inflation",
                reason="UK inflation falling (mirror of the owner's rule, unconfirmed): positive for "
                       "the pound, bullish for GBP/USD.")


# FTSE 100 falling (owner: "FTSE 100 Falls. The pound is likely to increase") -> +1. A stronger
# pound cuts the sterling value of the FTSE's overseas earnings, so the index and the pound tend
# to move opposite ways. Only the FALL side was rated; a rising FTSE is NOT scored.
FTSE_FALL = (r"\bftse( ?100| ?250)?\b.{0,25}\b(fall|falls|fell|drop|drops|dropped|slip|slips|slipped|"
             r"slide|slides|slid|sink|sinks|sank|lose|loses|lost|tumble\w*|slump\w*|retreat\w*|dip\w*)\b")


def _ftse(text):
    if not re.search(FTSE_FALL, text):
        return None
    return dict(direction=1, confidence=0.4, driver="risk",
                reason="FTSE 100 falling (owner ruling): pound likely to rise, bullish for GBP/USD.")


# US-Ukraine relationship worsening (owner: "US backed war failing over oil due to mid-term
# elections") -> +1, a US foreign-policy setback / disunity that is negative for the dollar.
# One rated example, so deliberately narrow and low confidence.
US_UA_PAIR = (r"\b(trump|white house|washington|us|u\.s\.)\b.{0,80}\b(zelensky\w*|ukrain\w*|kyiv)\b"
              r"|\b(zelensky\w*|ukrain\w*|kyiv)\b.{0,80}\b(trump|white house|washington)\b")
RIFT_WORDS = (r"\b(worsens?|worsened|distrust|rift|row|clash\w*|spat|falls? out|breakdown|strain\w*|"
              r"deteriorat\w+|sours?|soured|loggerheads|at odds|feud|bust-?up|disagree\w*|tensions?)\b")


def _us_ukraine_rift(text):
    if re.search(US_UA_PAIR, text) and re.search(RIFT_WORDS, text):
        return dict(direction=1, confidence=0.4, driver="politics",
                    reason="US-Ukraine relationship worsening (owner ruling): US setback, negative for "
                           "the dollar, bullish for GBP/USD.")
    return None


# A central-bank decision that is already expected is priced in (owner, 2026-10-29: "Federal Reserve is likely to hold
# interest rates steady in October" -- "it says expected"), so it carries no news of its own. The scorer used to read it
# as strong US data (-1). The actual decision ("Fed holds rates", "BoE raises rates") is untouched. The owner flagged that
# one story +1 for the week because the BoE was expected to hike while the Fed held (a divergence); a headline alone can't
# show that, so it stays a manual flag.
_EXPECTED_HOLD = (r"\b(likely|expected|set|seen|tipped|poised|predicted|widely expected|bets?|odds)\b.{0,25}"
                  r"\b(to )?(hold|keep|leave|maintain|stand pat|pause|no change)\b"
                  r"|\bno (hike|cut|change|move)s? (is |are )?(expected|likely|seen)\b")


def _expected_hold(text):
    if re.search(CENTRAL_BANK + r"|\b(fed|boe|ecb)\b", text) and re.search(_EXPECTED_HOLD, text):
        return dict(direction=0, confidence=0.3, driver="rates",
                    reason="Central-bank decision already expected (owner ruling): priced in, neutral for GBP/USD.")
    return None


# UK schemes that cut household energy use (owner flag 2026-10-29: "Homes across UK to get low- or no-interest loans to
# fund solar panels and heat pumps" = +1 -- an energy-saving scheme, energy is why inflation is high, and high UK inflation
# is bad for the pound). Owner: "only slightly", so low confidence.
_ENERGY_SAVING = r"\b(solar|heat pumps?|insulation|retrofit\w*|energy[- ]sav\w+|energy[- ]efficien\w+)\b"
_SCHEME_WORDS = r"\b(loans?|grants?|scheme|subsid\w+|funding|vouchers?|support|help|boost|rollout|roll-out|free)\b"


def _uk_energy_saving(text):
    if (re.search(_ENERGY_SAVING, text) and re.search(_SCHEME_WORDS, text)
            and re.search(UK_MARKERS, text) and not re.search(US_MARKERS, text)):
        return dict(direction=1, confidence=0.3, driver="energy",
                    reason="UK energy-saving scheme (owner ruling): eases energy costs and inflation, slightly "
                           "positive for the pound, bullish for GBP/USD.")
    return None


# UK Budget previews and doubt (owner flags 2026-10-28: "What could be in the Budget?", "Former Conservative chancellor
# unsure Budget will 'fix big problems'", "What do you want from the Budget?" = all -1). Owner's reasoning: nobody can price
# a Budget in advance, recent ones have been risky (the 2022 mini-budget took the pound near parity), and they tend to
# bring stealth taxes that put off UK investment. Event risk rather than anything in the headline, so low confidence.
# A headline reporting an actual measure (e.g. "Budget to include GBP1bn for housing") is not matched here.
_BUDGET_PREVIEW = (r"\bwhat (could|might|will|should|do you want|to expect)\b.{0,40}\bbudget\b"
                   r"|\bbudget (preview|predictions?|speculation|wish ?list|what to expect)\b"
                   r"|\b(unsure|doubts?|doubtful|fears?|worr\w+|sceptic\w*|skeptic\w*)\b.{0,50}\bbudget\b"
                   r"|\bbudget\b.{0,50}\b(unsure|doubts?|doubtful|fears?|worr\w+|sceptic\w*|skeptic\w*)\b")


def _uk_budget_preview(text):
    if re.search(_BUDGET_PREVIEW, text) and not re.search(US_MARKERS, text):
        return dict(direction=-1, confidence=0.3, driver="politics",
                    reason="UK Budget still to come (owner ruling): event risk and stealth-tax worries, slightly "
                           "negative for the pound, bearish for GBP/USD.")
    return None


def _special(text, hinted=False):
    """Owner's channels (energy, war, policy). Agreeing channels add confidence; conflicting
    channels leave the headline neutral with low confidence."""
    held = _expected_hold(text)
    if held:
        return held
    war = _war(text)
    if war:
        # Owner: a Ukrainian assault on Russia is positive for the pound even when it hits oil
        # infrastructure, so the war rule wins over the energy rule.
        war = dict(war, confidence=0.6)
        if _energy(text):
            war["reason"] += " Overrides the energy rule (owner ruling)."
        return war
    weak = _us_weakness(text)
    if weak:
        return weak
    results = [r for r in (_energy(text), _policy(text), _uncertainty(text), _investment(text, hinted),
                           _uk_energy_supply(text), _jobs_and_tax(text), _uk_inflation(text),
                           _ftse(text), _us_ukraine_rift(text), _uk_policy_reversal(text),
                           _wealth_leaving_uk(text), _hiring_weak(text), _uk_spending(text),
                           _uk_energy_saving(text), _uk_budget_preview(text)) if r]
    if not results:
        return None
    if len({r["direction"] for r in results}) > 1:
        return dict(direction=0, confidence=0.2, driver=results[0]["driver"],
                    reason="Conflicting rules (" + ", ".join(r["driver"] for r in results) + "): left neutral.")
    # Agreeing channels add 0.1 each, capped at 0.7 -- except a single rule that is already
    # higher than the cap (the owner's "weighted most" UK-inflation rule, 0.9) keeps its own.
    top = max(r["confidence"] for r in results)
    conf = min(max(0.7, top), top + 0.1 * (len(results) - 1))
    return dict(direction=results[0]["direction"], confidence=round(conf, 2),
                driver=results[0]["driver"], reason=" ".join(r["reason"] for r in results))


# Currency-move wording: the headline states the direction of the pair directly.
_UP_V = r"(rise|rises|rose|gain|gains|gained|climb|climbs|climbed|jump|jumps|jumped|surge|surges|surged|strengthen\w*|firm|firms|firmed|advance\w*|rall\w+)"
_DN_V = r"(fall|falls|fell|slip|slips|slipped|drop|drops|dropped|slide|slides|slid|sink|sinks|sank|weaken\w*|slump\w*|tumble\w*|lose|loses|lost)"
_MOVES = [
    (r"\b(pound|sterling|gbp)\b.{0,20}\b" + _UP_V + r"\b", 1),
    (r"\b(pound|sterling|gbp)\b.{0,20}\b" + _DN_V + r"\b", -1),
    (r"\b(dollar|greenback|usd)\b.{0,20}\b" + _UP_V + r"\b", -1),
    (r"\b(dollar|greenback|usd)\b.{0,20}\b" + _DN_V + r"\b", 1),
    (r"\b(weaker|softer|lower) (us )?(dollar|greenback)\b", 1),
    (r"\b(stronger|firmer|higher) (us )?(dollar|greenback)\b", -1),
    (r"\b(stronger|firmer) (pound|sterling)\b", 1),
    (r"\b(weaker|softer) (pound|sterling)\b", -1),
]


def _currency_move(text):
    total = sum(sign for pat, sign in _MOVES if re.search(pat, text))
    if total == 0:
        return None
    return dict(direction=1 if total > 0 else -1, confidence=0.6, driver="fx",
                reason="Headline states the pound/dollar move directly.")


def _hint_applied(text, region):
    """True when _apply_region_hint would add a marker (the headline names neither country)."""
    return region in ("UK", "US") and not re.search(UK_MARKERS, text) and not re.search(US_MARKERS, text)


def _apply_region_hint(text, region):
    """A source whose headlines are about one country rarely say so ("Inflation and government
    borrowing highest for a year ahead of next week's Budget" is a UK story with no UK word in it).
    When the headline names neither the UK nor the US itself, treat it as that region by adding the
    marker word, so every channel below sees it. A headline that names a country is never overridden."""
    if region in ("UK", "US") and not re.search(UK_MARKERS, text) and not re.search(US_MARKERS, text):
        return ("uk " if region == "UK" else "us ") + text
    return text


def score_rules(title: str, summary: str = "", region=None):
    raw = f"{title} {summary}".lower()
    hinted = _hint_applied(raw, region)
    text = _apply_region_hint(raw, region)
    special = _special(text, hinted)
    if special:
        return special
    move = _currency_move(text)
    if move:
        return move
    uk = bool(re.search(UK_MARKERS, text))
    us = bool(re.search(US_MARKERS, text))
    driver = _driver(text)
    if driver == "other":
        # The generic strong/weak WORD rules below only make sense for an economic subject. Without
        # one a word like "slowdown" or "hit" is just English ("Trump denies ordering Starlink slowdown in
        # Ukraine" was scored weak US economic news, bullish GBP/USD). The owner's specific channels
        # above (energy, war, politics, inflation, FTSE ...) run first and are not affected.
        return dict(direction=0, confidence=0.0, driver="other",
                    reason="No economic subject in the headline.")

    # Decide strong vs weak, letting explicit flip phrases win.
    tone = None
    for pat, label in FLIPS:
        if re.search(pat, text):
            tone = label
            break
    if tone is None:
        s, w = len(re.findall(STRONG, text)), len(re.findall(WEAK, text))
        if s == w:
            return dict(direction=0, confidence=0.0, driver=driver,
                        reason="No clear strong/weak signal in headline.")
        tone = "STRONG" if s > w else "WEAK"

    if uk == us:  # neither or both regions mentioned: can't attribute
        return dict(direction=0, confidence=0.1, driver=driver,
                    reason="Could not tell whether this is UK or US news.")

    # UK strong -> GBP up -> bullish. US strong -> USD up -> bearish for the pair.
    sign = 1 if tone == "STRONG" else -1
    direction = sign if uk else -sign
    region = "UK" if uk else "US"
    reason = f"{region} news reads {tone.lower()}, which is {'bullish' if direction > 0 else 'bearish'} for GBP/USD."
    return dict(direction=direction, confidence=0.55, driver=driver, reason=reason)


# ----------------------------------------------------------------- LLM scorer
LLM_PROMPT = """You are scoring financial news for its likely effect on the GBP/USD exchange rate.
Rules:
- UK data or policy that strengthens GBP (higher rates, stronger growth, hotter inflation that
  keeps the Bank of England hawkish) is bullish for GBP/USD.
- US data or policy that strengthens USD is bearish for GBP/USD; US weakness is bullish.
- Rising oil or gas prices, or tightening energy supply (new sanctions, output cuts), are negative
  for the pound and bearish for GBP/USD, whatever the cause. Falling prices or easing supply
  (sanctions eased, more output) are bullish for GBP/USD.
- Tanker attacks and Middle East oil disruptions are negative for the pound, especially when Iran
  is behind them (bearish GBP/USD, high confidence).
- Signs the US administration or Republicans are weakening (Democrats favoured in the midterms,
  negative Trump approval) are negative for the dollar, bullish GBP/USD, even if the story also
  mentions rising oil prices.
- Any political uncertainty (elections, leadership challenges, shutdowns, coalition rows) is
  negative for that country's currency: UK uncertainty is bearish GBP/USD, US is bullish.
- Loss of investment in a country (companies pulling out, scrapping projects) is negative for
  its currency; new investment is positive. UK loss = bearish GBP/USD, US loss = bullish.
- A Ukrainian assault on Russia is positive for the pound (bullish GBP/USD). A serious US policy
  U-turn by the administration is negative for the dollar (bullish GBP/USD).
- If a Ukrainian strike on Russia hits oil or gas sites, it is still bullish for GBP/USD: the
  war rule overrides the energy rule.
- Judge market impact relative to expectations, not whether the headline sounds good or bad.
- If the item is irrelevant to either economy, or too ambiguous, return direction 0, confidence 0.
Return JSON only, no prose:
{"direction": -1|0|1, "confidence": 0.0-1.0, "driver": "rates|inflation|jobs|growth|politics|risk|other", "reason": "one short sentence"}

Headline: %s
Snippet: %s"""


def _clean(result):
    d = int(result.get("direction", 0))
    d = max(-1, min(1, d))
    c = float(result.get("confidence", 0))
    c = max(0.0, min(1.0, c))
    return dict(direction=d, confidence=c,
                driver=str(result.get("driver", "other"))[:20],
                reason=str(result.get("reason", ""))[:240])


def score_llm(title: str, summary: str = ""):
    import anthropic  # imported lazily so rules-only use needs no dependency
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=config.LLM_MODEL,
        max_tokens=200,
        messages=[{"role": "user", "content": LLM_PROMPT % (title, summary[:500])}],
    )
    text = msg.content[0].text
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError(f"No JSON in model reply: {text[:100]}")
    return _clean(json.loads(match.group(0)))


def score(title: str, summary: str = "", region=None):
    """Returns (result_dict, scorer_name). region: optional "UK"/"US" hint from the feed (see config.FEEDS)."""
    if config.USE_LLM:
        try:
            return score_llm(title, summary), "llm"
        except Exception:
            pass
    return score_rules(title, summary, region), "rules"
