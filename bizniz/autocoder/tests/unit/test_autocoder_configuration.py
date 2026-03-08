from pathlib import Path
import os
import shutil
import pytest

from autocoder.autocoder import AutocoderProcessError, AutocoderBadAIResponseError, AutocoderProcessResult, Autocoder, AutocoderConfig, AutocoderEnvironment

from autocoder.clients.chatgpt.openai_chatgpt_client import ChatGPTClient

from typing import Optional, Callable, Any, Dict, List
from pydantic import ValidationError



def test_autocoder_initialization_configs(azure_config, validator_factory, input_data, process_prompt):
    '''
    The code_directory/module_name is where the autocoder will place the generated code. The 
    code_directory is mandatory and must be provided. The module_name is optional and defaults to 
    "code". However, the module_name cannot be set to None. The code file defaults to "generated_code.py". 
    The directory structure for the generated code will be {code_directory}/{module_name}/{filename}. 
    This test checks that:
    
    -- Error is raised if code_directory is not provided in the config
    -- The default module_name is set to "code"
    -- The default filename is set to "generated_code.py"
    -- The default code_directory is set to /tmp/autocoder/autocoder_config/{module_name
    -- The code directory is created if it does not exist
    -- Error is raised if the code_directory is not passed
    -- Error is raised if the module_name is set to None
    
    The configuration_directory specifies where the autocoder will store its configuration files. By 
    default, it should be set to /tmp/autocoder/autocoder_config. This test checks that the default 
    configuration directory is set correctly when no custom config is provided. The configuration
    directory should be created if it does not exist. It cannot be set to None.
    This test checks that:
    -- The default configuration_directory is set to /tmp/autocoder/autocoder_config
    -- The configuration directory is created if it does not exist
    -- Error is raised if the configuration_directory is set to None
    
    
    '''
    
    # TEST RAISES ERROR IF CODE_DIRECTORY IS NOT PROVIDED IN THE CONFIG ////////////////////
    Validator = validator_factory(True)
    # Check that error is raised if code_directory is not provided in the config
    with pytest.raises(ValidationError) as exc_info:
        autocoder = Autocoder(
            input_data=input_data,
            process_prompt=process_prompt,
            max_retries=2,
            client=ChatGPTClient(config=azure_config, api_key="test_api_key"),
            validator=Validator(),
            config=AutocoderConfig(),
        )
    
    assert "code_directory" in str(exc_info.value)
    # TEST DEFAULT CONFIG VALUES AND CODE DIRECTORY CREATION /////////////////////////////
    
    # TEST DEFAULT CONFIG VALUES AND CODE DIRECTORY CREATION /////////////////////////////
    autocoder = Autocoder(
        input_data=input_data,
        process_prompt=process_prompt,
        max_retries=2,
        client=ChatGPTClient(config=azure_config, api_key="test_api_key"),
        validator=Validator(),
        config=AutocoderConfig(
            code_directory="/tmp/autocoder/code_generator"
        ),
    )
    # The autocoder code directory should be set the to clients default 
    assert autocoder._config.module_name == "code"
    assert autocoder._config.code_directory == f"/tmp/autocoder/code_generator"
    
    # Check that the code directory was created
    assert os.path.exists(autocoder._config.code_directory)
    # Check that the code + module directory was created
    assert os.path.exists(os.path.join(autocoder._config.code_directory, autocoder._config.module_name))
    
    # Check that the filename is set to the default
    assert autocoder._config.filename == "generated_code.py"
    # Check that the code file exists
    assert os.path.exists(os.path.join(autocoder._config.code_directory, autocoder._config.module_name, autocoder._config.filename))
    
    # Check the default configuration directory is set correctly and created
    assert autocoder._config.configuration_directory == "/tmp/autocoder/autocoder_config"
    assert os.path.exists(autocoder._config.configuration_directory)
    
    # TEST ERROR IS RAISED IF MODULE_NAME IS SET TO NONE //////////////////////////////////
    with pytest.raises(AutocoderProcessError) as exc_info:
        autocoder = Autocoder(
            input_data=input_data,
            process_prompt=process_prompt,
            max_retries=2,
            client=ChatGPTClient(config=azure_config, api_key="test_api_key"),
            validator=Validator(),
            config=AutocoderConfig(
                code_directory="/tmp/autocoder_tmp",
                module_name=None
            ),
        )
    assert "module_name" in str(exc_info.value)
    
    
    # TEST CUSTOM CONFIGURATION_DIRECTORY IS SET AND CREATED IF NOT EXISTS //////////////////
    autocoder = Autocoder(
        input_data=input_data,
        process_prompt=process_prompt,
        max_retries=2,
        client=ChatGPTClient(config=azure_config, api_key="test_api_key"),
        validator=Validator(),
        config=AutocoderConfig(
            code_directory="/tmp/autocoder_tmp"
        ),
    )
    assert autocoder._config.configuration_directory == "/tmp/autocoder/autocoder_config"
    assert os.path.exists(autocoder._config.configuration_directory)
    
    # CLEANUP CREATED DIRECTORIES AND FILES ///////////////////////////////////////////////////////
    try:
        shutil.rmtree("/tmp/autocoder")
    except Exception as e:
        print(f"Error cleaning up code directory: {e}")
    try:
        shutil.rmtree("/tmp/autocoder/autocoder_config")
    except Exception as e:
        print(f"Error cleaning up configuration directory: {e}")
    try:        
        os.remove(os.path.join("/tmp/autocoder_tmp", "generated_code.py"))
    except Exception as e:
        print(f"Error cleaning up generated code file: {e}")
    
    



