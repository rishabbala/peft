# coding=utf-8
# Copyright 2023-present the HuggingFace Inc. team.
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
import copy
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import List, Optional, Tuple, Union

import torch
import torch.nn as nn
import torch.nn.functional as F

from ..config import PeftConfig
from ..import_utils import is_bnb_4bit_available, is_bnb_available
from ..utils import (
    TRANSFORMERS_MODELS_TO_ADAMIX_TARGET_MODULES_MAPPING,
    ModulesToSaveWrapper,
    PeftType,
    _get_submodules,
)

if is_bnb_available():
    pass


@dataclass
class AdaMixConfig(PeftConfig):
    """
    This is the configuration class to store the configuration of a [`AdaMixModel`].

    Args:
        target_modules (`Union[List[str],str]`): The names of the modules to apply AdaMix to.
        adapter_dim (`int`): The hidden dim of the adapter (r in the paper). The downsampling adapter has shape dxr and the upsampling adapter has shape rxd where d is the hidden_dim of the model
        num_expert (`int`): The number of exprts per adapter module
        sharing_down (`bool`): If the weights of the downsampling adapters are shared in each layer
        sharing_up (`bool`): If the weights of the upsampling adapters are shared in each layer
        return_two_views (`bool`): If two stochastic forward passes have to be made, and both outputs returned
        fan_in_fan_out (`bool`): Set this to True if the layer to replace stores weight like (fan_in, fan_out).
        For example, gpt-2 uses `Conv1D` which stores weights like (fan_in, fan_out) and hence this should be set to `True`.:
        modules_to_save (`List[str]`):List of modules apart from (IA)^3 layers to be set as trainable
            and saved in the final checkpoint.
    """

    target_modules: Optional[Union[List[str], str]] = field(
        default=None,
        metadata={
            "help": "List of module names or regex expression of the module names to replace with ia3."
            "For example, ['q', 'v'] or '.*decoder.*(SelfAttention|EncDecAttention).*(q|v)$' "
        },
    )
    adapter_dim: Optional[int] = field(
        default=16,
        metadata={
            "help": "The hidden dim of the adapter (r in the paper). The downsampling adapter has shape dxr and the upsampling adapter has shape rxd where d is the hidden_dim of the model "
        },
    )
    num_expert: Optional[int] = field(
        default=4,
        metadata={"help": "The number of exprts per adapter module"},
    )
    sharing_down: Optional[bool] = field(
        default=False,
        metadata={"help": "If the weights of the downsampling adapters are shared in each layer"},
    )
    sharing_up: Optional[bool] = field(
        default=True,
        metadata={"help": "If the weights of the upsampling adapters are shared in each layer"},
    )
    return_two_views: Optional[bool] = field(
        default=False,
        metadata={"help": "If two stochastic forward passes have to be made"},
    )
    fan_in_fan_out: bool = field(
        default=False,
        metadata={"help": "Set this to True if the layer to replace stores weight like (fan_in, fan_out)"},
    )
    modules_to_save: Optional[List[str]] = field(
        default=None,
        metadata={
            "help": "List of modules apart from (IA)^3 layers to be set as trainable and saved in the final checkpoint. "
            "For example, in Sequence Classification or Token Classification tasks, "
            "the final layer `classifier/score` are randomly initialized and as such need to be trainable and saved."
        },
    )

    def __post_init__(self):
        self.peft_type = PeftType.ADAMIX


