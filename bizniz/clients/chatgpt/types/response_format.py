from enum import Enum

import json
from typing import Optional, Dict, Any, Union, List


class ResponseFormat(Enum):
    # JSON = {"type": "json_object"}
    # TEXT = {"type": "text"}
    
    # JSON_SCHEMA = {
    #     "type": "json_schema",
    #     "json_schema": {
    #         "type": "object",
    #         "properties": {
    #             "code": {
    #                 "type": "string"
    #             }
    #         },
    #         "required": ["code"]
    #     }
    # }
    JSON = "json_object"
    TEXT = "text"
    JSON_SCHEMA = "json_schema"
    
    
def parse_response_format(response_format: Optional[ResponseFormat] = None, schema: Optional[Dict[str, Any]]=None) -> Dict[str, Any]:
    # Default to JSON response format if none provided
    if response_format is None:
        return {"type": "json_object"}
    
    
    if response_format is not None and isinstance(response_format, (str, ResponseFormat)):
        match response_format:
            case ResponseFormat.JSON | "json_object":
                return {"type": "json_object"}
            case ResponseFormat.TEXT | "text":
                return {"type": "text"}
            case ResponseFormat.JSON_SCHEMA | "json_schema":
                if schema is None or not isinstance(schema, dict):
                    raise ValueError("Schema must be provided for JSON_SCHEMA response format and must be a dict.")
                return {
                    "type": "json_schema",
                    "name": "response",
                    "schema": schema
                }
    else:
        return {"type": "json_object"}