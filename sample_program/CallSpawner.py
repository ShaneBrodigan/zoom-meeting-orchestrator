from datetime import datetime, timedelta
from multiprocessing import Process

from MeetingScheduler import MeetingScheduler
import os
from dotenv import load_dotenv
from MeetingJoiner import MeetingJoiner
import threading
import time
import signal

class CallSpawner(object):
    def __init__(self, num_of_bots: int, meeting_dur_in_mins: int, has_screenshare: bool):
        self.meeting_duration = meeting_dur_in_mins
        self.has_screenshare = has_screenshare
        meeting_id, meeting_pwd, zak_token = self.start_meeting()

        self.set_meeting_end_timer(self.meeting_duration)
        self.processes = []     # Stores threads used for bots joining meeting
        self.thread_bots_to_join(num_of_bots, meeting_id, meeting_pwd, zak_token)


    #Starts the call
    def start_meeting(self):
        load_dotenv()
        meet_sch_user_id = os.environ["ZOOM_S2S_ACCOUNT_ID"]
        meet_sch_client_id = os.environ["ZOOM_S2S_CLIENT_ID"]
        meet_sch_client_secret = os.environ["ZOOM_S2S_CLIENT_SECRET"]

        self.meeting_scheduler = MeetingScheduler(meet_sch_user_id, meet_sch_client_id, meet_sch_client_secret, self.meeting_duration)
        self.meeting_scheduler.establish_connection()
        meeting_id, meeting_pwd, zak_token = self.meeting_scheduler.create_meeting()
        return meeting_id, meeting_pwd, zak_token

    # Multithreads the bots to join the meeting as bot.run() method blocks
    def thread_bots_to_join(self, num_of_bots, meeting_id, meeting_pwd, zak_token):

        for i in range(num_of_bots):
            p = Process(target=self.req_bot_to_join, args=(meeting_id, meeting_pwd, zak_token))
            p.start()
            self.processes.append(p)

        for p in self.processes:
            p.join()


    # Gets bot to join as host
    def req_bot_to_join(self, meeting_id, meeting_pwd, zak_token):
        bot = MeetingJoiner(meeting_id, meeting_pwd, zak_token)
        bot.run()

    def set_meeting_end_timer(self, meeting_duration_in_mins):
        duration_in_seconds = meeting_duration_in_mins * 60
        self.timer = threading.Timer(duration_in_seconds, self.stop_meeting)
        self.timer.start()

    def stop_meeting(self):
        print("stop_meeting called")
        meeting_id = self.meeting_scheduler.end_meeting()
        print(f"Meeting id: {meeting_id} ended at {datetime.now()}")