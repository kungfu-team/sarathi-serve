# coding=utf-8
# Adapted from
# https://github.com/huggingface/transformers/blob/a5cc30d72ae2dc19af534e4b35c986cc28db1275/src/transformers/models/falcon/modeling_falcon.py
# Copyright 2023 The Sarathi team.
# Copyright 2023 the Falcon authors and HuggingFace Inc. team.  All rights
# reserved.
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
"""PyTorch Falcon model."""

import math
from typing import List, Optional, Union

import torch
from torch import nn
from torch.nn import LayerNorm
from transformers import FalconConfig as HF_FalconConfig

from sarathi.metrics.constants import OperationMetrics
from sarathi.metrics.cuda_timer import CudaTimer
from sarathi.model_executor.attention import get_attention_wrapper
from sarathi.model_executor.layers.rotary_embedding import get_rope
from sarathi.model_executor.parallel_utils.parallel_state import (
    get_pipeline_model_parallel_rank,
    get_pipeline_model_parallel_world_size,
    get_tensor_model_parallel_rank,
    get_tensor_model_parallel_world_size,
    is_pipeline_first_stage,
    is_pipeline_last_stage,
)
from sarathi.model_executor.parallel_utils.pipeline_parallel.mappings import recv, send
from sarathi.model_executor.parallel_utils.tensor_parallel import (
    ColumnParallelLinear,
    RowParallelLinear,
    VocabParallelEmbedding,
    reduce_from_tensor_model_parallel_region,
)
from sarathi.model_executor.weight_utils import (
    convert_pyslice_to_tensor,
    hf_model_weights_iterator,
    load_tensor_parallel_weights,
)
from sarathi.transformers_utils.configs import RWConfig
from sarathi.worker.cache_engine import KVCache

FalconConfig = Union[HF_FalconConfig, RWConfig]


# NOTE(Hesslow): Unfortunately we did not fuse matmul and bias during
# training, this means that there's one additional quantization to bfloat16
# between the operations. In order not to degrade the quality of our HF-port,
# we keep these characteristics in the final model.
class FalconLinear(nn.Linear):

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        hidden_states = x @ self.weight.T
        if self.bias is None:
            return hidden_states
        return hidden_states + self.bias


class FalconAttention(nn.Module):

    def __init__(self, config: FalconConfig):
        super().__init__()

        self.hidden_size = config.hidden_size
        tp_size = get_tensor_model_parallel_world_size()

        self.total_num_heads = config.num_attention_heads
        assert self.total_num_heads % tp_size == 0
        self.num_heads = self.total_num_heads // tp_size
        self.head_dim = self.hidden_size // self.total_num_heads
        assert self.head_dim * self.total_num_heads == self.hidden_size

        self.new_decoder_architecture = config.new_decoder_architecture
        self.multi_query = config.multi_query

        if self.new_decoder_architecture:
            self.total_num_kv_heads = config.num_kv_heads
            assert self.total_num_heads % tp_size == 0
            self.num_kv_heads = self.total_num_kv_heads // tp_size
            self.query_key_value = ColumnParallelLinear(
                self.hidden_size,
                (self.total_num_heads + 2 * self.total_num_kv_heads) * self.head_dim,
                bias=config.bias,
                gather_output=False,
                perform_initialization=False,
                skip_bias_add=True,
                linear_metric_name=OperationMetrics.ATTN_PRE_PROJ,
                communication_metric_name=OperationMetrics.ATTN_PRE_PROJ_ALL_GATHER,
            )
        elif self.multi_query:
            self.total_num_kv_heads = 1
            self.num_kv_heads = 1
            self.query = ColumnParallelLinear(
                self.hidden_size,
                self.total_num_heads * self.head_dim,
                bias=config.bias,
                gather_output=False,
                perform_initialization=False,
                skip_bias_add=True,
            )
            self.key_value = FalconLinear(
                self.hidden_size, 2 * self.head_dim, bias=config.bias
            )
        else:
            self.total_num_kv_heads = self.total_num_heads
            self.num_kv_heads = self.num_heads
            self.query_key_value = ColumnParallelLinear(
                self.hidden_size,
                (self.total_num_heads + 2 * self.total_num_kv_heads) * self.head_dim,
                bias=config.bias,
                gather_output=False,
                perform_initialization=False,
                skip_bias_add=True,
            )

        self.q_size = self.num_heads * self.head_dim
        self.kv_size = self.num_kv_heads * self.head_dim

        # Layer-wise attention scaling
        self.inv_norm_factor = 1.0 / math.sqrt(self.head_dim)
        self.reduce_row_parallel_results = not (
            config.new_decoder_architecture or config.parallel_attn
        )
        self.dense = RowParallelLinear(
            self.hidden_size,
            self.hidden_size,
            bias=config.bias,
            input_is_parallel=True,
            perform_initialization=False,
            skip_bias_add=True,
            reduce_results=self.reduce_row_parallel_results,
            linear_metric_name=OperationMetrics.ATTN_POST_PROJ,
            communication_metric_name=OperationMetrics.ATTN_POST_PROJ_ALL_REDUCE,
        )

        self.use_rotary = config.rotary
        self.use_alibi = config.alibi
        assert not (
            self.use_rotary and self.use_alibi
        ), "Rotary and alibi are mutually exclusive."

        if self.use_rotary:
            rope_theta = getattr(config, "rope_theta", 10000)
            max_position_embeddings = getattr(config, "max_position_embeddings", 8192)
            rope_scaling = getattr(config, "rope_scaling", None)
            self.rotary_emb = get_rope(
                head_size=self.head_dim,
                rotary_dim=self.head_dim,
                max_position=max_position_embeddings,
                base=rope_theta,
                is_neox_style=True,
                rope_scaling=rope_scaling,
            )
            self._attn_rope_timer = CudaTimer(OperationMetrics.ATTN_ROPE)
        elif self.use_alibi:
            raise NotImplementedError("ALiBi is not yet supported.")
        else:
            raise NotImplementedError("Standard attention is not yet supported.")

    def forward(
        self,
        positions: torch.Tensor,
        hidden_states: torch.Tensor,
        kv_cache: KVCache,
    ) -> torch.Tensor:
        if not self.new_decoder_architecture and self.multi_query:
            q, bias = self.query(hidden_states)
            if bias is not None:
                q += bias
            kv = self.key_value(hidden_states)
            k, v = kv.split([self.kv_size, self.kv_size], dim=-1)
        else:
            qkv, bias = self.query_key_value(hidden_states)
            if bias is not None:
                qkv += bias
            q, k, v = qkv.split([self.q_size, self.kv_size, self.kv_size], dim=-1)
        if self.use_rotary:
            with self._attn_rope_timer:
                q, k = self.rotary_emb(positions, q, k)

        attn_output = get_attention_wrapper().forward(
            q,
            k,
            v,
            kv_cache,
            self.inv_norm_factor,
        )
        attn_output, bias = self.dense(attn_output)
        return attn_output, bias


