from __future__ import annotations

import inspect
import json
import os
import random
import re
import sqlite3
import ssl
import time
import urllib.request
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Optional, cast

import numpy as np
import openai
import tiktoken

from rdagent.core.utils import LLM_CACHE_SEED_GEN, SingletonBaseClass, import_class
from rdagent.log import LogColors
from rdagent.log import rdagent_logger as logger
from rdagent.oai.llm_conf import LLM_SETTINGS
from rdagent.utils import md5_hash

DEFAULT_QLIB_DOT_PATH = Path("./")

from rdagent.oai.backend.base import APIBackend

try:
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider
except ImportError:
    logger.warning("azure.identity is not installed.")

try:
    import openai
except ImportError:
    logger.warning("openai is not installed.")

try:
    from llama import Llama
except ImportError:
    if LLM_SETTINGS.use_llama2:
        logger.warning("llama is not installed.")

try:
    from azure.ai.inference import ChatCompletionsClient
    from azure.ai.inference.models import (
        AssistantMessage,
        ChatRequestMessage,
        SystemMessage,
        UserMessage,
    )
    from azure.core.credentials import AzureKeyCredential
except ImportError:
    if LLM_SETTINGS.chat_use_azure_deepseek:
        logger.warning("azure.ai.inference or azure.core.credentials is not installed.")


class ConvManager:
    """
    This is a conversation manager of LLM
    It is for convenience of exporting conversation for debugging.
    """

    def __init__(
        self,
        path: Path | str = DEFAULT_QLIB_DOT_PATH / "llm_conv",
        recent_n: int = 10,
    ) -> None:
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self.recent_n = recent_n

    def _rotate_files(self) -> None:
        pairs = []
        for f in self.path.glob("*.json"):
            m = re.match(r"(\d+).json", f.name)
            if m is not None:
                n = int(m.group(1))
                pairs.append((n, f))
        pairs.sort(key=lambda x: x[0])
        for n, f in pairs[: self.recent_n][::-1]:
            if (self.path / f"{n+1}.json").exists():
                (self.path / f"{n+1}.json").unlink()
            f.rename(self.path / f"{n+1}.json")

    def append(self, conv: tuple[list, str]) -> None:
        self._rotate_files()
        with (self.path / "0.json").open("w") as file:
            json.dump(conv, file)
        # TODO: reseve line breaks to make it more convient to edit file directly.


class SQliteLazyCache(SingletonBaseClass):
    def __init__(self, cache_location: str) -> None:
        super().__init__()
        self.cache_location = cache_location
        db_file_exist = Path(cache_location).exists()
        # TODO: sqlite3 does not support multiprocessing.
        self.conn = sqlite3.connect(cache_location, timeout=20)
        self.c = self.conn.cursor()
        if not db_file_exist:
            self.c.execute(
                """
                CREATE TABLE chat_cache (
                    md5_key TEXT PRIMARY KEY,
                    chat TEXT
                )
                """,
            )
            self.c.execute(
                """
                CREATE TABLE embedding_cache (
                    md5_key TEXT PRIMARY KEY,
                    embedding TEXT
                )
                """,
            )
            self.c.execute(
                """
                CREATE TABLE message_cache (
                    conversation_id TEXT PRIMARY KEY,
                    message TEXT
                )
                """,
            )
            self.conn.commit()

    def chat_get(self, key: str) -> str | None:
        md5_key = md5_hash(key)
        self.c.execute("SELECT chat FROM chat_cache WHERE md5_key=?", (md5_key,))
        result = self.c.fetchone()
        return None if result is None else result[0]

    def embedding_get(self, key: str) -> list | dict | str | None:
        md5_key = md5_hash(key)
        self.c.execute("SELECT embedding FROM embedding_cache WHERE md5_key=?", (md5_key,))
        result = self.c.fetchone()
        return None if result is None else json.loads(result[0])

    def chat_set(self, key: str, value: str) -> None:
        md5_key = md5_hash(key)
        self.c.execute(
            "INSERT OR REPLACE INTO chat_cache (md5_key, chat) VALUES (?, ?)",
            (md5_key, value),
        )
        self.conn.commit()
        return None

    def embedding_set(self, content_to_embedding_dict: dict) -> None:
        for key, value in content_to_embedding_dict.items():
            md5_key = md5_hash(key)
            self.c.execute(
                "INSERT OR REPLACE INTO embedding_cache (md5_key, embedding) VALUES (?, ?)",
                (md5_key, json.dumps(value)),
            )
        self.conn.commit()

    def message_get(self, conversation_id: str) -> list[dict[str, Any]]:
        self.c.execute("SELECT message FROM message_cache WHERE conversation_id=?", (conversation_id,))
        result = self.c.fetchone()
        return [] if result is None else cast(list[dict[str, Any]], json.loads(result[0]))

    def message_set(self, conversation_id: str, message_value: list[dict[str, Any]]) -> None:
        self.c.execute(
            "INSERT OR REPLACE INTO message_cache (conversation_id, message) VALUES (?, ?)",
            (conversation_id, json.dumps(message_value)),
        )
        self.conn.commit()
        return None


