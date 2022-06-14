# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

from test.test_utils import assert_expected, set_rng_seed
from torch import nn
from torchmultimodal.models.albef import ALBEFModel


class TestALBEFModel:
    set_rng_seed(0)
    albef = ALBEFModel(
        nn.Linear(3, 2),
        nn.Linear(3, 2),
        nn.Linear(3, 2),
        nn.Linear(3, 2),
        nn.Linear(3, 2),
        embed_dim=2,
        queue_size=4,
    )

    def test_copy_params_momentum_models(self):
        self.albef.models_m = [nn.Linear(3, 2) for _ in range(5)]
        self.albef._copy_params_momentum_models()
        for model, model_m in zip(self.albef.models, self.albef.models_m):
            for param, param_m in zip(model.parameters(), model_m.parameters()):
                assert_expected(param, param_m)
                assert not param_m.requires_grad
