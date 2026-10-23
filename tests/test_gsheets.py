import unittest

from qb_agent.gsheets import Sheets


class GridExpansionTests(unittest.TestCase):
    def sheets(self, current):
        client = Sheets("key.json", "SHEET")
        calls = []

        def call(path, body=None):
            calls.append((path, body))
            if body is None:
                return {
                    "sheets": [{
                        "properties": {
                            "sheetId": 17,
                            "title": "CL NB Pipeline",
                            "gridProperties": {"columnCount": current},
                        }
                    }]
                }
            return {}

        client._call = call
        return client, calls

    def test_grid_is_expanded_before_writing_columns_beyond_its_limit(self):
        client, calls = self.sheets(24)

        client.ensure_column_count("CL NB Pipeline", 27)

        request = calls[1][1]["requests"][0]["appendDimension"]
        self.assertEqual(request["sheetId"], 17)
        self.assertEqual(request["dimension"], "COLUMNS")
        self.assertEqual(request["length"], 3)

    def test_an_already_wide_enough_grid_is_not_changed(self):
        client, calls = self.sheets(30)

        client.ensure_column_count("CL NB Pipeline", 27)

        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
