"""Display-only working-time arithmetic; never schedules or approves work."""
from datetime import datetime, timedelta, timezone, date
import math
import re

DEFAULT = {'utc_offset_minutes': 540, 'start': '09:00', 'end': '18:00',
           'workdays': [0, 1, 2, 3, 4], 'holidays': []}


def validate(cfg):
    if not isinstance(cfg, dict) or set(cfg) != set(DEFAULT):
        raise ValueError('勤務時間の設定項目が不正です。')
    offset = cfg['utc_offset_minutes']
    if type(offset) is not int or not -840 <= offset <= 840:
        raise ValueError('勤務時間のUTCオフセットが不正です。')
    for key in ('start', 'end'):
        if not isinstance(cfg[key], str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', cfg[key]):
            raise ValueError('勤務時間はHH:MMで指定してください。')
    if cfg['start'] >= cfg['end']:
        raise ValueError('勤務終了は開始より後に指定してください。')
    days = cfg['workdays']
    if not isinstance(days, list) or any(type(d) is not int or not 0 <= d <= 6 for d in days) or len(set(days)) != len(days):
        raise ValueError('勤務曜日が不正です。')
    holidays = cfg['holidays']
    if not isinstance(holidays, list) or len(holidays) > 400:
        raise ValueError('休日は400件以内で指定してください。')
    for value in holidays:
        if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise ValueError('休日はYYYY-MM-DDで指定してください。')
        date.fromisoformat(value)


def business_segments(start, end, cfg):
    validate(cfg)
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (start, end)):
        raise ValueError('計測時刻が不正です。')
    if end <= start:
        return []
    zone = timezone(timedelta(minutes=cfg['utc_offset_minutes']))
    first, last = datetime.fromtimestamp(start, zone), datetime.fromtimestamp(end, zone)
    if (last.date() - first.date()).days >= 400:
        raise ValueError('計算範囲外（400日以上）')
    day = first.replace(hour=0, minute=0, second=0, microsecond=0)
    segments = []
    while day.date() <= last.date():
        if day.weekday() in cfg['workdays'] and day.date().isoformat() not in cfg['holidays']:
            a, b = [day.replace(hour=int(cfg[k][:2]), minute=int(cfg[k][3:])).timestamp() for k in ('start', 'end')]
            a, b = max(a, start), min(b, end)
            if b > a:
                segments.append((a, b))
        day += timedelta(days=1)
    return segments


def split_seconds(start, end, cfg):
    inside = sum(b-a for a, b in business_segments(start, end, cfg))
    return inside, max(0, end-start) - inside
