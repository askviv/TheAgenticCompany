
from typing import List

from scoring import Result, Checkpoint, bonus_for_completing_final
from common import create_rocketchat_client, grader


# Create RocketChat instance
rocket = create_rocketchat_client()

@grader
def checkpoint1(trajectory):
    return check_channel_exists("sales-talk")

@grader
def check_channel_exists(channel_name):
    channels = rocket.channels_list().json()
    channel_names = channels.get("channels", [])
    return any(current_channel['name'] == channel_name and current_channel.get('t') == 'c' for current_channel in channel_names)

@grader
def check_invitation(channel_name, username):
    info = rocket.channels_info(channel=channel_name).json()
    if not info.get('success'):
        return False
    members = rocket.channels_members(room_id=info['channel']['_id']).json()
    return members.get('success', False) and any(
        member['username'] == username for member in members.get('members', []))

@grader
def check_user_role(username, role):
    roles = rocket.roles_get_users_in_role(role).json()
    users_list = roles.get("users", [])
    return any(user['username'] == username for user in users_list)


def grade_checkpoints(trajectory=""):
    checkpoints: List[Checkpoint] = []
    result = Result(checkpoints, bonus_for_completing_final)

    checkpoints.append(Checkpoint(1, int(checkpoint1(trajectory))))

    checkpoints.append(Checkpoint(1, int(check_invitation("sales-talk", "mark_johnson"))))

    checkpoints.append(Checkpoint(1, int(check_user_role("mark_johnson", "leader"))))

    return result


