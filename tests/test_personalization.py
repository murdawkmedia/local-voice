import importlib
import importlib.util
import unittest


class PersonalizationTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('local_voice.personalization'),
                             'Literal private personalization is not implemented')
        self.module = importlib.import_module('local_voice.personalization')

    def test_unicode_punctuation_and_case_insensitive_boundaries(self):
        text = 'murphy, MURPHY! unmurphy Murphyson; ÉLODIE / éloDIE.'
        result = self.module.apply_replacements(text, {'murphy': 'Murphy', 'élodie': 'Élodie'})
        self.assertEqual(result, 'Murphy, Murphy! unmurphy Murphyson; Élodie / Élodie.')

    def test_longest_overlaps_literal_escaping_and_no_cascade(self):
        result = self.module.apply_replacements('Ann Lee met Ann. C++ then a.b and xa.bx.',
                                                {'Ann': 'Anne', 'Ann Lee': 'Annalise',
                                                 'C++': 'C plus plus', 'a.b': 'literal',
                                                 'Anne': 'SHOULD NOT CASCADE'})
        self.assertEqual(result, 'Annalise met Anne. C plus plus then literal and xa.bx.')

    def test_snippets_require_entire_trigger_and_retain_raw_for_undo(self):
        profile = {'words': ['Name'], 'replacements': {'draft': 'polished'},
                   'snippets': {'sign off': 'Generated draft signoff'}}
        raw = '  SIGN OFF  '
        cleaned, retained = self.module.apply_personalization(raw, profile)
        self.assertEqual((cleaned, retained), ('Generated draft signoff', raw))
        self.assertEqual(self.module.apply_personalization('Please sign off draft.', profile),
                         ('Please sign off polished.', 'Please sign off draft.'))

    def test_empty_mapping_and_raw_whitespace_are_conservative(self):
        raw = '  generated   words\n'
        self.assertEqual(self.module.apply_personalization(raw, {}), ('generated   words', raw))
        self.assertEqual(self.module.apply_replacements(raw, {}), raw)

    def test_light_cleanup_removes_hesitations_and_retains_raw(self):
        cases = [
            ('Um, please send uh the file.', 'Please send the file.'),
            ('I can, um, send it, uh, tomorrow.', 'I can send it tomorrow.'),
            ('Umm. Send it. Uh, thanks!', 'Send it. Thanks!'),
            ('Please, ah, send it.', 'Please send it.'),
            ('um, uh, um.', ''),
            ('um, iPhone is ready.', 'iPhone is ready.'),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(self.module.apply_personalization(raw, {}, clean_fillers=True),
                                 (expected, raw))

    def test_cleanup_preserves_meaning_literals_and_existing_layout(self):
        cases = [
            'I guess I like it, you know. I, I really do.',
            'Ah, that explains it! Ah! Aha, uh-huh, uh-oh.',
            'ah, that explains it! ah! ah yes.',
            'UM and UH are codes. Ahmad met Uma in Durham.',
            'Type "um" or ‘uh’ or \'ah\'. Keep `um` and https://example.test/uh.',
            "I'm ready and don't need a rewrite.",
            '  - Keep   spacing.\n\n  - Keep punctuation!\n',
            'Contact um@example.test or open /tmp/uh and um.txt.',
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                self.assertEqual(self.module.apply_personalization(raw, {}, clean_fillers=True),
                                 (raw.strip(), raw))
        raw = 'First line.\n\n  - um, send   this.\n  - Keep that.'
        self.assertEqual(self.module.apply_personalization(raw, {}, clean_fillers=True)[0],
                         'First line.\n\n  - send   this.\n  - Keep that.')

    def test_cleanup_precedes_literal_dictionary_and_respects_opt_out(self):
        profile = {'replacements': {'draft': 'um, a literal replacement'},
                   'snippets': {'sign off': 'Um, an exact snippet.'}}
        self.assertEqual(self.module.apply_personalization('Um, draft.', profile, clean_fillers=True)[0],
                         'um, a literal replacement.')
        self.assertEqual(self.module.apply_personalization('sign off', profile, clean_fillers=True)[0],
                         'Um, an exact snippet.')
        self.assertEqual(self.module.apply_personalization('Um, draft.', {}, clean_fillers=False)[0],
                         'Um, draft.')


if __name__ == '__main__':
    unittest.main()