class FalconMLP(nn.Module):

    def __init__(self, config: FalconConfig):
        super().__init__()
        hidden_size = config.hidden_size

        self.dense_h_to_4h = ColumnParallelLinear(
            hidden_size,
            4 * hidden_size,
            bias=config.bias,
            gather_output=False,
            perform_initialization=False,
            skip_bias_add=True,
            linear_metric_name=OperationMetrics.MLP_UP_PROJ,
            communication_metric_name=OperationMetrics.MLP_UP_PROJ_ALL_GATHER,
        )
        self.act = nn.GELU()
        self.reduce_row_parallel_results = not (
            config.new_decoder_architecture or config.parallel_attn
        )
        self.dense_4h_to_h = RowParallelLinear(
            4 * hidden_size,
            hidden_size,
            bias=config.bias,
            input_is_parallel=True,
            perform_initialization=False,
            skip_bias_add=True,
            reduce_results=self.reduce_row_parallel_results,
            linear_metric_name=OperationMetrics.MLP_DOWN_PROJ,
            communication_metric_name=OperationMetrics.MLP_DOWN_PROJ_ALL_REDUCE,
        )
        self._mlp_activation_timer = CudaTimer(OperationMetrics.MLP_ACTIVATION)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # NOTE(zhuohan): Following huggingface, we do not fuse bias add here.
        x, bias = self.dense_h_to_4h(x)
        if bias is not None:
            x += bias
        with self._mlp_activation_timer:
            x = self.act(x)
        x, bias = self.dense_4h_to_h(x)
        return x, bias


