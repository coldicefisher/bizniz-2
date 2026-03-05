from autocoder.base_validator import BaseValidator, ValidationResult


class MockValidator(BaseValidator):
    def __init__(self):
        # self.should_pass = should_pass
        pass

    def validate(self, input_data: str, output_data: str = "", *args, **kwargs) -> ValidationResult:
        if isinstance(output_data, int):
            return ValidationResult(
                is_valid=True,
                errors=[],
                warnings=[]
            )
        else:
            return ValidationResult(
                is_valid=False,
                errors=["Output data is not an integer."],
                warnings=[]
            )