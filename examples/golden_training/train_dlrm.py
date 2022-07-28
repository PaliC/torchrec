#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import os
from typing import List, Optional

import torch
from torch import distributed as dist
from torch.distributed.elastic.multiprocessing.errors import record
from torch.utils.data import IterableDataset
from torchrec.datasets.criteo import DEFAULT_CAT_NAMES, DEFAULT_INT_NAMES
from torchrec.datasets.random import RandomRecDataset
from torchrec.distributed import TrainPipelineSparseDist

from torchrec.distributed.fbgemm_qcomm_codec import (
    CommType,
    get_qcomm_codecs_registry,
    QCommsConfig,
)
from torchrec.distributed.fused_embeddingbag import FusedEmbeddingBagCollectionSharder
from torchrec.distributed.model_parallel import DistributedModelParallel
from torchrec.models.dlrm import DLRM, DLRMTrain
from torchrec.modules.embedding_configs import EmbeddingBagConfig
from torchrec.modules.embedding_modules import EmbeddingBagCollection
from torchrec.modules.fused_embedding_modules import fuse_embedding_optimizer
from torchrec.optim.keyed import KeyedOptimizerWrapper
from torchrec.optim.rowwise_adagrad import RowWiseAdagrad
from tqdm import tqdm


def _get_random_dataset(
    num_embeddings: int,
    batch_size: int = 32,
) -> IterableDataset:
    return RandomRecDataset(
        keys=DEFAULT_CAT_NAMES,
        batch_size=batch_size,
        hash_size=num_embeddings,
        ids_per_feature=1,
        num_dense=len(DEFAULT_INT_NAMES),
    )


@record
def main() -> None:
    train()


def train(
    num_embeddings: int = 1024**2,
    embedding_dim: int = 128,
    dense_arch_layer_sizes: Optional[List[int]] = None,
    over_arch_layer_sizes: Optional[List[int]] = None,
    learning_rate: float = 0.1,
) -> None:
    """
    Constructs and trains a DLRM model (using random dummy data). Each script is run on each process (rank) in SPMD fashion.
    The embedding layers will be sharded across available ranks
    """
    if dense_arch_layer_sizes is None:
        dense_arch_layer_sizes = [64, 128]
    if over_arch_layer_sizes is None:
        over_arch_layer_sizes = [64, 1]

    # Init process_group , device, rank, backend
    rank = int(os.environ["LOCAL_RANK"])
    if torch.cuda.is_available():
        device: torch.device = torch.device(f"cuda:{rank}")
        backend = "nccl"
        torch.cuda.set_device(device)
    else:
        device: torch.device = torch.device("cpu")
        backend = "gloo"
    dist.init_process_group(backend=backend)

    # Construct DLRM module
    eb_configs = [
        EmbeddingBagConfig(
            name=f"t_{feature_name}",
            embedding_dim=embedding_dim,
            num_embeddings=num_embeddings,
            feature_names=[feature_name],
        )
        for feature_idx, feature_name in enumerate(DEFAULT_CAT_NAMES)
    ]
    dlrm_model = DLRM(
        embedding_bag_collection=EmbeddingBagCollection(
            tables=eb_configs, device=torch.device("meta")
        ),
        dense_in_features=len(DEFAULT_INT_NAMES),
        dense_arch_layer_sizes=dense_arch_layer_sizes,
        over_arch_layer_sizes=over_arch_layer_sizes,
        dense_device=device,
    )
    train_model = DLRMTrain(dlrm_model)

    # Enable optimizer fusion
    model = fuse_embedding_optimizer(
        train_model,
        optimizer_type=RowWiseAdagrad,
        optimizer_kwargs={
            "lr": learning_rate,
        },
        device=torch.device("meta"),
    )

    model = DistributedModelParallel(
        module=train_model,
        device=device,
        sharders=[
            FusedEmbeddingBagCollectionSharder(
                # Enable quantized comms. This will encode all tensors forward pass collective 
                # calls as float16, and all tensors in the backward pass as bfloat16.
                qcomm_codecs_registry=get_qcomm_codecs_registry(
                    qcomms_config=QCommsConfig(
                        forward_precision=CommType.FP16,
                        backward_precision=CommType.BF16,
                    ),
                    device=device,
                )
            )
        ],
    )

    non_fused_optimizer = KeyedOptimizerWrapper(
        dict(model.named_parameters()),
        lambda params: torch.optim.Adagrad(params, lr=learning_rate),
    )
    # Overlap comm/compute/device transfer during training through train_pipeline
    train_pipeline = TrainPipelineSparseDist(
        model,
        non_fused_optimizer,
        device,
    )

    # train model
    train_iterator = iter(
        _get_random_dataset(
            num_embeddings=num_embeddings,
        )
    )
    for _ in tqdm(range(int(1e4)), mininterval=5.0):
        train_pipeline.progress(train_iterator)


if __name__ == "__main__":
    main()