class FalconDecoderLayer(nn.Module):

    def __init__(self, config: FalconConfig):
        super().__init__()
        hidden_size = config.hidden_size
        self.num_heads = config.num_attention_heads
        self.self_attention = FalconAttention(config)
        self.mlp = FalconMLP(config)
        self.config = config

        if config.new_decoder_architecture:
            # The layer norm before self-attention
            self.ln_attn = LayerNorm(hidden_size, eps=config.layer_norm_epsilon)
            # The layer norm before the MLP
            self.ln_mlp = LayerNorm(hidden_size, eps=config.layer_norm_epsilon)
        else:
            self.input_layernorm = LayerNorm(hidden_size, eps=config.layer_norm_epsilon)
            if not config.parallel_attn:
                self.post_attention_layernorm = LayerNorm(
                    hidden_size, eps=config.layer_norm_epsilon
                )

        self.reduce_row_parallel_results = not (
            config.new_decoder_architecture or config.parallel_attn
        )

    def forward(
        self,
        positions: torch.Tensor,
        hidden_states: torch.Tensor,
        kv_cache: KVCache,
    ):
        residual = hidden_states

        if self.config.new_decoder_architecture:
            attention_layernorm_out = self.ln_attn(hidden_states)
            mlp_layernorm_out = self.ln_mlp(hidden_states)
        else:
            attention_layernorm_out = self.input_layernorm(hidden_states)

        # Self attention.
        attention_output, attention_bias = self.self_attention(
            positions=positions,
            hidden_states=attention_layernorm_out,
            kv_cache=kv_cache,
        )
        if self.reduce_row_parallel_results and attention_bias is not None:
            attention_output += attention_bias

        if not self.config.new_decoder_architecture:
            if self.config.parallel_attn:
                mlp_layernorm_out = attention_layernorm_out
            else:
                residual += attention_output
                mlp_layernorm_out = self.post_attention_layernorm(residual)

        # MLP.
        mlp_output, mlp_bias = self.mlp(mlp_layernorm_out)
        if self.reduce_row_parallel_results and mlp_bias is not None:
            mlp_output += mlp_bias

        if not self.reduce_row_parallel_results:
            # When MLP and Attention layers are parallel, we can use
            # only one all-reduce operator to reduce the results from
            # both MLP and Attention layers.
            mlp_output += attention_output
            mlp_output = reduce_from_tensor_model_parallel_region(mlp_output)
            if attention_bias is not None:
                mlp_output += attention_bias
            if mlp_bias is not None:
                mlp_output += mlp_bias

        output = mlp_output + residual

        return output


class FalconModel(nn.Module):

    def __init__(self, config: FalconConfig):
        super().__init__()
        self.config = config
        self.embed_dim = config.hidden_size
        self.num_heads = config.num_attention_heads
        self.use_alibi = config.alibi

        # Embedding + LN Embedding
        self.word_embeddings = None
        if is_pipeline_first_stage():
            self.word_embeddings = VocabParallelEmbedding(
                config.vocab_size,
                self.embed_dim,
                perform_initialization=False,
                linear_metric_name=OperationMetrics.EMBED_LINEAR,
                communication_metric_name=OperationMetrics.EMBED_ALL_REDUCE,
            )

        # Transformer blocks
        self.h = nn.ModuleList(
            [
                FalconDecoderLayer(config)
                for _ in range(
                    config.num_hidden_layers // get_pipeline_model_parallel_world_size()
                )
            ]
        )

        # Final Layer Norm
        self.ln_f = None
        if is_pipeline_last_stage():
            self.ln_f = LayerNorm(self.embed_dim, eps=config.layer_norm_epsilon)

    def forward(
        self,
        hidden_states: torch.Tensor,
        positions: torch.Tensor,
        kv_caches: List[KVCache],
    ) -> torch.Tensor:
        if self.word_embeddings:
            hidden_states = self.word_embeddings(hidden_states)

        for i in range(len(self.h)):
            layer = self.h[i]
            hidden_states = layer(
                positions,
                hidden_states,
                kv_caches[i],
            )
        if self.ln_f:
            hidden_states = self.ln_f(hidden_states)
        return hidden_states


