from inspect import signature
import torch
from torch import nn
from dgl import ops
from plr_embeddings import PLREmbeddings
from utils import _check_dim_and_num_heads_consistency


class ResidualModulesWrapper(nn.Module):
    def __init__(self, modules):
        super().__init__()

        if isinstance(modules, nn.Module):
            modules = [modules]

        for module in modules:
            module.takes_graph_as_input = ('graph' in signature(module.forward).parameters)

        self.wrapped_modules = nn.ModuleList(modules)

    def forward(self, graph, x):
        x_res = x
        for module in self.wrapped_modules:
            if module.takes_graph_as_input:
                x_res = module(graph, x_res)
            else:
                x_res = module(x_res)

        x = x + x_res

        return x


class FeedForwardModule(nn.Module):
    def __init__(self, dim, num_inputs=1, dropout=0):
        super().__init__()
        self.linear_1 = nn.Linear(in_features=dim * num_inputs, out_features=dim)
        self.dropout_1 = nn.Dropout(p=dropout)
        self.act = nn.GELU()
        self.linear_2 = nn.Linear(in_features=dim, out_features=dim)
        self.dropout_2 = nn.Dropout(p=dropout)

    def forward(self, x):
        x = self.linear_1(x)
        x = self.dropout_1(x)
        x = self.act(x)
        x = self.linear_2(x)
        x = self.dropout_2(x)

        return x


class GraphMeanAggregationModule(nn.Module):
    def __init__(self, **kwargs):
        super().__init__()

    def forward(self, graph, x):
        x_aggregated = ops.copy_u_mean(graph, x)
        x = torch.cat([x, x_aggregated], axis=-1)

        return x


class GraphMaxAggregationModule(nn.Module):
    def __init__(self, **kwargs):
        super().__init__()

    def forward(self, graph, x):
        x_aggregated = ops.copy_u_max(graph, x)
        x_aggregated[x_aggregated.isinf()] = 0
        x = torch.cat([x, x_aggregated], axis=-1)

        return x


class GraphAttnGATAggregationModule(nn.Module):
    def __init__(self, dim, num_heads, **kwargs):
        super().__init__()

        _check_dim_and_num_heads_consistency(dim=dim, num_heads=num_heads)
        self.dim = dim
        self.num_heads = num_heads
        self.head_dim = dim // num_heads

        self.attn_linear_u = nn.Linear(in_features=dim, out_features=num_heads)
        self.attn_linear_v = nn.Linear(in_features=dim, out_features=num_heads, bias=False)
        self.attn_act = nn.LeakyReLU(negative_slope=0.2)

    def forward(self, graph, x):
        attn_scores_u = self.attn_linear_u(x)
        attn_scores_v = self.attn_linear_v(x)
        attn_scores = ops.u_add_v(graph, attn_scores_u, attn_scores_v)
        attn_scores = self.attn_act(attn_scores)
        attn_probs = ops.edge_softmax(graph, attn_scores)

        x = x.reshape(-1, self.head_dim, self.num_heads)
        x_aggregated = ops.u_mul_e_sum(graph, x, attn_probs)
        x_aggregated = x_aggregated.reshape(-1, self.dim)

        x = torch.cat([x, x_aggregated], axis=-1)

        return x


class GraphAttnTrfAggregationModule(nn.Module):
    def __init__(self, dim, num_heads, dropout, **kwargs):
        super().__init__()

        _check_dim_and_num_heads_consistency(dim=dim, num_heads=num_heads)
        self.dim = dim
        self.num_heads = num_heads
        self.head_dim = dim // num_heads
        self.attn_scores_multiplier = 1 / torch.tensor(self.head_dim).sqrt()

        self.attn_qkv_linear = nn.Linear(in_features=dim, out_features=dim * 3)

        self.output_linear = nn.Linear(in_features=dim, out_features=dim)
        self.dropout = nn.Dropout(p=dropout)

    def forward(self, graph, x):
        qkvs = self.attn_qkv_linear(x)
        qkvs = qkvs.reshape(-1, self.num_heads, self.head_dim * 3)
        queries, keys, values = qkvs.split(split_size=(self.head_dim, self.head_dim, self.head_dim), dim=-1)

        attn_scores = ops.u_dot_v(graph, keys, queries) * self.attn_scores_multiplier
        attn_probs = ops.edge_softmax(graph, attn_scores)

        x_aggregated = ops.u_mul_e_sum(graph, values, attn_probs)
        x_aggregated = x_aggregated.reshape(-1, self.dim)

        x_aggregated = self.output_linear(x_aggregated)
        x_aggregated = self.dropout(x_aggregated)

        x = torch.cat([x, x_aggregated], axis=-1)

        return x


