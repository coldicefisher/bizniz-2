import textwrap
import base64
import json
import yaml
import os
import datetime
from enum import Enum
import requests

from openai import AzureOpenAI, BadRequestError


from pydantic import BaseModel, Field

from typing import Optional


class AltTextValidationError(Exception):
    """
    Custom exception for validation errors.
    """
    def __init__(self, message: str, model_name: str):
        super().__init__(message, model_name)
        self.model_name = model_name
        self.message = message
        
        
class AltTextGenerationError(Exception):
    """
    Custom exception for alt text generation errors.
    """
    def __init__(self, message: str, model_name: str):
        super().__init__(message, model_name)
        self.model_name = model_name
        self.message = message
        
        
class ValidateAltTextResponse(BaseModel):
    """
    Model for validating alt text.
    """
    is_valid: bool = Field(..., description="Indicates if the alt text is valid.")
    validation_result: str = Field(..., description="Explanation if invalid or alt text if valid.")
    is_artifact: bool = Field(..., description="Indicates if the image is an artifact.")


class GenerateAltTextResponse(BaseModel):
    """
    Model for generating alt text.
    """
    alt_text: str = Field(..., description="Generated alt text for the image.")
    is_artifact: bool = Field(..., description="Indicates if the image is an artifact.")
    

class AltTextGeneratorResponse(BaseModel):
    """
    Model for the response from the AltTextGenerator.
    """
    alt_text: Optional[str] = Field(None, description="OPTIONAL: Generated alt text for the image. If there is a problem with processing the image, then alt text will be null or a blank string and the validation_result will describe the problem.")
    is_valid: bool = Field(..., description="Indicates if the generated alt text is valid.")
    validation_result: Optional[str] = Field(None, description="OPTIONAL: Validation result or explanation if invalid or the AI model errored out.")
    is_artifact: bool = Field(..., description="Indicates if the image is an artifact.")
    model_name: str = Field(..., description="Name of the model used for generating alt text.")


class AltTextReturnMode(Enum):
    """
    Enum for the return mode of the alt text generation.
    """
    TEXT = "text"
    JSON = "json"
    MODEL = "model"
    
    
