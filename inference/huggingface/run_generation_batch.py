#!/usr/bin/env python
# coding=utf-8
# Copyright 2018 Google AI, Google Brain and Carnegie Mellon University Authors and the HuggingFace Inc. team.
# Copyright (c) 2018, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
""" Conditional text generation with the auto-regressive models of the library (GPT/GPT-2/CTRL/Transformer-XL/XLNet)
"""

# Updated from HuggingFace Transformers commit d9c62047a8d75e18d2849d345ab3394875a712ef


import argparse
import logging
import time
import numpy as np
import torch
import os
from transformers.models.gpt2.modeling_gpt2 import GPT2Block as gpt2_transformer
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    AutoConfig,
    AutoModelForSeq2SeqLM,
    T5ForConditionalGeneration,
    T5Tokenizer,
    CTRLLMHeadModel,
    CTRLTokenizer,
    GPT2LMHeadModel,
    GPT2Tokenizer,
    GPTNeoModel,
    OpenAIGPTLMHeadModel,
    OpenAIGPTTokenizer,
    TransfoXLLMHeadModel,
    TransfoXLTokenizer,
    XLMTokenizer,
    XLMWithLMHeadModel,
    XLNetLMHeadModel,
    XLNetTokenizer,
)

import deepspeed.module_inject as module_inject
import deepspeed
from deepspeed.runtime.zero.constants import *
from transformers.deepspeed import HfDeepSpeedConfig
from deepspeed.runtime.utils import see_memory_usage

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(name)s -   %(message)s",
    datefmt="%m/%d/%Y %H:%M:%S",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

MAX_LENGTH = int(10000)  # Hardcoded max length to avoid infinite loop

MODEL_CLASSES = {
    "t5": (AutoModelForSeq2SeqLM, AutoTokenizer),
    "opt": (AutoModelForCausalLM, AutoTokenizer),
    "gpt2": (GPT2LMHeadModel, GPT2Tokenizer),
    "gptneo": (GPTNeoModel, GPT2Tokenizer),
    "ctrl": (CTRLLMHeadModel, CTRLTokenizer),
    "openai-gpt": (OpenAIGPTLMHeadModel, OpenAIGPTTokenizer),
    "xlnet": (XLNetLMHeadModel, XLNetTokenizer),
    "transfo-xl": (TransfoXLLMHeadModel, TransfoXLTokenizer),
    "xlm": (XLMWithLMHeadModel, XLMTokenizer),
}

# Padding text to help Transformer-XL and XLNet with short prompts as proposed by Aman Rusia
# in https://github.com/rusiaaman/XLNet-gen#methodology
# and https://medium.com/@amanrusia/xlnet-speaks-comparison-to-gpt-2-ea1a4e9ba39e
PREFIX = """In 1991, the remains of Russian Tsar Nicholas II and his family
(except for Alexei and Maria) are discovered.
The voice of Nicholas's young son, Tsarevich Alexei Nikolaevich, narrates the
remainder of the story. 1883 Western Siberia,
a young Grigori Rasputin is asked by his father and a group of men to perform magic.
Rasputin has a vision and denounces one of the men as a horse thief. Although his
father initially slaps him for making such an accusation, Rasputin watches as the
man is chased outside and beaten. Twenty years later, Rasputin sees a vision of
the Virgin Mary, prompting him to become a priest. Rasputin quickly becomes famous,
with people, even a bishop, begging for his blessing. <eod> </s> <eos>"""


def set_seed(args):
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if args.n_gpu > 0:
        torch.cuda.manual_seed_all(args.seed)


def prepare_ctrl_input(args, _, tokenizer, prompt_text):
    if args.temperature > 0.7:
        logger.info("CTRL typically works better with lower temperatures (and lower top_k).")

    encoded_prompt = tokenizer.encode(prompt_text, add_special_tokens=False)
    if not any(encoded_prompt[0] == x for x in tokenizer.control_codes.values()):
        logger.info("WARNING! You are not starting your generation from a control code so you won't get good results")
    return prompt_text


def prepare_xlm_input(args, model, tokenizer, prompt_text):
    # kwargs = {"language": None, "mask_token_id": None}

    # Set the language
    use_lang_emb = hasattr(model.config, "use_lang_emb") and model.config.use_lang_emb
    if hasattr(model.config, "lang2id") and use_lang_emb:
        available_languages = model.config.lang2id.keys()
        if args.xlm_language in available_languages:
            language = args.xlm_language
        else:
            language = None
            while language not in available_languages:
                language = input("Using XLM. Select language in " + str(list(available_languages)) + " >>> ")

        model.config.lang_id = model.config.lang2id[language]
        # kwargs["language"] = tokenizer.lang2id[language]

    return prompt_text


def prepare_xlnet_input(args, _, tokenizer, prompt_text):
    prefix = args.prefix if args.prefix else args.padding_text if args.padding_text else PREFIX
    prompt_text = prefix + prompt_text
    return prompt_text


