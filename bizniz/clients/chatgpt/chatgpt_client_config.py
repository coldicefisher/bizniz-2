from typing import Optional
from pydantic import BaseModel
from typing import Dict


class ChatGPTClientConfig(BaseModel):

    is_azure: Optional[bool] = False

    api_base: Optional[str] = None
    api_version: Optional[str] = None

    available_models: Optional[Dict[str, str]] = None
    default_model: Optional[str] = None

    config_filepath: Optional[str] = None


