import math
import garminconnect
from typing import Mapping, List, Optional
import os
from datetime import date, timedelta, datetime
from dataclasses import dataclass
from dateutil.parser import parse
from flask import Flask, Response, send_file, request, copy_current_request_context
import json 
from flask_caching import Cache
import threading
from functools import wraps


def timestamp_from_millis(ts: int) -> datetime:
    return datetime.fromtimestamp(ts / 1000)


def authenticate() -> garminconnect.Garmin:
    email = ""
    password = ""
    creds_path = os.path.expanduser("~/.garth/creds")
    if os.path.exists(creds_path):
        with open(creds_path) as f:
            email, password = f.read().strip().split(":")
    elif "GARMIN_USERNAME" in os.environ and "GARMIN_PASSWORD" in os.environ:
        email = os.environ["GARMIN_USERNAME"]
        password = os.environ["GARMIN_PASSWORD"]
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
    garmin.garth.dump("~/.garth")
    return garmin


@dataclass
class ActivityInfo:
    id: int
    start_time: datetime
    end_time: datetime
    zone_info: Mapping[int, int]
    name: str
    description: Optional[str]
    distance: float


def get_zone_info(
    garmin: garminconnect.Garmin,
    activity_id: int,
    description: Optional[str],
    duration: float,
) -> Mapping[int, int]:
    if description is None:
        description = ""
    min_zone = 0
    if "ZONE(1)" in description:
        min_zone = 1
    elif "ZONE(2)" in description:
        min_zone = 2
    elif "ZONE(3)" in description:
        min_zone = 3
    resp = garmin.get_activity_hr_in_timezones(activity_id)
    ret = make_empty_zone_map()
    time_in_zones = 0
    for zone in resp:
        zone_number = zone["zoneNumber"]
        if zone_number < min_zone:
            zone_number = min_zone
        ret[zone_number] += zone["secsInZone"]
        time_in_zones += zone["secsInZone"]
    if min_zone > 0:
        time_in_zone_zero = duration - time_in_zones
        ret[min_zone] += time_in_zone_zero
    return ret

# Cache to store activity infos for each day
CACHED_ACTIVITY_INFOS: Mapping[str, List[ActivityInfo]] = {}

def get_activity_infos(
    garmin: garminconnect.Garmin, day_to_check: str
) -> List[ActivityInfo]:
    if day_to_check in CACHED_ACTIVITY_INFOS:
        return CACHED_ACTIVITY_INFOS[day_to_check]
    activity_infos: List[ActivityInfo] = []
    all_activities_resp = garmin.get_activities_fordate(day_to_check)
    for activity in all_activities_resp["ActivitiesForDay"]["payload"]:
        description = activity.get("description", None)
        duration = activity["duration"]
        activity_infos.append(
            ActivityInfo(
                id=activity["activityId"],
                start_time=parse(activity["startTimeLocal"]),
                end_time=parse(activity["startTimeLocal"])
                + timedelta(seconds=duration),
                zone_info=get_zone_info(
                    garmin, activity["activityId"], description, duration
                ),
                name=activity["activityName"],
                description=description,
                distance=activity.get("distance", 0)*0.00062137, # convert meters to miles
            )
        )
    if is_stable_day(day_to_check):
        CACHED_ACTIVITY_INFOS[day_to_check] = activity_infos
    return activity_infos


def make_empty_zone_map() -> Mapping[int, int]:
    return {0: 0, 1: 0, 2: 0, 3: 0, 4: 0, 5: 0}


CACHED_ZONE_INFO: Mapping[str, Mapping[int, int]] = {}


def get_zone_to_elapsed_time(
    garmin: garminconnect.Garmin, day_to_check: str
) -> Mapping[int, int]:
    # Check if the result has already been cached
    if day_to_check in CACHED_ZONE_INFO:
        return CACHED_ZONE_INFO[day_to_check]

    # Get the activity infos
    activity_infos = get_activity_infos(garmin, day_to_check)

    # Merge together the zone info across all the activities
    zone_to_elapsed_time = make_empty_zone_map()
    for activity in activity_infos:
        zone_to_elapsed_time = merge_zone_times(
            zone_to_elapsed_time, activity.zone_info
        )

    # Store it in the cache if it is stable
    if is_stable_day(day_to_check):
        CACHED_ZONE_INFO[day_to_check] = zone_to_elapsed_time
    return zone_to_elapsed_time


def is_stable_day(day_to_check: str) -> bool:
    return day_to_check != date.today().isoformat()        and day_to_check != (date.today() - timedelta(days=1)).isoformat()