class SessionChatHistoryCache(SingletonBaseClass):
    def __init__(self) -> None:
        """load all history conversation json file from self.session_cache_location"""
        self.cache = SQliteLazyCache(cache_location=LLM_SETTINGS.prompt_cache_path)

    def message_get(self, conversation_id: str) -> list[dict[str, Any]]:
        return self.cache.message_get(conversation_id)

    def message_set(self, conversation_id: str, message_value: list[dict[str, Any]]) -> None:
        self.cache.message_set(conversation_id, message_value)


class ChatSession:
    def __init__(self, api_backend: Any, conversation_id: str | None = None, system_prompt: str | None = None) -> None:
        self.conversation_id = str(uuid.uuid4()) if conversation_id is None else conversation_id
        self.system_prompt = system_prompt if system_prompt is not None else LLM_SETTINGS.default_system_prompt
        self.api_backend = api_backend

    def build_chat_completion_message(self, user_prompt: str) -> list[dict[str, Any]]:
        history_message = SessionChatHistoryCache().message_get(self.conversation_id)
        messages = history_message
        if not messages:
            messages.append({"role": LLM_SETTINGS.system_prompt_role, "content": self.system_prompt})
        messages.append(
            {
                "role": "user",
                "content": user_prompt,
            },
        )
        return messages

    def build_chat_completion_message_and_calculate_token(self, user_prompt: str) -> Any:
        messages = self.build_chat_completion_message(user_prompt)
        return self.api_backend._calculate_token_from_messages(messages)

    def build_chat_completion(self, user_prompt: str, *args, **kwargs) -> str:  # type: ignore[no-untyped-def]
        """
        this function is to build the session messages
        user prompt should always be provided
        """
        messages = self.build_chat_completion_message(user_prompt)

        with logger.tag(f"session_{self.conversation_id}"):
            response: str = self.api_backend._try_create_chat_completion_or_embedding(  # noqa: SLF001
                *args,
                messages=messages,
                chat_completion=True,
                **kwargs,
            )
            logger.log_object({"user": user_prompt, "resp": response}, tag="debug_llm")

        messages.append(
            {
                "role": "assistant",
                "content": response,
            },
        )
        SessionChatHistoryCache().message_set(self.conversation_id, messages)
        return response

    def get_conversation_id(self) -> str:
        return self.conversation_id

    def display_history(self) -> None:
        # TODO: Realize a beautiful presentation format for history messages
        pass


