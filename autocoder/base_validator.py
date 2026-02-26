from enum import Enum
import re
from abc import ABC, abstractmethod

from lxml import etree

from xml_ai_parser.utils.xml_normalizer import normalize_xml_input


class ValidationResult:
    
    def __init__(self, is_valid=False, errors=None, warnings=None):
        self.is_valid = True if is_valid else False
        self.errors = errors if errors is not None else []
        self.warnings = warnings if warnings is not None else []
        
            
        
    def __bool__(self):
        return self.is_valid
        
    def __str__(self):
        status = "VALID" if self.is_valid else "INVALID"
        return f"ValidationResult: {status}, Errors: {len(self.errors)}, Warnings: {len(self.warnings)}"
    

class BaseValidator(ABC):
    """Abstract base class for Autocoder validators."""
    
        
    @abstractmethod
    def validate(self, original_data: str, *args, **kwargs) -> ValidationResult:
        """Validate the data results.
        
        Args:
            original_data (str): The original data string.
        
        Returns:
            ValidationResult: The result of the validation.
        """
        pass
    
        
    @property
    def original_data(self) -> str:
        if hasattr(self, '_original_data'):
            return self._original_data
        else:
            return ""
    
    @property
    def mutated_data(self) -> str:
        if hasattr(self, '_mutated_data'):
            return self._mutated_data
        else:
            return ""
    
    
    
    def levenshtein(a, b):
        """Compute Levenshtein distance using a fast DP implementation. Useful for comparing string inputs to outputs."""
        if a == b:
            return 0
        if abs(len(a) - len(b)) > 5000:
            # Prevent huge memory use if something goes wrong
            return float('inf')
        
        prev = list(range(len(b) + 1))
        for i, ca in enumerate(a, start=1):
            curr = [i]
            for j, cb in enumerate(b, start=1):
                insert = curr[j-1] + 1
                delete = prev[j] + 1
                replace = prev[j-1] + (ca != cb)
                curr.append(min(insert, delete, replace))
            prev = curr
        return prev[-1]
