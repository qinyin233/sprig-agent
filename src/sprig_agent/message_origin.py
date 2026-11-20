"""Application observations are data, distinct from literal human requests."""
from .input_inbox import INPUT_ID
from .requirements import REQUIREMENT_KEY

ORIGIN = "sprig_origin"


def application_origin(message):
    if ORIGIN not in message:
        return False
    if (message[ORIGIN] != "application" or message.get("role") != "user"
            or not isinstance(message.get("content"), str)
            or any(key in message for key in (INPUT_ID, REQUIREMENT_KEY, "tool_calls", "tool_call_id", "native_state", "sprig_check"))):
        raise ValueError("invalid application message origin")
    return True


def application_message(content):
    return {"role": "user", "content": content, ORIGIN: "application"}


def user_request(message):
    return not application_origin(message) and REQUIREMENT_KEY not in message and message.get("role") == "user"