class AdaMixModel(torch.nn.Module):
    # TODO: Modify description
    """
    Creates a Infused Adapter by Inhibiting and Amplifying Inner Activations ((IA)^3) model from a pretrained
    transformers model. The method is described in detail in https://arxiv.org/abs/2205.05638

    Args:
        model ([`~transformers.PreTrainedModel`]): The model to be adapted.
        config ([`IA3Config`]): The configuration of the (IA)^3 model.

    Returns:
        `torch.nn.Module`: The (IA)^3 model.

    Example:

        ```py
        >>> from transformers import AutoModelForSeq2SeqLM, ia3Config
        >>> from peft import IA3Model, IA3Config

        >>> config = IA3Config(
        ...     peft_type="IA3",
        ...     task_type="SEQ_2_SEQ_LM",
        ...     target_modules=["k", "v", "w0"],
        ...     feedforward_modules=["w0"],
        ... )

        >>> model = AutoModelForSeq2SeqLM.from_pretrained("t5-base")
        >>> ia3_model = IA3Model(config, model)
        ```

    **Attributes**:
        - **model** ([`~transformers.PreTrainedModel`]) -- The model to be adapted.
        - **peft_config** ([`ia3Config`]): The configuration of the (IA)^3 model.
    """

    def __init__(self, model, config, adapter_name):
        super().__init__()
        self.model = model
        self.peft_config = config
        self.add_adapter(adapter_name, self.peft_config[adapter_name])
        self.forward = self.model.forward

    def add_adapter(self, adapter_name, config=None):
        if config is not None:
            model_config = self.model.config.to_dict() if hasattr(self.model.config, "to_dict") else self.model.config
            config = self._prepare_adamix_config(config, model_config)
            self.peft_config[adapter_name] = config
        self._find_and_replace(adapter_name)

        mark_only_adamix_as_trainable(self.model)
        if self.peft_config[adapter_name].inference_mode:
            _freeze_adapter(self.model, adapter_name)

    def _check_quantization_dependency(self):
        loaded_in_4bit = getattr(self.model, "is_loaded_in_4bit", False)
        if loaded_in_4bit:
            raise NotImplementedError(
                "4-bit quantization is not supported for AdaMix yet, 8-bit quantization can be used instead."
            )
        loaded_in_8bit = getattr(self.model, "is_loaded_in_8bit", False)
        if loaded_in_8bit and not is_bnb_available():
            raise ImportError(
                "To use AdaMix with 8-bit quantization, please install the `bitsandbytes` package. "
                "You can install it with `pip install bitsandbytes`."
            )

    def _create_new_module(self, adamix_config, adapter_name, target, dtype):
        if not hasattr(self.model.config, "hidden_size"):
            raise KeyError("Hidden size of the model must be known")

        new_module = ExpertSoup(
            self.model.config.hidden_size,
            adapter_name,
            adamix_config.adapter_dim,
            adamix_config.num_expert,
            adamix_config.sharing_down,
            adamix_config.sharing_up,
            adamix_config.return_two_views,
            adamix_config.inference_mode,
            dtype,
        )
        return new_module

    def _check_target_module_exists(self, adamix_config, key):
        if isinstance(adamix_config.target_modules, str):
            target_module_found = True if adamix_config.target_modules == key else False
        else:
            for target in adamix_config.target_modules:
                target_module_found = True if target == key else False
        return target_module_found

    def get_param_dtype(self, target):
        params = list(target.parameters())
        pos = 0
        while pos < len(params):
            if params[pos] is not None:
                dtype = params[pos].dtype
                return dtype
        raise ValueError("All parameters in the model are None")

    def _find_and_replace(self, adapter_name):
        adamix_config = self.peft_config[adapter_name]
        self._check_quantization_dependency()
        is_target_modules_in_base_model = False

        module_dict = dict(self.model.named_modules())
        for key in module_dict:
            module_dict[key] = type(module_dict[key]).__name__
            if not self._check_target_module_exists(adamix_config, module_dict[key]):
                continue

            if not is_target_modules_in_base_model:
                is_target_modules_in_base_model = True
            _, target, _ = _get_submodules(self.model, key)

            dtype = self.get_param_dtype(target)
            new_module = self._create_new_module(adamix_config, adapter_name, target, dtype)
            self._add_adapter_module(target, new_module)

        if not is_target_modules_in_base_model:
            raise ValueError(
                f"Target modules {adamix_config.target_modules} not found in the base model. "
                f"Please check the target modules and try again."
            )

    @staticmethod
    def _is_valid_match(key: str, target_key: str):
        """
        Helper function to match module names target_key and key. Makes sure that either the key is exactly the
        target_key or the target_key is a submodule of key
        """
        if key.endswith(target_key):
            if len(key) > len(target_key):
                return key.endswith("." + target_key)  # must be a sub module
            return True
        return False

    @staticmethod
    def composite_forward(f_out, f_in):
        def wrapper(hidden_states, *args, **kwargs):
            tuple_input = False
            hidden_states = f_in(hidden_states, *args, **kwargs)
            if isinstance(hidden_states, tuple):
                tuple_input = True
                hidden_states, other_args = hidden_states[0], hidden_states[1:]
            hidden_states = f_out(hidden_states)
            if tuple_input:
                return (hidden_states,) + other_args
            return hidden_states

        return wrapper

    def _add_adapter_module(self, target, new_module):
        target.add_module("adamix", new_module)
        target.forward = self.composite_forward(new_module.forward, target.forward)

    def __getattr__(self, name: str):
        """Forward missing attributes to the wrapped module."""
        try:
            return super().__getattr__(name)  # defer to nn.Module's logic
        except AttributeError:
            return getattr(self.model, name)

    def get_peft_config_as_dict(self, inference: bool = False):
        config_dict = {}
        for key, value in self.peft_config.items():
            config = {k: v.value if isinstance(v, Enum) else v for k, v in asdict(value).items()}
            if inference:
                config["inference_mode"] = True
        config_dict[key] = config
        return config

    def _set_adapter_layers(self, enabled=True):
        for module in self.model.modules():
            if isinstance(module, ExpertSoup):
                module.disable_adapters = False if enabled else True

    def enable_adapter_layers(self):
        self._set_adapter_layers(enabled=True)

    def disable_adapter_layers(self):
        self._set_adapter_layers(enabled=False)

    def set_adapter(self, adapter_name):
        for module in self.model.modules():
            if isinstance(module, ExpertSoup):
                module.active_adapter = adapter_name

    @staticmethod
    def _prepare_adamix_config(peft_config, model_config):
        if peft_config.target_modules is None:
            if model_config["model_type"] not in TRANSFORMERS_MODELS_TO_ADAMIX_TARGET_MODULES_MAPPING:
                raise ValueError("Please specify `target_modules` in `peft_config`")
            peft_config.target_modules = TRANSFORMERS_MODELS_TO_ADAMIX_TARGET_MODULES_MAPPING[
                model_config["model_type"]
            ]
        return peft_config

    def zero_adapters(self):
        r"""
        This method removes the added adapters. This is needed if someone wants to use the base model
        as a standalone model.
        """

        # if getattr(self.model, "is_loaded_in_8bit", False):
        #     raise ValueError("Cannot merge adamix layers when the model is loaded in 8-bit mode")

        key_list = [key for key, _ in self.model.named_modules() if "adamix" in key]
        for key in key_list:
            try:
                parent, target, target_name = _get_submodules(self.model, key)
            except AttributeError:
                continue
            if isinstance(target, ExpertSoup):
                delattr(parent, target_name)
        return self.model

    def get_two_view_from_model(self) -> Tuple[Tuple]:
        # NOTE: To perform backward pass on the two views, you must set retain_graph=True in the optimizer

        two_views = []
        for name, module in self.model.named_modules():
            if isinstance(module, ExpertSoup):
                two_views.append(module.two_views)

        if len(two_views) == 0:
            raise ValueError("Returned views are empty")

        return two_views


