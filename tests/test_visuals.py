import os
import tempfile
import unittest

from PIL import Image, ImageDraw

from visuals import font, fit_text, headline, tracked_text


class TypographyTests(unittest.TestCase):
    def test_bundled_fonts_work_outside_project_directory(self):
        cwd = os.getcwd()
        try:
            os.chdir(tempfile.gettempdir())
            font.cache_clear()
            self.assertEqual(font(24).getname()[0], 'Inter')
            self.assertEqual(font(80, display=True).getname()[0], 'Oswald')
            # Distinct Cyrillic shapes, rather than repeated missing-glyph boxes.
            self.assertNotEqual(bytes(font(24).getmask('К')), bytes(font(24).getmask('У')))
            self.assertNotEqual(bytes(font(24).getmask('Кубок')),
                                bytes(font(24, True).getmask('Кубок')))
        finally:
            os.chdir(cwd)

    def test_long_mixed_script_names_fit_reserved_space(self):
        draw = ImageDraw.Draw(Image.new('RGB', (500, 200)))
        for name in ('Очень длинное название команды go go power rangers', 'Победитель 1.16'):
            value, face = fit_text(draw, name, 28, 150, bold=True)
            self.assertLessEqual(draw.textlength(value, font=face), 150)
        self.assertTrue(fit_text(draw, 'Команда ' * 30, 28, 150)[0].endswith('…'))

    def test_headline_and_tracking_render_without_clipping(self):
        image = Image.new('RGB', (500, 200), 'white')
        draw = ImageDraw.Draw(image)
        headline(draw, (20, 20), 'Кубок мини-лиги', max_width=460)
        tracked_text(draw, (20, 130), 'GW9 · ФИНАЛ', font(20), spacing=3)
        # No drawing may extend into the right safety margin.
        self.assertIsNone(Image.eval(image.crop((480, 0, 500, 200)).convert('L'),
                                    lambda pixel: 255-pixel).getbbox())
