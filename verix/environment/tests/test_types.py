# verix/environment/tests/test_types.py

from verix.environment.types import ExecutionEnvironmentErrorDetails, ExecutionEnvironmentResult


def test_execution_environment_result_success():

    result = ExecutionEnvironmentResult(
        success=True,
        result={"data": [1, 2, 3]},
        execution_time=0.5,
        stdout="Hello World\n",
        stderr=""
    )

    assert result.success is True
    assert result.result == {"data": [1, 2, 3]}
    assert result.execution_time == 0.5
    assert result.stdout == "Hello World\n"
    assert result.stderr == ""

    dict_representation = result.model_dump(exclude_none=True)

    assert dict_representation == {
        "success": True,
        "result": {"data": [1, 2, 3]},
        "execution_time": 0.5,
        "stdout": "Hello World\n",
        "stderr": "",
        
    }
    
    

def test_execution_environment_error_details():
    
    error_details = ExecutionEnvironmentErrorDetails(
        stage="compile",
        type="SyntaxError",
        message="invalid syntax",
        line=1,
        code_line="print('Hello World'",
        traceback="Traceback (most recent call last): ...",
        
    )

    assert error_details.stage == "compile"
    assert error_details.type == "SyntaxError"
    assert error_details.message == "invalid syntax"
    assert error_details.line == 1
    assert error_details.code_line == "print('Hello World'"
    assert error_details.traceback == "Traceback (most recent call last): ..."
    
    

    dict_representation = error_details.model_dump()

    assert dict_representation == {
        "stage": "compile",
        "type": "SyntaxError",
        "message": "invalid syntax",
        "line": 1,
        "code_line": "print('Hello World'",
        "traceback": "Traceback (most recent call last): ...",
        
    }