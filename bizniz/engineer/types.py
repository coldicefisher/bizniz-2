from typing import Optional, List, Literal
from pydantic import BaseModel


class EngineeringRequirement(BaseModel):
    db_id: Optional[int] = None
    type: Literal["business", "functional", "nonfunctional"]
    text: str


class EngineeringUseCase(BaseModel):
    db_id: Optional[int] = None
    title: str
    description: str


class EngineeringIssue(BaseModel):
    db_id: Optional[int] = None
    title: str
    description: str
    code_file: str
    test_file: str


class EngineeringAnalysis(BaseModel):
    problem_id: int
    requirements: List[EngineeringRequirement] = []
    use_cases: List[EngineeringUseCase] = []
    issues: List[EngineeringIssue] = []


class AutoEngineerError(Exception):
    pass


class AutoEngineerBadAIResponseError(AutoEngineerError):
    pass
