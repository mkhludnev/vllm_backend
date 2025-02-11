# Copyright 2023, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#  * Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
#  * Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#  * Neither the name of NVIDIA CORPORATION nor the names of its
#    contributors may be used to endorse or promote products derived
#    from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS ``AS IS'' AND ANY
# EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
# PURPOSE ARE DISCLAIMED.  IN NO EVENT SHALL THE COPYRIGHT OWNER OR
# CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
# PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY
# OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import asyncio
import json
import os
import threading
from typing import AsyncGenerator

import numpy as np
import triton_python_backend_utils as pb_utils
from vllm import SamplingParams
from vllm.engine.arg_utils import AsyncEngineArgs
from vllm.engine.async_llm_engine import AsyncLLMEngine
from vllm.utils import random_uuid

_VLLM_ENGINE_ARGS_FILENAME = "model.json"


class TritonPythonModel:
    @staticmethod
    def auto_complete_config(auto_complete_model_config):
        inputs = [
            {"name": "text_input", "data_type": "TYPE_STRING", "dims": [1]},
            {
                "name": "stream",
                "data_type": "TYPE_BOOL",
                "dims": [1],
                "optional": True,
            },
            {
                "name": "sampling_parameters",
                "data_type": "TYPE_STRING",
                "dims": [1],
                "optional": True,
            },
        ]
        outputs = [{"name": "text_output", "data_type": "TYPE_STRING", "dims": [-1]}]

        # Store the model configuration as a dictionary.
        config = auto_complete_model_config.as_dict()
        input_names = []
        output_names = []
        for input in config["input"]:
            input_names.append(input["name"])
        for output in config["output"]:
            output_names.append(output["name"])

        # Add only missing inputs and output to the model configuration.
        for input in inputs:
            if input["name"] not in input_names:
                auto_complete_model_config.add_input(input)
        for output in outputs:
            if output["name"] not in output_names:
                auto_complete_model_config.add_output(output)

        # We need to use decoupled transaction policy for saturating
        # vLLM engine for max throughtput.
        # TODO [DLIS:5233]: Allow asynchronous execution to lift this
        # restriction for cases there is exactly a single response to
        # a single request.
        auto_complete_model_config.set_model_transaction_policy(dict(decoupled=True))

        # Disabling batching in Triton, let vLLM handle the batching on its own.
        auto_complete_model_config.set_max_batch_size(0)

        return auto_complete_model_config

    def initialize(self, args):
        self.logger = pb_utils.Logger
        self.model_config = json.loads(args["model_config"])

        # assert are in decoupled mode. Currently, Triton needs to use
        # decoupled policy for asynchronously forwarding requests to
        # vLLM engine.
        self.using_decoupled = pb_utils.using_decoupled_model_transaction_policy(
            self.model_config
        )
        assert (
            self.using_decoupled
        ), "vLLM Triton backend must be configured to use decoupled model transaction policy"

        engine_args_filepath = os.path.join(
            pb_utils.get_model_dir(), _VLLM_ENGINE_ARGS_FILENAME
        )
        assert os.path.isfile(
            engine_args_filepath
        ), f"'{_VLLM_ENGINE_ARGS_FILENAME}' containing vllm engine args must be provided in '{pb_utils.get_model_dir()}'"
        with open(engine_args_filepath) as file:
            vllm_engine_config = json.load(file)

        # Create an AsyncLLMEngine from the config from JSON
        self.llm_engine = AsyncLLMEngine.from_engine_args(
            AsyncEngineArgs(**vllm_engine_config)
        )

        output_config = pb_utils.get_output_config_by_name(
            self.model_config, "text_output"
        )
        self.output_dtype = pb_utils.triton_string_to_numpy(output_config["data_type"])

        # Counter to keep track of ongoing request counts
        self.ongoing_request_count = 0

        # Starting asyncio event loop to process the received requests asynchronously.
        self._loop = asyncio.get_event_loop()
        self._loop_thread = threading.Thread(
            target=self.engine_loop, args=(self._loop,)
        )
        self._shutdown_event = asyncio.Event()
        self._loop_thread.start()

    def create_task(self, coro):
        """
        Creates a task on the engine's event loop which is running on a separate thread.
        """
        assert (
            self._shutdown_event.is_set() is False
        ), "Cannot create tasks after shutdown has been requested"

        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def engine_loop(self, loop):
        """
        Runs the engine's event loop on a separate thread.
        """
        asyncio.set_event_loop(loop)
        self._loop.run_until_complete(self.await_shutdown())

    async def await_shutdown(self):
        """
        Primary coroutine running on the engine event loop. This coroutine is responsible for
        keeping the engine alive until a shutdown is requested.
        """
        # first await the shutdown signal
        while self._shutdown_event.is_set() is False:
            await asyncio.sleep(5)

        # Wait for the ongoing_requests
        while self.ongoing_request_count > 0:
            self.logger.log_info(
                "[vllm] Awaiting remaining {} requests".format(
                    self.ongoing_request_count
                )
            )
            await asyncio.sleep(5)

        for task in asyncio.all_tasks(loop=self._loop):
            if task is not asyncio.current_task():
                task.cancel()

        self.logger.log_info("[vllm] Shutdown complete")

    def get_sampling_params_dict(self, params_json):
        """
        This functions parses the dictionary values into their
        expected format.
        """

        params_dict = json.loads(params_json)

        # Special parsing for the supported sampling parameters
        bool_keys = ["ignore_eos", "skip_special_tokens", "use_beam_search"]
        for k in bool_keys:
            if k in params_dict:
                params_dict[k] = bool(params_dict[k])

        float_keys = [
            "frequency_penalty",
            "length_penalty",
            "presence_penalty",
            "temperature",
            "top_p",
        ]
        for k in float_keys:
            if k in params_dict:
                params_dict[k] = float(params_dict[k])

        int_keys = ["best_of", "max_tokens", "n", "top_k"]
        for k in int_keys:
            if k in params_dict:
                params_dict[k] = int(params_dict[k])

        return params_dict

    def create_response(self, vllm_output):
        """
        Parses the output from the vLLM engine into Triton
        response.
        """
        prompt = vllm_output.prompt
        text_outputs = [
            (prompt + output.text).encode("utf-8") for output in vllm_output.outputs
        ]
        triton_output_tensor = pb_utils.Tensor(
            "text_output", np.asarray(text_outputs, dtype=self.output_dtype)
        )
        return pb_utils.InferenceResponse(output_tensors=[triton_output_tensor])

    async def generate(self, request):
        """
        Forwards single request to LLM engine and returns responses.
        """
        response_sender = request.get_response_sender()
        self.ongoing_request_count += 1
        try:
            request_id = random_uuid()
            prompt = pb_utils.get_input_tensor_by_name(
                request, "text_input"
            ).as_numpy()[0]
            if isinstance(prompt, bytes):
                prompt = prompt.decode("utf-8")
            stream = pb_utils.get_input_tensor_by_name(request, "stream")
            if stream:
                stream = stream.as_numpy()[0]
            else:
                stream = False

            # Request parameters are not yet supported via
            # BLS. Provide an optional mechanism to receive serialized
            # parameters as an input tensor until support is added

            parameters_input_tensor = pb_utils.get_input_tensor_by_name(
                request, "sampling_parameters"
            )
            if parameters_input_tensor:
                parameters = parameters_input_tensor.as_numpy()[0].decode("utf-8")
            else:
                parameters = request.parameters()

            sampling_params_dict = self.get_sampling_params_dict(parameters)
            sampling_params = SamplingParams(**sampling_params_dict)

            last_output = None
            async for output in self.llm_engine.generate(
                prompt, sampling_params, request_id
            ):
                if response_sender.is_cancelled():
                    self.logger.log_info("[vllm] Cancelling the request")
                    await self.llm_engine.abort(request_id)
                    self.logger.log_info("[vllm] Successfully cancelled the request")
                    break
                if stream:
                    if output.finished:
                        response_sender.send(
                            self.create_response(output),
                            flags=pb_utils.TRITONSERVER_RESPONSE_COMPLETE_FINAL,
                        )
                    else:
                        response_sender.send(self.create_response(output))
                else:
                    last_output = output

            if not stream:
                response_sender.send(
                    self.create_response(last_output),
                    flags=pb_utils.TRITONSERVER_RESPONSE_COMPLETE_FINAL,
                )

        except Exception as e:
            self.logger.log_info(f"[vllm] Error generating stream: {e}")
            error = pb_utils.TritonError(f"Error generating stream: {e}")
            triton_output_tensor = pb_utils.Tensor(
                "text_output", np.asarray(["N/A"], dtype=self.output_dtype)
            )
            response = pb_utils.InferenceResponse(
                output_tensors=[triton_output_tensor], error=error
            )
            response_sender.send(
                response, flags=pb_utils.TRITONSERVER_RESPONSE_COMPLETE_FINAL
            )
            raise e
        finally:
            self.ongoing_request_count -= 1

    def execute(self, requests):
        """
        Triton core issues requests to the backend via this method.

        When this method returns, new requests can be issued to the backend. Blocking
        this function would prevent the backend from pulling additional requests from
        Triton into the vLLM engine. This can be done if the kv cache within vLLM engine
        is too loaded.
        We are pushing all the requests on vllm and let it handle the full traffic.
        """
        for request in requests:
            self.create_task(self.generate(request))
        return None

    def finalize(self):
        """
        Triton virtual method; called when the model is unloaded.
        """
        self.logger.log_info("[vllm] Issuing finalize to vllm backend")
        self._shutdown_event.set()
        if self._loop_thread is not None:
            self._loop_thread.join()
            self._loop_thread = None
