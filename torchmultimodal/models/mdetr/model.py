# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import math
from typing import List, NamedTuple, Optional, Tuple

import torch
from torch import nn, Tensor
from torchmultimodal.models.mdetr.image_encoder import (
    mdetr_resnet101_backbone,
    PositionEmbedding2D,
)
from torchmultimodal.models.mdetr.text_encoder import mdetr_roberta_text_encoder
from torchmultimodal.models.mdetr.transformer import (
    mdetr_transformer,
    MDETRTransformerOutput,
)
from torchmultimodal.modules.layers.mlp import MLP


class MDETRModelOutput(NamedTuple):
    transformer_output: MDETRTransformerOutput
    pred_logits: torch.Tensor
    pred_boxes: torch.Tensor
    extra_embeddings: Optional[torch.Tensor]


class MDETR(nn.Module):
    """
    MDETR (https://arxiv.org/abs/2104.12763) is a modulated detection model
    used to detect objects in an image conditioned on text or captions.
    This class contains the entire MDETR architecture, including the
    image backbone, text encoder, and multimodal transformer. (Note that the
    matcher and losses are provided elsewhere.)

    Args:   image_backbone (nn.Module): Torch module of the backbone to be used.
                See image_encoder.py.
            text_encoder (nn.Module): Torch module of the text encoder to be used.
                See text_encoder.py.
            transformer (nn.Module): The multimodal transformer module. See the
                Transformer class in this file.
            pos_embed (nn.Module): Module for positional embedding of images.
            text_projection (nn.Module): Module to resize text encoder outputs before feeding
                them to the multimodal transformer.
            image_projection (nn.Module): Projection module applied to image embeddings
                prior to the multimodal transformer.
            query_embed (nn.Module): Learned object query embeddings (used in
                transformer decoder).
            bbox_embed (nn.Module): Embedding mapping transformer outputs to
                bounding boxes.
            class_embed (nn.Module): Embedding mapping transformer outputs to classes.
            extra_query_embeddings (Optional[nn.Embedding]): Additional query embeddings,
                as used in e.g. VQA. Default: None

    Inputs: images (List[Tensor]): A list of image Tensors (possibly of different sizes).
            text (List[Tensor]): A list of Tensors of tokenized texts (possibly of different lengths).

    Returns:
        A dict with the following elements:
           - "pred_logits": the classification logits (including no-object) for all queries.
                            Shape= [batch_size x num_queries x (num_classes + 1)]
           - "pred_boxes": The normalized boxes coordinates for all queries, represented as
                           (center_x, center_y, height, width). These values are normalized in [0, 1],
                           relative to the size of each individual image (disregarding possible padding).
                           See PostProcess for information on how to retrieve the unnormalized bounding box.
    """

    def __init__(
        self,
        image_backbone: nn.Module,
        text_encoder: nn.Module,
        transformer: nn.Module,
        pos_embed: nn.Module,
        text_projection: nn.Module,
        image_projection: nn.Module,
        query_embed: nn.Embedding,
        bbox_embed: nn.Module,
        class_embed: nn.Module,
        extra_query_embeddings: Optional[nn.Embedding] = None,
    ):
        super().__init__()
        self.image_backbone = image_backbone
        self.text_encoder = text_encoder
        self.text_projection = text_projection
        self.transformer = transformer
        self.pos_embed = pos_embed
        self.image_projection = image_projection
        self.query_embed = query_embed
        self.bbox_embed = bbox_embed
        self.class_embed = class_embed
        self.extra_query_embeddings = extra_query_embeddings

    def _pad_images(self, images: List[Tensor]) -> Tuple[Tensor, Tensor]:
        max_size = tuple(max(s) for s in zip(*[img.shape for img in images]))
        batch_shape = (len(images),) + max_size
        b, _, h, w = batch_shape

        dtype = images[0].dtype
        device = images[0].device
        padded_images = torch.zeros(batch_shape, dtype=dtype, device=device)
        mask = torch.ones((b, h, w), dtype=torch.bool, device=device)
        for img, pad_img, m in zip(images, padded_images, mask):
            pad_img[: img.shape[0], : img.shape[1], : img.shape[2]].copy_(img)
            m[: img.shape[1], : img.shape[2]] = False
        return padded_images, mask

    def _pad_text(
        self, text: List[Tensor], padding_idx: int = 1
    ) -> Tuple[Tensor, Tensor]:
        padded_text = nn.utils.rnn.pad_sequence(
            text, batch_first=True, padding_value=padding_idx
        )
        mask = padded_text == padding_idx
        return padded_text, mask

    def forward(self, images: List[Tensor], text: List[Tensor]) -> MDETRModelOutput:

        images, image_mask = self._pad_images(images)
        text, text_attention_mask = self._pad_text(text)
        encoded_text = self.text_encoder(text, text_attention_mask)

        # Transpose memory because pytorch's attention expects sequence first
        text_memory = encoded_text.transpose(0, 1)

        image_embeddings, image_mask = self.image_backbone(images, image_mask)
        pos = self.pos_embed(image_mask).to(image_embeddings.dtype)
        query_embed = self.query_embed.weight

        # If extra embeddings are provided for VQA, we concatenate them with
        # the other query embeddings prior to the transformer
        if self.extra_query_embeddings is not None:
            n_extra_embeddings = self.extra_query_embeddings.num_embeddings
            query_embed = torch.cat([query_embed, self.extra_query_embeddings.weight])

        text_memory_resized = self.text_projection(text_memory)

        transformer_output = self.transformer(
            self.image_projection(image_embeddings),
            image_mask,
            query_embed,
            pos,
            text_memory=text_memory_resized,
            text_attention_mask=text_attention_mask,
        )

        # Detach the extra embeddings from the hidden states returned by the decoder
        if self.extra_query_embeddings is not None:
            extra_embeddings = transformer_output.decoder_hidden_states[
                0, :, -n_extra_embeddings:
            ]
            decoder_hidden_states_truncated = transformer_output.decoder_hidden_states[
                :, :, :-n_extra_embeddings
            ]
            transformer_output = transformer_output._replace(
                decoder_hidden_states=decoder_hidden_states_truncated
            )
        else:
            extra_embeddings = None
        final_hidden_state = transformer_output.decoder_hidden_states[-1]
        outputs_class = self.class_embed(final_hidden_state)
        outputs_coord = self.bbox_embed(final_hidden_state).sigmoid()

        return MDETRModelOutput(
            transformer_output, outputs_class, outputs_coord, extra_embeddings
        )


