from math import ceil
from typing import Any, Dict, List, Optional, Tuple, Union

from ConfigSpace.configuration_space import ConfigurationSpace
from ConfigSpace.hyperparameters import (
    UniformFloatHyperparameter,
)

import numpy as np

import torch
from torch import embedding, nn

from autoPyTorch.datasets.base_dataset import BaseDatasetPropertiesType
from autoPyTorch.pipeline.components.setup.network_embedding.base_network_embedding import NetworkEmbeddingComponent
from autoPyTorch.utils.common import HyperparameterSearchSpace, add_hyperparameter


class _LearnedEntityEmbedding(nn.Module):
    """ Learned entity embedding module for categorical features"""

    def __init__(self, config: Dict[str, Any], num_categories_per_col: np.ndarray, num_features_excl_embed: int):
        """
        Args:
            config (Dict[str, Any]): The configuration sampled by the hyperparameter optimizer
            num_categories_per_col (np.ndarray): number of categories per categorical columns that will be embedded
            num_features_excl_embed (int): number of features in X excluding the features that need to be embedded
        """
        super().__init__()
        self.config = config
        # list of number of categories of categorical data
        # or 0 for numerical data
        self.num_categories_per_col = num_categories_per_col
        self.embed_features = self.num_categories_per_col > 0

        self.num_embed_features = self.num_categories_per_col[self.embed_features]

        self.num_output_dimensions = [1] * num_features_excl_embed
        self.num_output_dimensions.extend([ceil(config["dimension_reduction_" + str(i)] * num_in) for i, num_in in
                                           enumerate(self.num_embed_features)])

        self.num_out_feats = num_features_excl_embed + sum(self.num_output_dimensions)

        self.ee_layers = self._create_ee_layers()

    def get_partial_models(self, subset_features: List[int]) -> "_LearnedEntityEmbedding":
        """
        extract a partial models that only works on a subset of the data that ought to be passed to the embedding
        network, this function is implemented for time series forecasting tasks where the known future features is only
        a subset of the past features
        Args:
            subset_features (List[int]):
                a set of index identifying which features will pass through the partial model

        Returns:
            partial_model (_LearnedEntityEmbedding)
                a new partial model
        """
        num_input_features = self.num_input_features[subset_features]
        num_numerical_features = sum([sf < self.num_numerical for sf in subset_features])

        num_output_dimensions = [self.num_output_dimensions[sf] for sf in subset_features]
        embed_features = [self.embed_features[sf] for sf in subset_features]

        ee_layers = []
        ee_layer_tracker = 0
        for sf in subset_features:
            if self.embed_features[sf]:
                ee_layers.append(self.ee_layers[ee_layer_tracker])
                ee_layer_tracker += 1
        ee_layers = nn.ModuleList(ee_layers)

        return PartialLearnedEntityEmbedding(num_input_features, num_numerical_features, embed_features,
                                             num_output_dimensions, ee_layers)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # pass the columns of each categorical feature through entity embedding layer
        # before passing it through the model
        concat_seq = []

        x_pointer = 0
        layer_pointer = 0
        for x_pointer, embed in enumerate(self.embed_features):
            current_feature_slice = x[:, x_pointer]
            if not embed:
                x_pointer += 1
                concat_seq.append(current_feature_slice.view(-1, 1))
                continue
            current_feature_slice = current_feature_slice.to(torch.int)
            concat_seq.append(self.ee_layers[layer_pointer](current_feature_slice))
            layer_pointer += 1

        return torch.cat(concat_seq, dim=1)

    def _create_ee_layers(self) -> nn.ModuleList:
        # entity embeding layers are Linear Layers
        layers = nn.ModuleList()
        for num_cat, embed, num_out in zip(self.num_categories_per_col,
                                           self.embed_features,
                                           self.num_output_dimensions):
            if not embed:
                continue
            layers.append(nn.Embedding(num_cat, num_out))
        return layers


class PartialLearnedEntityEmbedding(_LearnedEntityEmbedding):
    """
    Construct a partial Embedding network that is derived from a learned embedding network and only applied to a subset
    of the input features. This is applied to forecasting tasks where not all the features might be known beforehand
    """
    def __init__(self,
                 num_input_features: np.ndarray,
                 num_numerical_features: int,
                 embed_features: List[bool],
                 num_output_dimensions: List[int],
                 ee_layers: nn.Module
                 ):
        super(_LearnedEntityEmbedding, self).__init__()
        self.num_numerical = num_numerical_features
        # list of number of categories of categorical data
        # or 0 for numerical data
        self.num_input_features = num_input_features
        categorical_features: np.ndarray = self.num_input_features > 0

        self.num_categorical_features = self.num_input_features[categorical_features]

        self.embed_features = embed_features

        self.num_output_dimensions = num_output_dimensions
        self.num_out_feats = self.num_numerical + sum(self.num_output_dimensions)

        self.ee_layers = ee_layers


class LearnedEntityEmbedding(NetworkEmbeddingComponent):
    """
    Class to learn an embedding for categorical hyperparameters.
    """

    def __init__(self, random_state: Optional[np.random.RandomState] = None, **kwargs: Any):
        super().__init__(random_state=random_state)
        self.config = kwargs

    def build_embedding(self, num_categories_per_col: np.ndarray, num_features_excl_embed: int) -> nn.Module:

        embedding = _LearnedEntityEmbedding(config=self.config,
                                       num_categories_per_col=num_categories_per_col,
                                       num_features_excl_embed=num_features_excl_embed)

        return embedding, embedding.num_output_dimensions

    @staticmethod
    def get_hyperparameter_search_space(
        dataset_properties: Optional[Dict[str, BaseDatasetPropertiesType]] = None,
        dimension_reduction: HyperparameterSearchSpace = HyperparameterSearchSpace(hyperparameter="dimension_reduction",
                                                                                   value_range=(0, 1),
                                                                                   default_value=0.5),
    ) -> ConfigurationSpace:
        cs = ConfigurationSpace()
        if dataset_properties is not None:
            for i in range(len(dataset_properties['categorical_columns'])
                           if isinstance(dataset_properties['categorical_columns'], List) else 0):
                # currently as we dont have information about the embedding columns
                # we search for more dimensions than necessary. This can be solved by
                # not having `min_unique_values_for_embedding` as a hyperparameter and
                # instead passing it as a parameter to the feature validator, which
                # allows us to pass embed_columns to the dataset properties.
                # TODO: test the trade off
                # Another solution is to combine `OneHotEncoding`, `Embedding` and `NoEncoding`
                # in one custom transformer. this will also allow users to use this transformer
                # outside the pipeline
                ee_dimensions_search_space = HyperparameterSearchSpace(hyperparameter="dimension_reduction_" + str(i),
                                                                       value_range=dimension_reduction.value_range,
                                                                       default_value=dimension_reduction.default_value,
                                                                       log=dimension_reduction.log)
                add_hyperparameter(cs, ee_dimensions_search_space, UniformFloatHyperparameter)
        return cs

    @staticmethod
    def get_properties(dataset_properties: Optional[Dict[str, BaseDatasetPropertiesType]] = None
                       ) -> Dict[str, Union[str, bool]]:
        return {
            'shortname': 'embedding',
            'name': 'LearnedEntityEmbedding',
            'handles_tabular': True,
            'handles_image': False,
            'handles_time_series': True,
        }
