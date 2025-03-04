import asyncio
import json
import logging
import os
import pickle
from math import ceil
from random import random
import time
import anthropic
import openai
import tiktoken
from tqdm import tqdm
from tqdm.asyncio import tqdm_asyncio
import transformers
from vllm import LLM, SamplingParams
import numpy as np
import torch

# openai.api_key = os.environ["OPENAI_API_KEY"]
# ANTHROPIC_CLIENT = anthropic.AsyncAnthropic()

HISTORY_FILE = "history.jsonl"
CACHE_FILE = "query_cache.pkl"
EXP_CAP = 4

logger = logging.getLogger(__name__)

MODEL2COST = {
    "gpt-3.5-turbo-0613": {"input": 0.0015, "output": 0.002},
    "gpt-4-0613": {"input": 0.03, "output": 0.06},
}

MODEL2BATCH_SIZE = {
    "gpt-3.5-turbo-0613": 500,
    "gpt-4-0613": 500,
    "claude-2": 100,
    "Meta-Llama-3-8B-Instruct": 500,
}

GPT_MODELS = {"gpt-3.5-turbo-0613", "gpt-4-0613"}
CLAUDE_MODELS = {"claude-2"}
VLLM_MODELS = {"Meta-Llama-3-8B-Instruct", "Meta-Llama-3-70B-Instruct"}
TRANSFORMERS_MODELS = {"Meta-Llama-3-8B-Instruct", "Meta-Llama-3-70B-Instruct"}

GLOBAL_VLLM_MODEL = None
GLOBAL_TRANSFORMERS_MODEL = None
GLOBAL_TOKENIZER = None

async def query_openai(
    prompt,
    model_name,
    system_msg,
    history,
    max_tokens=None,
    temperature=0,
    retry=100,
    n=1,
    history_file=HISTORY_FILE,
    **kwargs,
):
    # reference: https://github.com/ekinakyurek/mylmapis/blob/b0adb192135898fba9e9dc88f09a18dc64c1f1a9/src/network_manager.py
    messages = []
    if system_msg is not None:
        messages += [{"role": "system", "content": system_msg}]
    if history is not None:
        messages += history
    messages += [{"role": "user", "content": prompt}]
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    kwargs["temperature"] = temperature
    kwargs["n"] = n

    for i in range(retry + 1):
        wait_time = (1 << min(i, EXP_CAP)) + random() / 10
        try:
            response = await openai.ChatCompletion.acreate(
                model=model_name, messages=messages, **kwargs
            )
            with open(history_file, "a") as f:
                f.write(json.dumps((model_name, messages, kwargs, response)) + "\n")
            if any(choice["finish_reason"] != "stop" for choice in response["choices"]):
                print("Truncated response!")
                print(response)
            contents = [choice["message"]["content"] for choice in response["choices"]]
            if n == 1:
                return contents[0]
            else:
                return contents
        except (
            openai.error.APIError,
            openai.error.TryAgain,
            openai.error.Timeout,
            openai.error.APIConnectionError,
            openai.error.ServiceUnavailableError,
            openai.error.RateLimitError,
        ) as e:
            if i == retry:
                raise e
            else:
                await asyncio.sleep(wait_time)
        except openai.error.InvalidRequestError as e:
            logger.error(e)
            if n == 1:
                return ""
            else:
                return [""] * n


async def query_anthropic(
    prompt,
    model_name,
    system_msg,
    history,
    max_tokens=9000,
    temperature=0,
    retry=100,
    history_file=HISTORY_FILE,
    **kwargs,
):
    assert system_msg is None
    prompt_history = []
    prompt = f"{anthropic.HUMAN_PROMPT} {prompt} {anthropic.AI_PROMPT}"
    messages = []
    if history is not None:
        prompt_history = ""
        for his in history:
            if his["role"] == "user":
                prompt_history += f"{anthropic.HUMAN_PROMPT} {his['content']} "
            elif his["role"] == "assistant":
                prompt_history += f"{anthropic.AI_PROMPT} {his['content']} "
        prompt = prompt_history + prompt
    messages += [{"role": "user", "content": prompt}]
    if max_tokens is None:
        max_tokens = 9000
    kwargs["max_tokens"] = max_tokens
    kwargs["temperature"] = temperature

    for i in range(retry + 1):
        wait_time = (1 << min(i, EXP_CAP)) + random() / 10
        try:
            response = await ANTHROPIC_CLIENT.completions.create(
                prompt=prompt,
                stop_sequences=[anthropic.HUMAN_PROMPT],
                model=model_name,
                max_tokens_to_sample=max_tokens,
                temperature=temperature,
            )
            with open(history_file, "a") as f:
                f.write(
                    json.dumps((model_name, messages, kwargs, response.completion))
                    + "\n"
                )
            if response.stop_reason != "stop_sequence":
                print("Truncated response!")
                print(response)
            return response.completion.lstrip()
        except anthropic.BadRequestError as e:
            logger.error(e)
            return ""
        except anthropic.APIError as e:
            if i == retry:
                raise e
            else:
                await asyncio.sleep(wait_time)

