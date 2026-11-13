import unittest

from victus_fan.logparse import filter_entries, parse_log

LOG = """2026-11-12 18:44:36,421 INFO victus_fan.backends.windows: CPU-Temperatur von: ACPI THRM_0
2026-11-12 19:00:27,782 INFO victus_fan.service: Bildschirm aus - die Firmware regelt
2026-11-12 19:01:10,000 WARNING victus_fan.service: Drehzahl [2700, 0] U/min weicht ab
2026-11-12 19:01:11,000 ERROR victus_fan.service: Unerwarteter Fehler
Traceback (most recent call last):
  File "x.py", line 1, in <module>
ValueError: kaputt
2026-11-12 19:02:00,123 INFO Aktuell (2.7.0)
"""


class LogWindowTest(unittest.TestCase):
    def test_parse(self):
        entries = parse_log(LOG)
        self.assertEqual(len(entries), 5)
        first = entries[0]
        self.assertEqual((first.date, first.time, first.level), ("12.11.", "18:44:36", "Info"))
        self.assertEqual(first.message, "CPU-Temperatur von: ACPI THRM_0")    # ohne Modulname
        self.assertEqual(entries[2].level, "Warnung")
        self.assertEqual(entries[3].level, "Fehler")
        self.assertIn("ValueError: kaputt", entries[3].message)               # Traceback bleibt dabei
        self.assertEqual(entries[4].message, "Aktuell (2.7.0)")               # update.log-Format

    def test_filter(self):
        entries = parse_log(LOG)
        self.assertEqual([e.level for e in filter_entries(entries, True, "")], ["Warnung", "Fehler"])
        self.assertEqual(len(filter_entries(entries, False, "bildschirm")), 1)
        self.assertEqual(len(filter_entries(entries, True, "bildschirm")), 0)

    def test_empty(self):
        self.assertEqual(parse_log(""), [])


if __name__ == "__main__":
    unittest.main()