def prepare_transfoxl_input(args, _, tokenizer, prompt_text):
    prefix = args.prefix if args.prefix else args.padding_text if args.padding_text else PREFIX
    prompt_text = prefix + prompt_text
    return prompt_text


PREPROCESSING_FUNCTIONS = {
    "ctrl": prepare_ctrl_input,
    "xlm": prepare_xlm_input,
    "xlnet": prepare_xlnet_input,
    "transfo-xl": prepare_transfoxl_input,
}


def adjust_length_to_model(length, max_sequence_length):
    if length < 0 and max_sequence_length > 0:
        length = max_sequence_length
    elif 0 < max_sequence_length < length:
        length = max_sequence_length  # No generation bigger than model size
    elif length < 0:
        length = MAX_LENGTH  # avoid infinite loop
    return length

def print_latency(latency_set, title="", warmup=1):
    # warmup queries
    latency_set = latency_set[warmup:]
    count = len(latency_set)
    if count > 0:
        latency_set.sort()
        n50 = (count - 1) * 0.5 + 1
        n90 = (count - 1) * 0.9 + 1
        n95 = (count - 1) * 0.95 + 1
        n99 = (count - 1) * 0.99 + 1
        n999 = (count - 1) * 0.999 + 1

        avg = sum(latency_set) / count
        p50 = latency_set[int(n50) - 1]
        p90 = latency_set[int(n90) - 1]
        p95 = latency_set[int(n95) - 1]
        p99 = latency_set[int(n99) - 1]
        p999 = latency_set[int(n999) - 1]

        print("====== latency stats {0} ======", title)
        print("\tAvg Latency: {0:8.2f} ms".format(avg * 1000))
        print("\tP50 Latency: {0:8.2f} ms".format(p50 * 1000))
        print("\tP90 Latency: {0:8.2f} ms".format(p90 * 1000))
        print("\tP95 Latency: {0:8.2f} ms".format(p95 * 1000))
        print("\tP99 Latency: {0:8.2f} ms".format(p99 * 1000))
        print("\t999 Latency: {0:8.2f} ms".format(p999 * 1000))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--model_type",
        default=None,
        type=str,
        required=True,
        help="Model type selected in the list: " + ", ".join(MODEL_CLASSES.keys()),
    )
    parser.add_argument(
        "--model_name_or_path",
        default=None,
        type=str,
        required=True,
        help="Path to pre-trained model or shortcut name selected in the list: " + ", ".join(MODEL_CLASSES.keys()),
    )
    parser.add_argument(
        "--sample_input",
        default=None,
        type=str,
        required=False,
        help="Path to pre-trained model or shortcut name selected in the list: " + ", ".join(MODEL_CLASSES.keys()),
    )

    parser.add_argument("--prompt", type=str, default="")
    parser.add_argument("--length", type=int, default=20)
    parser.add_argument("--stop_token", type=str, default=None, help="Token at which text generation is stopped")

    parser.add_argument(
        "--temperature",
        type=float,
        default=1.0,
        help="temperature of 1.0 has no effect, lower tend toward greedy sampling",
    )
    parser.add_argument(
        "--repetition_penalty", type=float, default=1.0, help="primarily useful for CTRL model; in that case, use 1.2"
    )
    parser.add_argument("--k", type=int, default=0)
    parser.add_argument("--p", type=float, default=0.9)

    parser.add_argument("--prefix", type=str, default="", help="Text added prior to input.")
    parser.add_argument("--padding_text", type=str, default="", help="Deprecated, the use of `--prefix` is preferred.")
    parser.add_argument("--xlm_language", type=str, default="", help="Optional language when used with the XLM model.")

    parser.add_argument("--local_rank", type=int, default=0, help="local rank")
    parser.add_argument("--seed", type=int, default=42, help="random seed for initialization")
    parser.add_argument("--no_cuda", action="store_true", help="Avoid using CUDA when available")
    parser.add_argument("--num_return_sequences", type=int, default=1, help="The number of samples to generate.")
    parser.add_argument(
        "--fp16",
        action="store_true",
        help="Whether to use 16-bit (mixed) precision (through NVIDIA apex) instead of 32-bit",
    )
    parser.add_argument('--ds-inference', action="store_true", help="Use deepspeed")
    parser.add_argument('--ds-zero-inference', action="store_true", help="Use deepspeed ZeRO")
    parser.add_argument('--ds_config_path', type=str, default="tmp_config.json", help="path to DeepSpeed ZeRO config")
    parser.add_argument("--batch_size", type=int, default=1, help="batch size")
    args = parser.parse_args()

    args.device = torch.device("cuda" if torch.cuda.is_available() and not args.no_cuda else "cpu")
    args.n_gpu = 0 if args.no_cuda else torch.cuda.device_count()

    logger.warning(
        "device: %s, n_gpu: %s, 16-bits training: %s",
        args.device,
        args.n_gpu,
        args.fp16,
    )

    set_seed(args)

    # Initialize the model and tokenizer
    try:
        args.model_type = args.model_type.lower()
        model_class, tokenizer_class = MODEL_CLASSES[args.model_type]
    except KeyError:
        raise KeyError("the model {} you specified is not supported. You are welcome to add it and open a PR :)")

    def _load_model(model_name_or_path):
        return model_class.from_pretrained(model_name_or_path, ignore_mismatched_sizes=True)

    # initialize deepspeed engine
    if args.ds_inference:
        model = _load_model(args.model_name_or_path) # model_class.from_pretrained(args.model_name_or_path)
        if args.fp16:
            model.half()
        model.cuda(torch.cuda.current_device())
        injection_policy={gpt2_transformer:
                          module_inject.replace_policy.HFGPT2LayerPolicy}
        model = deepspeed.init_inference(model,
                                         mp_size=1,
                                         dtype=(torch.half if args.fp16 else torch.float),
                                         injection_policy=injection_policy,
                                         replace_with_kernel_inject=True)
        model = model.module
    elif args.ds_zero_inference:
        ds_config_path = args.ds_config_path
        assert os.path.exists(ds_config_path), '{ds_config_path} does not exist'
        import json
        ds_config = json.load(open(ds_config_path, "r"))
        dschf = HfDeepSpeedConfig(ds_config)  # keep this object alive
        model = _load_model(args.model_name_or_path) # model_class.from_pretrained(args.model_name_or_path)

        # config = AutoConfig.from_pretrained(args.model_name_or_path)
        # model_hidden_size = config.n_embd

        def check_zero_ds_config(config):
            config_zero = config.get(ZERO_OPTIMIZATION, {})
            stage = config_zero.get(ZERO_OPTIMIZATION_STAGE, None)
            if stage != ZERO_OPTIMIZATION_WEIGHTS:
                assert False, "DeepSpeed ZeRO inference is only supported for ZeRO 3 optimization stage"
        check_zero_ds_config(ds_config)

        # initialise Deepspeed ZeRO and store only the engine object
        ds_engine = deepspeed.initialize(model=model,
                                         config_params=ds_config)[0]
        ds_engine.module.eval()  # inference
        model = ds_engine.module
        model.eval()

    else:
        model = _load_model(args.model_name_or_path) # model_class.from_pretrained(args.model_name_or_path)
        if args.fp16:
            model.half()
        model.cuda(torch.cuda.current_device())

    see_memory_usage(f'after model loaded', force=True)
    tokenizer = tokenizer_class.from_pretrained(args.model_name_or_path)
    tokenizer.padding_side = "left"
    # Define PAD Token = EOS Token = 50256
    tokenizer.pad_token = tokenizer.eos_token
    model.config.pad_token_id = model.config.eos_token_id

    logger.info(args)
    if args.sample_input:
        fname = open(args.sample_input, "r", encoding="utf8")
        prompt_text = fname.readlines()
    else:
        prompt_text = (args.prompt if args.prompt else input("Model prompt >>> "),)
    
    prompt_count = len(prompt_text)

    # Different models need different input formatting and/or extra arguments
    requires_preprocessing = args.model_type in PREPROCESSING_FUNCTIONS.keys()
    eprompt = []
    if requires_preprocessing:
        prepare_input = PREPROCESSING_FUNCTIONS.get(args.model_type)
        for input_text in prompt_text:
            preprocessed_prompt_text.append(prepare_input(args, model, tokenizer, prompt_text))

            if model.__class__.__name__ in ["TransfoXLLMHeadModel"]:
                tokenizer_kwargs = {"add_space_before_punct_symbol": True}
            else:
                tokenizer_kwargs = {}
            for ppt in preprocessed_prompt_text:
                eprompt.append(tokenizer.encode(
                    ppt, add_special_tokens=False, return_tensors="pt", **tokenizer_kwargs
                ))
    else:
        prefix = args.prefix if args.prefix else args.padding_text
        for ppt in prompt_text:
            eprompt.append(tokenizer.encode(prefix + ppt, add_special_tokens=False, return_tensors="pt"))

    # replicate the last prompt text for batch inference
    prompt_text = prompt_text[-1]

    input_ids = tokenizer([prompt_text]*args.batch_size, return_tensors="pt", padding=True).input_ids.cuda()
    latencies = []
    with torch.no_grad():
        for i in range(prompt_count):
            see_memory_usage(f'before generate {i}', force=True)
            torch.cuda.synchronize()
            t0 = time.time()

            output_sequences = model.generate(
                input_ids=input_ids,
                max_length=args.length + len(input_ids[0]),
                #max_new_tokens=args.length,
                min_length=args.length + len(input_ids[0]),
                do_sample=True
            )
            torch.cuda.synchronize()
            see_memory_usage(f'after generate {i}', force=True)
            latencies.append((time.time()-t0) / args.length / args.batch_size)
            generated_sequences = tokenizer.batch_decode(output_sequences, skip_special_tokens=True)

            if 0:
                for generated_sequence_idx, generated_sequence in enumerate(generated_sequences):
                    print("=== GENERATED SEQUENCE {} ===".format(generated_sequence_idx + 1))
                    print(generated_sequence)
    print_latency(latencies)
    return generated_sequences

if __name__ == "__main__":
    main()