async def query_vllm(
    prompt,
    model_name,
    system_msg,
    history,
    max_tokens=None,
    temperature=0,
    retry=100,
    n=1,
    history_file=HISTORY_FILE,
    **kwargs,  
):
    # reference: https://github.com/ekinakyurek/mylmapis/blob/b0adb192135898fba9e9dc88f09a18dc64c1f1a9/src/network_manager.py
    messages = []
    if system_msg is not None:
        messages += [{"role": "system", "content": system_msg}]
    if history is not None:
        messages += history
    messages += [{"role": "user", "content": prompt}]
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    kwargs["temperature"] = temperature
    kwargs["n"] = n

    for i in range(retry + 1):
        from openai import OpenAI
        openai_api_key = "EMPTY"
        openai_api_base = "http://localhost:8000/v1"
        client = OpenAI(
            api_key=openai_api_key,
            base_url=openai_api_base,
        )
        response = client.chat.completions.create(
            model=model_name, messages=messages, **kwargs
        ).to_dict()
        with open(history_file, "a") as f:
            f.write(json.dumps((model_name, messages, kwargs, response)) + "\n")
        if any(choice["finish_reason"] != "stop" for choice in response["choices"]):
            print("Truncated response!")
            print(response)
        contents = [choice["message"]["content"] for choice in response["choices"]]
        if n == 1:
            return contents[0]
        else:
            return contents
        
async def query_transformers_model_direct(
    prompt,
    model_name,
    system_msg,
    history,
    max_tokens=None,
    temperature=0,
    retry=100,
    n=1,
    history_file=HISTORY_FILE,
    **kwargs,  
):
    # reference: https://github.com/ekinakyurek/mylmapis/blob/b0adb192135898fba9e9dc88f09a18dc64c1f1a9/src/network_manager.py
    messages = []
    if system_msg is not None:
        messages += [{"role": "system", "content": system_msg}]
    if history is not None:
        messages += history
    messages += [{"role": "user", "content": prompt}]
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    kwargs["temperature"] = temperature
    kwargs["n"] = n
    terminators = [
        GLOBAL_TRANSFORMERS_MODEL.tokenizer.eos_token_id,
        GLOBAL_TRANSFORMERS_MODEL.tokenizer.convert_tokens_to_ids("<|eot_id|>")
    ]

    outputs = GLOBAL_TRANSFORMERS_MODEL(
        messages,
        max_new_tokens=max_tokens,
        eos_token_id=terminators,
        do_sample=True if temperature != 0 else False,
        temperature=temperature,
        num_return_sequences=n
    )

    with open(history_file, "a") as f:
        f.write(json.dumps((model_name, messages, kwargs, outputs)) + "\n")
    contents = [output['generated_text'][-1]['content'] for output in outputs]
    if n == 1:
        return contents[0]
    else:
        return contents
        
