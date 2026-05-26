from MeetingScheduler import MeetingScheduler
from MeetingJoiner import MeetingJoiner
from CallSpawner import CallSpawner
import os
from dotenv import load_dotenv
import time
import signal

def runner():
    load_dotenv()

    # Meeting Scheduler Credentials
    meet_sch_user_id = os.environ["ZOOM_S2S_ACCOUNT_ID"]
    meet_sch_client_id = os.environ["ZOOM_S2S_CLIENT_ID"]
    meet_sch_client_secret = os.environ["ZOOM_S2S_CLIENT_SECRET"]


    call_spawner = CallSpawner(2, 1, True)


if __name__ == "__main__":
    runner()