def mark_only_adamix_as_trainable(model: nn.Module) -> None:
    for n, p in model.named_parameters():
        if "adamix" not in n:
            p.requires_grad = False


# # Below code is based on https://github.com/microsoft/lora/blob/main/loralib/layers.py
# # and modified to work with PyTorch FSDP


# #  ------------------------------------------------------------------------------------------
# #  Copyright (c) Microsoft Corporation. All rights reserved.
# #  Licensed under the MIT License (MIT). See LICENSE in the repo root for license information.
# #  ------------------------------------------------------------------------------------------


class MixtureSoup(nn.Module):
    def __init__(self, expert, adapter_name, inference_mode=False, num_local_experts=1):
        super().__init__()
        self.inference_mode = inference_mode
        self.dict_keys = ["_".join([adapter_name, str(i)]) for i in range(num_local_experts)]
        self.expert_mixture = nn.ModuleDict({})
        for i in self.dict_keys:
            self.expert_mixture[i] = copy.deepcopy(expert)
            # torch.nn.init.kaiming_uniform_(self.expert_mixture[i].weight.data)
            # torch.nn.init.zeros_(self.expert_mixture[i].bias.data)
        self.num_local_experts = num_local_experts

    def get_expert_by_idx(self, idx):
        return self.expert_mixture[self.dict_keys[idx]]

    def expert_soup_forward(self, x):
        output = F.linear(x, self.parameter_dict["weight"], self.parameter_dict["bias"])
        return output

    def expert_soup(self):
        self.parameter_dict = {"weight": 0, "bias": 0}
        for idx in range(self.num_local_experts):
            single_expert = self.expert_mixture[idx]
            for s_name, s_param in single_expert.named_parameters():
                if "weight" in s_name:
                    p_name = "weight"
                else:
                    p_name = "bias"
                self.parameter_dict[p_name] = self.parameter_dict[p_name] + s_param / self.num_local_experts

    def forward(self, x: torch.Tensor):
        expert_output = None

        if not self.inference_mode:
            expert_idx = torch.randint(low=0, high=self.num_local_experts, size=(1,)).item()  # selected expert
            expert_output = self.get_expert_by_idx(expert_idx)(x)

        else:
            self.expert_soup()
            expert_output = self.expert_soup_forward(x)

        return expert_output


