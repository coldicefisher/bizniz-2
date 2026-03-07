# verix/environment/types.py

from pydantic import BaseModel, Field
from typing import Optional, Any, Dict, List





class ExecutionTrace(BaseModel):
    stage: str
    message: str
    timestamp: float
    metadata: Optional[Dict[str, Any]] = None



class ExecutionEnvironmentErrorDetails(BaseModel):
    stage: Optional[str] = None
    type: str
    message: str

    line: Optional[int] = None
    code_line: Optional[str] = None
    traceback: Optional[str] = None

    


class ExecutionEnvironmentResult(BaseModel):

    success: bool

    result: Optional[Any] = None

    error: Optional[ExecutionEnvironmentErrorDetails] = None

    execution_time: Optional[float] = None

    stdout: Optional[str] = None
    stderr: Optional[str] = None

    metadata: Optional[Dict[str, Any]] = None
    
    traces: Optional[List[ExecutionTrace]] = None





class ExecutionCallSpec(BaseModel):
    """
    Defines how generated code should be invoked.
    """

    symbol: str = Field(
        ...,
        description="Symbol path to execute (e.g., 'add', 'Calculator.add', 'Calculator().add')"
    )

    args: List[Any] = Field(default_factory=list)

    kwargs: Dict[str, Any] = Field(default_factory=dict)