def query_vllm_model_direct(
    prompt,
    model_name,
    system_msg,
    history,
    max_tokens=None,
    temperature=0,
    retry=100,
    n=1,
    history_file=HISTORY_FILE,
    **kwargs, 
):  
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    kwargs["temperature"] = temperature
    kwargs["n"] = n
    messages = []
    for _ in range(len(prompt)):
        message = []
        if system_msg is not None:
            message += [{"role": "system", "content": system_msg}]
        if history[_] is not None:
            message += history[_]
        message += [{"role": "user", "content": prompt[_]}]
        for __ in range(n):
            messages.append(message)
    prompts = [GLOBAL_TOKENIZER.apply_chat_template(_, tokenize=False) for _ in messages]
    sampling_params = SamplingParams(temperature=temperature, max_tokens=max_tokens)
    responses = GLOBAL_VLLM_MODEL.generate(prompts, sampling_params)
    responses = [_.outputs[0].text.replace("<|start_header_id|>assistant<|end_header_id|>\n\n", "") for _ in responses]
    with open(history_file, "a") as f:
        for _ in range(len(prompt)):
            f.write(json.dumps((model_name, messages[_ * n], kwargs, responses[_])) + "\n")
    if n == 1:
        return responses
    else:
        return np.array(responses).reshape(len(responses) // n, n)

# def query_transformers_model_direct(
#     prompt,
#     model_name,
#     system_msg,
#     history,
#     max_tokens=None,
#     temperature=0,
#     retry=100,
#     n=1,
#     history_file=HISTORY_FILE,
#     **kwargs, 
# ):  
#     if max_tokens is not None:
#         kwargs["max_tokens"] = max_tokens
#     kwargs["temperature"] = temperature
#     kwargs["n"] = n
#     messages = []
#     for _ in range(len(prompt)):
#         message = []
#         if system_msg is not None:
#             message += [{"role": "system", "content": system_msg}]
#         if history[_] is not None:
#             message += history[_]
#         message += [{"role": "user", "content": prompt[_]}]
#         for __ in range(n):
#             messages.append(message)
#     prompts = [GLOBAL_TOKENIZER.apply_chat_template(_, tokenize=False) for _ in messages]
#     sampling_params = SamplingParams(temperature=temperature, max_tokens=max_tokens)
#     responses = GLOBAL_VLLM_MODEL.generate(prompts, sampling_params)
#     responses = [_.outputs[0].text.replace("<|start_header_id|>assistant<|end_header_id|>\n\n", "") for _ in responses]
#     with open(history_file, "a") as f:
#         for _ in range(len(prompt)):
#             f.write(json.dumps((model_name, messages[_ * n], kwargs, responses[_])) + "\n")
#     if n == 1:
#         return responses
#     else:
#         return np.array(responses).reshape(len(responses) // n, n)
    
def init_global_vllm_model(
    model_name,
    tensor_parallel_size
):
    global GLOBAL_VLLM_MODEL, GLOBAL_TOKENIZER
    GLOBAL_VLLM_MODEL = LLM(model=model_name, tensor_parallel_size=tensor_parallel_size)
    GLOBAL_TOKENIZER = transformers.AutoTokenizer.from_pretrained(model_name)

def init_global_transformers_model(
    model_name,
):
    global GLOBAL_TRANSFORMERS_MODEL, GLOBAL_TOKENIZER
    GLOBAL_TRANSFORMERS_MODEL = transformers.pipeline(
        "text-generation",
        model=model_name,
        model_kwargs={"torch_dtype": torch.bfloat16},
        device_map="auto",
    )
    GLOBAL_TOKENIZER = transformers.AutoTokenizer.from_pretrained(model_name)

def query_batch_wrapper(
    fn, prompts, model_name, system_msg, histories, *args, **kwargs
):
    async def _query(prompts, histories):
        async_responses = [
            fn(prompt, model_name, system_msg, his, *args, **kwargs)
            for prompt, his in zip(prompts, histories)
        ]
        return await tqdm_asyncio.gather(*async_responses)

    all_results = asyncio.run(_query(prompts, histories))
    return all_results


def query_batch(
    prompts,
    model_name,
    system_msg=None,
    histories=None,
    max_tokens=None,
    temperature=0,
    retry=100,
    num_beams=1,
    skip_cache=False,
    n=1,
    cache_file=CACHE_FILE,
    history_file=HISTORY_FILE,
    **openai_kwargs,
):
    cache = {}
    if not skip_cache and os.path.exists(cache_file):
        cache = pickle.load(open(cache_file, "rb"))

    prompt2key = lambda p, h: (
        p,
        model_name,
        system_msg,
        tuple([tuple(e.items()) for e in h]) if h is not None else None,
        max_tokens,
        temperature,
        num_beams,
        n,
    )

    unseen_prompt_pairs = set()
    if histories is None:
        histories = [None] * len(prompts)
    for prompt, his in zip(prompts, histories):
        key = prompt2key(prompt, his)
        if (
            (key not in cache)
            or (key in cache and n == 1 and cache[key] is None)  # previous call failed
            or (key in cache and n > 1 and None in cache[key])
        ):
            history_cache_key = tuple([tuple(e.items()) for e in his]) if his else None
            prompt_key = prompt if isinstance(prompt, str) else tuple(prompt)
            unseen_prompt_pairs.add((prompt_key, history_cache_key))
    # # import pdb; pdb.set_trace()
    unseen_prompts = []
    unseen_histories = []
    for prompt, his in unseen_prompt_pairs:
        unseen_prompts.append(prompt)
        if his is not None:
            his = [dict(e) for e in his]
        unseen_histories.append(his)

    if len(unseen_prompts) > 0:
        logger.info("History:")
        logger.info(unseen_histories[0])
        logger.info("Prompt:")
        logger.info(unseen_prompts[0])
        logger.info(f"Calling {model_name} for {len(unseen_prompts)} prompts")

        num_calls_per_n = 1 if model_name in GPT_MODELS else n
        batch_size = ceil(MODEL2BATCH_SIZE[model_name if "/" not in model_name else model_name.split("/")[-1]] / num_calls_per_n)

        total_batches = ceil(len(unseen_prompts) / batch_size)
        for start in tqdm(
            range(0, len(unseen_prompts), batch_size),
            desc="Querying batch",
            total=total_batches,
        ):
            unseen_prompts_batch = unseen_prompts[start : start + batch_size]
            unseen_histories_batch = unseen_histories[start : start + batch_size]
            
            if GLOBAL_TRANSFORMERS_MODEL != None:
                responses = query_batch_wrapper(
                    query_transformers_model_direct,
                    unseen_prompts_batch,
                    model_name,
                    system_msg,
                    unseen_histories_batch,
                    max_tokens,
                    temperature,
                    retry,
                    n,
                    history_file,
                    **openai_kwargs,
                )
            elif GLOBAL_VLLM_MODEL != None:
                responses = query_vllm_model_direct(
                    unseen_prompts_batch,
                    model_name,
                    system_msg,
                    unseen_histories_batch,
                    max_tokens,
                    temperature,
                    retry,
                    n,
                    history_file,
                    **openai_kwargs,
                )
            elif model_name in GPT_MODELS:
                responses = query_batch_wrapper(
                    query_openai,
                    unseen_prompts_batch,
                    model_name,
                    system_msg,
                    unseen_histories_batch,
                    max_tokens,
                    temperature,
                    retry,
                    n,
                    history_file,
                    **openai_kwargs,
                )
            elif model_name in VLLM_MODELS:
                responses = query_batch_wrapper(
                    query_vllm,
                    unseen_prompts_batch,
                    model_name,
                    system_msg,
                    unseen_histories_batch,
                    max_tokens,
                    temperature,
                    retry,
                    n,
                    history_file,
                    **openai_kwargs,
                )
            elif model_name in CLAUDE_MODELS:
                assert system_msg is None
                if n > 1:
                    num_prompts = len(unseen_prompts_batch)
                    orig_unseen_prompts_batch = unseen_prompts_batch
                    unseen_prompts_batch = [
                        prompt for prompt in unseen_prompts_batch for _ in range(n)
                    ]
                    orig_unseen_histories_batch = unseen_histories_batch
                    unseen_histories_batch = [
                        his for his in unseen_histories_batch for _ in range(n)
                    ]
                responses = query_batch_wrapper(
                    query_anthropic,
                    unseen_prompts_batch,
                    model_name,
                    system_msg,
                    unseen_histories_batch,
                    max_tokens,
                    temperature,
                    retry,
                    **openai_kwargs,
                )
                if n > 1:
                    responses = [
                        responses[i : i + n] for i in range(0, len(responses), n)
                    ]
                    assert (
                        len(responses) * n
                        == num_prompts * n
                        == sum(len(r) for r in responses)
                        == len(unseen_prompts_batch)
                        == len(unseen_histories_batch)
                    )
                    unseen_prompts_batch = orig_unseen_prompts_batch
                    unseen_histories_batch = orig_unseen_histories_batch
            else:
                raise NotImplementedError

            # Reload cache for better concurrency. Otherwise multiple query processes can overwrite
            # each other
            # import pdb; pdb.set_trace()
            cache = {}
            if not skip_cache:
                if os.path.exists(cache_file):
                    cache = pickle.load(open(cache_file, "rb"))
            for prompt, his, response in zip(
                unseen_prompts_batch, unseen_histories_batch, responses
            ):
                key = prompt2key(prompt, his)
                cache[key] = response
            if not skip_cache:
                pickle.dump(cache, open(cache_file, "wb"))

    return [cache[prompt2key(prompt, his)] for prompt, his in zip(prompts, histories)]


def get_cost(input, outputs, model_name=None, history=None):
    if model_name not in MODEL2COST:
        return 0
    costs = MODEL2COST[model_name]
    enc = tiktoken.encoding_for_model(model_name)
    input_tokens = len(enc.encode(input))
    if isinstance(outputs, str):
        outputs = [outputs]
    for output in outputs:
        output_tokens = len(enc.encode(output))
        cost = input_tokens * costs["input"] + output_tokens * costs["output"]
        if history is not None:
            for his in history:
                if his["role"] == "user":
                    cost += len(enc.encode(his["content"])) * costs["input"]
                elif his["role"] == "assistant":
                    cost += len(enc.encode(his["content"])) * costs["input"]
    return cost / 1000


def format_history(query, response):
    return [
        {"role": "user", "content": query},
        {"role": "assistant", "content": response},
    ]