class FalconForCausalLM(nn.Module):

    def __init__(self, config: FalconConfig):
        super().__init__()
        self.config = config

        self.is_pipeline_first_stage = is_pipeline_first_stage()
        self.is_pipeline_last_stage = is_pipeline_last_stage()

        self.transformer = FalconModel(config)

        self.lm_head = None
        if self.is_pipeline_last_stage:
            self.lm_head = ColumnParallelLinear(
                config.hidden_size,
                config.vocab_size,
                bias=False,
                gather_output=False,
                perform_initialization=False,
            )

    def forward(
        self,
        hidden_states: torch.Tensor,
        positions: torch.Tensor,
        kv_caches: List[KVCache],
    ) -> torch.Tensor:
        if not self.is_pipeline_first_stage:
            # hidden_states_shape: num_tokens x hidden_size
            hidden_states = torch.empty(
                (positions.shape[0], self.config.hidden_size),
                dtype=self.config.dtype,
                device=hidden_states.device,
            )
            hidden_states = recv(hidden_states)

        hidden_states = self.transformer(hidden_states, positions, kv_caches)

        if not self.is_pipeline_last_stage:
            send(hidden_states)

        return hidden_states

    _column_parallel_weights = [
        "word_embeddings.weight",
        "lm_head.weight",
        "dense_h_to_4h.weight",
        "dense_h_to_4h.bias",
    ]
    _row_parallel_weights = ["dense.weight", "dense_4h_to_h.weight"]

    def load_weights(
        self,
        model_name_or_path: str,
        cache_dir: Optional[str] = None,
        load_format: str = "auto",
        revision: Optional[str] = None,
    ):
        tp_size = get_tensor_model_parallel_world_size()
        tp_rank = get_tensor_model_parallel_rank()
        pp_size = get_pipeline_model_parallel_world_size()
        pp_rank = get_pipeline_model_parallel_rank()

        assert self.config.num_hidden_layers % pp_size == 0
        layers_per_stage = self.config.num_hidden_layers // pp_size

        first_layer_id = layers_per_stage * pp_rank
        last_layer_id = layers_per_stage * (pp_rank + 1) - 1

        hidden_size = self.config.hidden_size
        total_num_heads = self.config.num_attention_heads
        num_heads = total_num_heads // tp_size
        head_size = hidden_size // total_num_heads
        head_start = tp_rank * num_heads
        head_end = (tp_rank + 1) * num_heads
        if self.config.new_decoder_architecture:
            total_num_kv_heads = self.config.num_kv_heads
            num_kv_heads = total_num_kv_heads // tp_size
            separated_q_kv = False
            kv_head_start = tp_rank * num_kv_heads
            kv_head_end = (tp_rank + 1) * num_kv_heads
        elif self.config.multi_query:
            total_num_kv_heads = 1
            num_kv_heads = 1
            separated_q_kv = True
            kv_head_start = 0
            kv_head_end = 1
        else:
            total_num_kv_heads = total_num_heads
            num_kv_heads = total_num_kv_heads // tp_size
            separated_q_kv = False
            kv_head_start = tp_rank * num_kv_heads
            kv_head_end = (tp_rank + 1) * num_kv_heads
        num_query_heads_per_kv_head = total_num_heads // total_num_kv_heads
        state_dict = self.state_dict()

        for name, loaded_weight in hf_model_weights_iterator(
            model_name_or_path, cache_dir, load_format, revision
        ):

            if pp_rank != 0 and "word_embeddings" in name:
                continue

            if pp_rank != pp_size - 1 and ("lm_head" in name or "ln_f" in name):
                continue

            if "transformer.h" in name:
                layer_id = int(name.split(".")[2])
                if layer_id < first_layer_id or layer_id > last_layer_id:
                    continue

                new_layer_id = layer_id - first_layer_id
                name = name.replace(f".{layer_id}.", f".{new_layer_id}.")

            if "query_key_value" in name:
                loaded_weight = convert_pyslice_to_tensor(loaded_weight)
                loaded_weight_size = loaded_weight.size()
                loaded_weight = loaded_weight.view(
                    total_num_kv_heads,
                    num_query_heads_per_kv_head + 2,
                    head_size,
                    *loaded_weight_size[1:],
                )

                wq = loaded_weight[:, :-2].reshape(-1, *loaded_weight_size[1:])
                wk = loaded_weight[:, [-2]].reshape(-1, *loaded_weight_size[1:])
                wv = loaded_weight[:, [-1]].reshape(-1, *loaded_weight_size[1:])

                wq = wq[head_size * head_start : head_size * head_end]
                wk = wk[head_size * kv_head_start : head_size * kv_head_end]
                wv = wv[head_size * kv_head_start : head_size * kv_head_end]

                if separated_q_kv:
                    loaded_weight_q = wq
                    loaded_weight_kv = torch.cat([wk, wv], dim=0)
                    q_weight_name = name.replace("query_key_value", "query")
                    kv_weight_name = name.replace("query_key_value", "key_value")
                    load_tensor_parallel_weights(
                        state_dict[q_weight_name],
                        loaded_weight_q,
                        q_weight_name,
                        self._column_parallel_weights,
                        self._row_parallel_weights,
                        tp_rank,
                    )
                    load_tensor_parallel_weights(
                        state_dict[kv_weight_name],
                        loaded_weight_kv,
                        kv_weight_name,
                        self._column_parallel_weights,
                        self._row_parallel_weights,
                        tp_rank,
                    )
                    continue
                else:
                    loaded_weight = torch.cat([wq, wk, wv], dim=0)

            param = state_dict[name]
            load_tensor_parallel_weights(
                param,
                loaded_weight,
                name,
                self._column_parallel_weights,
                self._row_parallel_weights,
                tp_rank,
            )
