import math
import garminconnect
from typing import Mapping, List
import os
from datetime import date, timedelta, datetime
from dataclasses import dataclass
from dateutil.parser import parse
from flask import Flask, Response


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
        raise Exception("No garmin credentials available!")

    garmin = garminconnect.Garmin()
    garth_path = os.path.expanduser("~/.garth/")
    if os.path.exists(garth_path):
        garmin.login(tokenstore=garth_path)
    if garmin.display_name:
        return garmin
    print("Falling back to logging in via email/password")
    garmin = garminconnect.Garmin(email, password)
    garmin.login()
    garmin.garth.dump( "~/.garth")
    return garmin

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

CACHED_ZONE_INFO: Mapping[str, Mapping[int, int]] = {}

def get_zone_to_elapsed_time(garmin: garminconnect.Garmin, day_to_check: str) -> Mapping[int, int]:
    # Check if the result has already been cached
    if day_to_check in CACHED_ZONE_INFO:
        return CACHED_ZONE_INFO[day_to_check]

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

    # Merge together the zone info across all the activities
    zone_to_elapsed_time = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    for activity in activity_infos:
        zone_to_elapsed_time = merge_zone_times(zone_to_elapsed_time, activity.zone_info)

    # Store it in the cache if it is stable
    if day_to_check != date.today().isoformat():
        CACHED_ZONE_INFO[day_to_check] = zone_to_elapsed_time
    return zone_to_elapsed_time

def merge_zone_times(*zone_times: Mapping[int, int]) -> Mapping[int, int]:
    ret = {}
    for zt in zone_times:
        for k, v in zt.items():
            ret[k] = ret.get(k, 0) + v
    return ret

def average_time_in_zone_2_plus(garmin: garminconnect.Garmin, num_days: int, start_days_ago: int) -> timedelta:
    zone_to_elapsed_time = {}
    for days_ago in range(0, num_days):
        day_to_check = (date.today() - timedelta(days=days_ago) - timedelta(days=start_days_ago)).isoformat()
        ztet = get_zone_to_elapsed_time(garmin, day_to_check)
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
    ret += "Daily Load: "
    ret += str(math.floor(average_time_in_zone_2_plus(garmin, 1, 0).seconds/60))
    ret += "\n\n"

    ret += "Weekly Stats:\n"
    ret += f"Week of {date.today().isoformat()}: " + pretty_print_td(average_time_in_zone_2_plus(garmin, 7, 0)) + "\n"
    ret += f"Week of {(date.today()-timedelta(days=7)).isoformat()}: " + pretty_print_td(average_time_in_zone_2_plus(garmin, 7, 7)) + "\n"
    ret += f"Week of {(date.today()-timedelta(days=14)).isoformat()}: " + pretty_print_td(average_time_in_zone_2_plus(garmin, 7, 14)) + "\n"
    ret += f"Week of {(date.today()-timedelta(days=21)).isoformat()}: " + pretty_print_td(average_time_in_zone_2_plus(garmin, 7, 21)) + "\n"
    ret += f"Week of {(date.today()-timedelta(days=28)).isoformat()}: " + pretty_print_td(average_time_in_zone_2_plus(garmin, 7, 28)) + "\n"
    ret += "\n"

    ret += "Trailing Load Average:\n"
    for i in range(14):
        ret += f"{(date.today()-timedelta(days=i)).isoformat()}: Load " + str(math.floor(average_time_in_zone_2_plus(garmin, 7, i).seconds/60)) + "\n"
    ret += "\n"

    ret += "Monthly Zone Breakdown:\n"
    zone_to_elapsed_time = {}
    for days_ago in range(0, 30):
        day_to_check = (date.today() - timedelta(days=days_ago)).isoformat()
        ztet = get_zone_to_elapsed_time(garmin, day_to_check)
        zone_to_elapsed_time = merge_zone_times(zone_to_elapsed_time, ztet)
    for i in range(0, 6):
        ret += f"Zone {i}: {pretty_print_td(timedelta(seconds=zone_to_elapsed_time[i]))}\n"
    ret += "\n"
    return ret

app = Flask(__name__)

@app.route('/garmin-stats')
def garmin_stats():
    return Response(build_stats(), mimetype='text/plain')