class DeprecBackend(APIBackend):
    """
    This is a unified interface for different backends.

    (xiao) thinks integrate all kinds of API in a single class is not a good design.
    So we should split them into different classes in `oai/backends/` in the future.
    """

    # FIXME: (xiao) We should avoid using self.xxxx.
    # Instead, we can use LLM_SETTINGS directly. If it's difficult to support different backend settings, we can split them into multiple BaseSettings.
    def __init__(  # noqa: C901, PLR0912, PLR0915
        self,
        *,
        chat_api_key: str | None = None,
        chat_model: str | None = None,
        chat_api_base: str | None = None,
        chat_api_version: str | None = None,
        embedding_api_key: str | None = None,
        embedding_model: str | None = None,
        embedding_api_base: str | None = None,
        embedding_api_version: str | None = None,
        use_chat_cache: bool | None = None,
        dump_chat_cache: bool | None = None,
        use_embedding_cache: bool | None = None,
        dump_embedding_cache: bool | None = None,
    ) -> None:
        if LLM_SETTINGS.use_llama2:
            self.generator = Llama.build(
                ckpt_dir=LLM_SETTINGS.llama2_ckpt_dir,
                tokenizer_path=LLM_SETTINGS.llama2_tokenizer_path,
                max_seq_len=LLM_SETTINGS.chat_max_tokens,
                max_batch_size=LLM_SETTINGS.llams2_max_batch_size,
            )
            self.encoder = None
        elif LLM_SETTINGS.use_gcr_endpoint:
            gcr_endpoint_type = LLM_SETTINGS.gcr_endpoint_type
            if gcr_endpoint_type == "llama2_70b":
                self.gcr_endpoint_key = LLM_SETTINGS.llama2_70b_endpoint_key
                self.gcr_endpoint_deployment = LLM_SETTINGS.llama2_70b_endpoint_deployment
                self.gcr_endpoint = LLM_SETTINGS.llama2_70b_endpoint
            elif gcr_endpoint_type == "llama3_70b":
                self.gcr_endpoint_key = LLM_SETTINGS.llama3_70b_endpoint_key
                self.gcr_endpoint_deployment = LLM_SETTINGS.llama3_70b_endpoint_deployment
                self.gcr_endpoint = LLM_SETTINGS.llama3_70b_endpoint
            elif gcr_endpoint_type == "phi2":
                self.gcr_endpoint_key = LLM_SETTINGS.phi2_endpoint_key
                self.gcr_endpoint_deployment = LLM_SETTINGS.phi2_endpoint_deployment
                self.gcr_endpoint = LLM_SETTINGS.phi2_endpoint
            elif gcr_endpoint_type == "phi3_4k":
                self.gcr_endpoint_key = LLM_SETTINGS.phi3_4k_endpoint_key
                self.gcr_endpoint_deployment = LLM_SETTINGS.phi3_4k_endpoint_deployment
                self.gcr_endpoint = LLM_SETTINGS.phi3_4k_endpoint
            elif gcr_endpoint_type == "phi3_128k":
                self.gcr_endpoint_key = LLM_SETTINGS.phi3_128k_endpoint_key
                self.gcr_endpoint_deployment = LLM_SETTINGS.phi3_128k_endpoint_deployment
                self.gcr_endpoint = LLM_SETTINGS.phi3_128k_endpoint
            else:
                error_message = f"Invalid gcr_endpoint_type: {gcr_endpoint_type}"
                raise ValueError(error_message)
            self.headers = {
                "Content-Type": "application/json",
                "Authorization": ("Bearer " + self.gcr_endpoint_key),
            }
            self.gcr_endpoint_temperature = LLM_SETTINGS.gcr_endpoint_temperature
            self.gcr_endpoint_top_p = LLM_SETTINGS.gcr_endpoint_top_p
            self.gcr_endpoint_do_sample = LLM_SETTINGS.gcr_endpoint_do_sample
            self.gcr_endpoint_max_token = LLM_SETTINGS.gcr_endpoint_max_token
            if not os.environ.get("PYTHONHTTPSVERIFY", "") and hasattr(ssl, "_create_unverified_context"):
                ssl._create_default_https_context = ssl._create_unverified_context  # noqa: SLF001
            self.chat_model_map = json.loads(LLM_SETTINGS.chat_model_map)
            self.chat_model = LLM_SETTINGS.chat_model if chat_model is None else chat_model
            self.encoder = None
        elif LLM_SETTINGS.chat_use_azure_deepseek:
            self.client = ChatCompletionsClient(
                endpoint=LLM_SETTINGS.chat_azure_deepseek_endpoint,
                credential=AzureKeyCredential(LLM_SETTINGS.chat_azure_deepseek_key),
            )
            self.chat_model_map = json.loads(LLM_SETTINGS.chat_model_map)
            self.encoder = None
            self.chat_model = "deepseek-R1"
            self.chat_stream = LLM_SETTINGS.chat_stream
        else:
            self.chat_use_azure = LLM_SETTINGS.chat_use_azure or LLM_SETTINGS.use_azure
            self.embedding_use_azure = LLM_SETTINGS.embedding_use_azure or LLM_SETTINGS.use_azure
            self.chat_use_azure_token_provider = LLM_SETTINGS.chat_use_azure_token_provider
            self.embedding_use_azure_token_provider = LLM_SETTINGS.embedding_use_azure_token_provider
            self.managed_identity_client_id = LLM_SETTINGS.managed_identity_client_id

            # Priority: chat_api_key/embedding_api_key > openai_api_key > os.environ.get("OPENAI_API_KEY")
            # TODO: Simplify the key design. Consider Pandatic's field alias & priority.
            self.chat_api_key = (
                chat_api_key
                or LLM_SETTINGS.chat_openai_api_key
                or LLM_SETTINGS.openai_api_key
                or os.environ.get("OPENAI_API_KEY")
            )
            self.embedding_api_key = (
                embedding_api_key
                or LLM_SETTINGS.embedding_openai_api_key
                or LLM_SETTINGS.openai_api_key
                or os.environ.get("OPENAI_API_KEY")
            )

            self.chat_model = LLM_SETTINGS.chat_model if chat_model is None else chat_model
            self.chat_model_map = json.loads(LLM_SETTINGS.chat_model_map)
            self.encoder = self._get_encoder()
            self.chat_openai_base_url = LLM_SETTINGS.chat_openai_base_url
            self.embedding_openai_base_url = LLM_SETTINGS.embedding_openai_base_url
            self.chat_api_base = LLM_SETTINGS.chat_azure_api_base if chat_api_base is None else chat_api_base
            self.chat_api_version = (
                LLM_SETTINGS.chat_azure_api_version if chat_api_version is None else chat_api_version
            )
            self.chat_stream = LLM_SETTINGS.chat_stream
            self.chat_seed = LLM_SETTINGS.chat_seed

            self.embedding_model = LLM_SETTINGS.embedding_model if embedding_model is None else embedding_model
            self.embedding_api_base = (
                LLM_SETTINGS.embedding_azure_api_base if embedding_api_base is None else embedding_api_base
            )
            self.embedding_api_version = (
                LLM_SETTINGS.embedding_azure_api_version if embedding_api_version is None else embedding_api_version
            )

            if (self.chat_use_azure or self.embedding_use_azure) and (
                self.chat_use_azure_token_provider or self.embedding_use_azure_token_provider
            ):
                dac_kwargs = {}
                if self.managed_identity_client_id is not None:
                    dac_kwargs["managed_identity_client_id"] = self.managed_identity_client_id
                credential = DefaultAzureCredential(**dac_kwargs)
                token_provider = get_bearer_token_provider(
                    credential,
                    "https://cognitiveservices.azure.com/.default",
                )
            self.chat_client: openai.OpenAI = (
                openai.AzureOpenAI(
                    azure_ad_token_provider=token_provider if self.chat_use_azure_token_provider else None,
                    api_key=self.chat_api_key if not self.chat_use_azure_token_provider else None,
                    api_version=self.chat_api_version,
                    azure_endpoint=self.chat_api_base,
                )
                if self.chat_use_azure
                else openai.OpenAI(api_key=self.chat_api_key, base_url=self.chat_openai_base_url)
            )

            self.embedding_client: openai.OpenAI = (
                openai.AzureOpenAI(
                    azure_ad_token_provider=token_provider if self.embedding_use_azure_token_provider else None,
                    api_key=self.embedding_api_key if not self.embedding_use_azure_token_provider else None,
                    api_version=self.embedding_api_version,
                    azure_endpoint=self.embedding_api_base,
                )
                if self.embedding_use_azure
                else openai.OpenAI(api_key=self.embedding_api_key, base_url=self.embedding_openai_base_url)
            )

        self.dump_chat_cache = LLM_SETTINGS.dump_chat_cache if dump_chat_cache is None else dump_chat_cache
        self.use_chat_cache = LLM_SETTINGS.use_chat_cache if use_chat_cache is None else use_chat_cache
        self.dump_embedding_cache = (
            LLM_SETTINGS.dump_embedding_cache if dump_embedding_cache is None else dump_embedding_cache
        )
        self.use_embedding_cache = (
            LLM_SETTINGS.use_embedding_cache if use_embedding_cache is None else use_embedding_cache
        )
        if self.dump_chat_cache or self.use_chat_cache or self.dump_embedding_cache or self.use_embedding_cache:
            self.cache_file_location = LLM_SETTINGS.prompt_cache_path
            self.cache = SQliteLazyCache(cache_location=self.cache_file_location)

        # transfer the config to the class if the config is not supposed to change during the runtime
        self.use_llama2 = LLM_SETTINGS.use_llama2
        self.use_gcr_endpoint = LLM_SETTINGS.use_gcr_endpoint
        self.chat_use_azure_deepseek = LLM_SETTINGS.chat_use_azure_deepseek
        self.retry_wait_seconds = LLM_SETTINGS.retry_wait_seconds

    def _get_encoder(self) -> tiktoken.Encoding:
        """
        tiktoken.encoding_for_model(self.chat_model) does not cover all cases it should consider.

        This function attempts to handle several edge cases.
        """

        # 1) cases
        def _azure_patch(model: str) -> str:
            """
            When using Azure API, self.chat_model is the deployment name that can be any string.
            For example, it may be `gpt-4o_2024-08-06`. But tiktoken.encoding_for_model can't handle this.
            """
            return model.replace("_", "-")

        model = self.chat_model
        try:
            encoding = tiktoken.encoding_for_model(model)
        except KeyError:
            logger.warning(f"Failed to get encoder. Trying to patch the model name")
            for patch_func in [_azure_patch]:
                try:
                    encoding = tiktoken.encoding_for_model(patch_func(model))
                except KeyError:
                    logger.error(f"Failed to get encoder even after patching with {patch_func.__name__}")
                    raise
        return encoding

    def build_chat_session(
        self,
        conversation_id: str | None = None,
        session_system_prompt: str | None = None,
    ) -> ChatSession:
        """
        conversation_id is a 256-bit string created by uuid.uuid4() and is also
        the file name under session_cache_folder/ for each conversation
        """
        return ChatSession(self, conversation_id, session_system_prompt)

    def _build_messages(
        self,
        user_prompt: str,
        system_prompt: str | None = None,
        former_messages: list[dict[str, Any]] | None = None,
        *,
        shrink_multiple_break: bool = False,
    ) -> list[dict[str, Any]]:
        """
        build the messages to avoid implementing several redundant lines of code

        """
        if former_messages is None:
            former_messages = []
        # shrink multiple break will recursively remove multiple breaks(more than 2)
        if shrink_multiple_break:
            while "\n\n\n" in user_prompt:
                user_prompt = user_prompt.replace("\n\n\n", "\n\n")
            if system_prompt is not None:
                while "\n\n\n" in system_prompt:
                    system_prompt = system_prompt.replace("\n\n\n", "\n\n")
        system_prompt = LLM_SETTINGS.default_system_prompt if system_prompt is None else system_prompt
        messages = [
            {
                "role": LLM_SETTINGS.system_prompt_role,
                "content": system_prompt,
            },
        ]
        messages.extend(former_messages[-1 * LLM_SETTINGS.max_past_message_include :])
        messages.append(
            {
                "role": "user",
                "content": user_prompt,
            },
        )
        return messages

    def build_messages_and_create_chat_completion(  # type: ignore[no-untyped-def]
        self,
        user_prompt: str,
        system_prompt: str | None = None,
        former_messages: list | None = None,
        chat_cache_prefix: str = "",
        shrink_multiple_break: bool = False,
        *args,
        **kwargs,
    ) -> str:
        if former_messages is None:
            former_messages = []
        messages = self._build_messages(
            user_prompt,
            system_prompt,
            former_messages,
            shrink_multiple_break=shrink_multiple_break,
        )

        resp = self._try_create_chat_completion_or_embedding(  # type: ignore[misc]
            *args,
            messages=messages,
            chat_completion=True,
            chat_cache_prefix=chat_cache_prefix,
            **kwargs,
        )
        if isinstance(resp, list):
            raise ValueError("The response of _try_create_chat_completion_or_embedding should be a string.")
        logger.log_object({"system": system_prompt, "user": user_prompt, "resp": resp}, tag="debug_llm")
        return resp

    def create_embedding(self, input_content: str | list[str], *args, **kwargs) -> list[Any] | Any:  # type: ignore[no-untyped-def]
        input_content_list = [input_content] if isinstance(input_content, str) else input_content
        resp = self._try_create_chat_completion_or_embedding(  # type: ignore[misc]
            input_content_list=input_content_list,
            embedding=True,
            *args,
            **kwargs,
        )
        if isinstance(input_content, str):
            return resp[0]
        return resp

    def _create_chat_completion_auto_continue(
        self,
        messages: list[dict[str, Any]],
        *args: Any,
        json_mode: bool = False,
        chat_cache_prefix: str = "",
        seed: Optional[int] = None,
        **kwargs: Any,
    ) -> str:
        """
        Call the chat completion function and automatically continue the conversation if the finish_reason is length.
        """
        if seed is None and LLM_SETTINGS.use_auto_chat_cache_seed_gen:
            seed = LLM_CACHE_SEED_GEN.get_next_seed()
        input_content_json = json.dumps(messages)
        input_content_json = (
            chat_cache_prefix + input_content_json + f"<seed={seed}/>"
        )  # FIXME this is a hack to make sure the cache represents the round index
        if self.use_chat_cache:
            cache_result = self.cache.chat_get(input_content_json)
            if cache_result is not None:
                if LLM_SETTINGS.log_llm_chat_content:
                    logger.info(f"{LogColors.CYAN}Response:{cache_result}{LogColors.END}", tag="llm_messages")
                return cache_result

        all_response = ""
        new_messages = deepcopy(messages)
        for _ in range(3):
            if "json_mode" in kwargs:
                del kwargs["json_mode"]
            response, finish_reason = self._create_chat_completion_inner_function(
                new_messages, json_mode=json_mode, *args, **kwargs
            )  # type: ignore[misc]
            all_response += response
            if finish_reason is None or finish_reason != "length":
                if self.chat_use_azure_deepseek:
                    match = re.search(r"<think>(.*?)</think>(.*)", all_response, re.DOTALL)
                    think_part, all_response = match.groups() if match else ("", all_response)
                    if LLM_SETTINGS.log_llm_chat_content:
                        logger.info(f"{LogColors.CYAN}Think:{think_part}{LogColors.END}", tag="llm_messages")
                        logger.info(f"{LogColors.CYAN}Response:{all_response}{LogColors.END}", tag="llm_messages")

                if json_mode:
                    try:
                        json.loads(all_response)
                    except:
                        match = re.search(r"```json(.*?)```", all_response, re.DOTALL)
                        all_response = match.groups()[0] if match else all_response
                        json.loads(all_response)
                if self.dump_chat_cache:
                    self.cache.chat_set(input_content_json, all_response)
                return all_response
            new_messages.append({"role": "assistant", "content": response})
        raise RuntimeError("Failed to continue the conversation after 3 retries.")

    def _try_create_chat_completion_or_embedding(  # type: ignore[no-untyped-def]
        self,
        max_retry: int = 10,
        chat_completion: bool = False,
        embedding: bool = False,
        *args,
        **kwargs,
    ) -> str | list[float]:
        assert not (chat_completion and embedding), "chat_completion and embedding cannot be True at the same time"
        max_retry = LLM_SETTINGS.max_retry if LLM_SETTINGS.max_retry is not None else max_retry
        for i in range(max_retry):
            try:
                if embedding:
                    return self._create_embedding_inner_function(*args, **kwargs)
                if chat_completion:
                    return self._create_chat_completion_auto_continue(*args, **kwargs)
            except openai.BadRequestError as e:  # noqa: PERF203
                logger.warning(str(e))
                logger.warning(f"Retrying {i+1}th time...")
                if (
                    "'messages' must contain the word 'json' in some form" in e.message
                    or "\\'messages\\' must contain the word \\'json\\' in some form" in e.message
                ):
                    kwargs["add_json_in_prompt"] = True
                elif embedding and "maximum context length" in e.message:
                    kwargs["input_content_list"] = [
                        content[: len(content) // 2] for content in kwargs.get("input_content_list", [])
                    ]
            except Exception as e:  # noqa: BLE001
                logger.warning(str(e))
                logger.warning(f"Retrying {i+1}th time...")
                time.sleep(self.retry_wait_seconds)
        error_message = f"Failed to create chat completion after {max_retry} retries."
        raise RuntimeError(error_message)

    def _create_embedding_inner_function(  # type: ignore[no-untyped-def]
        self, input_content_list: list[str], *args, **kwargs
    ) -> list[Any]:  # noqa: ARG002
        content_to_embedding_dict = {}
        filtered_input_content_list = []
        if self.use_embedding_cache:
            for content in input_content_list:
                cache_result = self.cache.embedding_get(content)
                if cache_result is not None:
                    content_to_embedding_dict[content] = cache_result
                else:
                    filtered_input_content_list.append(content)
        else:
            filtered_input_content_list = input_content_list

        if len(filtered_input_content_list) > 0:
            for sliced_filtered_input_content_list in [
                filtered_input_content_list[i : i + LLM_SETTINGS.embedding_max_str_num]
                for i in range(0, len(filtered_input_content_list), LLM_SETTINGS.embedding_max_str_num)
            ]:
                if self.embedding_use_azure:
                    response = self.embedding_client.embeddings.create(
                        model=self.embedding_model,
                        input=sliced_filtered_input_content_list,
                    )
                else:
                    response = self.embedding_client.embeddings.create(
                        model=self.embedding_model,
                        input=sliced_filtered_input_content_list,
                    )
                for index, data in enumerate(response.data):
                    content_to_embedding_dict[sliced_filtered_input_content_list[index]] = data.embedding

                if self.dump_embedding_cache:
                    self.cache.embedding_set(content_to_embedding_dict)
        return [content_to_embedding_dict[content] for content in input_content_list]

    def _build_log_messages(self, messages: list[dict[str, Any]]) -> str:
        log_messages = ""
        for m in messages:
            log_messages += (
                f"\n{LogColors.MAGENTA}{LogColors.BOLD}Role:{LogColors.END}"
                f"{LogColors.CYAN}{m['role']}{LogColors.END}\n"
                f"{LogColors.MAGENTA}{LogColors.BOLD}Content:{LogColors.END} "
                f"{LogColors.CYAN}{m['content']}{LogColors.END}\n"
            )
        return log_messages

    def _create_chat_completion_inner_function(  # type: ignore[no-untyped-def] # noqa: C901, PLR0912, PLR0915
        self,
        messages: list[dict[str, Any]],
        temperature: float | None = None,
        max_tokens: int | None = None,
        frequency_penalty: float | None = None,
        presence_penalty: float | None = None,
        json_mode: bool = False,
        add_json_in_prompt: bool = False,
        *args,
        **kwargs,
    ) -> tuple[str, str | None]:
        """
        seed : Optional[int]
            When retrying with cache enabled, it will keep returning the same results.
            To make retries useful, we need to enable a seed.
            This seed is different from `self.chat_seed` for GPT. It is for the local cache mechanism enabled by RD-Agent locally.
        """

        # TODO: we can add this function back to avoid so much `self.cfg.log_llm_chat_content`
        if LLM_SETTINGS.log_llm_chat_content:
            logger.info(self._build_log_messages(messages), tag="llm_messages")
        # TODO: fail to use loguru adaptor due to stream response

        if temperature is None:
            temperature = LLM_SETTINGS.chat_temperature
        if max_tokens is None:
            max_tokens = LLM_SETTINGS.chat_max_tokens
        if frequency_penalty is None:
            frequency_penalty = LLM_SETTINGS.chat_frequency_penalty
        if presence_penalty is None:
            presence_penalty = LLM_SETTINGS.chat_presence_penalty

        # Use index 4 to skip the current function and intermediate calls,
        # and get the locals of the caller's frame.
        caller_locals = inspect.stack()[4].frame.f_locals
        if "self" in caller_locals:
            tag = caller_locals["self"].__class__.__name__
        else:
            tag = inspect.stack()[4].function
        model = self.chat_model_map.get(tag, self.chat_model)

        finish_reason = None
        if self.use_llama2:
            response = self.generator.chat_completion(
                messages,
                max_gen_len=max_tokens,
                temperature=temperature,
            )
            resp = response[0]["generation"]["content"]
            if LLM_SETTINGS.log_llm_chat_content:
                logger.info(f"{LogColors.CYAN}Response:{resp}{LogColors.END}", tag="llm_messages")
        elif self.use_gcr_endpoint:
            body = str.encode(
                json.dumps(
                    {
                        "input_data": {
                            "input_string": messages,
                            "parameters": {
                                "temperature": self.gcr_endpoint_temperature,
                                "top_p": self.gcr_endpoint_top_p,
                                "max_new_tokens": self.gcr_endpoint_max_token,
                            },
                        },
                    },
                ),
            )

            req = urllib.request.Request(self.gcr_endpoint, body, self.headers)  # noqa: S310
            response = urllib.request.urlopen(req)  # noqa: S310
            resp = json.loads(response.read().decode())["output"]
            if LLM_SETTINGS.log_llm_chat_content:
                logger.info(f"{LogColors.CYAN}Response:{resp}{LogColors.END}", tag="llm_messages")
        elif self.chat_use_azure_deepseek:
            azure_style_message: list[ChatRequestMessage] = []
            for message in messages:
                if message["role"] == "system":
                    azure_style_message.append(SystemMessage(content=message["content"]))
                elif message["role"] == "user":
                    azure_style_message.append(UserMessage(content=message["content"]))
                elif message["role"] == "assistant":
                    azure_style_message.append(AssistantMessage(content=message["content"]))

            response = self.client.complete(
                messages=azure_style_message,
                stream=self.chat_stream,
                temperature=temperature,
                max_tokens=max_tokens,
                frequency_penalty=frequency_penalty,
                presence_penalty=presence_penalty,
            )
            if self.chat_stream:
                resp = ""
                # TODO: with logger.config(stream=self.chat_stream): and add a `stream_start` flag to add timestamp for first message.
                if LLM_SETTINGS.log_llm_chat_content:
                    logger.info(f"{LogColors.CYAN}Response:{LogColors.END}", tag="llm_messages")

                for chunk in response:
                    content = (
                        chunk.choices[0].delta.content
                        if len(chunk.choices) > 0 and chunk.choices[0].delta.content is not None
                        else ""
                    )
                    if LLM_SETTINGS.log_llm_chat_content:
                        logger.info(LogColors.CYAN + content + LogColors.END, raw=True, tag="llm_messages")
                    resp += content
                    if len(chunk.choices) > 0 and chunk.choices[0].finish_reason is not None:
                        finish_reason = chunk.choices[0].finish_reason
            else:
                resp = response.choices[0].message.content
                finish_reason = response.choices[0].finish_reason
                if LLM_SETTINGS.log_llm_chat_content:
                    logger.info(f"{LogColors.CYAN}Response:{resp}{LogColors.END}", tag="llm_messages")
        else:
            call_kwargs = dict(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                stream=self.chat_stream,
                seed=self.chat_seed,
                frequency_penalty=frequency_penalty,
                presence_penalty=presence_penalty,
            )
            if json_mode:
                if add_json_in_prompt:
                    for message in messages[::-1]:
                        message["content"] = message["content"] + "\nPlease respond in json format."
                        if message["role"] == LLM_SETTINGS.system_prompt_role:
                            # NOTE: assumption: systemprompt is always the first message
                            break
                call_kwargs["response_format"] = {"type": "json_object"}
            response = self.chat_client.chat.completions.create(**call_kwargs)

            if self.chat_stream:
                resp = ""
                # TODO: with logger.config(stream=self.chat_stream): and add a `stream_start` flag to add timestamp for first message.
                if LLM_SETTINGS.log_llm_chat_content:
                    logger.info(f"{LogColors.CYAN}Response:{LogColors.END}", tag="llm_messages")

                for chunk in response:
                    content = (
                        chunk.choices[0].delta.content
                        if len(chunk.choices) > 0 and chunk.choices[0].delta.content is not None
                        else ""
                    )
                    if LLM_SETTINGS.log_llm_chat_content:
                        logger.info(LogColors.CYAN + content + LogColors.END, raw=True, tag="llm_messages")
                    resp += content
                    if len(chunk.choices) > 0 and chunk.choices[0].finish_reason is not None:
                        finish_reason = chunk.choices[0].finish_reason

                if LLM_SETTINGS.log_llm_chat_content:
                    logger.info("\n", raw=True, tag="llm_messages")

            else:
                resp = response.choices[0].message.content
                finish_reason = response.choices[0].finish_reason
                if LLM_SETTINGS.log_llm_chat_content:
                    logger.info(f"{LogColors.CYAN}Response:{resp}{LogColors.END}", tag="llm_messages")
                    logger.info(
                        json.dumps(
                            {
                                "tag": tag,
                                "total_tokens": response.usage.total_tokens,
                                "prompt_tokens": response.usage.prompt_tokens,
                                "completion_tokens": response.usage.completion_tokens,
                                "model": model,
                            }
                        ),
                        tag="llm_messages",
                    )
        return resp, finish_reason

    def _calculate_token_from_messages(self, messages: list[dict[str, Any]]) -> int:
        if self.chat_use_azure_deepseek:
            return 0
        if self.encoder is None:
            raise ValueError("Encoder is not initialized.")
        if self.use_llama2 or self.use_gcr_endpoint:
            logger.warning("num_tokens_from_messages() is not implemented for model llama2.")
            return 0  # TODO implement this function for llama2

        if "gpt4" in self.chat_model or "gpt-4" in self.chat_model:
            tokens_per_message = 3
            tokens_per_name = 1
        else:
            tokens_per_message = 4  # every message follows <start>{role/name}\n{content}<end>\n
            tokens_per_name = -1  # if there's a name, the role is omitted
        num_tokens = 0
        for message in messages:
            num_tokens += tokens_per_message
            for key, value in message.items():
                num_tokens += len(self.encoder.encode(value))
                if key == "name":
                    num_tokens += tokens_per_name
        num_tokens += 3  # every reply is primed with <start>assistant<message>
        return num_tokens

    def build_messages_and_calculate_token(
        self,
        user_prompt: str,
        system_prompt: str | None,
        former_messages: list[dict[str, Any]] | None = None,
        *,
        shrink_multiple_break: bool = False,
    ) -> int:
        if former_messages is None:
            former_messages = []
        messages = self._build_messages(
            user_prompt, system_prompt, former_messages, shrink_multiple_break=shrink_multiple_break
        )
        return self._calculate_token_from_messages(messages)
