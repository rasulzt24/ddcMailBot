from datetime import time

from django.test import SimpleTestCase

from .services.bitrix import _parse, _state, seconds_of_day

OPENED = ("{'ID':'77657','STATE':'OPENED','CAN_EDIT':'N','REPORT_REQ':'Y','TM_FREE':true,'INFO':{'DATE_START':"
          "'1791172500','DATE_FINISH':'','TIME_START':'32100','TIME_FINISH':'','DURATION':'0','TIME_LEAKS':'0',"
          "'ACTIVE':true,'PAUSED':false,'CURRENT_STATUS':'OPENED','RECOMMENDED_CLOSE_TIMESTAMP':'1791204900'},"
          "'SOCSERV_ENABLED':false,'PLANNER':{'EVENTS':[],'TASKS':[],'TASK_ADD_URL':"
          "'/company/personal/user/623/tasks/task/edit/0/?ADD_TO_TIMEMAN=Y'},'FULL':true,'REPORT':''}")
CLOSED = ("{'ID':'77657','STATE':'CLOSED','CAN_EDIT':'N','CAN_OPEN':'OPEN','INFO':{'DATE_START':'1791172500',"
          "'DATE_FINISH':'1791207300','TIME_START':'32100','TIME_FINISH':'66900','DURATION':'34800',"
          "'PAUSED':false,'CURRENT_STATUS':'CLOSED'},'FULL':true}")


class BitrixParseTests(SimpleTestCase):
    def test_opened(self):
        st = _state(_parse(OPENED))
        self.assertEqual((st.state, st.record_id), ("OPENED", "77657"))
        self.assertTrue(st.is_open)
        self.assertEqual(st.start.strftime("%H:%M"), "08:55")  # Asia/Almaty, UTC+5
        self.assertIsNone(st.finish)

    def test_closed(self):
        st = _state(_parse(CLOSED))
        self.assertFalse(st.is_open)
        self.assertEqual((st.start.strftime("%H:%M"), st.finish.strftime("%H:%M")), ("08:55", "18:35"))
        self.assertEqual(st.duration, 34800)

    def test_timestamp_is_seconds_of_day(self):
        self.assertEqual(seconds_of_day(time(8, 55)), 32100)
        self.assertEqual(seconds_of_day(time(18, 35)), 66900)

    def test_error_response(self):
        from .services.bitrix import BitrixError
        with self.assertRaises(BitrixError):
            _parse("{'error':'Рабочий день уже начат','error_id':'ALREADY_OPENED'}")