class RNNSequenceEncoderModule(nn.Module):
    rnn_types = {
        'LSTM': nn.LSTM,
        'GRU': nn.GRU
    }

    def __init__(self, rnn_type_name, num_layers, dim, dropout=0, **kwargs):
        super().__init__()

        RNN = self.rnn_types[rnn_type_name]
        self.rnn = RNN(input_size=dim, hidden_size=dim, num_layers=num_layers, dropout=dropout,
                       batch_first=True)

    def forward(self, x):
        x, _ = self.rnn(x)

        return x


class TransformerSequenceEncoderModule(nn.Module):
    def __init__(self, num_layers, dim, num_heads, seq_len, bidir_attn=False, dropout=0, **kwargs):
        super().__init__()

        _check_dim_and_num_heads_consistency(dim=dim, num_heads=num_heads)

        self.bidir_attn = bidir_attn
        self.attn_mask = None if bidir_attn else nn.Transformer.generate_square_subsequent_mask(seq_len)

        self.positional_embeddings = nn.Embedding(num_embeddings=seq_len, embedding_dim=dim)

        self.transformer_blocks = nn.ModuleList([
            nn.TransformerEncoderLayer(d_model=dim, nhead=num_heads, dim_feedforward=dim, dropout=dropout,
                                       activation='gelu', norm_first=True, batch_first=True)
            for _ in range(num_layers)
        ])

    def forward(self, x):
        pos_embs = self.positional_embeddings.weight[None, ...]
        x = x + pos_embs

        for transformer_block in self.transformer_blocks:
            x = transformer_block(x, src_mask=self.attn_mask, is_causal=not self.bidir_attn)

        return x


