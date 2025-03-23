"""
This is some common utils functions.
it is not binding to the scenarios or framework (So it is not placed in rdagent.core.utils)
"""

# TODO: merge the common utils in `rdagent.core.utils` into this folder
# TODO: split the utils in this module into different modules in the future.

import hashlib
import importlib
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Union

from rdagent.oai.llm_conf import LLM_SETTINGS
from rdagent.utils.agent.tpl import T


def get_module_by_module_path(module_path: Union[str, ModuleType]) -> ModuleType:
    """Load module from path like a/b/c/d.py or a.b.c.d

    :param module_path:
    :return:
    :raises: ModuleNotFoundError
    """
    if module_path is None:
        raise ModuleNotFoundError("None is passed in as parameters as module_path")

    if isinstance(module_path, ModuleType):
        module = module_path
    else:
        if module_path.endswith(".py"):
            module_name = re.sub("^[^a-zA-Z_]+", "", re.sub("[^0-9a-zA-Z_]", "", module_path[:-3].replace("/", "_")))
            module_spec = importlib.util.spec_from_file_location(module_name, module_path)
            if module_spec is None:
                raise ModuleNotFoundError(f"Cannot find module at {module_path}")
            module = importlib.util.module_from_spec(module_spec)
            sys.modules[module_name] = module
            if module_spec.loader is not None:
                module_spec.loader.exec_module(module)
            else:
                raise ModuleNotFoundError(f"Cannot load module at {module_path}")
        else:
            module = importlib.import_module(module_path)
    return module


def convert2bool(value: Union[str, bool]) -> bool:
    """
    Motivation: the return value of LLM is not stable. Try to convert the value into bool
    """
    # TODO: if we have more similar functions, we can build a library to converting unstable LLM response to stable results.
    if isinstance(value, str):
        v = value.lower().strip()
        if v in ["true", "yes", "ok"]:
            return True
        if v in ["false", "no"]:
            return False
        raise ValueError(f"Can not convert {value} to bool")
    elif isinstance(value, bool):
        return value
    else:
        raise ValueError(f"Unknown value type {value} to bool")


def remove_ansi_codes(s: str) -> str:
    """
    It is for removing ansi ctrl characters in the string(e.g. colored text)
    """
    ansi_escape = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
    return ansi_escape.sub("", s)


def filter_redundant_text(stdout: str) -> str:
    """
    Filter out progress bars from stdout using regex.
    """
    from rdagent.oai.llm_utils import APIBackend  # avoid circular import

    # Initial progress bar regex pattern
    progress_bar_re = (
        r"(\d+/\d+\s+[━]+\s+\d+s?\s+\d+ms/step.*?\u0008+|"
        r"\d+/\d+\s+[━]+\s+\d+s?\s+\d+ms/step|"
        r"\d+/\d+\s+[━]+\s+\d+s?\s+\d+ms/step.*|"
        r"\d+/\d+\s+[━]+.*?\u0008+|"
        r"\d+/\d+\s+[━]+.*|[ ]*\u0008+|"
        r"\d+%\|[█▏▎▍▌▋▊▉]+\s+\|\s+\d+/\d+\s+\[\d{2}:\d{2}<\d{2}:\d{2},\s+\d+\.\d+it/s\]|"
        r"\d+%\|[█]+\|\s+\d+/\d+\s+\[\d{2}:\d{2}<\d{2}:\d{2},\s*\d+\.\d+it/s\])"
    )

    filtered_stdout = remove_ansi_codes(stdout)
    filtered_stdout = re.sub(progress_bar_re, "", filtered_stdout)
    filtered_stdout = re.sub(r"\s*\n\s*", "\n", filtered_stdout)

    needs_sub = True
    # Attempt further filtering up to 5 times
    for _ in range(5):
        filtered_stdout_shortened = filtered_stdout
        system_prompt = T(".prompts:filter_redundant_text.system").r()

        for __ in range(10):
            user_prompt = T(".prompts:filter_redundant_text.user").r(
                stdout=filtered_stdout_shortened,
            )
            stdout_token_size = APIBackend().build_messages_and_calculate_token(
                user_prompt=user_prompt,
                system_prompt=system_prompt,
            )
            if stdout_token_size < LLM_SETTINGS.chat_token_limit * 0.1:
                return filtered_stdout_shortened
            elif stdout_token_size > LLM_SETTINGS.chat_token_limit * 0.6:
                filtered_stdout_shortened = (
                    filtered_stdout_shortened[: int(LLM_SETTINGS.chat_token_limit * 0.3)]
                    + filtered_stdout_shortened[-int(LLM_SETTINGS.chat_token_limit * 0.3) :]
                )
            else:
                break

        response = json.loads(
            APIBackend().build_messages_and_create_chat_completion(
                user_prompt=user_prompt,
                system_prompt=system_prompt,
                json_mode=True,
                json_target_type=dict,
            )
        )
        needs_sub = response.get("needs_sub", True)
        regex_patterns = response.get("regex_patterns", [])
        try:
            if isinstance(regex_patterns, list):
                for pattern in regex_patterns:
                    filtered_stdout = re.sub(pattern, "", filtered_stdout)
            else:
                filtered_stdout = re.sub(regex_patterns, "", filtered_stdout)

            if not needs_sub:
                break
            filtered_stdout = re.sub(r"\s*\n\s*", "\n", filtered_stdout)
        except re.error as e:  # sometime the generated regex pattern is invalid  and yield exception.
            from rdagent.log import rdagent_logger as logger

            logger.error(f"Error in filtering progress bar: due to {e}")
    return filtered_stdout


def remove_path_info_from_str(base_path: Path, target_string: str) -> str:
    """
    Remove the absolute path from the target string
    """
    target_string = re.sub(str(base_path), "...", target_string)
    target_string = re.sub(str(base_path.absolute()), "...", target_string)
    return target_string


def md5_hash(input_string: str) -> str:
    hash_md5 = hashlib.md5(usedforsecurity=False)
    input_bytes = input_string.encode("utf-8")
    hash_md5.update(input_bytes)
    return hash_md5.hexdigest()