class AltTextGenerator:
    
    
    def __init__(self, model_name: str=None, api_version: str=None):
        
        self._config = None
        with open("/etc/jhup/azure_openai.yaml", "r") as f:
            self._config = yaml.safe_load(f)
            
            
        self._api_key = self._config.get("api_key", None)
        self._api_base = self._config.get("api_base", None)
        self._available_models = self._config.get("available_models", [])
        self._default_model = model_name or self._config.get("default_model", None)
        self._api_version = api_version or self._config.get("api_version", "2024-10-21")
        
        if not self._api_key or not self._api_base:
            raise ValueError("API key and base URL must be set in azure_openai.yaml")
        
        self._client = AzureOpenAI(
            api_key=self._api_key,
            azure_endpoint=self._api_base,
            api_version=self._api_version,
        )
        
        
    @property
    def available_models(self):
        return self._available_models

    
    @property
    def default_model(self):
        return self._default_model


    @property
    def api_key(self):
        return self._api_key


    @property
    def api_base(self):
        return self._api_base
    
    @property
    def api_version(self):
        return self._api_version
    
    
    @property
    def client(self):
        return self._client
    
    @property
    def config(self):
        return self._config
    
    
    def _generate_alt_text(self, image_path: str, model_name: str='gpt-4o', return_json: bool=True, additional_prompt: str=None):
        """
        Generate alt text for the given image using the specified model.
        :param image_path: Path to the image file.
        :param model_name: Name of the model to use for generating alt text.
        :param return_json: If True, returns a dictionary with alt text and validation results; otherwise, returns just the alt text.
        :param additional_prompt: Additional prompt to include in the alt text generation.
        :return: Generated alt text.
        """
        
        
        if model_name is None:
            raise ValueError("Model name must be specified. Typically 'gpt-4o' or 'gpt-4o-mini'.")
        
        
        else:
            if model_name not in self.available_models:
                raise ValueError(f"Model {model_name} is not available. Choose from {self.available_models}.")    
        
        
        prompt = self.config.get("alt_text_prompt", "")
        if additional_prompt is not None:
            prompt += f" {additional_prompt}"
            
        if not prompt:
            raise ValueError("Alt text prompt must be defined in the configuration file.")
        
        
        prompt = ' '.join(textwrap.dedent(prompt).split()) # Make it a single line

        with open(image_path, "rb") as image_file:
            image_data = base64.b64encode(image_file.read()).decode('utf-8')
        
        image_data_url = f"data:image/jpeg;base64,{image_data}"
        
        try:
            response = self.client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": "You are an expert in generating alt text for images."},
                    {
                        "role": "user", 
                        "content": [
                            {
                                "type": "text",
                                "text": prompt,
                            },
                            {
                                "type": "image_url",
                                "image_url": {"url": image_data_url},
                            }
                        ]
                    }
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "GenerateAltTextResponse",
                        "schema": GenerateAltTextResponse.model_json_schema(),
                    }
                }   
            )
            response_json = response.choices[0].message.content.strip()
            response_json = json.loads(response_json)
            response_model = GenerateAltTextResponse(**response_json)
            
            
            if return_json:
                return response_model.model_dump()
            else:
                return response_model.alt_text
        
        except BadRequestError as e:
            # Handle bad request errors from the OpenAI API
            # Handle HTTP errors from the OpenAI API
            # REMEMBER: Don't handle ALL errors!
            
            raise AltTextGenerationError(
                message=f"Bad request error generating alt text for image {image_path}: {str(e)}",
                model_name=model_name
            ) from e
            
    
    
    def _validate_alt_text(self, alt_text: str, image_path: str, is_artifact: bool=False, model_name: str='gpt-4o'):
        """
        Given an alt text string and an image path, calls ChatGPT to ensure that the alt text is appropriate.
        :param alt_text: The alt text to validate.
        :param image_path: Path to the image file.
        :param model_name: Name of the model to use for validation.
        """
        
        with open(image_path, "rb") as image_file:
            image_data = base64.b64encode(image_file.read()).decode('utf-8')
            
        image_data_url = f"data:image/jpeg;base64,{image_data}"
        prompt = f"""
        Please validate the following alt text for the image provided. The alt text should be descriptive,
        unbiased, and not contain any emotional language. If the alt text is appropriate, return true for 
        is_valid and the alt_text for validation_result.
        
        You are given the alt_text, the image, and whether the image was marked as an artifact.
        If the image is not an artifact but marked as one, return false for is_artifact, false for is_valid, and provide a brief 
        explanation of why it is not an artifact for validation_result.
        
        If the alt_text is not appropriate for images that are not marked as is_artifact, return false for invalid and provide a 
        brief explanation of why the alt_text is invalid for validation_result.
        
        For images that are marked as is_artifact, the alt_text should not matter because it is an artifact. The provided alt_text
        should be ignored in this case, and the validation_result should be "Image is an artifact, alt text is not applicable." if
        the image is validated as an artifact. Otherwise, the validation_result shoudl be the brief explanation of why the image is 
        not an artifact and marked as is_invalid.
        
        Artifacts are defined as: A visual element that is purely decorative or structural and does not convey meaningful content, 
        such as borders, logos used for branding, or background patterns.
        
        Alt text: {alt_text}.
        is_artifact: {is_artifact}
        """
        prompt = ' '.join(textwrap.dedent(prompt).split()) # Make it a single line
        
        response = self.client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": "You are an expert in validating alt text for images."},
                {
                    "role": "user", 
                    "content": [
                        {
                            "type": "text",
                            "text": prompt,
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": image_data_url},
                        }
                    ]
                }
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "ValidateAltTextResponse",
                    "schema": ValidateAltTextResponse.model_json_schema(),
                }
            }
        )
        response_json = response.choices[0].message.content.strip()
        response_json = json.loads(response_json)
        
        
        response_model = ValidateAltTextResponse(**response_json)
        
        
        return response_model.model_dump()
        
        
        
    def get_alt_text(self, image_path: str, model_name: str='gpt-4o', return_mode: AltTextReturnMode=AltTextReturnMode.JSON, additional_prompt: str=None):
        """
        Generate alt text for the given image using the specified model.
        
        :param image_path: Path to the image file.
        :param model_name: Name of the model to use for generating alt text.
        :param return_mode: AltTextReturnMode, JSON=returns a dictionary with alt text and validation results;TEXT=returns just the alt text; MODEL=returns the model response.
        :param additional_prompt: Additional prompt to include in the alt text generation.
        :return: Generated alt text.
        """
        if model_name is None:
            raise ValueError("Model name must be specified. Typically 'gpt-4o' or 'gpt-4o-mini'.")
        
        
        alt_text_response: dict = self._generate_alt_text(image_path=image_path, model_name=model_name, return_json=True, additional_prompt=additional_prompt)
        validation_result_response: dict = self._validate_alt_text(
                                                        alt_text=alt_text_response.get('alt_text') or '', 
                                                        is_artifact=alt_text_response.get('is_artifact'),
                                                        image_path=image_path, 
                                                        model_name=model_name,
                                                    )
        
        
        self.log_alt_text(
            image_path=image_path, 
            alt_text=alt_text_response.get('alt_text'), 
            is_valid=validation_result_response.get('is_valid'), 
            is_artifact=alt_text_response.get('is_artifact'), 
            model_name=model_name, 
            validation_result=validation_result_response.get('validation_result', None)
        )
        
        if return_mode == AltTextReturnMode.TEXT:
            if validation_result_response.get('is_valid', False):
                return alt_text_response.get('alt_text')
            else:
                raise AltTextValidationError(message=f"Generated alt text is invalid: {validation_result_response.get('validation_result')}", model_name=model_name)
            
        
        return_data = {**alt_text_response, **validation_result_response}
        if return_data.get('alt_text') == return_data.get('validation_result'):
            return_data['validation_result'] = None
        if return_mode == AltTextReturnMode.JSON:
            return return_data
        elif return_mode == AltTextReturnMode.MODEL:
            return AltTextGeneratorResponse(**return_data, model_name=model_name)
        else:
            raise ValueError(f"Invalid return mode: {return_mode}. Use AltTextReturnMode.TEXT, AltTextReturnMode.JSON, or AltTextReturnMode.MODEL.")
                
            
            
            
    def log_alt_text(self, image_path: str, alt_text: str, is_valid: bool, model_name: str, is_artifact: bool=False, validation_result: str=None):
        """
        Log the generated alt text to a file or DB.
        
        :param image_path: Path to the image file.
        :param alt_text: Generated alt text.
        :param model_name: Name of the model used for generating alt text.
        """
        
        log_base_path = self._config['log_dir']
        today = datetime.datetime.now().strftime("%Y%m%d")
        log_file_path = os.path.join(log_base_path, f"ai_generated_alt_text.{today}.json")
        if not os.path.exists(log_base_path):
            os.makedirs(log_base_path)
            
        
        if not is_valid and not validation_result:
            validation_result = "No validation result provided."
            
        log_entry = {
            "image_path": image_path,
            "alt_text": alt_text,
            "is_valid": is_valid,
            "is_artifact": is_artifact,
            "validation_result": "Valid" if is_valid else validation_result,
            "model_name": model_name,
            "api_version": self.api_version,
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        
        with open(log_file_path, "a") as log_file:
            log_file.write(json.dumps(log_entry) + "\n")
            
            
    
    def process_folder_of_images(self, folder_path: str, model_name: str='gpt-4o', validate_image_extensions: bool=False):
        """
        Process all images in a folder and generate alt text for each.
        
        :param folder_path: Path to the folder containing images.
        :param model_name: Name of the model to use for generating alt text.
        """
        if not os.path.exists(folder_path):
            raise ValueError(f"Folder {folder_path} does not exist.")
        
        image_files = []
        if validate_image_extensions:
            image_files = [f for f in os.listdir(folder_path) if f.lower().endswith(('.png', '.jpg', '.jpeg', '.gif'))]
        else:
            image_files = os.listdir(folder_path)
        
        if not image_files:
            return []
        
        results = []
        for image_file in image_files:
            image_path = os.path.join(folder_path, image_file)
            try:
                alt_text = self.get_alt_text(image_path=image_path, model_name=model_name)
                results.append({"image": image_file, "alt_text": alt_text})
            except AltTextValidationError as e:
                results.append({"image": image_file, "error": str(e)})
            except Exception as e:
                results.append({"image": image_file, "error": f"Error processing image: {str(e)}"})
        
        
        return results