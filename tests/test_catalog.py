import unittest

from exhibitflow.catalog import select_label


class CatalogBaseline(unittest.TestCase):
    def test_latest_editorial_record(self):
        records = [
            {"object_id": "art-a", "language": "zh", "revision": 1,
             "received_at": 1, "title": "铜灯", "date_label": "年代未定", "source_id": "scan-1"},
            {"object_id": "art-a", "language": "zh", "revision": 2,
             "received_at": 3, "title": "铜灯", "date_label": "公元二世纪", "source_id": "scan-2"},
        ]
        self.assertEqual(select_label(records, "art-a", "zh")["source_id"], "scan-2")
        self.assertIsNone(select_label(records, "art-a", "en"))
