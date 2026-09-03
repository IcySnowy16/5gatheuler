"""Inline keyboards for the availability flow (calendar and time pickers)."""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def month_calendar(year: int, month: int, code: str) -> InlineKeyboardMarkup:
    """Month grid; past days are inert. Callback: date|Y|M|D|CODE, nav|Y|M|CODE."""
    today = date.today()
    keyboard = [[InlineKeyboardButton(f"{calendar.month_name[month]} {year}", callback_data="ignore")],
                [InlineKeyboardButton(d, callback_data="ignore") for d in "MTWTFSS"]]
    for week in calendar.monthcalendar(year, month):
        row = []
        for day in week:
            if day == 0 or date(year, month, day) < today:
                row.append(InlineKeyboardButton(" ", callback_data="ignore"))
            else:
                row.append(InlineKeyboardButton(str(day), callback_data=f"date|{year}|{month}|{day}|{code}"))
        keyboard.append(row)
    prev_m = datetime(year, month, 1) - timedelta(days=1)
    next_m = datetime(year, month, 28) + timedelta(days=4)
    nav = []
    if (prev_m.year, prev_m.month) >= (today.year, today.month):
        nav.append(InlineKeyboardButton("<", callback_data=f"nav|{prev_m.year}|{prev_m.month}|{code}"))
    nav.append(InlineKeyboardButton(">", callback_data=f"nav|{next_m.year}|{next_m.month}|{code}"))
    keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("Done adding", callback_data=f"done|{code}")])
    return InlineKeyboardMarkup(keyboard)


def start_time_picker(year: int, month: int, day: int, code: str) -> InlineKeyboardMarkup:
    keyboard = [[InlineKeyboardButton(f"Start time for {year}-{month:02d}-{day:02d}",
                                      callback_data="ignore")]]
    row = []
    for hour in range(8, 24):
        for minute in (0, 30):
            row.append(InlineKeyboardButton(
                f"{hour:02d}:{minute:02d}",
                callback_data=f"t_start|{year}|{month}|{day}|{hour}|{minute}|{code}"))
            if len(row) == 4:
                keyboard.append(row)
                row = []
    if row:
        keyboard.append(row)
    keyboard.append([
        InlineKeyboardButton("Back to calendar", callback_data=f"nav|{year}|{month}|{code}"),
        InlineKeyboardButton("Done adding", callback_data=f"done|{code}"),
    ])
    return InlineKeyboardMarkup(keyboard)


def end_time_picker(year: int, month: int, day: int, start_h: int, start_m: int,
                    code: str) -> InlineKeyboardMarkup:
    keyboard = [[InlineKeyboardButton(f"Start {start_h:02d}:{start_m:02d} - pick end:",
                                      callback_data="ignore")]]
    row = []
    h, m = start_h, start_m + 30
    if m >= 60:
        h, m = h + 1, m - 60
    while h < 24 or (h == 24 and m == 0):
        row.append(InlineKeyboardButton(
            f"{h:02d}:{m:02d}",
            callback_data=f"t_save|{year}|{month}|{day}|{start_h}|{start_m}|{h}|{m}|{code}"))
        if len(row) == 4:
            keyboard.append(row)
            row = []
        m += 30
        if m >= 60:
            h, m = h + 1, m - 60
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("Back to start time",
                                          callback_data=f"date|{year}|{month}|{day}|{code}")])
    return InlineKeyboardMarkup(keyboard)
