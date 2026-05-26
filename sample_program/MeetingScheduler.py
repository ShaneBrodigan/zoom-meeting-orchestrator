import os
import base64
import requests

class MeetingScheduler:

    def __init__(self, account_id, client_id, client_secret, duration_in_mins):
        self.account_id = account_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.duration_in_mins = duration_in_mins
        self.meeting_id = None
        self.meeting_pwd = None

    # Establishes Rest connection and saves access_token as instance variable
    def establish_connection(self):
        self.access_token = None

        credentials = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()

        token_response = requests.post(
            f"https://zoom.us/oauth/token?grant_type=account_credentials&account_id={self.account_id}",
            headers={"Authorization": f"Basic {credentials}"}
        )

        data_token = token_response.json()
        self.access_token = data_token["access_token"]
        if(self.access_token is not None):
            print("Connection Established")

    # Creates an instant meeting
    def create_meeting(self):
        meeting_response = requests.post(
            "https://api.zoom.us/v2/users/me/meetings",
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json"
            },
            json={
                "topic": "Bot Meeting",
                "type": 1, # 1 = instant meeting
            }
        )

        meeting = meeting_response.json()
        if meeting is not None :
            self.meeting_id = meeting["id"]
            self.meeting_pwd = meeting["password"]
            print(f"Meeting Created at: {self.meeting_id}")

        self.zak_token = self.get_zak_token()
        return self.meeting_id, self.meeting_pwd, self.zak_token

    # Give bot the host abilities for the call
    def get_zak_token(self):
        response = requests.get(
            "https://api.zoom.us/v2/users/me/token?type=zak",
            headers={"Authorization": f"Bearer {self.access_token}"}
        )
        print(response.json())
        return response.json()["token"]

    # Ends an instant meeting
    def end_meeting(self):
        response = requests.put(
            f"https://api.zoom.us/v2/meetings/{self.meeting_id}/status",
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json"
            },
            json={"action": "end"}
        )

        print(f"End meeting response: {response.status_code}")
        print(f"End meeting response body: {response.json()}")

        return self.meeting_id