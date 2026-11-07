from datetime import datetime,timezone
import unittest
from spark_intake import deadline_parts

class Timestamp:
    microsecond=0
    def __init__(self,nanosecond):self.nanosecond=nanosecond
    def utctimetuple(self):return datetime.fromtimestamp(1800000000,timezone.utc).utctimetuple()

class DeadlineTests(unittest.TestCase):
    def test_whole_second_exactly_seven_days(self):
        self.assertEqual(deadline_parts(Timestamp(0)),(1800000000,1800604800))
    def test_one_nanosecond_cannot_round_to_early_deletion(self):
        self.assertEqual(deadline_parts(Timestamp(1))[1],1800604801)
    def test_fractional_acceptance_waits_until_next_worker_second(self):
        self.assertEqual(deadline_parts(Timestamp(500000000)),(1800000000.5,1800604801))
    def test_last_nanosecond_never_adds_more_than_one_second(self):
        self.assertEqual(deadline_parts(Timestamp(999999999))[1],1800604801)
    def test_standard_datetime_uses_microseconds(self):
        value=datetime.fromtimestamp(1800000000.000001,timezone.utc)
        self.assertEqual(deadline_parts(value)[1],1800604801)

if __name__=='__main__':unittest.main()
