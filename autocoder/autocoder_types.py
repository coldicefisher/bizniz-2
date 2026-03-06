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


class AutocoderEnvironment(BaseModel):
    exposed_globals: dict = Field(default_factory=dict)
    exposed_builtins: dict = Field(default_factory=dict)
    allowed_modules: dict = Field(default_factory=dict)


    
        

class AutocoderConfig(BaseModel):
    code_directory: str
    filename: str = "generated_code.py"
    module_name: Optional[str] = "code"
    
    configuration_directory: Optional[str] = "/tmp/autocoder/autocoder_config"
    environment_settings: Optional[AutocoderEnvironment] = None
    build_on_current_code: bool = True
    

class AutocoderFailedError(BaseModel):
    error: str
    code: str
    failed_at: Literal["evaluation", "validation", "ai_verification"]
    recommended_code_changes: Optional[str] = None
    def __str__(self):
        s = ""
        
        match self.failed_at:
            case "evaluation":
                s += f"Code evaluation failed with error: {self.error}"
                
            case "validation":
                s += f"Code validation failed with error: {self.error}"
                
            case "ai_verification":
                s += f"AI verification of results and code failed with error: {self.error}"
        
        s += f"\n\nCode:\n\n{self.code}"
        if self.recommended_code_changes is not None:
            s += f"\n\nRecommended code changes {'from AI' if self.failed_at == 'ai_verification' else ''}:\n\n{self.recommended_code_changes}"
            
        return s


class AutocoderEnvironmentErrorDetails(BaseModel):
    stage: Optional[str] = None
    type: str
    message: str
    line: Optional[int] = None
    code_line: Optional[str] = None
    traceback: Optional[str] = None
    stdout: Optional[str] = None
    stderr: Optional[str] = None   
    


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
    # event: Literal[
    #     'generate_start', 'generate_success', 'generate_failure',
    #     'evaluation_start', 'evaluation_success', 'evaluation_failure',
    #     'validation_start', 'validation_success', 'validation_failure',
    #     'ai_verification_start', 'ai_verification_success', 'ai_verification_failure',
    #     'repair_start', 'repair_success', 'repair_failure',
    #     'success' 
    # ]
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
    

