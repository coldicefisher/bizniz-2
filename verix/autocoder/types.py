# autocoder/types.py


import datetime

from typing import Optional, Callable, Union, Any, Dict, List, Tuple, Literal

from pydantic import BaseModel, Field


class AutocoderProcessError(Exception):
    pass


class AutocoderBadAIResponseError(AutocoderProcessError):
    pass


class AutocoderProcessResult(BaseModel):
    code: Optional[str] = None
    output: Optional[Any] = None


class AutocoderAIVerificationResult(BaseModel):
    is_valid: bool
    code: Optional[str] = None
    errors: Optional[List[str]] = None  

        
    

class AutocoderFailedError(BaseModel):
    error: str
    code: str
    failed_at: Literal["evaluation", "validation", "ai_verification"]
    recommended_code_changes: Optional[str] = None
    
    
    @classmethod
    def from_dict(cls, d: dict, stage: Optional[str] = None):
        _stage = d.get("stage", stage)
        
            
        return cls(
            stage=_stage,
            type=d.get("type", ""),
            message=d.get("message", ""),
            line=d.get("line"),
            code_line=d.get("code_line"),
            traceback=d.get("traceback"),
            stdout=d.get("stdout"),
            stderr=d.get("stderr")
        )


    def __str__(self):
        
        s = f"Error Type: {self.type}\nMessage: {self.message}"
        if self.stage is not None:
            s = f"\nStage: {self.stage}\n" + s
        if self.line is not None:
            s += f"\nLine Number: {self.line}"
        if self.code_line is not None:
            s += f"\nCode Line: {self.code_line}"
        if self.traceback is not None:
            s += f"\nTraceback:\n{self.traceback}"
        if self.stdout is not None:
            s += f"\nStandard Output:\n{self.stdout}"
        if self.stderr is not None:
            s += f"\nStandard Error:\n{self.stderr}"
            
        return s
    
    
class AutocoderOnEventCallback(BaseModel):
    stage: Literal["generate", "evaluation", "validation", "ai_verification", "repair", "process"]
    status: Literal["start", "success", "failure"]
    attempt: Optional[int] = None
    code: Optional[str] = None
    error: Optional[str] = None
    prompt: Optional[str] = None
    response: Optional[str] = None
    input_data: Optional[str] = None
    timestamp: datetime.datetime = Field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc))
    

class AutocoderFailedErrorList(BaseModel):
    errors: List[AutocoderFailedError]
    
    def __str__(self):
        s = (
            "The following errors have occured during the autocoding process in one of the three stages (`code evaluation`, `code validation using custom function`, `AI verification`).\n"
            "Code evaluation is the first stage where the AI generated code is executed with a given input data. Validation is a custom function that tests the output of the code\n"
            "against expected results. AI verification is where the AI reviews the code and its output for correctness.\n\n"
        )
        
        for i, error in enumerate(self.errors):
            s += f"Attempt {i+1}:\n{str(error)}\n\n"
        return s
    
    def __iter__(self):
        return super().__iter__()
    
    def __len__(self):
        return super().__len__()
    
    def __getitem__(self, index):
        return super().__getitem__(index)
    
    def append(self, error: AutocoderFailedError):
        self.errors.append(error)
    