def merge_zone_times(*zone_times: Mapping[int, int]) -> Mapping[int, int]:
    ret = {}
    for zt in zone_times:
        for k, v in zt.items():
            ret[k] = ret.get(k, 0) + v
    return ret


def calculate_load(
    garmin: garminconnect.Garmin, num_days: int, start_days_ago: int
) -> int:
    zone_to_elapsed_time = {}
    for days_ago in range(0, num_days):
        day_to_check = (
            date.today() - timedelta(days=days_ago) - timedelta(days=start_days_ago)
        ).isoformat()
        ztet = get_zone_to_elapsed_time(garmin, day_to_check)
        zone_to_elapsed_time = merge_zone_times(zone_to_elapsed_time, ztet)
    return td_to_load(calculate_load_time(zone_to_elapsed_time))

def calculate_strength_load(
            garmin: garminconnect.Garmin, num_days: int, start_days_ago: int
) -> int:
    sum = 0 
    for days_ago in range(0, num_days):
        day_to_check = (
            date.today() - timedelta(days=days_ago) - timedelta(days=start_days_ago)
        ).isoformat()
        activities = get_activity_infos(garmin, day_to_check)
        for activity in activities:
            if activity.name == 'Strength':
                sum += (activity.end_time-activity.start_time).seconds / 60
    return int(sum) 

def calculate_load_time(ztet: Mapping[int, int]) -> timedelta:
    return timedelta(seconds=(ztet[1] * 0.5) + ztet[2] + ztet[3] + ztet[4] + ztet[5])


def td_to_load(td: timedelta) -> int:
    return math.floor(td.seconds / 60)


def pretty_print_td(td: timedelta) -> str:
    days = td.days
    hours = math.floor(td.seconds / 60 / 60)
    minutes = math.floor((td.seconds / 60) % 60)
    if days > 0:
        return f"{days} days, {hours} hours, and {minutes} minutes"
    if hours > 0:
        return f"{hours} hours and {minutes} minutes"
    return f"{minutes} minutes"


def lpad(s: str, n: int) -> str:
    if len(s) >= n:
        return s
    num_needed = n - len(s)
    return (" " * num_needed) + s


def build_stats() -> str:
    garmin = authenticate()
    ret = ""
    ret += "Daily Load: "
    ret += str(calculate_load(garmin, 1, 0))
    ret += "\n"
    for activity in get_activity_infos(garmin, date.today().isoformat()):
        ret += f"* {activity.name} ({activity.description}): {pretty_print_td(activity.end_time - activity.start_time)} - Load {td_to_load(calculate_load_time(activity.zone_info))}\n"
    ret += "\n"

    ret += "Historical Daily Load:\n"
    for i in range(14):
        load = calculate_load(garmin, 1, i)
        ret += f"{(date.today()-timedelta(days=i)).isoformat()}: Load "
        ret += lpad(str(load), 3)
        # Add a basic graph to visualize the trailing load
        ret += " "
        ret += "-" * math.floor(load / 10)
        ret += "\n"
    ret += "\n"

    ret += "Historical Weekly Load Average:\n"
    for i in range(14):
        load = calculate_load(garmin, 7, i)
        ret += f"{(date.today()-timedelta(days=i)).isoformat()}: Load "
        ret += lpad(str(load), 3)
        # Add a basic graph to visualize the trailing load
        ret += " "
        ret += "-" * math.floor(load / 10)
        ret += "\n"
    ret += "\n"

    ret += "Historical Weekly Stats:\n"
    for week_num in range(0, 20):
        load = calculate_load(garmin, 7, week_num * 7)
        ret += f"Week of {(date.today()-timedelta(days=week_num*7)).isoformat()}: "
        ret += lpad(str(load), 3)
        # Add a basic graph to visualize the trailing load
        ret += " "
        ret += "-" * math.floor(load / 10)
        ret += "\n"
    ret += "\n"

    ret += "Monthly Zone Breakdown:\n"
    zone_to_elapsed_time = {}
    for days_ago in range(0, 30):
        day_to_check = (date.today() - timedelta(days=days_ago)).isoformat()
        ztet = get_zone_to_elapsed_time(garmin, day_to_check)
        zone_to_elapsed_time = merge_zone_times(zone_to_elapsed_time, ztet)
    for i in range(0, 6):
        ret += (
            f"Zone {i}: {pretty_print_td(timedelta(seconds=zone_to_elapsed_time[i]))}\n"
        )
    ret += "\n"
    return ret


app = Flask(__name__)
config = {
    "DEBUG": True,          # some Flask specific configs
    "CACHE_TYPE": "SimpleCache",  # Flask-Caching related configs
    "CACHE_DEFAULT_TIMEOUT": 300
}
app.config.from_mapping(config)
cache = Cache(app)