class FeaturesPreparatorForDeepModels(nn.Module):
    def __init__(self, features_dim, use_learnable_node_embeddings, num_nodes, learnable_node_embeddings_dim,
                 initialize_learnable_node_embeddings_with_deepwalk, deepwalk_node_embeddings,
                 use_plr_for_num_features, num_features_mask, plr_num_features_frequencies_dim,
                 plr_num_features_frequencies_scale, plr_num_features_embedding_dim,
                 plr_num_features_shared_linear, plr_num_features_shared_frequencies,
                 use_plr_for_past_targets, past_targets_mask, plr_past_targets_frequencies_dim,
                 plr_past_targets_frequencies_scale, plr_past_targets_embedding_dim,
                 plr_past_targets_shared_linear, plr_past_targets_shared_frequencies):
        super().__init__()

        output_dim = features_dim

        self.use_learnable_node_embeddings = use_learnable_node_embeddings
        if use_learnable_node_embeddings:
            output_dim += learnable_node_embeddings_dim
            if initialize_learnable_node_embeddings_with_deepwalk:
                if learnable_node_embeddings_dim != deepwalk_node_embeddings.shape[1]:
                    raise ValueError(f'initialize_learnable_node_embeddings_with_deepwalk argument is True, but the '
                                     f'value of learnable_node_embeddings_dim argument does not match the dimension of '
                                     f'the precomputed DeepWalk node embeddings: '
                                     f'{learnable_node_embeddings_dim} != {deepwalk_node_embeddings.shape[1]}')

                self.node_embeddings = nn.Embedding(num_embeddings=num_nodes,
                                                    embedding_dim=learnable_node_embeddings_dim,
                                                    _weight=deepwalk_node_embeddings)
            else:
                self.node_embeddings = nn.Embedding(num_embeddings=num_nodes,
                                                    embedding_dim=learnable_node_embeddings_dim)

        self.use_plr_for_num_features = use_plr_for_num_features
        if use_plr_for_num_features:
            num_features_dim = num_features_mask.sum()
            output_dim = output_dim - num_features_dim + num_features_dim * plr_num_features_embedding_dim
            self.plr_embeddings_num_features = PLREmbeddings(features_dim=num_features_dim,
                                                             frequencies_dim=plr_num_features_frequencies_dim,
                                                             frequencies_scale=plr_num_features_frequencies_scale,
                                                             embedding_dim=plr_num_features_embedding_dim,
                                                             shared_linear=plr_num_features_shared_linear,
                                                             shared_frequencies=plr_num_features_shared_frequencies)

            self.register_buffer('num_features_mask', num_features_mask)

        self.use_plr_for_past_targtes = use_plr_for_past_targets
        if use_plr_for_past_targets:
            past_targets_dim = past_targets_mask.sum()
            output_dim = output_dim - past_targets_dim + past_targets_dim * plr_past_targets_embedding_dim
            self.plr_embeddings_past_targets = PLREmbeddings(features_dim=past_targets_dim,
                                                             frequencies_dim=plr_past_targets_frequencies_dim,
                                                             frequencies_scale=plr_past_targets_frequencies_scale,
                                                             embedding_dim=plr_past_targets_embedding_dim,
                                                             shared_linear=plr_past_targets_shared_linear,
                                                             shared_frequencies=plr_past_targets_shared_frequencies)

            if use_plr_for_num_features:
                past_targets_mask = past_targets_mask[~num_features_mask]
                num_features_dim = num_features_mask.sum()
                embedded_num_features_dim = num_features_dim * plr_num_features_embedding_dim
                embedded_num_features_shift = torch.zeros(embedded_num_features_dim, dtype=bool)
                past_targets_mask = torch.cat([embedded_num_features_shift, past_targets_mask], axis=0)

            self.register_buffer('past_targets_mask', past_targets_mask)

        self.output_dim = output_dim

    def forward(self, x):
        if self.use_plr_for_num_features:
            x_num = x[..., self.num_features_mask]
            x_num_embedded = self.plr_embeddings_num_features(x_num).flatten(start_dim=-2)
            x = torch.cat([x_num_embedded, x[..., ~self.num_features_mask]], axis=-1)

        if self.use_plr_for_past_targtes:
            x_targets = x[..., self.past_targets_mask]
            x_targets_embedded = self.plr_embeddings_past_targets(x_targets).flatten(start_dim=-2)
            x = torch.cat([x_targets_embedded, x[..., ~self.past_targets_mask]], axis=-1)

        if self.use_learnable_node_embeddings:
            graph_batch_size = x.shape[0] // self.node_embeddings.weight.shape[0]
            node_embs = self.node_embeddings.weight.repeat(graph_batch_size, 1)

            if x.dim() == 3:
                # SequenceInputModel is used, so sequence dimension needs to be added.
                seq_len = x.shape[1]
                node_embs = node_embs.unsqueeze(1).expand(-1, seq_len, -1)

            x = torch.cat([x, node_embs], axis=-1)

        return x


NEIGHBORHOOD_AGGREGATION_MODULES = {
    'MeanAggr': GraphMeanAggregationModule,
    'MaxAggr': GraphMaxAggregationModule,
    'AttnGATAggr': GraphAttnGATAggregationModule,
    'AttnTrfAggr': GraphAttnTrfAggregationModule
}

SEQUENCE_ENCODER_MODULES = {
    'RNN': RNNSequenceEncoderModule,
    'Transformer': TransformerSequenceEncoderModule
}

NORMALIZATION_MODULES = {
    'none': nn.Identity,
    'LayerNorm': nn.LayerNorm,
    'BatchNorm': nn.BatchNorm1d
}
