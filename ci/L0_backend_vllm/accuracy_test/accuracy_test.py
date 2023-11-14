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

import pickle
import sys
import unittest
from functools import partial

import tritonclient.grpc as grpcclient
from tritonclient.utils import *

sys.path.append("../../common")
from test_util import TestResultCollector, UserData, callback, create_vllm_request


class VLLMTritonAccuracyTest(TestResultCollector):
    def test_vllm_model(self):
        with grpcclient.InferenceServerClient(url="localhost:8001") as triton_client:
            model_name = "vllm_opt"
            user_data = UserData()
            stream = False
            prompts = [
                "The most dangerous animal is",
                "The capital of France is",
                "The future of AI is",
            ]
            number_of_vllm_reqs = len(prompts)
            sampling_parameters = {"temperature": "0", "top_p": "1", "top_k": "-1"}
            triton_vllm_output = []

            triton_client.start_stream(callback=partial(callback, user_data))
            for i in range(number_of_vllm_reqs):
                request_data = create_vllm_request(
                    prompts[i], i, stream, sampling_parameters, model_name
                )
                triton_client.async_stream_infer(
                    model_name=model_name,
                    request_id=request_data["request_id"],
                    inputs=request_data["inputs"],
                    outputs=request_data["outputs"],
                    parameters=sampling_parameters,
                )

            for i in range(number_of_vllm_reqs):
                result = user_data._completed_requests.get()
                self.assertIsNot(type(result), InferenceServerException, str(result))

                output = result.as_numpy("text_output")
                self.assertIsNotNone(output, "`text_output` should not be None")

                triton_vllm_output.extend(output)

            triton_client.stop_stream()

            with open("python_vllm_output.pkl", "rb") as f:
                python_vllm_output = pickle.load(f)
                self.assertEqual(python_vllm_output, triton_vllm_output)


if __name__ == "__main__":
    unittest.main()
