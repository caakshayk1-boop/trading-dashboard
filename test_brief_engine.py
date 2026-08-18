#!/usr/bin/env python3
"""Regression tests for the Daily Intelligence Brief.

Every case here is a real defect found while building it against live wire
copy, not a hypothetical. No network: clustering, scoring and the QA gate are
pure functions and are tested as such.
"""
import unittest
from datetime import datetime, timezone

import brief_engine as be


def art(title, source="Economic Times", tier=3, summary="", hours_ago=1):
    return {"title": title, "link": f"https://x/{abs(hash(title))}", "source": source,
            "tier": tier, "summary": summary,
            "published": datetime.now(timezone.utc)}


class TestClustering(unittest.TestCase):
    def test_one_shared_word_is_not_an_event(self):
        """Three unrelated India stories merged into ONE six-article event on
        the strength of the single word 'India'. The entity score divided by
        min(), so one shared proper noun scored a perfect 1.0."""
        a = art("India built the world's biggest digital payments miracle")
        b = art("India's microfinance overhang is finally easing", "Livemint")
        c = art("India's unemployment rate falls to four-month low", "ET Economy")
        for x, y in ((a, b), (b, c), (a, c)):
            self.assertLess(be.similarity(x, y), be.MERGE_THRESHOLD,
                            f"{x['title'][:30]!r} merged with {y['title'][:30]!r}")

    def test_two_distinctive_shared_terms_is_an_event(self):
        """Real pair from the live wire — three outlets on one block deal."""
        a = art("Paytm block deal: Resilient Asset to sell up to Rs 4,895 crore stake")
        b = art("Paytm promoter entity Resilient Asset Management to sell up to 4.98% stake",
                "Livemint")
        self.assertGreaterEqual(be.similarity(a, b), be.MERGE_THRESHOLD)

    def test_generic_market_words_alone_do_not_merge(self):
        """'Ahead of Market: 10 things', 'Market Trading Guide' and an
        unrelated anchor-investor story all share {market, stock}."""
        a = art("Ahead of Market: 10 things that will decide stock action")
        b = art("Sunshine Pictures raises Rs 85 crore from anchor investors", "Livemint")
        self.assertLess(be.similarity(a, b), be.MERGE_THRESHOLD)

    def test_clustering_does_not_chain(self):
        """Single-link clustering let A~B and B~C pull C in beside A even when
        A and C shared nothing — that is how three unrelated India stories
        became one six-article event. Members are now compared to the cluster
        SEED, so C has to resemble A directly or it starts its own cluster.

        Titles are chosen so the seed sorts first: cluster() orders by
        (tier, title), and 'Adani' precedes the others alphabetically.
        """
        a = art("Adani Ports quarterly cargo volume rises", "Bloomberg", 2)
        b = art("Adani Ports cargo volume rises as Adani Green raises debt", "Bloomberg", 2)
        c = art("Adani Green raises debt for renewable expansion", "Bloomberg", 2)
        self.assertGreaterEqual(be.similarity(b, a), be.MERGE_THRESHOLD, "b should join a")
        self.assertGreaterEqual(be.similarity(b, c), be.MERGE_THRESHOLD, "b resembles c too")
        self.assertLess(be.similarity(c, a), be.MERGE_THRESHOLD, "a and c are unrelated")

        clusters = be.cluster([a, b, c])
        for cl in clusters:
            titles = [x["title"] for x in cl]
            self.assertFalse(a["title"] in titles and c["title"] in titles,
                             "unrelated first and last article ended up together")


class TestScoring(unittest.TestCase):
    def test_more_independent_sources_scores_higher(self):
        one = [art("Fed holds rates steady", "Bloomberg", 2)]
        many = [art("Fed holds rates steady", "Bloomberg", 2),
                art("Fed keeps rates on hold", "Financial Times", 2),
                art("Fed leaves policy unchanged", "WSJ Markets", 2)]
        self.assertGreater(be.importance(many), be.importance(one))

    def test_confidence_is_not_importance(self):
        """A single-source story can be the most important thing that day and
        still be Low confidence — the spec insists these stay separable."""
        solo = [art("Central bank announces emergency rate cut", "Moneycontrol", 3,
                    summary="inflation policy rate")]
        self.assertEqual(be.confidence(solo), "Low")
        self.assertGreaterEqual(be.importance(solo), 2)

    def test_geopolitics_beats_technology_for_category(self):
        """An oil-and-Middle-East story was filed under Technology because a
        bare `ai` token matched somewhere in the summary."""
        c = [art("Oil climbs as Middle East tensions rise", "Bloomberg", 2,
                 summary="Iran tensions lifted crude and AI stocks slipped")]
        self.assertEqual(be.categorize(c), "World")


