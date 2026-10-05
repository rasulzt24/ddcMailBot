from aiogram.fsm.state import State, StatesGroup


class ConnectSG(StatesGroup):
    email = State()
    password = State()


class ComposeSG(StatesGroup):
    to = State()
    cc = State()
    subject = State()
    body = State()
    files = State()
    confirm = State()


class SearchSG(StatesGroup):
    query = State()


class SettingsSG(StatesGroup):
    signature = State()
    display_name = State()


class AdminSG(StatesGroup):
    broadcast = State()


class WorkdaySG(StatesGroup):
    time = State()
    login = State()
    password = State()
    remind_time = State()
