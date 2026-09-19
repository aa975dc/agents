import unittest

from ledger import total_expenses


class ExpenseTests(unittest.TestCase):
    def test_two_expenses(self):
        self.assertEqual(total_expenses([20, 30]), 50)

    def test_no_expenses(self):
        self.assertEqual(total_expenses([]), 0)

    def test_negative_expense(self):
        with self.assertRaises(ValueError):
            total_expenses([20, -1])

    def test_non_integer_expense(self):
        for amount in (1.5, "20", True):
            with self.subTest(amount=amount):
                with self.assertRaises(TypeError):
                    total_expenses([amount])


if __name__ == "__main__":
    unittest.main()
