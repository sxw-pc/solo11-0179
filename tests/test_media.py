import unittest

from exhibitflow.media import permit_render


class MediaBaseline(unittest.TestCase):
    def test_width_and_period_gate(self):
        license_record = {'artifact': 'artifact-1', 'max_width': 800, 'start': 1, 'end': 10}
        render = {'artifact': 'artifact-1', 'width': 800, 'day': 5}
        self.assertTrue(permit_render(render, license_record))
        self.assertFalse(permit_render({**render, 'width': 801}, license_record))
