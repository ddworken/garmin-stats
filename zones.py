# coding: utf-8
import math
import garminconnect
from typing import Mapping, Any, List
import os
from datetime import date, timedelta, datetime
import json
from dataclasses import dataclass
from dateutil.parser import parse
import cachetools.func


def timestamp_from_millis(ts: int) -> datetime:
    return datetime.fromtimestamp(ts/1000)

def authenticate() -> garminconnect.Garmin:
    email = ''
    password =''
    creds_path=os.path.expanduser('~/.garth/creds')
    if os.path.exists(creds_path):
        with open(creds_path) as f:
            email, password = f.read().strip().split(':')
    elif 'GARMIN_USERNAME' in os.environ and 'GARMIN_PASSWORD' in os.environ:
        email = os.environ['GARMIN_USERNAME']
        password = os.environ['GARMIN_PASSWORD']
    else:
        raise Exception(str(os.environ))

    garmin = garminconnect.Garmin(email, password)
    garmin.login()
    if garmin.display_name:
        return garmin
    garmin.login(tokenstore=os.path.expanduser("~/.garth/"))
    garmin.garth.dump( "~/.garth")
    return garmin

def get_zone(hr: int) -> int:
    if hr < 98:
        return 0
    if hr <= 116:
        return 1
    if hr <= 136:
        return 2
    if hr <= 155:
        return 3
    if hr <= 175:
        return 4
    return 5

@dataclass
class ActivityInfo:
    id: int
    start_time: datetime
    end_time: datetime
    zone_info: Mapping[int, int]

def get_zone_info(garmin: garminconnect.Garmin, activity_id: int) -> Mapping[int, int]:
    resp = garmin.get_activity_hr_in_timezones(activity_id)
    ret = {}
    for zone in resp:
        ret[zone['zoneNumber']] = zone['secsInZone']
    return ret

@cachetools.func.ttl_cache(maxsize=1000, ttl=60)
def get_zone_to_elapsed_time(garmin: garminconnect.Garmin, day_to_check: str, include_non_activity_data: bool) -> Mapping[int, int]:
    # Get the activity infos
    activity_infos: List[ActivityInfo] = []
    all_activities_resp = garmin.get_activities_fordate(day_to_check)
    for activity in all_activities_resp['ActivitiesForDay']['payload']:
        activity_infos.append(ActivityInfo(
            id=activity['activityId'],
            start_time=parse(activity['startTimeLocal']),
            end_time=parse(activity['startTimeLocal']) + timedelta(seconds=activity['duration']),
            zone_info=get_zone_info(garmin, activity['activityId']),
        ))

    # Get the HR data (that has a 2 minute granularity) and calculate based on it, but exclude any HRs during activities
    hr_data = garmin.get_heart_rates(day_to_check)
    zone_to_elapsed_time = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    previousTs = timestamp_from_millis(hr_data['heartRateValues'][0][0])
    for unixTs, hr in hr_data['heartRateValues']:
        ts = timestamp_from_millis(unixTs)
        time_elapsed = (ts - previousTs)
        if hr is None:
            continue
        previousTs = ts
        if is_during_activity(ts, activity_infos):
            continue
        if include_non_activity_data:
            zone_to_elapsed_time[get_zone(hr)] += time_elapsed.total_seconds() # TODOOOOOOO

    # Merge in the more precise data from during activities that has a better than 2 minute granularity
    for activity in activity_infos:
        zone_to_elapsed_time = merge_zone_times(zone_to_elapsed_time, activity.zone_info)
    return zone_to_elapsed_time

def is_during_activity(ts: datetime, activity_infos: List[ActivityInfo]) -> bool:
    for activity in activity_infos:
        if ts > activity.start_time and ts < activity.end_time:
            return True
    return False

def merge_zone_times(*zone_times: Mapping[int, int]) -> Mapping[int, int]:
    ret = {}
    for zt in zone_times:
        for k, v in zt.items():
            ret[k] = ret.get(k, 0) + v
    return ret

def average_time_in_zone_2_plus(garmin: garminconnect.Garmin, num_days: int, start_days_ago: int, include_non_activity_data=False) -> timedelta:
    zone_to_elapsed_time = {}
    for days_ago in range(0, num_days):
        day_to_check = (date.today() - timedelta(days=days_ago) - timedelta(days=start_days_ago)).isoformat()
        ztet = get_zone_to_elapsed_time(garmin, day_to_check, include_non_activity_data)
        zone_to_elapsed_time = merge_zone_times(zone_to_elapsed_time, ztet)
    return timedelta(seconds=zone_to_elapsed_time[2] + zone_to_elapsed_time[3] + zone_to_elapsed_time[4] + zone_to_elapsed_time[5])

def pretty_print_td(td: timedelta) -> str:
    days = td.days
    hours = math.floor(td.seconds/60/60)
    minutes = math.floor((td.seconds/60) % 60)
    if days > 0:
        return f"{days} days, {hours} hours, and {minutes} minutes"
    if hours > 0:
        return f"{hours} hours and {minutes} minutes"
    return f"{minutes} minutes"

def build_stats() -> str:
    garmin = authenticate()
    ret = ""
    ret += "Weekly Stats:\n"
    ret += f"Week of {date.today().isoformat()}: " + pretty_print_td(average_time_in_zone_2_plus(garmin, 7, 0)) + "\n"
    ret += f"Week of {(date.today()-timedelta(days=7)).isoformat()}: " + pretty_print_td(average_time_in_zone_2_plus(garmin, 7, 7)) + "\n"
    ret += f"Week of {(date.today()-timedelta(days=14)).isoformat()}: " + pretty_print_td(average_time_in_zone_2_plus(garmin, 7, 14)) + "\n"
    ret += f"Week of {(date.today()-timedelta(days=21)).isoformat()}: " + pretty_print_td(average_time_in_zone_2_plus(garmin, 7, 21)) + "\n"
    ret += f"Week of {(date.today()-timedelta(days=28)).isoformat()}: " + pretty_print_td(average_time_in_zone_2_plus(garmin, 7, 28)) + "\n"
    ret += "\n"

    ret += "Trailing Load Average\n"
    for i in range(14):
        ret += f"{(date.today()-timedelta(days=i)).isoformat()}: Load " + str(math.floor(average_time_in_zone_2_plus(garmin, 7, i).seconds/60)) + "\n"
    ret += "\n"

    ret += "Monthly Zone Breakdown\n"
    zone_to_elapsed_time = {}
    for days_ago in range(0, 30):
        day_to_check = (date.today() - timedelta(days=days_ago)).isoformat()
        ztet = get_zone_to_elapsed_time(garmin, day_to_check, include_non_activity_data=False)
        zone_to_elapsed_time = merge_zone_times(zone_to_elapsed_time, ztet)
    for i in range(0, 6):
        ret += f"Zone {i}: {pretty_print_td(timedelta(seconds=zone_to_elapsed_time[i]))}\n"
    ret += "\n"
    return ret

from flask import Flask, Response
app = Flask(__name__)

@app.route('/garmin-stats')
def garmin_stats():
    return Response(build_stats(), mimetype='text/plain')