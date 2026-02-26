from enum import Enum

class ResponseFormat(Enum):
    JSON = {"type": "json_object"}
    TEXT = {"type": "text"}