@app.route("/garmin-stats")
def garmin_stats():
    return Response(build_stats(), mimetype="text/plain")


def download_files():
    garmin = authenticate()
    activities = garmin.get_activities_by_date(
        '2023-01-01', '2024-08-16'
    )
    print(f"{len(activities)=}")
    for i, activity in enumerate(activities):
        activity_id = activity["activityId"]
        activity_name = activity["activityName"]
        if activity_name in {'Elliptical', 'Indoor Climbing', 'Indoor Rowing', 'Treadmill Running', 'Strength', 'Cardio', 'Seattle Walking', 'Seattle Running', 'Stair Stepper', 'Indoor Cycling'}:
            continue
        print(f"{i=}/{len(activities)} {activity_name=}")

        gpx_data = garmin.download_activity(
            activity_id, dl_fmt=garmin.ActivityDownloadFormat.GPX
        )
        output_file = f"./gpx-export/{activity_name}-{activity_id}.gpx"
        with open(output_file, "wb") as fb:
            fb.write(gpx_data)


@app.route("/graph.html")
def index():
    return send_file('graph.html')

def cached_with_background_refresh(timeout=300, query_string=True):
    def decorator(func):
        @wraps(func)
        @cache.cached(timeout=timeout, query_string=query_string, unless=lambda: not request.args.get('ec'))
        def cached_func(*args, **kwargs):
            return func(*args, **kwargs)

        @wraps(func)
        def wrapper(*args, **kwargs):
            result = cached_func(*args, **kwargs)

            @copy_current_request_context
            def func_with_context(*args, **kwargs):
                return func(*args, **kwargs)

            # Start a background thread to refresh the cache
            if request.args.get('ec'):
                thread = threading.Thread(target=func_with_context, args=args, kwargs=kwargs)
                thread.start()
            
            return result

        return wrapper
    return decorator


@app.route("/api/stats")
@cached_with_background_refresh()
def api_stats():    
    garmin = authenticate()
    today = date.today()
    stats = []

    for days_ago in range(int(request.args.get('n', 90))):
        load = calculate_load(garmin, int(request.args.get('load_period', 90)), days_ago)
        stats.append({
            "date": (today - timedelta(days=days_ago)).isoformat(),
            "quantity": load,
            "strength_load": calculate_strength_load(garmin, int(request.args.get('load_period', 90)), days_ago)
        })

    return Response(json.dumps(stats), mimetype="application/json")

@app.route("/api/events")
@cached_with_background_refresh()
def api_events():
    garmin = authenticate()
    today = date.today()
    events = []

    # Fetch activities for the last 90 days
    n_days = int(request.args.get('n', 90))  # Get 'n' from query params, default to 90
    for days_ago in range(n_days):
        day_to_check = (today - timedelta(days=days_ago)).isoformat()
        activities = get_activity_infos(garmin, day_to_check)
        
        for activity in activities:
            duration = (activity.end_time - activity.start_time).total_seconds() / 3600  # Convert to hours
            if duration > 6:
                events.append({
                    "date": activity.start_time.date().isoformat(),
                    "eventName": activity.name
                })
    return Response(json.dumps(events), mimetype="application/json")



@app.route("/api/today")
@cached_with_background_refresh()
def api_today():
    garmin = authenticate()
    
    # Calculate today's total load
    today_load = calculate_load(garmin, 1, 0)
    
    # Get today's activities
    today = date.today().isoformat()
    activities = get_activity_infos(garmin, today)
    
    # Prepare the events list
    events = []
    for activity in activities:
        event_load = td_to_load(calculate_load_time(activity.zone_info))
        description = activity.description
        if not description and activity.distance > 0:
            description = f"{activity.distance:.2f} miles"
        events.append({
            "name": activity.name,
            "description": description,
            "load": event_load,
        })
    
    # Prepare the response
    response_data = {
        "date": today,
        "daily_load": today_load,
        "weekly_load": calculate_load(garmin, 7, 0),
        "weekly_strength_load": calculate_strength_load(garmin, 7, 0),
        "events": events,
        "resting_heart_rate": garmin.get_rhr_day(today)['allMetrics']['metricsMap']['WELLNESS_RESTING_HEART_RATE'][0]['value'],
        "sleep_score": garmin.get_sleep_data(today)['dailySleepDTO']['sleepScores']['overall']['value'],
    }
    
    return Response(json.dumps(response_data), mimetype="application/json")


if __name__ == "__main__":
    # print(build_stats())
    # download_files()
    print(calculate_strength_load(authenticate(), 7, 0))
    pass  
