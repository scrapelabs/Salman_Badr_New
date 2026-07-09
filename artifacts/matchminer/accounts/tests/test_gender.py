from django.test import SimpleTestCase

from accounts.live_scrapers._gender import draw_gender_code, is_mixed_draw


class DrawGenderCodeTests(SimpleTestCase):
    def test_standard_tennis_abbreviations_are_recognized(self):
        self.assertEqual(draw_gender_code("MS 4 (WTN 25.00 - 40.00)"), "M")
        self.assertEqual(draw_gender_code("MD Band 5"), "M")
        self.assertEqual(draw_gender_code("BS 100 U12"), "M")
        self.assertEqual(draw_gender_code("BD 200 U12"), "M")
        self.assertEqual(draw_gender_code("WS 4 (WTN 27.00 - 40.00)"), "F")
        self.assertEqual(draw_gender_code("WD Band 5"), "F")
        self.assertEqual(draw_gender_code("GS U10 April"), "F")
        self.assertEqual(draw_gender_code("GD 1000 U16/18"), "F")

    def test_irish_apostrophe_and_plural_variants_are_recognized(self):
        self.assertEqual(draw_gender_code("Mens Singles Band 3"), "M")
        self.assertEqual(draw_gender_code("Mens' Singles Band 5"), "M")
        self.assertEqual(draw_gender_code("Boy's Orange Ball"), "M")
        self.assertEqual(draw_gender_code("Womens Singles"), "F")
        self.assertEqual(draw_gender_code("Women's Singles"), "F")
        self.assertEqual(draw_gender_code("Girl's Orange Ball"), "F")

    def test_ladies_abbreviations_are_recognized(self):
        self.assertEqual(draw_gender_code("LS"), "F")
        self.assertEqual(draw_gender_code("LD B"), "F")

    def test_conflicting_gender_signals_stay_unassigned(self):
        self.assertEqual(draw_gender_code("Mens and Ladies Doubles"), "")
        self.assertEqual(draw_gender_code("Boys and Girls Green Ball"), "")

    def test_mixed_draw_codes_stay_unassigned(self):
        self.assertEqual(draw_gender_code("XD 5 (WTN 28.00 - 40.00)"), "")
        self.assertTrue(is_mixed_draw("XD 5 (WTN 28.00 - 40.00)"))
        self.assertEqual(draw_gender_code("Mixed Doubles"), "")