def test_autocoder_initialization_configs(azure_config, validator_factory, input_data, process_prompt):
    '''
        
    '''
    
    # TEST RAISES ERROR IF CODE_DIRECTORY IS NOT PROVIDED IN THE CONFIG ////////////////////
    Validator = validator_factory(True)
    # Check that error is raised if code_directory is not provided in the config
    with pytest.raises(ValidationError) as exc_info:
        autocoder = Autocoder(
            input_data=input_data,
            process_prompt=process_prompt,
            max_retries=2,
            client=ChatGPTClient(config=azure_config, api_key="test_api_key"),
            validator=Validator(),
            config=AutocoderConfig(),
        )
    
    assert "code_directory" in str(exc_info.value)
    # TEST DEFAULT CONFIG VALUES AND CODE DIRECTORY CREATION /////////////////////////////
    
    # TEST DEFAULT CONFIG VALUES AND CODE DIRECTORY CREATION /////////////////////////////
    autocoder = Autocoder(
        input_data=input_data,
        process_prompt=process_prompt,
        max_retries=2,
        client=ChatGPTClient(config=azure_config, api_key="test_api_key"),
        validator=Validator(),
        config=AutocoderConfig(
            code_directory="/tmp/autocoder/code_generator",
            
        ),
    )
    # The autocoder code directory should be set the to clients default 
    assert autocoder._config.module_name == "code"
    assert autocoder._config.code_directory == f"/tmp/autocoder/code_generator"
    
    # Check that the code directory was created
    assert os.path.exists(autocoder._config.code_directory)
    # Check that the code + module directory was created
    assert os.path.exists(os.path.join(autocoder._config.code_directory, autocoder._config.module_name))
    
    # Check that the filename is set to the default
    assert autocoder._config.filename == "generated_code.py"
    # Check that the code file exists
    assert os.path.exists(os.path.join(autocoder._config.code_directory, autocoder._config.module_name, autocoder._config.filename))
    
    # Check the default configuration directory is set correctly and created
    assert autocoder._config.configuration_directory == "/tmp/autocoder/autocoder_config"
    assert os.path.exists(autocoder._config.configuration_directory)
    
    # TEST ERROR IS RAISED IF MODULE_NAME IS SET TO NONE //////////////////////////////////
    with pytest.raises(AutocoderProcessError) as exc_info:
        autocoder = Autocoder(
            input_data=input_data,
            process_prompt=process_prompt,
            max_retries=2,
            client=ChatGPTClient(config=azure_config, api_key="test_api_key"),
            validator=Validator(),
            config=AutocoderConfig(
                code_directory="/tmp/autocoder_tmp",
                module_name=None
            ),
        )
    assert "module_name" in str(exc_info.value)
    
    
    # TEST CUSTOM CONFIGURATION_DIRECTORY IS SET AND CREATED IF NOT EXISTS //////////////////
    autocoder = Autocoder(
        input_data=input_data,
        process_prompt=process_prompt,
        max_retries=2,
        client=ChatGPTClient(config=azure_config, api_key="test_api_key"),
        validator=Validator(),
        config=AutocoderConfig(
            code_directory="/tmp/autocoder_tmp"
        ),
    )
    assert autocoder._config.configuration_directory == "/tmp/autocoder/autocoder_config"
    assert os.path.exists(autocoder._config.configuration_directory)
    
    # CLEANUP CREATED DIRECTORIES AND FILES ///////////////////////////////////////////////////////
    try:
        shutil.rmtree("/tmp/autocoder")
    except Exception as e:
        print(f"Error cleaning up code directory: {e}")
    try:
        shutil.rmtree("/tmp/autocoder/autocoder_config")
    except Exception as e:
        print(f"Error cleaning up configuration directory: {e}")
    try:        
        os.remove(os.path.join("/tmp/autocoder_tmp", "generated_code.py"))
    except Exception as e:
        print(f"Error cleaning up generated code file: {e}")
    
    