class FeatureResizer(nn.Module):
    """
    This class takes as input a set of embeddings of dimension C1 and outputs a set of
    embedding of dimension C2, after a linear transformation, dropout and normalization (LN).

    Args:   input_feat_size (int): Dimension of input features.
            output_feat_size (int): Dimension of output features.
            dropout (float): Dropout probability for final features. Default: 0.1
            do_ln (bool): Whether to perform layer normalization after the linear layer.

    Inputs: encoder_features (Tensor): Features to be resized.
    """

    def __init__(
        self,
        input_feat_size: int,
        output_feat_size: int,
        dropout: float = 0.1,
        do_ln: bool = True,
    ):
        super().__init__()
        self.do_ln = do_ln
        # Object feature encoding
        self.fc = nn.Linear(input_feat_size, output_feat_size, bias=True)
        self.layer_norm = nn.LayerNorm(output_feat_size, eps=1e-12) if do_ln else None
        self.dropout = nn.Dropout(dropout)

    def forward(self, encoder_features: Tensor) -> Tensor:
        x = self.fc(encoder_features)
        if self.do_ln:
            x = self.layer_norm(x)
        output = self.dropout(x)
        return output


def mdetr_resnet101(
    num_queries: int = 100,
    num_classes: int = 255,
    embedding_dim: int = 768,
    transformer_d_model: int = 256,
    transformer_num_heads: int = 8,
    transformer_encoder_layers: int = 6,
    transformer_decoder_layers: int = 6,
    transformer_dim_feedforward: int = 2048,
    transformer_dropout: float = 0.1,
    return_intermediate_dec: bool = True,
    num_extra_query_embeddings: Optional[int] = None,
) -> MDETR:
    image_backbone = mdetr_resnet101_backbone()
    pos_embed = PositionEmbedding2D(128, scale=2 * math.pi)
    # Size of layer4 output in ResNet101. See
    # https://github.com/pytorch/vision/blob/main/torchvision/models/resnet.py#L204
    image_backbone_num_channels = 2048
    text_encoder = mdetr_roberta_text_encoder()
    transformer = mdetr_transformer(
        transformer_d_model,
        transformer_num_heads,
        transformer_encoder_layers,
        transformer_decoder_layers,
        transformer_dim_feedforward,
        transformer_dropout,
        return_intermediate_dec,
    )
    hidden_dim = transformer_d_model
    text_projection = FeatureResizer(embedding_dim, hidden_dim)
    image_projection = nn.Conv2d(image_backbone_num_channels, hidden_dim, kernel_size=1)
    query_embed = nn.Embedding(num_queries, hidden_dim)
    # 4 gives the number of coordinates that represent the bounding box
    bbox_embed = MLP(hidden_dim, 4, [hidden_dim] * 2, dropout=0.0)
    # The + 1 here corresponds to the "no class" label
    class_embed = nn.Linear(hidden_dim, num_classes + 1)
    if num_extra_query_embeddings is not None:
        extra_query_embeddings = nn.Embedding(num_extra_query_embeddings, hidden_dim)
    else:
        extra_query_embeddings = None

    mdetr = MDETR(
        image_backbone,
        text_encoder,
        transformer,
        pos_embed,
        text_projection,
        image_projection,
        query_embed,
        bbox_embed,
        class_embed,
        extra_query_embeddings,
    )
    return mdetr