class TestQAGate(unittest.TestCase):
    SRC = ("A new set of Trump tariffs for Canada could take effect Wednesday. "
           "Mark Carney in last-ditch effort to avoid Trump's tariffs. "
           "Paytm block deal: Resilient Asset Management to sell up to "
           "Rs 4,895 crore stake at 3% discount.")

    def ok(self, **kw):
        base = {"headline": "Carney seeks to avoid Trump tariffs on Canada",
                "bullets": ["Mark Carney is making a last-ditch effort to avoid the tariffs.",
                            "A new set of Trump tariffs for Canada could take effect Wednesday."]}
        base.update(kw)
        return base

    def test_clean_event_passes(self):
        self.assertIsNone(be.qa_reject(self.ok(), self.SRC))

    def test_invented_person_is_rejected(self):
        """The failure numbers alone did not catch: an article naming Mark
        Carney produced 'Justin Trudeau will speak with President Biden'."""
        ev = self.ok(bullets=["The tariffs would affect Canadian goods.",
                              "Canadian Prime Minister Justin Trudeau will speak with President Biden."])
        r = be.qa_reject(ev, self.SRC)
        self.assertIsNotNone(r)
        # Either invented name is a correct verdict; the gate reports the
        # first it meets.
        self.assertTrue("Justin" in r or "Trudeau" in r or "Biden" in r, r)

    def test_demonym_is_not_treated_as_invented(self):
        """'Canadian' derives from 'Canada' in the source — blaming it hides
        the real invention beside it."""
        ev = self.ok(bullets=["Canadian goods face the new tariffs.",
                              "Mark Carney is seeking an exemption."])
        self.assertIsNone(be.qa_reject(ev, self.SRC))

    def test_sentence_initial_capitals_are_not_names(self):
        """Every bullet starts with a capital. A plain capitalised-word scan
        read "Higher", "Diplomatic", "Using" and "Strong" as invented names and
        rejected six of eight events on the first live run — the section
        degraded to headline-only copy for no reason."""
        for opener in ("Higher", "Diplomatic", "Threats", "Using", "Strong"):
            ev = self.ok(bullets=[f"{opener} costs are expected across the sector.",
                                  "Mark Carney is seeking an exemption."])
            self.assertIsNone(be.qa_reject(ev, self.SRC),
                              f"{opener!r} wrongly treated as a name")

    def test_unicode_hyphens_are_not_invented_names(self):
        """The model emits U+2011 non-breaking hyphens, so "US\u2011Iran",
        "SEBI\u2011registered" and "High\u2011frequency" failed to match the plain
        "US-Iran" in the source and were rejected as invented names."""
        src = ("US-Iran talks stall as Trump threatens Oman. "
               "SEBI-registered intermediaries only, the regulator said. "
               "High-frequency trading came under scrutiny.")
        ev = {"headline": "Regulator warns on trading tips",
              "bullets": ["Investors should use SEBI\u2011registered intermediaries only.",
                          "High\u2011frequency activity drew scrutiny during US\u2011Iran volatility."]}
        self.assertIsNone(be.qa_reject(ev, src))

    def test_bullets_are_treated_as_separate_sentences(self):
        """Bullets carry no terminating punctuation. Joining them with a space
        fused them into one sentence, so every bullet after the first lost its
        opener protection."""
        ev = self.ok(bullets=["Mark Carney is seeking an exemption",
                              "Higher costs are expected across the sector"])
        self.assertIsNone(be.qa_reject(ev, self.SRC))

    def test_invented_number_is_rejected(self):
        ev = self.ok(bullets=["The deal is valued at Rs 9,712 crore.",
                              "Resilient Asset Management is the seller."])
        self.assertIn("9,712", be.qa_reject(ev, self.SRC) or "")

    def test_unicode_spacing_is_not_a_hallucination(self):
        """The model returns '4,895 crore' (narrow no-break space) where
        the source has '4,895 crore'. A literal compare rejected a correct
        event as invented."""
        ev = self.ok(bullets=["The block deal is valued at up to Rs 4,895 crore.",
                              "Resilient Asset Management is the seller."])
        self.assertIsNone(be.qa_reject(ev, self.SRC))

    def test_duplicate_and_near_duplicate_bullets_rejected(self):
        self.assertIsNotNone(be.qa_reject(
            self.ok(bullets=["Trump plans tariffs on Canada.",
                             "Trump plans tariffs on Canada."]), self.SRC))
        self.assertIsNotNone(be.qa_reject(
            self.ok(bullets=["Trump plans new tariffs on Canada soon.",
                             "Trump plans new tariffs on Canada."]), self.SRC))

    def test_filler_phrasing_rejected(self):
        self.assertIsNotNone(be.qa_reject(
            self.ok(bullets=["This is significant because trade matters.",
                             "Carney is responding to the plan."]), self.SRC))

    def test_market_impact_needs_an_asset_and_a_valid_direction(self):
        self.assertIsNotNone(be.qa_reject(
            self.ok(marketImpact=[{"asset": "", "direction": "Positive"}]), self.SRC))
        self.assertIsNotNone(be.qa_reject(
            self.ok(marketImpact=[{"asset": "Gold", "direction": "Up"}]), self.SRC))
        self.assertIsNone(be.qa_reject(
            self.ok(marketImpact=[{"asset": "Gold", "direction": "Unclear"}]), self.SRC))

    def test_too_few_bullets_or_long_headline_rejected(self):
        self.assertIsNotNone(be.qa_reject(self.ok(bullets=["Only one."]), self.SRC))
        self.assertIsNotNone(be.qa_reject(
            self.ok(headline=" ".join(["word"] * 15)), self.SRC))


class TestFallback(unittest.TestCase):
    def test_deterministic_summary_never_invents(self):
        """With no model the section degrades to plainer copy, never to
        invented copy: the headline is the best outlet's own and the bullets
        are the other outlets' headlines."""
        c = [art("Fed holds rates steady", "Bloomberg", 2),
             art("Fed keeps policy unchanged", "Livemint", 3)]
        d = be.deterministic_summary(c)
        self.assertEqual(d["headline"], "Fed holds rates steady")
        self.assertFalse(d["generated"])
        self.assertTrue(any("Livemint" in b for b in d["bullets"]))


if __name__ == "__main__":
    unittest.main(verbosity=1)