def test_autocoder_callbacks_passed_in_config(azure_config, validator_factory, input_data, process_prompt):
    '''
    This test checks that the callbacks passed in the config are set correctly on the autocoder instance. The callbacks should be set to the values passed in the 
    config. If no callbacks are passed in the config, the callbacks should be set to None.
    '''
    Validator = validator_factory(True)
    # Define dummy callbacks
    def dummy_on_status_message_callback(*args, **kwargs):
        pass
    def dummy_on_save_history_callback(*args, **kwargs):
        pass
    
    # Test that callbacks passed in the config are set correctly on the autocoder instance
    autocoder = Autocoder(
        input_data=input_data,
        process_prompt=process_prompt,
        max_retries=2,
        client=ChatGPTClient(config=azure_config, api_key="test_api_key"),
        validator=Validator(),
        config=AutocoderConfig(
            code_directory="/tmp/autocoder_tmp",
        ),
        on_status_message=dummy_on_status_message_callback,
        on_save_history=dummy_on_save_history_callback,
    )
    assert autocoder._on_status_message == dummy_on_status_message_callback
    assert autocoder._on_save_history == dummy_on_save_history_callback
    
    # Test that if no callbacks are passed in the config, the callbacks are set to None
    autocoder = Autocoder(
        input_data=input_data,
        process_prompt=process_prompt,
        max_retries=2,
        client=ChatGPTClient(config=azure_config, api_key="test_api_key"),
        validator=Validator(),
        config=AutocoderConfig(
            code_directory="/tmp/autocoder_tmp",
        ),
    )
    assert autocoder._on_status_message is None
    assert autocoder._on_save_history is None
    # CLEANUP CREATED DIRECTORIES AND FILES ///////////////////////////////////////////////////////
    try:
        shutil.rmtree("/tmp/autocoder_tmp")
    except Exception as e:
        print(f"Error cleaning up code directory: {e}")
    try:
        shutil.rmtree("/tmp/autocoder/autocoder_config")
    except Exception as e:
        print(f"Error cleaning up configuration directory: {e}")
    try:        
        os.remove(os.path.join("/tmp/autocoder_tmp", "generated_code.py"))
    except Exception as e:
        print(f"Error cleaning up generated code file: {e}")
    
    