class ExpertSoup(nn.Module):
    def __init__(
        self,
        hidden_dim,
        adapter_name,
        adapter_dim,
        num_expert=4,
        sharing_down=False,
        sharing_up=True,
        return_two_views=False,
        inference_mode=False,
        dtype=torch.float16,
    ):
        super().__init__()

        self.disable_adapters = False
        self.return_two_views = return_two_views
        self.inference_mode = inference_mode
        if sharing_down:
            self.MoA_down = MixtureSoup(nn.Linear(hidden_dim, adapter_dim), adapter_name, self.inference_mode, 1)
        else:
            self.MoA_down = MixtureSoup(
                nn.Linear(hidden_dim, adapter_dim), adapter_name, self.inference_mode, num_expert
            )

        if sharing_up:
            self.MoA_up = MixtureSoup(nn.Linear(adapter_dim, hidden_dim), adapter_name, self.inference_mode, 1)
        else:
            self.MoA_up = MixtureSoup(
                nn.Linear(adapter_dim, hidden_dim), adapter_name, self.inference_mode, num_expert
            )

        self.two_views = []
        for p in self.parameters():
            p.data = p.data.to(dtype)

    # NOTE: During training, you must forward pass the input twice to get two outputs and apply the KL div minimization
    # Use the first ouput as input to next layer
    # During inference, only one forward pass is required
    def forward(self, x):
        if not self.disable_adapters:
            result1 = F.gelu(self.MoA_down(x))
            result1 = self.MoA_up(result1)
            result1 = result1 + x

            if self.training and self.return_two_views:
                result2 = F.gelu(self.MoA_down(x))
                result2 = self.MoA_up(result2)
                result2 = result2 + x
                self.two_views = torch.stack([result1, result2], dim=0)
            return result1
        else:
            return x
